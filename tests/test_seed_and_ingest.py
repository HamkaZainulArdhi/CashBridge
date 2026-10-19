"""Seed data integrity + CSV ingest tests."""
from __future__ import annotations

from cashbridge import db, ingest


def test_seed_creates_three_businesses(seeded):
    conn = seeded["conn"]
    businesses = db.list_businesses(conn)
    assert [b["sector"] for b in businesses] == ["retail", "catering", "ecommerce"]
    ground_truth = conn.execute(
        "SELECT COUNT(*) AS n FROM ground_truth_transactions"
    ).fetchone()["n"]
    assert ground_truth > 0
    open_receivables = conn.execute(
        "SELECT COUNT(*) AS n FROM receivables WHERE status = 'open'"
    ).fetchone()["n"]
    assert open_receivables > 0


def test_seed_balances_positive(seeded):
    for key, business_id in seeded["ids"].items():
        balance = db.opening_balance(seeded["conn"], business_id, seeded["as_of"])
        assert balance > 0, f"{key} has non-positive balance {balance}"


def test_categorize_rules():
    assert ingest.categorize("Weekly salary - 2 staff", "out") == ("payroll", 0.9)
    assert ingest.categorize("Stock purchase - supplier", "out")[0] == "inventory"
    assert ingest.categorize("Marketplace payout - Tokoo", "in")[0] == "revenue"
    assert ingest.categorize("Electricity & water", "out")[0] == "utilities"
    category, confidence = ingest.categorize("Mystery expense", "out")
    assert category == "other_opex" and confidence < 0.7


CSV_SIMPLE = """date,description,counterparty,amount,direction
2026-01-03,Supplier payment,Gudang Grosir,-2500000,out
2026-01-04,Daily sales,Walk-in,1200000,in
2026-01-05,Ads spend,Tokoo Ads,450000,out
"""

CSV_LOCAL_FORMAT = """tanggal,keterangan,jumlah,tipe
01/07/2026,Belanja stok supplier,1.250.000,keluar
02/07/2026,Penjualan toko,"2,400,000",masuk
"""


def test_parse_csv_simple():
    rows, warnings = ingest.parse_csv(CSV_SIMPLE)
    assert len(rows) == 3 and warnings == []
    assert rows.iloc[0]["direction"] == "out"
    assert rows.iloc[1]["direction"] == "in"
    assert rows.iloc[0]["amount"] == 2_500_000


def test_parse_csv_local_formats():
    rows, warnings = ingest.parse_csv(CSV_LOCAL_FORMAT)
    assert len(rows) == 2
    assert rows.iloc[0]["direction"] == "out"
    assert abs(rows.iloc[0]["amount"] - 1_250_000) < 1
    assert rows.iloc[1]["direction"] == "in"
    assert abs(rows.iloc[1]["amount"] - 2_400_000) < 1


def test_bad_rows_reported_not_silent():
    bad = "date,description,amount\nnot-a-date,Foo,1000\n2026-01-03,Bar,abc\n"
    rows, warnings = ingest.parse_csv(bad)
    assert len(rows) == 0
    assert len(warnings) == 2


def test_ingest_and_dedup(mutable_env):
    conn = mutable_env["conn"]
    business_id = mutable_env["ids"]["sembako"]
    rows, _ = ingest.parse_csv(CSV_SIMPLE)

    first = ingest.ingest_rows(conn, business_id, rows)
    assert first["inserted"] == 3
    assert first["duplicates"] == 0

    second = ingest.ingest_rows(conn, business_id, rows)
    assert second["inserted"] == 0
    assert second["duplicates"] == 3
