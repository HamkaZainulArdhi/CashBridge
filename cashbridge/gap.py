"""Cash-gap detection against the business safety threshold."""
from __future__ import annotations

from typing import Optional

from . import config, util


def detect_gaps(conn, business_id: int, forecast: dict, safety_balance: float,
                persist: bool = True) -> list:
    """Find contiguous stretches where the projected balance dips below safety.

    A gap is ``expected`` when the central path dips below the threshold, and
    only a ``risk`` when just the pessimistic path does.
    """
    days = forecast["days"]
    flags = []
    for idx, row in enumerate(days):
        low, mid = row["balance_low"], row["balance_mid"]
        if low < safety_balance or mid < safety_balance:
            flags.append({"idx": idx, "low": low, "mid": mid, "expected": mid < safety_balance})

    if not flags:
        return []

    groups = []
    current = [flags[0]]
    for item in flags[1:]:
        if item["idx"] == current[-1]["idx"] + 1:
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    groups.append(current)

    gaps = []
    for group in groups:
        min_balance = min(min(g["low"], g["mid"]) for g in group)
        depth = max(0.0, safety_balance - min_balance)
        severity = "expected" if any(g["expected"] for g in group) else "risk"
        shortfall = util.round_100k(depth * 1.10) if depth > 0 else 0.0
        if shortfall and shortfall < config.MIN_LOAN_PRINCIPAL:
            shortfall = float(config.MIN_LOAN_PRINCIPAL)
        gap = {
            "business_id": business_id,
            "forecast_id": forecast.get("forecast_id"),
            "start_date": days[group[0]["idx"]]["date"],
            "end_date": days[group[-1]["idx"]]["date"],
            "min_balance": util.money(min_balance),
            "depth": util.money(depth),
            "duration_days": len(group),
            "shortfall": shortfall,
            "severity": severity,
        }
        if persist:
            cur = conn.execute(
                """INSERT INTO gaps
                   (business_id, forecast_id, start_date, end_date, min_balance, depth,
                    duration_days, shortfall, severity)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (gap["business_id"], gap["forecast_id"], gap["start_date"], gap["end_date"],
                 gap["min_balance"], gap["depth"], gap["duration_days"], gap["shortfall"],
                 gap["severity"]),
            )
            gap["gap_id"] = cur.lastrowid
        gaps.append(gap)
    if persist:
        conn.commit()
    return gaps
