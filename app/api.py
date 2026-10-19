"""JSON API: dashboard data for the UI + the sandbox bank endpoints.

Two groups:
  /api/business/...   — read models and safe actions for the web UI
  /api/bank/...       — the simulated bank: products, offers, disbursement,
                        repayment. These are the only endpoints that move
                        sandbox money, and every one of them funnels through
                        the same policy engine + audit chain as everything else.

Error contract: service/policy errors come back as JSON
{"detail": {"code": ..., "reason": ...}} with the HTTP status mapped below.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import audit, orchestrator, service, tools
from cashbridge import db, forecast as forecast_mod, products as products_mod, recommend as recommend_mod, weekly

router = APIRouter(prefix="/api")

_CODE_STATUS = {
    "not_found": 404,
    "unknown_tool": 404,
    "tier_denied": 403,
    "consent_revoked": 403,
    "duplicate_action": 409,
    "approval_missing": 409,
    "invalid_state": 409,
    "amount_cap": 422,
    "ineligible": 422,
    "no_gap": 422,
    "invalid_args": 400,
    "no_adjustments": 400,
}


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _fail(error: dict):
    code = error.get("code", "error")
    raise HTTPException(status_code=_CODE_STATUS.get(code, 400),
                        detail={"code": code, "reason": error.get("reason", "")})


def _business_or_404(conn, business_id: int) -> dict:
    business = db.get_business(conn, business_id)
    if business is None:
        raise HTTPException(status_code=404, detail={"code": "not_found",
                                                     "reason": f"Unknown business: {business_id}"})
    return business


# ------------------------------------------------------------------ ui data

class AskBody(BaseModel):
    question: str
    tier: str = "T2"
    engine: str = "auto"  # auto | mock | llm — "mock" pins the template engine (tests)


class SimulateBody(BaseModel):
    adjustments: dict
    horizon_days: Optional[int] = None


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/businesses")
def api_businesses(conn=Depends(get_conn)):
    return {"businesses": service.list_overview(conn)}


@router.get("/business/{business_id}/forecast")
def api_forecast(business_id: int, horizon_days: int = 14, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    result = tools.execute_tool(conn, "run_forecast",
                                {"business_id": business_id, "horizon_days": horizon_days},
                                granted_tier="T0", actor="ui")
    if "error" in result:
        _fail(result["error"])
    return result


@router.get("/business/{business_id}/recommendation")
def api_recommendation(business_id: int, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    return recommend_mod.recommend(conn, business_id)


@router.get("/business/{business_id}/bills")
def api_bills(business_id: int, horizon_days: int = 14, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    bills = forecast_mod.upcoming_bills(conn, business_id, horizon_days=horizon_days)
    sandbox = service.sandbox_summary(conn, business_id)
    repayments = [r for r in sandbox["repayments"] if r["status"] == "scheduled"]
    return {"bills": bills, "bills_total": sum(b["amount"] for b in bills),
            "scheduled_repayments": repayments}


@router.get("/business/{business_id}/delta")
def api_delta(business_id: int, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    return weekly.weekly_delta(conn, business_id)


@router.post("/business/{business_id}/ask")
def api_ask(business_id: int, body: AskBody, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    if body.tier not in ("T0", "T1", "T2", "T3"):
        raise HTTPException(status_code=400, detail={"code": "invalid_args",
                                                     "reason": "tier must be T0..T3"})
    if body.engine not in ("auto", "mock", "llm"):
        raise HTTPException(status_code=400, detail={"code": "invalid_args",
                                                     "reason": "engine must be auto, mock, or llm"})
    force_engine = None if body.engine == "auto" else body.engine
    return orchestrator.answer_question(conn, business_id, body.question,
                                        granted_tier=body.tier, actor="owner",
                                        force_engine=force_engine)


@router.post("/business/{business_id}/simulate")
def api_simulate(business_id: int, body: SimulateBody, conn=Depends(get_conn)):
    _business_or_404(conn, business_id)
    args = {"business_id": business_id, "adjustments": body.adjustments}
    if body.horizon_days:
        args["horizon_days"] = body.horizon_days
    result = tools.execute_tool(conn, "simulate_scenario", args,
                                granted_tier="T2", actor="ui")
    if "error" in result:
        _fail(result["error"])
    return result


# ------------------------------------------------------------ sandbox bank

class OfferBody(BaseModel):
    business_id: int
    product_id: str


class DecisionBody(BaseModel):
    actor: str = "owner"
    note: Optional[str] = None


@router.get("/bank/products")
def bank_products(conn=Depends(get_conn)):
    products_mod.ensure_products(conn)
    return {"products": products_mod.list_products(conn)}


@router.get("/bank/loans")
def bank_loans(business_id: Optional[int] = None, conn=Depends(get_conn)):
    if business_id is not None:
        _business_or_404(conn, business_id)
        return service.sandbox_summary(conn, business_id)
    loans = [dict(r) for r in conn.execute("SELECT * FROM loans ORDER BY id")]
    repayments = [dict(r) for r in conn.execute("SELECT * FROM repayments ORDER BY id")]
    ledger = [dict(r) for r in conn.execute("SELECT * FROM sandbox_ledger ORDER BY id")]
    return {"loans": loans, "repayments": repayments, "ledger": ledger}


@router.post("/bank/offers", status_code=201)
def bank_draft_offer(body: OfferBody, conn=Depends(get_conn)):
    _business_or_404(conn, body.business_id)
    result = service.draft_offer(conn, body.business_id, body.product_id,
                                 actor="bank_sandbox", granted_tier="T2")
    if "error" in result:
        _fail(result["error"])
    return {"offer_id": result["id"], "status": result["status"],
            "product_id": result["product_id"], "principal": result["principal"],
            "tenor_days": result["tenor_days"], "total_repayment": result["total_repayment"],
            "coverage_ratio": result["coverage_ratio"],
            "requires_approval": "disbursement happens only after an explicit owner approval"}


@router.post("/bank/offers/{offer_id}/approve")
def bank_approve(offer_id: int, body: DecisionBody, conn=Depends(get_conn)):
    result = service.decide_offer(conn, offer_id, "approved", actor=body.actor, note=body.note)
    if "error" in result:
        _fail(result["error"])
    return {"offer_id": offer_id, "status": result["status"]}


@router.post("/bank/offers/{offer_id}/reject")
def bank_reject(offer_id: int, body: DecisionBody, conn=Depends(get_conn)):
    result = service.decide_offer(conn, offer_id, "rejected", actor=body.actor, note=body.note)
    if "error" in result:
        _fail(result["error"])
    return {"offer_id": offer_id, "status": result["status"]}


@router.post("/bank/offers/{offer_id}/disburse", status_code=201)
def bank_disburse(offer_id: int, idempotency_key: Optional[str] = Header(default=None),
                  conn=Depends(get_conn)):
    result = service.disburse_offer(conn, offer_id, idempotency_key=idempotency_key,
                                    actor="bank_sandbox")
    if "error" in result:
        _fail(result["error"])
    return result


@router.post("/bank/repayments/{repayment_id}/pay")
def bank_pay(repayment_id: int, conn=Depends(get_conn)):
    # duplicate attempts are guarded by the repayment row state itself
    result = service.pay_repayment(conn, repayment_id, actor="bank_sandbox")
    if "error" in result:
        _fail(result["error"])
    return result


@router.get("/bank/audit")
def bank_audit(limit: int = 25, conn=Depends(get_conn)):
    chain = audit.verify_chain(conn)
    return {"chain": chain, "entries": audit.recent(conn, limit)}
