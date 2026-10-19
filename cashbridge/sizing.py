"""Loan sizing, repayment schedules, and repayment-capacity checks.

Capacity heuristic (v1): recent net operating cash flow per week, projected
over the product tenor, must cover the installment total at least
``min_coverage_ratio`` times. Simple, explainable, and conservative — exactly
what we want standing between a user and a loan they cannot repay.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Optional

from . import config, db, util


def size_principal(gap: dict, max_principal: Optional[float] = None) -> float:
    cap = max_principal if max_principal is not None else config.settings.sandbox_max_principal
    principal = max(float(gap["shortfall"]), float(config.MIN_LOAN_PRINCIPAL))
    principal = util.round_100k(principal)
    return min(principal, util.round_100k(cap))


def repayment_total(principal: float, flat_fee_pct: float) -> float:
    return util.money(principal * (1.0 + flat_fee_pct / 100.0))


def recent_weekly_net(conn, business_id: int, as_of,
                      window_days: Optional[int] = None) -> float:
    """Average weekly net operating cash flow over the recent baseline window."""
    window = int(window_days or config.HISTORY_BASELINE_DAYS)
    start = util.parse_date(as_of) - timedelta(days=window)
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END), 0) AS net
           FROM transactions
           WHERE business_id = ? AND date > ? AND date <= ?
             AND category NOT IN ('financing', 'other_income')""",
        (business_id, util.format_date(start), util.format_date(as_of)),
    ).fetchone()
    return float(row["net"]) / (window / 7.0)


def coverage_ratio(weekly_net: float, tenor_days: int, repayment: float) -> float:
    if repayment <= 0:
        return 0.0
    return round(weekly_net * (tenor_days / 7.0) / repayment, 2)


def _nearest_receivable(receivables: list, due, used: set) -> Optional[dict]:
    best, best_delta = None, None
    for r in receivables:
        if r["id"] in used:
            continue
        expected = util.parse_date(r["expected_settlement_date"])
        delta = abs((expected - due).days)
        if delta <= 21 and (best_delta is None or delta < best_delta):
            best, best_delta = r, delta
    return best


def build_schedule(principal: float, product: dict, start, receivables: list) -> list:
    """Weekly installments, each linked to the nearest expected receivable."""
    total = repayment_total(principal, product["flat_fee_pct"])
    installments = max(1, math.ceil(product["tenor_days"] / 7))
    base = util.round_1k(total / installments)
    start = util.parse_date(start)
    used: set = set()
    schedule = []
    remaining = total
    for i in range(1, installments + 1):
        due = start + timedelta(days=7 * i)
        amount = base if i < installments else util.round_1k(remaining)
        link = _nearest_receivable(receivables, due, used)
        if link:
            used.add(link["id"])
        schedule.append({
            "due_date": due.isoformat(),
            "amount": amount,
            "linked_receivable_id": link["id"] if link else None,
            "linked_debtor": link["debtor"] if link else None,
        })
        remaining -= amount
    return schedule


def evaluate_product(conn, business_id: int, gap: dict, product: dict, as_of) -> dict:
    """Size the loan for this gap and check it against one product's rules."""
    principal = size_principal(gap)
    total = repayment_total(principal, product["flat_fee_pct"])
    weekly = recent_weekly_net(conn, business_id, as_of)
    coverage = coverage_ratio(weekly, product["tenor_days"], total)

    eligible, reason = True, None
    if principal > product["max_amount"]:
        eligible = False
        reason = (f"Required principal {util.fmt_money(principal)} exceeds the product cap "
                  f"of {util.fmt_money(product['max_amount'])}.")
    elif coverage < product["min_coverage_ratio"]:
        eligible = False
        reason = (f"Repayment capacity too thin: projected net cash over {product['tenor_days']} days "
                  f"covers only {coverage}x of the installment total "
                  f"(needs {product['min_coverage_ratio']}x).")

    schedule = []
    if eligible:
        schedule = build_schedule(principal, product, as_of, db.open_receivables(conn, business_id))

    return {
        "product_id": product["id"],
        "name": product["name"],
        "provider": product["provider"],
        "description": product["description"],
        "flat_fee_pct": product["flat_fee_pct"],
        "tenor_days": product["tenor_days"],
        "min_coverage_ratio": product["min_coverage_ratio"],
        "principal": principal,
        "total_repayment": total,
        "coverage_ratio": coverage,
        "weekly_net_baseline": util.money(weekly),
        "eligible": eligible,
        "reason": reason,
        "schedule": schedule,
    }
