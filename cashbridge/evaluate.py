"""Evaluation helpers: ground-truth comparison for forecast coverage and gaps.

These functions read ``ground_truth_transactions``; the product code never does.
"""
from __future__ import annotations

from datetime import timedelta

from . import db, util


def _daily_sums(conn, business_id: int, start, end, table: str = "ground_truth_transactions") -> dict:
    rows = conn.execute(
        f"""SELECT date,
                   COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END), 0) AS net
            FROM {table}
            WHERE business_id = ? AND date > ? AND date <= ?
            GROUP BY date""",
        (business_id, util.format_date(start), util.format_date(end)),
    ).fetchall()
    return {r["date"]: float(r["net"]) for r in rows}


def ground_truth_balance_path(conn, business_id: int, as_of, horizon_days: int = 14) -> list:
    """Actual balance after each of the next ``horizon_days`` days."""
    as_of = util.parse_date(as_of)
    opening = db.opening_balance(conn, business_id, as_of)
    per_day = _daily_sums(conn, business_id, as_of, as_of + timedelta(days=horizon_days))
    path = []
    balance = opening
    for i in range(1, horizon_days + 1):
        d = as_of + timedelta(days=i)
        balance += per_day.get(d.isoformat(), 0.0)
        path.append({"date": d.isoformat(), "balance": util.money(balance)})
    return path


def band_coverage(forecast_days: list, actual_path: list) -> dict:
    """Share of actual balances that fall inside the [low, high] band."""
    actual_by_date = {row["date"]: row["balance"] for row in actual_path}
    inside = total = 0
    misses = []
    for row in forecast_days:
        actual = actual_by_date.get(row["date"])
        if actual is None:
            continue
        total += 1
        if row["balance_low"] <= actual <= row["balance_high"]:
            inside += 1
        else:
            misses.append({"date": row["date"], "actual": actual,
                           "low": row["balance_low"], "high": row["balance_high"]})
    return {
        "coverage": round(inside / total, 3) if total else 0.0,
        "days_checked": total,
        "days_inside": inside,
        "misses": misses,
    }


def ground_truth_gap(conn, business_id: int, as_of, safety_balance: float,
                     horizon_days: int = 14) -> dict:
    """Did the business actually dip below safety in the next N days?"""
    path = ground_truth_balance_path(conn, business_id, as_of, horizon_days)
    below = [p for p in path if p["balance"] < safety_balance]
    if not below:
        return {"has_gap": False, "min_balance": min(p["balance"] for p in path),
                "first_date": None, "horizon_days": horizon_days}
    return {
        "has_gap": True,
        "min_balance": min(p["balance"] for p in below),
        "first_date": below[0]["date"],
        "horizon_days": horizon_days,
    }
