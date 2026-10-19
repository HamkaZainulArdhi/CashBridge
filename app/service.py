"""Domain service layer: consent, dashboards, offers, approvals, sandbox actions.

This is the only place that mutates money-adjacent state (offers, loans,
repayments, ledger). Every mutation goes through the policy engine and writes
an audit entry. The LLM never calls these directly — it goes through the tool
layer, which uses this service.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from . import audit, policy
from cashbridge import config, db, products as products_mod, recommend as recommend_mod, util

DEFAULT_SCOPE = "transactions:read,loan:simulate"


class ServiceError(Exception):
    """Raised for not-found or invalid-state operations."""


# ------------------------------------------------------------------ consent

def grant_consent(conn, business_id: int, scope: str = DEFAULT_SCOPE, actor: str = "owner") -> dict:
    if db.get_business(conn, business_id) is None:
        raise ServiceError(f"Unknown business: {business_id}")
    cur = conn.execute("INSERT INTO consents (business_id, scope) VALUES (?,?)", (business_id, scope))
    conn.commit()
    entry = audit.append(conn, actor, "consent_granted", "business", str(business_id), {"scope": scope})
    return {"consent_id": cur.lastrowid, "business_id": business_id, "scope": scope, "audit": entry}


def revoke_consent(conn, business_id: int, actor: str = "owner") -> dict:
    cur = conn.execute(
        "UPDATE consents SET revoked_at = datetime('now') WHERE business_id = ? AND revoked_at IS NULL",
        (business_id,),
    )
    conn.commit()
    entry = audit.append(conn, actor, "consent_revoked", "business", str(business_id),
                         {"revoked_count": cur.rowcount})
    return {"business_id": business_id, "revoked_count": cur.rowcount, "audit": entry}


def consent_active(conn, business_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM consents WHERE business_id = ? AND revoked_at IS NULL LIMIT 1",
        (business_id,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- dashboards

def list_overview(conn, as_of=None) -> list:
    as_of = util.parse_date(as_of) if as_of else date.today()
    out = []
    for business in db.list_businesses(conn):
        out.append({
            "id": business["id"],
            "name": business["name"],
            "sector": business["sector"],
            "safety_balance": business["safety_balance"],
            "balance": db.opening_balance(conn, business["id"], as_of),
            "consent_active": consent_active(conn, business["id"]),
        })
    return out


def dashboard(conn, business_id: int, as_of=None) -> dict:
    result = recommend_mod.recommend(conn, business_id, as_of=as_of)
    result["consent_active"] = consent_active(conn, business_id)
    result["offers"] = list_offers(conn, business_id)
    result["sandbox"] = sandbox_summary(conn, business_id)
    return result


# ------------------------------------------------------------------- offers

def _offer_row(conn, offer_id: int) -> dict:
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if row is None:
        raise ServiceError(f"Unknown offer: {offer_id}")
    return dict(row)


def list_offers(conn, business_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM offers WHERE business_id = ? ORDER BY id DESC", (business_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def draft_offer(conn, business_id: int, product_id: str, actor: str = "agent",
                granted_tier: str = "T2", as_of=None) -> dict:
    """Draft a proportional short-term loan offer for one eligible product."""
    decision = policy.check_action("draft_offer", granted_tier=granted_tier,
                                   consent_active=consent_active(conn, business_id))
    if not decision["allow"]:
        audit.append(conn, actor, "offer_draft_denied", "business", str(business_id),
                     {"product_id": product_id, "reason": decision["reason"], "code": decision["code"]})
        return {"error": decision}

    products_mod.ensure_products(conn)
    recommendation = recommend_mod.recommend(conn, business_id, as_of=as_of)
    if not recommendation["gap"]:
        return {"error": {"code": "no_gap", "reason": "No cash gap projected; a loan is not warranted."}}

    evaluation = next(
        (e for e in recommendation["loan"]["products"] if e["product_id"] == product_id), None
    )
    if evaluation is None:
        raise ServiceError(f"Unknown product for this business: {product_id}")
    if not evaluation["eligible"]:
        audit.append(conn, actor, "offer_draft_blocked", "business", str(business_id),
                     {"product_id": product_id, "reason": evaluation["reason"]})
        return {"error": {"code": "ineligible", "reason": evaluation["reason"]}}

    gap = recommendation["gap"]
    rationale = (
        f"Gap of {util.fmt_money(gap['depth'])} projected starting {gap['start_date']}. "
        f"Principal targets the shortfall with a 10% buffer. "
        f"Repayment capacity covers {evaluation['coverage_ratio']}x of installments "
        f"(minimum {evaluation['min_coverage_ratio']}x)."
    )
    cur = conn.execute(
        """INSERT INTO offers
           (business_id, gap_id, product_id, principal, tenor_days, total_repayment,
            coverage_ratio, schedule, rationale, status)
           VALUES (?,?,?,?,?,?,?,?,?,'draft')""",
        (business_id, gap.get("gap_id"), product_id, evaluation["principal"],
         evaluation["tenor_days"], evaluation["total_repayment"], evaluation["coverage_ratio"],
         json.dumps(evaluation["schedule"]), rationale),
    )
    conn.commit()
    offer_id = cur.lastrowid
    audit.append(conn, actor, "offer_drafted", "offer", str(offer_id),
                 {"business_id": business_id, "product_id": product_id,
                  "principal": evaluation["principal"], "coverage_ratio": evaluation["coverage_ratio"]})
    return {**_offer_row(conn, offer_id), "evaluation": evaluation, "recommendation_message": recommendation["message"]}


def decide_offer(conn, offer_id: int, decision: str, actor: str = "owner",
                 note: Optional[str] = None) -> dict:
    """Owner approves or rejects a draft offer. This is the human-in-the-loop gate."""
    if decision not in ("approved", "rejected"):
        raise ServiceError(f"Decision must be 'approved' or 'rejected', got {decision!r}")
    offer = _offer_row(conn, offer_id)
    if offer["status"] != "draft":
        return {"error": {"code": "invalid_state",
                          "reason": f"Offer {offer_id} is '{offer['status']}', only drafts can be decided."}}
    conn.execute("INSERT INTO approvals (offer_id, actor, decision, note) VALUES (?,?,?,?)",
                 (offer_id, actor, decision, note))
    conn.execute("UPDATE offers SET status = ? WHERE id = ?", (decision, offer_id))
    conn.commit()
    audit.append(conn, actor, f"offer_{decision}", "offer", str(offer_id),
                 {"note": note, "principal": offer["principal"], "product_id": offer["product_id"]})
    return _offer_row(conn, offer_id)


def disburse_offer(conn, offer_id: int, idempotency_key: Optional[str] = None,
                   actor: str = "agent") -> dict:
    """Execute the sandbox disbursement for an approved offer. Simulated funds only."""
    offer = _offer_row(conn, offer_id)
    key = idempotency_key or f"offer-{offer_id}-disburse"
    used_keys = {r["idempotency_key"] for r in
                 conn.execute("SELECT idempotency_key FROM sandbox_ledger WHERE idempotency_key IS NOT NULL")}
    approved = conn.execute(
        "SELECT 1 FROM approvals WHERE offer_id = ? AND decision = 'approved' LIMIT 1", (offer_id,)
    ).fetchone() is not None

    decision = policy.check_action(
        "disburse_sandbox", granted_tier="T3", consent_active=consent_active(conn, offer["business_id"]),
        amount=offer["principal"], max_amount=config.settings.sandbox_max_principal,
        idempotency_key=key, used_keys=used_keys, approved=approved and offer["status"] == "approved",
    )
    if not decision["allow"]:
        audit.append(conn, actor, "disbursement_denied", "offer", str(offer_id),
                     {"code": decision["code"], "reason": decision["reason"]})
        return {"error": decision}

    cur = conn.execute(
        "INSERT INTO loans (offer_id, principal, status, disbursed_at) VALUES (?,?,'active',datetime('now'))",
        (offer_id, offer["principal"]),
    )
    loan_id = cur.lastrowid
    schedule = json.loads(offer["schedule"] or "[]")
    for installment in schedule:
        conn.execute(
            """INSERT INTO repayments (loan_id, due_date, amount, status, linked_receivable_id)
               VALUES (?,?,?,'scheduled',?)""",
            (loan_id, installment["due_date"], installment["amount"],
             installment.get("linked_receivable_id")),
        )
    conn.execute(
        "INSERT INTO sandbox_ledger (business_id, loan_id, entry_type, amount, idempotency_key) VALUES (?,?,'disbursement',?,?)",
        (offer["business_id"], loan_id, offer["principal"], key),
    )
    conn.execute("UPDATE offers SET status = 'disbursed' WHERE id = ?", (offer_id,))
    conn.commit()
    audit.append(conn, actor, "loan_disbursed_sandbox", "loan", str(loan_id),
                 {"offer_id": offer_id, "principal": offer["principal"],
                  "installments": len(schedule), "idempotency_key": key})
    return {"loan_id": loan_id, "offer_id": offer_id, "principal": offer["principal"],
            "installments": len(schedule), "idempotency_key": key}


# ------------------------------------------------------------------ sandbox

def sandbox_summary(conn, business_id: int) -> dict:
    loans = [dict(r) for r in conn.execute(
        """SELECT l.*, o.product_id, o.status AS offer_status
           FROM loans l JOIN offers o ON o.id = l.offer_id
           WHERE o.business_id = ? ORDER BY l.id""", (business_id,))]
    repayments = [dict(r) for r in conn.execute(
        """SELECT rp.* FROM repayments rp JOIN loans l ON l.id = rp.loan_id
           JOIN offers o ON o.id = l.offer_id
           WHERE o.business_id = ? ORDER BY rp.due_date""", (business_id,))]
    ledger = [dict(r) for r in conn.execute(
        "SELECT * FROM sandbox_ledger WHERE business_id = ? ORDER BY id", (business_id,))]
    return {"loans": loans, "repayments": repayments, "ledger": ledger,
            "total_disbursed": sum(e["amount"] for e in ledger if e["entry_type"] == "disbursement"),
            "total_repaid": sum(e["amount"] for e in ledger if e["entry_type"] == "repayment")}


def pay_repayment(conn, repayment_id: int, actor: str = "owner") -> dict:
    """Simulate an installment being paid from incoming receivables."""
    row = conn.execute(
        """SELECT rp.*, l.id AS loan_id, o.business_id
           FROM repayments rp JOIN loans l ON l.id = rp.loan_id JOIN offers o ON o.id = l.offer_id
           WHERE rp.id = ?""", (repayment_id,)).fetchone()
    if row is None:
        raise ServiceError(f"Unknown repayment: {repayment_id}")
    repayment = dict(row)
    if repayment["status"] == "paid":
        return {"error": {"code": "duplicate_action", "reason": "This installment is already marked paid."}}

    key = f"repay-{repayment_id}"
    used_keys = {r["idempotency_key"] for r in
                 conn.execute("SELECT idempotency_key FROM sandbox_ledger WHERE idempotency_key IS NOT NULL")}
    decision = policy.check_action("schedule_repayment", granted_tier="T3",
                                   consent_active=consent_active(conn, repayment["business_id"]),
                                   amount=repayment["amount"],
                                   max_amount=config.settings.sandbox_max_principal,
                                   idempotency_key=key, used_keys=used_keys, approved=True)
    if not decision["allow"]:
        audit.append(conn, actor, "repayment_denied", "repayment", str(repayment_id),
                     {"code": decision["code"], "reason": decision["reason"]})
        return {"error": decision}

    conn.execute("UPDATE repayments SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
                 (repayment_id,))
    conn.execute(
        "INSERT INTO sandbox_ledger (business_id, loan_id, entry_type, amount, idempotency_key) VALUES (?,?,'repayment',?,?)",
        (repayment["business_id"], repayment["loan_id"], repayment["amount"], key),
    )
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM repayments WHERE loan_id = ? AND status != 'paid'",
        (repayment["loan_id"],)).fetchone()["n"]
    if remaining == 0:
        conn.execute("UPDATE loans SET status = 'settled' WHERE id = ?", (repayment["loan_id"],))
        conn.execute("UPDATE offers SET status = 'settled' WHERE id = (SELECT offer_id FROM loans WHERE id = ?)",
                     (repayment["loan_id"],))
    conn.commit()
    audit.append(conn, actor, "repayment_paid_sandbox", "repayment", str(repayment_id),
                 {"amount": repayment["amount"], "loan_id": repayment["loan_id"], "remaining": remaining})
    return {"repayment_id": repayment_id, "status": "paid", "remaining": remaining}
