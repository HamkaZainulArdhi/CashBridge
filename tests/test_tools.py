"""Tool-layer tests: policy dispatch, adjustment whitelist, tier gates."""
from __future__ import annotations

import pytest

from app import audit as audit_mod
from app import service, tools as tools_mod


@pytest.fixture()
def consented(seeded):
    """Explicit data-use consent for the demo businesses (as in onboarding)."""
    for key, business_id in seeded["ids"].items():
        if not service.consent_active(seeded["conn"], business_id):
            service.grant_consent(seeded["conn"], business_id)
    return seeded


def test_simulate_scenario_whitelist_and_clamps(consented):
    conn, online = consented["conn"], consented["ids"]["online"]
    result = tools_mod.execute_tool(
        conn, "simulate_scenario",
        {"business_id": online,
         "adjustments": {"payout_shift_days": -3, "hack": "drop me",
                         "inventory_delay_days": 99,
                         "category_factors": {"other_opex": 0.6, "bogus": 5}}},
        granted_tier="T2", actor="agent")
    assert "error" not in result, result
    assert result["applied_adjustments"] == {"payout_shift_days": -3.0,
                                             "inventory_delay_days": 14.0,
                                             "category_factors": {"other_opex": 0.6}}
    assert "simulated_forecast" in result


def test_simulate_requires_adjustments(consented):
    result = tools_mod.execute_tool(
        consented["conn"], "simulate_scenario",
        {"business_id": consented["ids"]["online"], "adjustments": {"nonsense": 1}},
        granted_tier="T2", actor="agent")
    assert result["error"]["code"] == "no_adjustments"


def test_simulate_is_read_only(consented):
    conn = consented["conn"]
    before = conn.execute("SELECT COUNT(*) AS n FROM forecasts").fetchone()["n"]
    tools_mod.execute_tool(conn, "run_forecast", {"business_id": consented["ids"]["online"]},
                           granted_tier="T0", actor="agent")
    tools_mod.execute_tool(conn, "simulate_scenario",
                           {"business_id": consented["ids"]["online"],
                            "adjustments": {"payout_shift_days": -3}},
                           granted_tier="T2", actor="agent")
    after = conn.execute("SELECT COUNT(*) AS n FROM forecasts").fetchone()["n"]
    assert after == before  # simulations are never persisted


def test_unknown_tool_rejected(seeded):
    result = tools_mod.execute_tool(seeded["conn"], "wire_money", {}, granted_tier="T3",
                                    actor="agent")
    assert result["error"]["code"] == "unknown_tool"


def test_unknown_business_returns_error(seeded):
    result = tools_mod.execute_tool(seeded["conn"], "get_cash_overview",
                                    {"business_id": 99999}, granted_tier="T0", actor="agent")
    assert result["error"]["code"] == "not_found"


def test_receivable_debtor_is_wrapped(seeded):
    result = tools_mod.execute_tool(seeded["conn"], "get_receivables",
                                    {"business_id": seeded["ids"]["online"]},
                                    granted_tier="T0", actor="agent")
    assert result["receivables"]
    assert result["receivables"][0]["debtor"].startswith("<untrusted_data")


def test_draft_tier_gate_and_audit(consented):
    conn, online = consented["conn"], consented["ids"]["online"]
    result = tools_mod.execute_tool(conn, "draft_offer",
                                    {"business_id": online, "product_id": "receivable_bridge"},
                                    granted_tier="T1", actor="agent")
    assert result["error"]["code"] == "tier_denied"
    denied = [e["action"] for e in audit_mod.recent(conn, 10)]
    assert "tool_denied:draft_offer" in denied
    assert audit_mod.verify_chain(conn)["ok"]
