"""Loan sizing, repayment schedule, and recommendation-ordering tests."""
from __future__ import annotations

import math

from cashbridge import config, sizing, util
from cashbridge.recommend import recommend


def test_principal_proportional_to_gap(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["online"], as_of=seeded["as_of"])
    gap = result["gap"]
    principal = result["loan"]["principal_needed"]
    assert payload_ok(principal), "principal must be a round, positive amount"
    assert principal >= config.MIN_LOAN_PRINCIPAL
    # Sized to the gap (+10% buffer, 100k rounding), never capped at a max plafon.
    assert principal <= util.round_100k(gap["depth"] * 1.10) + 100_000


def payload_ok(amount: float) -> bool:
    return amount > 0 and abs(amount / 100_000 - round(amount / 100_000)) < 1e-9


def test_schedule_weekly_and_linked_to_receivables(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["online"], as_of=seeded["as_of"])
    best = result["loan"]["eligible"][0]
    schedule = best["schedule"]
    expected_installments = math.ceil(best["tenor_days"] / 7)
    assert len(schedule) == expected_installments
    total = sum(item["amount"] for item in schedule)
    assert abs(total - best["total_repayment"]) <= 2000
    linked = [item for item in schedule if item["linked_receivable_id"]]
    assert linked, "repayments should be linked to expected receivables"


def test_non_loan_options_ranked(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["katering"], as_of=seeded["as_of"])
    options = result["non_loan"]
    assert options
    assert [o["rank"] for o in options] == list(range(1, len(options) + 1))
    improvements = [o["impact"]["improvement"] for o in options]
    assert improvements == sorted(improvements, reverse=True)
    for option in options:
        impact = option["impact"]
        assert isinstance(impact["new_min_low"], (int, float))
        assert impact["improvement"] >= 0
        assert isinstance(option["detail"], str) and option["detail"]


def test_products_catalog(seeded):
    from cashbridge import products

    products.ensure_products(seeded["conn"])
    catalog = products.list_products(seeded["conn"])
    assert len(catalog) == 3
    for product in catalog:
        assert product["max_amount"] > 0
        assert product["min_coverage_ratio"] >= 1.0
