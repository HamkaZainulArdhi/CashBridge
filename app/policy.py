"""Policy engine: four authorization tiers plus hard guardrails.

T0 view data · T1 give advice · T2 create drafts · T3 execute sandbox actions

Every sensitive action goes through ``check_action`` before anything happens.
The engine is a pure function layer: callers supply the context (granted tier,
consent state, amounts, idempotency bookkeeping) and get an explicit
allow/deny decision with a machine-readable code. Denials are reasons to log,
never silent failures. The LLM has no way to bypass this layer — tools check it
themselves.
"""
from __future__ import annotations

from typing import Optional

TIER_ORDER = ["T0", "T1", "T2", "T3"]

TIER_DESCRIPTIONS = {
    "T0": "view data",
    "T1": "give advice",
    "T2": "create drafts",
    "T3": "execute sandbox actions",
}

# Action -> minimum tier required.
ACTION_TIERS = {
    "view_data": "T0",
    "summarize": "T0",
    "explain": "T0",
    "advise": "T1",
    "recommend_options": "T1",
    "simulate_scenario": "T1",
    "draft_offer": "T2",
    "request_approval": "T2",
    "disburse_sandbox": "T3",
    "schedule_repayment": "T3",
}


def tier_allows(granted: str, required: str) -> bool:
    return TIER_ORDER.index(granted) >= TIER_ORDER.index(required)


def check_action(action: str, *, granted_tier: str = "T0", consent_active: bool = True,
                 amount: Optional[float] = None, max_amount: Optional[float] = None,
                 idempotency_key: Optional[str] = None, used_keys: Optional[set] = None,
                 approved: Optional[bool] = None) -> dict:
    """Return {"allow": bool, "code": str, "reason": str}.

    Codes: ok | unknown_action | consent_revoked | tier_denied | amount_cap |
    duplicate_action | approval_missing
    """
    required = ACTION_TIERS.get(action)
    if required is None:
        return {"allow": False, "code": "unknown_action",
                "reason": f"Unknown action '{action}' is not on the allowlist."}

    if action != "view_data" and not consent_active:
        return {"allow": False, "code": "consent_revoked",
                "reason": "Data-use consent has been revoked; the action is blocked."}

    if not tier_allows(granted_tier, required):
        return {"allow": False, "code": "tier_denied",
                "reason": f"Action '{action}' requires tier {required} "
                          f"({TIER_DESCRIPTIONS[required]}), granted tier is {granted_tier}."}

    if amount is not None and max_amount is not None and amount > max_amount:
        return {"allow": False, "code": "amount_cap",
                "reason": f"Amount {amount:,.0f} exceeds the sandbox cap {max_amount:,.0f}."}

    if idempotency_key and used_keys is not None and idempotency_key in used_keys:
        return {"allow": False, "code": "duplicate_action",
                "reason": f"Idempotency key '{idempotency_key}' was already used; "
                          f"refusing to execute twice."}

    if action in ("disburse_sandbox", "schedule_repayment") and approved is not True:
        return {"allow": False, "code": "approval_missing",
                "reason": f"Action '{action}' requires an explicit owner approval recorded "
                          f"before execution."}

    return {"allow": True, "code": "ok", "reason": f"Action '{action}' permitted at tier {granted_tier}."}
