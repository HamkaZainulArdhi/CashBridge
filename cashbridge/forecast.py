"""Deterministic cash-flow forecast with low/mid/high scenarios.

Everything here is computed from the transaction history and open receivables:
recurring bills are inferred, variable spend is projected from recent weekday
medians, and open receivables are settled according to their expected dates.

The three scenarios are transparent parameter sets (not black boxes):
  * low  — payouts slip 5 days and pay 90%, revenue softer, costs higher
  * mid  — expected timing and amounts
  * high — payouts arrive 2 days early, revenue slightly stronger, costs lower

No LLM anywhere in this module. Numbers in, numbers out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from . import config, db, util

OUTFLOW_CATEGORIES = ["payroll", "rent", "utilities", "inventory", "other_opex"]


@dataclass
class ScenarioParams:
    payout_shift_days: int = 0
    payout_factor: float = 1.0
    cash_rev_factor: float = 1.0
    cost_factor: float = 1.0
    inventory_delay_days: int = 0
    category_factors: dict = field(default_factory=dict)


BASE_SCENARIOS = {
    "low": ScenarioParams(payout_shift_days=7, payout_factor=0.90,
                          cash_rev_factor=0.80, cost_factor=1.15),
    "mid": ScenarioParams(),
    "high": ScenarioParams(payout_shift_days=-3, payout_factor=1.0,
                           cash_rev_factor=1.10, cost_factor=0.90),
}

_ADJUSTABLE = ("payout_shift_days", "payout_factor", "cash_rev_factor",
               "cost_factor", "inventory_delay_days")


def _scenarios_with_adjustments(adjustments: Optional[dict]) -> dict:
    out = {}
    for name, base in BASE_SCENARIOS.items():
        params = replace(base)
        if adjustments:
            for key in _ADJUSTABLE:
                if adjustments.get(key) is not None:
                    setattr(params, key, adjustments[key])
            if adjustments.get("category_factors"):
                params.category_factors = {**params.category_factors,
                                           **adjustments["category_factors"]}
        out[name] = params
    return out


# --------------------------------------------------------------------- profiles

def _load_history(conn, business_id: int, as_of) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT date, amount, direction, category, is_cash_sale
           FROM transactions
           WHERE business_id = ? AND date <= ?""",
        conn,
        params=(business_id, util.format_date(as_of)),
        parse_dates=["date"],
    )


def _recurring_occurrences(df: pd.DataFrame, category: str, as_of: date,
                           horizon_days: int) -> Optional[list]:
    """Detect weekly/biweekly/monthly repeats. Returns occurrences in the
    horizon, or None when the category does not repeat on a stable cadence."""
    sub = df[(df["category"] == category) & (df["direction"] == "out")]
    if sub.empty:
        return None
    daily = sub.groupby(sub["date"].dt.date)["amount"].sum().sort_index()
    dates = list(daily.index)
    if len(dates) < 2:
        return None
    diffs = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    med = util.safe_median(diffs)
    if 6 <= med <= 8:
        step = 7
    elif 13 <= med <= 16:
        step = 14
    elif 27 <= med <= 32:
        step = None  # monthly via day-of-month
    else:
        return None
    amount = util.safe_median(list(daily)[-4:])
    occurrences = []
    last = dates[-1]
    if step is not None:
        k = 1
        while True:
            nxt = last + timedelta(days=step * k)
            offset = (nxt - as_of).days
            if offset > horizon_days:
                break
            if offset >= 1:
                occurrences.append({"day_offset": offset, "amount": float(amount),
                                    "category": category})
            k += 1
    else:
        dom = last.day

        def _clamped(year, month, day):
            while True:
                try:
                    return date(year, month, day)
                except ValueError:
                    day -= 1

        candidate = _clamped(as_of.year, as_of.month, dom)
        if candidate <= as_of:
            nxt = util.month_add(date(as_of.year, as_of.month, 1), 1)
            candidate = _clamped(nxt.year, nxt.month, dom)
        offset = (candidate - as_of).days
        if 1 <= offset <= horizon_days:
            occurrences.append({"day_offset": offset, "amount": float(amount),
                                "category": category})
    return occurrences


def build_profiles(conn, business_id: int, as_of: date, horizon_days: int) -> dict:
    """Learn this business's rhythm: recurring bills + variable weekday medians."""
    df = _load_history(conn, business_id, as_of)
    recent = df[df["date"].dt.date > as_of - timedelta(days=28)]

    cash_in = recent[(recent["category"] == "revenue") & (recent["is_cash_sale"] == 1)]
    cash_rev_by_wd = util.weekday_means(zip(cash_in["date"].dt.date, cash_in["amount"]), 28)
    if cash_in.empty:
        cash_rev_by_wd = {}  # no walk-in revenue at all (e.g. catering)

    scheduled_by_off: dict = {}
    variable_by_category: dict = {}
    for category in OUTFLOW_CATEGORIES:
        occurrences = _recurring_occurrences(df, category, as_of, horizon_days)
        if occurrences is not None:
            for occurrence in occurrences:
                off = occurrence["day_offset"]
                scheduled_by_off[off] = scheduled_by_off.get(off, 0.0) + occurrence["amount"]
        else:
            sub = recent[(recent["category"] == category) & (recent["direction"] == "out")]
            if not sub.empty:
                variable_by_category[category] = util.weekday_means(
                    zip(sub["date"].dt.date, sub["amount"]), 28
                )
    return {
        "cash_rev_by_wd": cash_rev_by_wd,
        "scheduled_by_off": scheduled_by_off,
        "variable_by_category": variable_by_category,
        "history_rows": int(len(df)),
    }


# ------------------------------------------------------------------ simulation

def _payout_schedule(receivables: list, params: ScenarioParams, as_of: date,
                     horizon_days: int) -> dict:
    by_off: dict = {}
    for r in receivables:
        expected = util.parse_date(r["expected_settlement_date"]) + timedelta(
            days=params.payout_shift_days)
        offset = max(1, (expected - as_of).days)
        if offset <= horizon_days:
            by_off[offset] = by_off.get(offset, 0.0) + float(r["amount"]) * params.payout_factor
    return by_off


def _day_flows(profiles: dict, params: ScenarioParams, day: date,
               payout_by_off: dict, offset: int):
    inflow = payout_by_off.get(offset, 0.0)
    if profiles["cash_rev_by_wd"]:
        inflow += profiles["cash_rev_by_wd"][day.weekday()] * params.cash_rev_factor

    outflow = profiles["scheduled_by_off"].get(offset, 0.0)
    for category, wd_medians in profiles["variable_by_category"].items():
        source_day = day
        if category == "inventory" and params.inventory_delay_days:
            source_day = day - timedelta(days=int(params.inventory_delay_days))
        amount = wd_medians[source_day.weekday()] * params.cost_factor
        amount *= params.category_factors.get(category, 1.0)
        outflow += amount
    return inflow, outflow


def run_forecast(conn, business_id: int, as_of=None, horizon_days: Optional[int] = None,
                 adjustments: Optional[dict] = None, persist: bool = True) -> dict:
    """Project the daily balance for the next ``horizon_days`` in three scenarios.

    ``adjustments`` overrides scenario knobs (used for what-if simulations);
    pass ``persist=False`` for simulations so they are not stored as forecasts.
    """
    as_of = util.parse_date(as_of) if as_of else date.today()
    horizon = int(horizon_days or config.HORIZON_DAYS)
    opening = db.opening_balance(conn, business_id, as_of)
    receivables = db.open_receivables(conn, business_id)
    profiles = build_profiles(conn, business_id, as_of, horizon)
    scenarios = _scenarios_with_adjustments(adjustments)
    payout_by_scenario = {
        name: _payout_schedule(receivables, params, as_of, horizon)
        for name, params in scenarios.items()
    }

    balances = {"low": opening, "mid": opening, "high": opening}
    days = []
    for offset in range(1, horizon + 1):
        day = as_of + timedelta(days=offset)
        row = {"date": day.isoformat()}
        for name in ("low", "mid", "high"):
            inflow, outflow = _day_flows(profiles, scenarios[name], day,
                                         payout_by_scenario[name], offset)
            balances[name] += inflow - outflow
            row[f"balance_{name}"] = util.money(balances[name])
            if name == "mid":
                row["inflow_mid"] = util.money(inflow)
                row["outflow_mid"] = util.money(outflow)
        days.append(row)

    result = {
        "business_id": business_id,
        "as_of": as_of.isoformat(),
        "horizon_days": horizon,
        "opening_balance": util.money(opening),
        "open_receivables_total": util.money(sum(r["amount"] for r in receivables)),
        "days": days,
        "adjustments": adjustments or {},
        "scenarios": {name: dict(p.__dict__) for name, p in scenarios.items()},
    }
    if persist:
        cur = conn.execute(
            """INSERT INTO forecasts (business_id, as_of, horizon_days, opening_balance, params)
               VALUES (?,?,?,?,?)""",
            (business_id, result["as_of"], horizon, result["opening_balance"],
             json.dumps({"scenarios": result["scenarios"],
                         "adjustments": result["adjustments"]})),
        )
        forecast_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO forecast_days
               (forecast_id, date, balance_low, balance_mid, balance_high, inflow_mid, outflow_mid)
               VALUES (?,?,?,?,?,?,?)""",
            [(forecast_id, r["date"], r["balance_low"], r["balance_mid"],
              r["balance_high"], r["inflow_mid"], r["outflow_mid"]) for r in days],
        )
        conn.commit()
        result["forecast_id"] = forecast_id
    return result


def min_balance(forecast: dict, scenario: str = "low") -> float:
    return min(row[f"balance_{scenario}"] for row in forecast["days"])


def upcoming_bills(conn, business_id: int, as_of=None, horizon_days: int = 14) -> list:
    """Recurring outflows expected in the next horizon (bill reminders).

    Uses the same cadence detection as the forecast, so the reminders and the
    projection can never disagree. Returns [{date, days_until, category, amount}].
    """
    as_of = util.parse_date(as_of) if as_of else date.today()
    horizon = int(horizon_days or config.HORIZON_DAYS)
    df = _load_history(conn, business_id, as_of)
    bills = []
    for category in OUTFLOW_CATEGORIES:
        occurrences = _recurring_occurrences(df, category, as_of, horizon)
        for occurrence in occurrences or []:
            bills.append({
                "date": (as_of + timedelta(days=occurrence["day_offset"])).isoformat(),
                "days_until": occurrence["day_offset"],
                "category": category,
                "amount": util.money(occurrence["amount"]),
            })
    bills.sort(key=lambda bill: (bill["date"], bill["category"]))
    return bills
