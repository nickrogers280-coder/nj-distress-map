"""Bank-owned (REO) detection from the sheriff's Sold/Cancelled lists."""
import json
from pathlib import Path

import pytest

from tracker import pipeline
from tracker.civilview import ListingRow, SaleDetail, outcome_from_history, sale_outcome, sold_form_data
from tracker.db import DB
from tracker.export import build_map_data
from tracker.parcels import ParcelMatch

FX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("status,expected", [
    # wordings seen on the live county sites (Nov 2026)
    ("Purchased - Plaintiff", "reo"),
    ("Plaintiff Buy Back", "reo"),
    ("Purchased at Auction by Plaintiff (Buy Back)", "reo"),
    ("Sold to Plaintiff", "reo"),
    ("Sold to Plaintiff - Assigned", "reo"),
    ("Purchased - Buy Back", "reo"),
    ("Purchased - Plaintiff FHA/HUD CAFMV", "reo"),
    ("Assignment of Bid", "reo"),
    ("Purchased - 3rd Party", "third_party"),
    ("Purchased at Auction by 3rd Party", "third_party"),
    ("Sold to Third Party Purchaser", "third_party"),
    ("Sold to FHA 3rd Party Purchase", "third_party"),
    ("Purchased - 3rd Party - CWPP", "third_party"),
    ("Cancelled", "cancelled"),
    ("Settled - Foreclosure", "cancelled"),
    ("Adjourned - Plaintiff", None),
    ("Plaintiff Adjournment", None),
    ("Bankruptcy", None),
    ("Scheduled", None),
    ("Purchased", None),
])
def test_sale_outcome_vocabulary(status, expected):
    assert sale_outcome(status) == expected


def test_outcome_uses_latest_outcome_line():
    hist = [("Scheduled", "2026-08-13"), ("Adjourned - Plaintiff - New Sale Date:", "2026-09-10"),
            ("Purchased at Auction by Plaintiff (Buy Back)", "2026-09-10")]
    assert outcome_from_history(hist) == ("reo", "2026-09-10", "Purchased at Auction by Plaintiff (Buy Back)")
    assert outcome_from_history([("Scheduled", "2026-01-01")]) == (None, None, None)


def test_sold_form_data_switches_to_sold_list():
    data, action = sold_form_data((FX / "list_hudson.html").read_text())
    assert action == "/Sales/SalesSearch"
    assert data["IsOpen"] == "false"


class FakeCV:
    def __init__(self):
        self.sold = {10: [
            ListingRow(property_id="S1", county_id=10, sale_date="2026-09-10", address="390 FOREST STREET KEARNY NJ"),
            ListingRow(property_id="S2", county_id=10, sale_date="2026-09-10", address="12 THIRD PARTY AVE"),
            ListingRow(property_id="S3", county_id=10, sale_date="2026-10-01", address="5 PENDING ST"),
            ListingRow(property_id="S4", county_id=10, sale_date="2025-01-01", address="OLD SALE RD"),
        ]}
        self.details = {
            "S1": SaleDetail(property_id="S1", county="Hudson", sheriff_number="20060001",
                             plaintiff="SAMPLE COMMERCIAL BANK", defendant="FOREST STREET HOLDINGS LLC",
                             street="390 FOREST STREET", city="KEARNY", description="BLOCK 12 LOT 3",
                             upset=2_400_000.0, judgment=2_250_000.0,
                             status_history=[("Scheduled", "2026-08-13"),
                                             ("Purchased at Auction by Plaintiff (Buy Back)", "2026-09-10")]),
            "S2": SaleDetail(property_id="S2", county="Hudson", street="12 THIRD PARTY AVE", city="BAYONNE",
                             status_history=[("Purchased - 3rd Party", "2026-09-10")]),
            "S3": SaleDetail(property_id="S3", county="Hudson", street="5 PENDING ST", city="BAYONNE",
                             status_history=[("Bankruptcy", "2026-10-01")]),
        }
        self.calls = []

    def list_open(self, cid):
        return []

    def list_sold(self, cid):
        return list(self.sold.get(cid, []))

    def detail(self, pid):
        self.calls.append(pid)
        return self.details[pid]


class FakeParcels:
    def lookup(self, county, city, street, block, lot, qual):
        if block == "12":
            return ParcelMatch.from_attrs({"PAMS_PIN": "0907_12_3", "MUN_NAME": "KEARNY TOWN", "PCLBLOCK": "12",
                                           "PCLLOT": "3", "PROP_CLASS": "4B", "NET_VALUE": 3_100_000}, "block/lot")
        return None

    def centroid(self, pin):
        return (40.75, -74.12)


CFG = {"counties": [10], "reo_lookback_days": 365, "max_sold_details_per_run": 100}


def test_reo_pass(tmp_path):
    db = DB(tmp_path / "t.db")
    cv = FakeCV()
    s = pipeline.run_reo(CFG, cv, FakeParcels(), db, ts="2026-11-15T11:45:00+00:00")
    assert s["reo_found"] == 1 and s["reo_tracked"] == 1 and s["reo_baseline"] is True
    assert "S4" not in cv.calls                       # outside the look-back window
    reo = db.get("10:20060001")
    assert reo["outcome"] == "reo" and reo["outcome_date"] == "2026-09-10"
    assert reo["category"] == "target" and reo["is_open"] == 0 and reo["lat"] == 40.75
    assert db.get("S2") is None                       # third-party sales aren't stored as properties
    assert db.sold_checked("10:@12 THIRD PARTY AVE")["outcome"] == "third_party"
    assert db.get_meta("reo_baseline_done") == "1"

    # The map shows the REO property; the baseline event is not shown as an alert.
    db.log_run("2026-11-15T11:45:00+00:00", "2026-11-15T12:00:00+00:00", s)
    from datetime import datetime, timezone
    m = build_map_data(db, now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    p = next(x for x in m["properties"] if x["property_id"] == "10:20060001")
    assert p["outcome"] == "reo" and p["outcome_status"].startswith("Purchased at Auction by Plaintiff")
    assert m["alerts"] == []

    # Next run: S1/S2 are settled; S3 (no outcome yet) waits for the re-check interval.
    cv.calls.clear()
    pipeline.run_reo(CFG, cv, FakeParcels(), db, ts="2026-11-16T11:45:00+00:00")
    assert cv.calls == []
    # A week later S3 is re-checked; now it has been bought back -> real alert.
    cv.details["S3"].status_history.append(("Sold to Plaintiff", "2026-11-20"))
    cv.details["S3"].description = "BLOCK 12 LOT 3"
    s3 = pipeline.run_reo(CFG, cv, FakeParcels(), db, ts="2026-11-23T11:45:00+00:00")
    assert cv.calls == ["S3"] and s3["reo_found"] == 1 and s3["reo_baseline"] is False
    ev = db.conn.execute("SELECT kind, baseline FROM events WHERE property_id='10:@5 PENDING ST'").fetchall()
    assert [tuple(e) for e in ev] == [("reo", 0)]


def test_cap_spreads_baseline_over_runs(tmp_path):
    db = DB(tmp_path / "t.db")
    s = pipeline.run_reo({**CFG, "max_sold_details_per_run": 1}, FakeCV(), FakeParcels(), db,
                         ts="2026-11-15T11:45:00+00:00")
    assert s["reo_capped"] is True and db.get_meta("reo_baseline_done") is None


def test_tracked_listing_closing_as_reo(tmp_path):
    """A property already on the map that comes off the open list as a buy-back."""
    db = DB(tmp_path / "t.db")
    db.upsert({"property_id": "P1", "county_id": 10, "county": "Hudson", "is_open": 1, "category": "target",
               "street": "1 MAIN ST", "status_history": json.dumps([["Scheduled", "2026-11-19"]])})
    db.commit()

    class CV:
        def list_open(self, cid):
            return [ListingRow(property_id="OTHER", county_id=10)]

        def detail(self, pid):
            if pid == "OTHER":
                return SaleDetail(property_id="OTHER", street="9 X ST", city="BAYONNE")
            return SaleDetail(property_id=pid, status_history=[("Scheduled", "2026-11-19"),
                                                               ("Purchased - Plaintiff", "2026-11-19")])

    pipeline.run({"counties": [10]}, CV(), FakeParcels(), db, ts="2026-11-20T11:45:00+00:00")
    p = db.get("P1")
    assert p["outcome"] == "reo" and p["is_open"] == 0
    kinds = [r[0] for r in db.conn.execute("SELECT kind FROM events WHERE property_id='P1'")]
    assert kinds == ["closed", "reo"]


def test_migration_adds_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE listings (property_id TEXT PRIMARY KEY, county TEXT)")
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, property_id TEXT, ts TEXT, kind TEXT, "
              "old_value TEXT, new_value TEXT, notified INTEGER DEFAULT 0)")
    c.commit()
    c.close()
    db = DB(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(listings)")}
    assert {"outcome", "outcome_date", "lat", "category"} <= cols
