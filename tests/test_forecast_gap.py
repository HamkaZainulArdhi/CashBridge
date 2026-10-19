"""Forecast, gap detection, and ground-truth alignment tests."""
from __future__ import annotations

from cashbridge import db, evaluate, util
from cashbridge.forecast import run_forecast
from cashbridge.recommend import recommend


def test_forecast_shape(seeded):
    conn, as_of = seeded["conn"], seeded["as_of"]
    forecast = run_forecast(conn, seeded["ids"]["online"], as_of=as_of)
    assert forecast["horizon_days"] == 14
    assert len(forecast["days"]) == 14
    assert forecast["days"][0]["date"] == util.add_days(as_of, 1).isoformat()
    for prev, cur in zip(forecast["days"], forecast["days"][1:]):
        assert util.days_between(prev["date"], cur["date"]) == 1
    assert abs(forecast["opening_balance"]
               - db.opening_balance(conn, seeded["ids"]["online"], as_of)) < 1


def test_forecast_persists_days(seeded):
    conn = seeded["conn"]
    forecast = run_forecast(conn, seeded["ids"]["sembako"], as_of=seeded["as_of"])
    stored = db.get_forecast(conn, forecast["forecast_id"])
    assert stored is not None
    assert len(stored["days"]) == 14


def test_sembako_healthy_no_gap(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["sembako"], as_of=seeded["as_of"])
    assert result["decision"] == "no_loan_needed"
    assert result["gap"] is None


def test_online_gap_with_loan_available(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["online"], as_of=seeded["as_of"])
    assert result["gap"] is not None, "online store should show a demo cash gap"
    assert result["decision"] == "loan_available"
    assert result["non_loan"], "non-loan options must be presented before loans"
    assert result["loan"]["eligible"][0]["principal"] > 0


def test_katering_loan_blocked(seeded):
    result = recommend(seeded["conn"], seeded["ids"]["katering"], as_of=seeded["as_of"])
    assert result["gap"] is not None, "catering should show a demo cash gap"
    assert result["decision"] == "loan_blocked"
    assert "capacity" in result["loan"]["block_reason"]
    assert result["non_loan"], "blocked loans must still offer non-loan options"
    assert result["gap"]["severity"] in ("expected", "risk")


def test_forecast_band_covers_ground_truth(seeded):
    conn, as_of = seeded["conn"], seeded["as_of"]
    min_coverage = {"sembako": 0.7, "online": 0.6, "katering": 0.4}
    for key, business_id in seeded["ids"].items():
        forecast = run_forecast(conn, business_id, as_of=as_of)
        actual = evaluate.ground_truth_balance_path(conn, business_id, as_of)
        coverage = evaluate.band_coverage(forecast["days"], actual)
        assert coverage["coverage"] >= min_coverage[key], (
            f"{key}: band coverage {coverage['coverage']} below {min_coverage[key]}; "
            f"misses: {coverage['misses'][:3]}"
        )


def test_ground_truth_gap_labels(seeded):
    """Generator honesty check: online should really dip; sembako should not."""
    conn, as_of = seeded["conn"], seeded["as_of"]
    sembako = db.get_business(conn, seeded["ids"]["sembako"])
    online = db.get_business(conn, seeded["ids"]["online"])
    assert not evaluate.ground_truth_gap(conn, sembako["id"], as_of, sembako["safety_balance"])["has_gap"]
    assert evaluate.ground_truth_gap(conn, online["id"], as_of, online["safety_balance"])["has_gap"]
