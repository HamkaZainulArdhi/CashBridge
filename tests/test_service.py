"""Service-layer tests: consent, offer lifecycle, sandbox execution, repayments."""
from __future__ import annotations

import pytest

from app import audit as audit_mod
from app import service
from cashbridge import db, seed
from cashbridge.recommend import recommend


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(tmp_path / "svc.db")
    db.init_db(conn)
    ids = seed.run_seed(conn, as_of=None, seed=42)
    yield {"conn": conn, "ids": ids}
    conn.close()


def _eligible_product(conn, business_id) -> str:
    recommendation = recommend(conn, business_id)
    assert recommendation["decision"] == "loan_available", recommendation["decision"]
    return recommendation["loan"]["eligible"][0]["product_id"]


def _disburse_flow(env):
    """consent -> draft -> approve -> disburse, returning (offer, disbursement)."""
    conn, online = env["conn"], env["ids"]["online"]
    service.grant_consent(conn, online)
    offer = service.draft_offer(conn, online, _eligible_product(conn, online))
    assert "error" not in offer, offer
    assert offer["status"] == "draft"
    decided = service.decide_offer(conn, offer["id"], "approved", actor="owner")
    assert decided["status"] == "approved"
    return offer, service.disburse_offer(conn, offer["id"])


def test_consent_lifecycle(env):
    conn, online = env["conn"], env["ids"]["online"]
    assert not service.consent_active(conn, online)
    service.grant_consent(conn, online)
    assert service.consent_active(conn, online)
    service.revoke_consent(conn, online)
    assert not service.consent_active(conn, online)
    assert audit_mod.verify_chain(conn)["ok"]


def test_draft_requires_consent(env):
    conn, online = env["conn"], env["ids"]["online"]
    result = service.draft_offer(conn, online, "receivable_bridge")
    assert result["error"]["code"] == "consent_revoked"
    assert audit_mod.verify_chain(conn)["ok"]


def test_offer_lifecycle_and_idempotency(env):
    conn = env["conn"]
    offer, disbursement = _disburse_flow(env)
    assert disbursement["loan_id"]
    assert disbursement["installments"] >= 4

    # replaying the same action is refused at the policy layer
    again = service.disburse_offer(conn, offer["id"])
    assert again["error"]["code"] == "duplicate_action"

    # even a fresh idempotency key cannot replay: approval binds to the draft state
    replay = service.disburse_offer(conn, offer["id"], idempotency_key="manual-replay")
    assert replay["error"]["code"] == "approval_missing"

    summary = service.sandbox_summary(conn, offer["business_id"])
    assert summary["total_disbursed"] == offer["principal"]
    assert len(summary["repayments"]) == disbursement["installments"]
    assert all(r["status"] == "scheduled" for r in summary["repayments"])
    assert audit_mod.verify_chain(conn)["ok"]


def test_disburse_requires_approval(env):
    conn, online = env["conn"], env["ids"]["online"]
    service.grant_consent(conn, online)
    offer = service.draft_offer(conn, online, _eligible_product(conn, online))
    result = service.disburse_offer(conn, offer["id"])
    assert result["error"]["code"] == "approval_missing"


def test_revoke_blocks_disbursement(env):
    conn, online = env["conn"], env["ids"]["online"]
    service.grant_consent(conn, online)
    offer = service.draft_offer(conn, online, _eligible_product(conn, online))
    service.decide_offer(conn, offer["id"], "approved", actor="owner")
    service.revoke_consent(conn, online)
    result = service.disburse_offer(conn, offer["id"])
    assert result["error"]["code"] == "consent_revoked"


def test_ineligible_offer_blocked(env):
    conn, katering = env["conn"], env["ids"]["katering"]
    service.grant_consent(conn, katering)
    result = service.draft_offer(conn, katering, "receivable_bridge")
    assert result["error"]["code"] == "ineligible"
    assert "capacity" in result["error"]["reason"].lower()
    # the refusal is audited, not silent
    actions = [e["action"] for e in audit_mod.recent(conn, 10)]
    assert "offer_draft_blocked" in actions


def test_repayments_settle_loan(env):
    conn = env["conn"]
    offer, _ = _disburse_flow(env)
    summary = service.sandbox_summary(conn, offer["business_id"])
    expected_total = sum(r["amount"] for r in summary["repayments"])

    first = service.pay_repayment(conn, summary["repayments"][0]["id"])
    assert first["status"] == "paid"
    duplicate = service.pay_repayment(conn, summary["repayments"][0]["id"])
    assert duplicate["error"]["code"] == "duplicate_action"

    for repayment in summary["repayments"][1:]:
        assert service.pay_repayment(conn, repayment["id"])["status"] == "paid"

    summary = service.sandbox_summary(conn, offer["business_id"])
    assert all(r["status"] == "paid" for r in summary["repayments"])
    assert summary["loans"][0]["status"] == "settled"
    assert abs(summary["total_repaid"] - expected_total) < 0.01
    offer_status = conn.execute("SELECT status FROM offers WHERE id = ?",
                                (offer["id"],)).fetchone()["status"]
    assert offer_status == "settled"
    assert audit_mod.verify_chain(conn)["ok"]
