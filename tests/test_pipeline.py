"""End-to-end pipeline test with fake sheriff-sale and parcel clients (no network)."""
import json
from pathlib import Path

from tracker import notify, pipeline
from tracker.civilview import ListingRow, SaleDetail, parse_detail, parse_list
from tracker.db import DB
from tracker.parcels import ParcelMatch

FX = Path(__file__).parent / "fixtures"


class FakeCV:
    def __init__(self):
        self.lists = {10: parse_list((FX / "list_hudson.html").read_text(), 10)}
        hudson = parse_detail((FX / "detail_hudson.html").read_text(), "2243981658")
        self.details = {
            "2243981658": hudson,
            "2243990001": SaleDetail(
                property_id="2243990001", county="Hudson", sheriff_number="20061234",
                court_case="F-009999-25", sale_date="2026-12-10", plaintiff="VALLEY NATIONAL BANK",
                defendant="TONNELLE HOLDINGS LLC; JOHN DOE; MIDLAND FUNDING LLC",
                address="455 TONNELLE AVENUE JERSEY CITY NJ 07307", street="455 TONNELLE AVENUE",
                city="JERSEY CITY", zip="07307", description="BLOCK 1501 LOT 22",
                judgment=3_850_000.0, upset=4_100_000.0, attorney="X LAW",
                status_history=[("Scheduled", "2026-12-10")]),
            "2243990002": SaleDetail(
                property_id="2243990002", county="Hudson", sheriff_number="20061999",
                sale_date="2026-12-10", plaintiff="ACME CAPITAL LENDING, LLC",
                defendant="HARBOR DRIVE PARTNERS, LLC, ET AL.", street="88 HARBOR DRIVE",
                city="BAYONNE", zip="07002", description="", upset=1_200_000.0,
                status_history=[("Scheduled", "2026-12-10")]),
        }
        self.detail_calls = 0

    def list_open(self, cid):
        return list(self.lists.get(cid, []))

    def detail(self, pid):
        self.detail_calls += 1
        return self.details[pid]


class FakeParcels:
    def lookup(self, county, city, street, block, lot, qual):
        if block == "40":  # the North Bergen condo
            return ParcelMatch.from_attrs({"PAMS_PIN": "0908_40_3.04_C0304", "MUN_NAME": "NORTH BERGEN TWP",
                                           "PCLBLOCK": "40", "PCLLOT": "3.04", "PCLQCODE": "C0304",
                                           "PROP_CLASS": "2", "NET_VALUE": 320700}, "block/lot+qual")
        if block == "1501":
            return ParcelMatch.from_attrs({"PAMS_PIN": "0906_1501_22", "MUN_NAME": "JERSEY CITY",
                                           "PCLBLOCK": "1501", "PCLLOT": "22", "PROP_CLASS": "4A",
                                           "NET_VALUE": 5_200_000, "BLDG_DESC": "1S-B-RETAIL",
                                           "CALC_ACRE": 0.84, "YR_CONSTR": 1962, "SALE_PRICE": 2_900_000,
                                           "DEED_DATE": "150611"}, "block/lot")
        return None

    def centroid(self, pin):
        return (40.7451, -74.0602)


CFG = {"counties": [10], "target_classes": ["1", "4A", "4B", "4C"], "include_possible": True,
       "refresh_tracked_details_hours": 20}


def test_full_cycle(tmp_path):
    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()

    # ---- run 1: baseline
    stats = pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")
    assert stats["baseline"] and stats["new"] == 3 and stats["new_tracked"] == 2
    condo = db.get("2243981658")
    store = db.get("2243990001")
    harbor = db.get("2243990002")
    assert condo["category"] == "other" and condo["prop_class"] == "2"
    assert store["category"] == "target" and store["class_label"] == "Commercial"
    assert store["lat"] == 40.7451 and store["last_sale_date"] == "2015-06-11"
    assert harbor["category"] == "possible"

    report = notify.build_report(db, stats, CFG, "2026-11-14T11:45:00+00:00")
    assert [c["property_id"] for c in report["new_target"]] == ["2243990001"]
    assert [c["property_id"] for c in report["new_possible"]] == ["2243990002"]
    assert report["new_target"][0]["debt_to_value"] == 79
    html = notify.render(report)
    assert "455 Tonnelle Avenue" in html and "Possibly commercial" in html
    assert "baseline" in notify.subject(report, CFG)
    db.mark_notified(report["event_ids"])

    # ---- run 2 (next day): Tonnelle adjourned with a new upset, Harbor drops off the list
    d = cv.details["2243990001"]
    d.status_history = d.status_history + [("Adjourned - Plaintiff", "2027-01-07")]
    d.sale_date, d.upset = "2027-01-07", 4_250_000.0
    cv.lists[10] = [r for r in cv.lists[10] if r.property_id != "2243990002"]
    cv.details["2243990002"].status_history.append(("Cancelled", "2026-11-14"))
    cv.lists[10][1].sale_date = "2027-01-07"

    calls_before = cv.detail_calls
    stats2 = pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T11:45:00+00:00")
    assert not stats2["baseline"] and stats2["new"] == 0 and stats2["closed"] == 1
    # Only the tracked property gets its detail re-read (plus the final-status read for Harbor);
    # the residential condo is not re-fetched.
    assert cv.detail_calls - calls_before == 2

    report2 = notify.build_report(db, stats2, CFG, "2026-11-15T11:45:00+00:00")
    assert len(report2["changes"]) == 1
    lines = {ln["what"]: ln for ln in report2["changes"][0]["lines"]}
    assert lines["Status"]["new"] == "Adjourned - Plaintiff (Jan 7, 2027)"
    assert lines["Sale date"]["new"] == "Jan 7, 2027"
    assert lines["Upset price"]["old"] == "$4,100,000" and lines["Upset price"]["new"] == "$4,250,000"
    assert report2["closed"][0]["final"] == "Cancelled"
    assert db.get("2243990002")["is_open"] == 0
    subj = notify.subject(report2, CFG)
    assert "2 updates" in subj
    db.mark_notified(report2["event_ids"])

    # ---- run 3 (a few hours later): nothing due, nothing new
    stats3 = pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T15:00:00+00:00")
    report3 = notify.build_report(db, stats3, CFG, "2026-11-15T15:00:00+00:00")
    assert not report3["has_news"]


def test_county_failure_does_not_close_listings(tmp_path):
    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()
    pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")

    def boom(cid):
        raise ConnectionError("site down")
    cv.list_open = boom
    stats = pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T11:45:00+00:00")
    assert stats["county_errors"] == {"Hudson": "site down"}
    assert stats["closed"] == 0
    assert db.get("2243990001")["is_open"] == 1


def test_map_export_excludes_baseline_alerts(tmp_path):
    from datetime import datetime, timezone
    from tracker.export import build_map_data

    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()
    s1 = pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")
    db.log_run("2026-11-14T11:45:00+00:00", "2026-11-14T12:30:00+00:00", s1)
    d = cv.details["2243990001"]
    d.status_history = d.status_history + [("Adjourned - Plaintiff", "2027-01-07")]
    pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T11:45:00+00:00")
    m = build_map_data(db, now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    ids = {p["property_id"] for p in m["properties"]}
    assert ids == {"2243990001", "2243990002"}          # residential condo excluded
    store = next(p for p in m["properties"] if p["property_id"] == "2243990001")
    assert store["lat"] == 40.7451 and store["geo_source"] == "parcel"
    assert store["status_history"][-1] == ["Adjourned - Plaintiff", "2027-01-07"]
    assert [a["kind"] for a in m["alerts"]] == ["status"]  # baseline "new" events are not alerts
    assert m["last_run"]["baseline"] is True


def test_geocoder_fallback_for_unmatched(tmp_path):
    class Geo:
        def locate(self, street, city, zip_code):
            return (40.6687, -74.1143) if city == "BAYONNE" else None
    db = DB(tmp_path / "t.db")
    pipeline.run(CFG, FakeCV(), FakeParcels(), db, ts="2026-11-14T11:45:00+00:00", geocoder=Geo())
    h = db.get("2243990002")
    assert (h["lat"], h["lon"], h["geo_source"]) == (40.6687, -74.1143, "address")
