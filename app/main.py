"""CashBridge web app: FastAPI factory + server-rendered UI.

Pages (Jinja2):
  /                     business overview
  /business/{id}        dashboard: forecast, gap, options, offer flow, weekly
                        delta, bill reminders, ask-the-copilot, simulations
  /business/{id}/upload CSV (re-)upload
  /onboard              first-run: create a business, upload its transactions
  /bank                 sandbox bank page (products, loans, ledger, API docs)
  /audit                tamper-evident audit trail

The JSON + sandbox bank API lives in ``app.api``.

Run it:
    uvicorn app.main:create_app --factory --port 8000
or
    python scripts/run_web.py
"""
from __future__ import annotations

import json
import mimetypes
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, audit, service
from cashbridge import config, db, forecast as forecast_mod
from cashbridge import ingest, recommend as recommend_mod, util, weekly

APP_DIR = Path(__file__).resolve().parent
mimetypes.add_type("font/woff2", ".woff2")  # bundled webfonts serve with the right type


def _short_money(value) -> str:
    """Compact money for ticker chips: 48.5M, 1.2B, 940K."""
    v = float(value or 0)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B".replace(".0B", "B")
    if a >= 1e6:
        return f"{v / 1e6:.1f}M".replace(".0M", "M")
    if a >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["idr"] = lambda v: util.fmt_money(float(v or 0))
templates.env.filters["signed"] = lambda v: ("+" if float(v or 0) > 0 else "") + util.fmt_money(float(v or 0))
templates.env.filters["short"] = _short_money
templates.env.filters["signed_short"] = lambda v: (
    ("+" if float(v or 0) > 0 else "") + _short_money(float(v or 0))
)
templates.env.filters["pretty"] = lambda s: json.dumps(json.loads(s or "{}"), indent=2, sort_keys=True)


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _redirect(url: str, msg: str = None, level: str = "info") -> RedirectResponse:
    if msg:
        from urllib.parse import quote
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}msg={quote(msg)}&level={level}"
    return RedirectResponse(url=url, status_code=303)


def _parse_money(text, default: float = 0.0) -> float:
    """Accept '15000000', '15.000.000', '15,000,000', '15jt' is not supported."""
    if text is None:
        return default
    s = str(text).strip().replace("Rp", "").replace("rp", "").replace(" ", "")
    if not s:
        return default
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = (head.replace(",", "") + "." + tail) if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return default


def _engine_label() -> str:
    if config.settings.mock_llm or not config.settings.openrouter_api_key:
        return "template engine (offline)"
    return f"LLM: {config.settings.openrouter_model}"


def _business_strip(conn, active_id=None) -> list:
    """Business ticker rows for the strip under the top nav (name, balance, 7-day delta)."""
    rows = []
    for overview in service.list_overview(conn):
        delta = weekly.weekly_delta(conn, overview["id"])
        rows.append({
            "id": overview["id"],
            "name": overview["name"],
            "sector": overview["sector"],
            "balance": overview["balance"],
            "safety_balance": overview["safety_balance"],
            "consent_active": overview["consent_active"],
            "delta7": float(delta["delta"]["net"] or 0),
            "active": overview["id"] == active_id,
        })
    return rows


def _dashboard_context(conn, business_id: int, msg: str = None, level: str = "info") -> dict:
    business = db.get_business(conn, business_id)
    recommendation = recommend_mod.recommend(conn, business_id)
    forecast = recommendation["forecast"]
    days = forecast["days"]
    chart = {
        "labels": [day["date"][5:] for day in days],
        "low": [day["balance_low"] for day in days],
        "mid": [day["balance_mid"] for day in days],
        "high": [day["balance_high"] for day in days],
        "safety": recommendation["safety_balance"],
    }
    offers = service.list_offers(conn, business_id)
    for offer in offers:
        last = conn.execute(
            "SELECT decision, actor, note, created_at FROM approvals "
            "WHERE offer_id = ? ORDER BY id DESC LIMIT 1", (offer["id"],)).fetchone()
        offer["last_approval"] = dict(last) if last else None
    sandbox = service.sandbox_summary(conn, business_id)
    upload = dict(conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM transactions "
        "WHERE business_id = ? AND source = 'csv'", (business_id,)).fetchone())
    return {
        "business": business,
        "balance": db.opening_balance(conn, business_id, recommendation["as_of"]),
        "consent_active": service.consent_active(conn, business_id),
        "rec": recommendation,
        "chart": chart,
        "offers": offers,
        "sandbox": sandbox,
        "scheduled": [r for r in sandbox["repayments"] if r["status"] == "scheduled"],
        "bills": forecast_mod.upcoming_bills(conn, business_id),
        "delta": weekly.weekly_delta(conn, business_id),
        "upload": upload,
        "audit_entries": audit.recent(conn, 6),
        "engine_label": _engine_label(),
        "strip": _business_strip(conn, active_id=business_id),
        "nav_active": "businesses",
        "msg": msg,
        "level": level,
    }


def create_app(db_path=None) -> FastAPI:
    app = FastAPI(title="CashBridge", docs_url="/api/docs", redoc_url=None)
    app.state.db_path = str(db_path or config.settings.db_path)
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.include_router(api.router)

    conn = db.connect(app.state.db_path)
    db.init_db(conn)
    conn.close()

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def index(request: Request, msg: str = None, level: str = "info", conn=Depends(get_conn)):
        businesses = _business_strip(conn)
        for business in businesses:
            recommendation = recommend_mod.recommend(conn, business["id"], persist=False)
            business["decision"] = recommendation["decision"]
            business["gap"] = recommendation["gap"]
        return templates.TemplateResponse(request, "index.html", {
            "businesses": businesses, "strip": businesses, "nav_active": "businesses",
            "engine_label": _engine_label(), "msg": msg, "level": level})

    @app.get("/business/{business_id}")
    def dashboard(request: Request, business_id: int, msg: str = None, level: str = "info",
                  conn=Depends(get_conn)):
        if db.get_business(conn, business_id) is None:
            return _redirect("/", f"Unknown business {business_id}", "error")
        context = _dashboard_context(conn, business_id, msg=msg, level=level)
        return templates.TemplateResponse(request, "dashboard.html", context)

    # ---------------------------------------------------------------- actions

    @app.post("/business/{business_id}/consent/grant")
    def consent_grant(business_id: int, conn=Depends(get_conn)):
        service.grant_consent(conn, business_id, actor="owner")
        return _redirect(f"/business/{business_id}", "Data-use consent granted.", "success")

    @app.post("/business/{business_id}/consent/revoke")
    def consent_revoke(business_id: int, conn=Depends(get_conn)):
        service.revoke_consent(conn, business_id, actor="owner")
        return _redirect(f"/business/{business_id}",
                         "Consent revoked. Analysis actions are now blocked until you re-grant.",
                         "info")

    @app.post("/business/{business_id}/offers")
    def offer_draft(business_id: int, product_id: str = Form(...), conn=Depends(get_conn)):
        result = service.draft_offer(conn, business_id, product_id, actor="owner")
        if "error" in result:
            return _redirect(f"/business/{business_id}", result["error"]["reason"], "error")
        return _redirect(f"/business/{business_id}",
                         f"Offer #{result['id']} drafted. Review it, then approve or reject.",
                         "success")

    @app.post("/offers/{offer_id}/approve")
    def offer_approve(offer_id: int, conn=Depends(get_conn)):
        result = service.decide_offer(conn, offer_id, "approved", actor="owner")
        return _offer_redirect(conn, offer_id, result, "Offer approved.")

    @app.post("/offers/{offer_id}/reject")
    def offer_reject(offer_id: int, conn=Depends(get_conn)):
        result = service.decide_offer(conn, offer_id, "rejected", actor="owner")
        return _offer_redirect(conn, offer_id, result, "Offer rejected.")

    @app.post("/offers/{offer_id}/disburse")
    def offer_disburse(offer_id: int, conn=Depends(get_conn)):
        result = service.disburse_offer(conn, offer_id, actor="owner")
        return _offer_redirect(conn, offer_id, result,
                               "Sandbox disbursement executed. Repayment schedule created.")

    def _offer_redirect(conn, offer_id: int, result: dict, success_msg: str):
        row = conn.execute("SELECT business_id FROM offers WHERE id = ?", (offer_id,)).fetchone()
        target = f"/business/{row['business_id']}" if row else "/"
        if "error" in result:
            return _redirect(target, result["error"]["reason"], "error")
        return _redirect(target, success_msg, "success")

    @app.post("/repayments/{repayment_id}/pay")
    def repayment_pay(repayment_id: int, conn=Depends(get_conn)):
        row = conn.execute(
            """SELECT o.business_id FROM repayments rp
               JOIN loans l ON l.id = rp.loan_id JOIN offers o ON o.id = l.offer_id
               WHERE rp.id = ?""", (repayment_id,)).fetchone()
        target = f"/business/{row['business_id']}" if row else "/"
        result = service.pay_repayment(conn, repayment_id, actor="owner")
        if "error" in result:
            return _redirect(target, result["error"]["reason"], "error")
        note = ("All installments paid; the sandbox loan is settled."
                if result["remaining"] == 0
                else f"Installment paid (sandbox). {result['remaining']} remaining.")
        return _redirect(target, note, "success")

    # ----------------------------------------------------------------- upload

    @app.post("/business/{business_id}/upload")
    async def upload(business_id: int, file: UploadFile = File(...), conn=Depends(get_conn)):
        if db.get_business(conn, business_id) is None:
            return _redirect("/", f"Unknown business {business_id}", "error")
        data = await file.read()
        try:
            rows, warnings = ingest.parse_csv(data)
        except ValueError as exc:
            return _redirect(f"/business/{business_id}", str(exc), "error")
        if rows.empty:
            return _redirect(f"/business/{business_id}",
                             "No usable rows found in the file; check the column headers.", "error")
        result = ingest.ingest_rows(conn, business_id, rows, source="csv")
        audit.append(conn, "owner", "csv_uploaded", "business", str(business_id),
                     {"file": file.filename, "inserted": result["inserted"],
                      "duplicates": result["duplicates"], "warnings": len(warnings),
                      "needs_review": result["review_count"]})
        parts = [f"{result['inserted']} new transactions imported"]
        if result["duplicates"]:
            parts.append(f"{result['duplicates']} duplicates skipped")
        if warnings:
            parts.append(f"{len(warnings)} rows need review")
        if result["review_count"]:
            parts.append(f"{result['review_count']} categorized with low confidence")
        level = "success" if result["inserted"] else "info"
        return _redirect(f"/business/{business_id}", "; ".join(parts) + ".", level)

    # ---------------------------------------------------------------- onboard

    @app.get("/onboard")
    def onboard_form(request: Request, msg: str = None, level: str = "info", conn=Depends(get_conn)):
        return templates.TemplateResponse(request, "onboard.html",
                                          {"msg": msg, "level": level,
                                           "strip": _business_strip(conn),
                                           "nav_active": "onboard",
                                           "engine_label": _engine_label()})

    @app.post("/onboard")
    async def onboard_submit(request: Request,
                             name: str = Form(...),
                             sector: str = Form("retail"),
                             safety_balance: str = Form("0"),
                             opening_balance: str = Form("0"),
                             file: UploadFile = File(None),
                             conn=Depends(get_conn)):
        cur = conn.execute("INSERT INTO businesses (name, sector, safety_balance) VALUES (?,?,?)",
                           (name.strip(), sector.strip() or "retail",
                            _parse_money(safety_balance)))
        business_id = cur.lastrowid
        opening = _parse_money(opening_balance)
        if opening > 0:
            conn.execute(
                """INSERT INTO transactions
                   (business_id, date, description, counterparty, amount, direction, category,
                    is_cash_sale, source)
                   VALUES (?,?,?,?,?,'in','other_income',0,'onboard')""",
                (business_id, util.format_date(date.today()), "Opening balance", "Owner",
                 util.money(opening)))
        conn.commit()
        service.grant_consent(conn, business_id, actor="owner")

        imported = None
        if file is not None and file.filename:
            data = await file.read()
            try:
                rows, warnings = ingest.parse_csv(data)
                if not rows.empty:
                    imported = ingest.ingest_rows(conn, business_id, rows, source="csv")
                    audit.append(conn, "owner", "csv_uploaded", "business", str(business_id),
                                 {"file": file.filename, "inserted": imported["inserted"],
                                  "duplicates": imported["duplicates"], "warnings": len(warnings)})
            except ValueError as exc:
                return _redirect(f"/business/{business_id}",
                                 f"Business created, but the CSV was rejected: {exc}", "error")

        msg = "Business created with consent granted."
        if imported:
            msg += f" {imported['inserted']} transactions imported."
        return _redirect(f"/business/{business_id}", msg, "success")

    # ------------------------------------------------------------- reference

    @app.get("/audit")
    def audit_page(request: Request, limit: int = 100, conn=Depends(get_conn)):
        return templates.TemplateResponse(request, "audit.html", {
            "chain": audit.verify_chain(conn),
            "entries": audit.recent(conn, min(max(limit, 10), 500)),
            "strip": _business_strip(conn),
            "nav_active": "audit",
            "engine_label": _engine_label()})

    @app.get("/bank")
    def bank_page(request: Request, conn=Depends(get_conn)):
        ledger = [dict(r) for r in conn.execute(
            """SELECT sl.*, b.name AS business_name FROM sandbox_ledger sl
               JOIN businesses b ON b.id = sl.business_id ORDER BY sl.id DESC LIMIT 50""")]
        loans = [dict(r) for r in conn.execute(
            """SELECT l.*, o.product_id, o.status AS offer_status, o.principal,
                      b.name AS business_name
               FROM loans l JOIN offers o ON o.id = l.offer_id
               JOIN businesses b ON b.id = o.business_id ORDER BY l.id DESC LIMIT 50""")]
        products = [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY tenor_days")]
        return templates.TemplateResponse(request, "bank.html", {
            "products": products, "loans": loans, "ledger": ledger,
            "chain": audit.verify_chain(conn),
            "strip": _business_strip(conn),
            "nav_active": "bank",
            "engine_label": _engine_label()})

    return app
