"""End-to-end pipeline test with fake sheriff-sale and parcel clients (no network)."""
import json
from pathlib import Path

from tracker import notify, pipeline
from tracker.civilview import ListingRow, SaleDetail, parse_detail, parse_list
from tracker.db import DB
from tracker.parcels import ParcelMatch
from datetime import datetime, timezone
from tracker.export import build_map_data

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
        if street.startswith("88 HARBOR"):  # a vacant waterfront lot, matched by address
            return ParcelMatch.from_attrs({"PAMS_PIN": "0901_77_1", "MUN_NAME": "BAYONNE CITY", "PCLBLOCK": "77",
                                           "PCLLOT": "1", "PROP_CLASS": "1", "NET_VALUE": 1_450_000,
                                           "PROP_LOC": "88 HARBOR DR"}, "address")
        return None

    def centroid(self, pin):
        return None if pin == "0901_77_1" else (40.7451, -74.0602)


CFG = {"counties": [10], "target_classes": ["1", "4A", "4B", "4C"], "include_possible": True,
       "refresh_tracked_details_hours": 20}


def test_full_cycle(tmp_path):
    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()

    # ---- run 1: baseline
    stats = pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")
    assert stats["baseline"] and stats["new"] == 3 and stats["new_tracked"] == 2
    condo = db.get("10:20052781")
    store = db.get("10:20061234")
    harbor = db.get("10:20061999")
    assert condo["category"] == "other" and condo["prop_class"] == "2"
    assert store["category"] == "target" and store["class_label"] == "Commercial"
    assert store["lat"] == 40.7451 and store["last_sale_date"] == "2015-06-11"
    assert harbor["category"] == "land" and harbor["class_label"] == "Vacant land"

    report = notify.build_report(db, stats, CFG, "2026-11-14T11:45:00+00:00")
    assert [c["property_id"] for c in report["new_target"]] == ["10:20061234"]
    assert [c["property_id"] for c in report["new_possible"]] == ["10:20061999"]
    assert report["new_target"][0]["debt_to_value"] == 79
    html = notify.render(report)
    assert "455 Tonnelle Avenue" in html and "Vacant land (" in html
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
    assert db.get("10:20061999")["is_open"] == 0
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
    assert db.get("10:20061234")["is_open"] == 1


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
    assert ids == {"10:20061234", "10:20061999"}          # residential condo excluded
    store = next(p for p in m["properties"] if p["property_id"] == "10:20061234")
    assert store["lat"] == 40.7451 and store["geo_source"] == "parcel"
    assert store["status_history"][-1] == ["Adjourned - Plaintiff", "2027-01-07"]
    assert [a["kind"] for a in m["alerts"]] == ["status"]  # baseline "new" events are not alerts
    assert m["last_run"]["baseline"] is True


def test_geocoder_fallback_when_parcel_has_no_shape(tmp_path):
    class Geo:
        def locate(self, street, city, zip_code):
            return (40.6687, -74.1143) if city == "BAYONNE" else None
    db = DB(tmp_path / "t.db")
    pipeline.run(CFG, FakeCV(), FakeParcels(), db, ts="2026-11-14T11:45:00+00:00", geocoder=Geo())
    h = db.get("10:20061999")
    assert (h["lat"], h["lon"], h["geo_source"]) == (40.6687, -74.1143, "address")


def test_rotating_civilview_ids_keep_one_listing(tmp_path):
    """CivilView renumbers every listing when a county refreshes; that must not look like
    the old listings closing and new ones appearing."""
    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()
    pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")
    # Refresh: same listings, new PropertyIds.
    for r in cv.lists[10]:
        new = str(int(r.property_id) + 500000)
        cv.details[new] = cv.details[r.property_id]
        r.property_id = new
    s = pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T11:45:00+00:00")
    assert s["new"] == 0 and s["closed"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
    store = db.get("10:20061234")
    assert store["cv_id"] == "2244490001" and store["is_open"] == 1
    m = build_map_data(db, now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    p = next(p for p in m["properties"] if p["property_id"] == "10:20061234")
    assert p["detail_url"].endswith("PropertyId=2244490001")


def test_repair_merges_duplicates_from_old_keys(tmp_path):
    """Databases built before stable keys hold one copy per PropertyId; the repair merges them."""
    import sqlite3
    path = tmp_path / "old.db"
    db = DB(path)
    db.conn.execute("DELETE FROM meta WHERE key='keyed_by_sheriff'")
    for pid, first, last, is_open in [("111", "2026-11-11", "2026-11-11", 0), ("222", "2026-11-12", "2026-11-12", 0),
                                      ("333", "2026-11-13", "2026-11-13", 1)]:
        db.upsert({"property_id": pid, "county_id": 25, "county": "Atlantic", "sheriff_number": "F-18001921",
                   "first_seen": first, "last_seen": last, "is_open": is_open, "category": "target",
                   "lat": 39.4 if pid == "111" else None, "lon": -74.5 if pid == "111" else None})
        db.add_event(pid, first, "new")
        if not is_open:
            db.add_event(pid, last, "closed")
    db.commit(); db.conn.close()
    db = DB(path)
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM listings")]
    assert len(rows) == 1
    r = rows[0]
    assert r["property_id"] == "25:F-18001921" and r["is_open"] == 1 and r["cv_id"] == "333"
    assert r["first_seen"] == "2026-11-11" and r["lat"] == 39.4
    kinds = [e[0] for e in db.conn.execute("SELECT kind FROM events ORDER BY ts")]
    assert kinds == ["new"]


def test_rotating_ids_without_sheriff_numbers_on_list(tmp_path):
    """Middlesex-style lists have no sheriff number column; rows are matched by address."""
    db = DB(tmp_path / "t.db")
    cv, parcels = FakeCV(), FakeParcels()
    for r in cv.lists[10]:
        r.sheriff_number = None
    pipeline.run(CFG, cv, parcels, db, ts="2026-11-14T11:45:00+00:00")
    calls = cv.detail_calls
    for r in cv.lists[10]:
        new = str(int(r.property_id) + 7)
        cv.details[new] = cv.details[r.property_id]
        r.property_id = new
    s = pipeline.run(CFG, cv, parcels, db, ts="2026-11-15T11:45:00+00:00")
    assert s["new"] == 0 and s["closed"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
    assert db.get("10:20061234")["cv_id"] == "2243990008"


def test_condos_excluded_and_land_separate():
    base = {"prop_class": "4A", "class_label": "Commercial", "net_value": 900000, "street": "10 MAIN ST"}
    assert pipeline.classify(base, CFG)[0] == "target"
    assert pipeline.classify({**base, "prop_class": "4C"}, CFG)[0] == "target"
    assert pipeline.classify({**base, "qual": "C0102"}, CFG)[0] == "other"           # commercial condo
    assert pipeline.classify({**base, "street": "10 MAIN ST UNIT 2"}, CFG)[0] == "other"
    assert pipeline.classify({**base, "bldg_desc": "COMM CONDO"}, CFG)[0] == "other"
    assert pipeline.classify({**base, "description": "Being known as Condominium Unit 4"}, CFG)[0] == "other"
    assert pipeline.classify({**base, "prop_class": "1", "class_label": "Vacant land"}, CFG)[0] == "land"
    assert pipeline.classify({**base, "prop_class": "1", "street": "322 RIDGE RD Q2",
                              "description": "Dimensions: Condominium Unit"}, CFG)[0] == "other"
    assert pipeline.classify({**base, "prop_class": "1", "net_value": 0}, CFG)[0] == "other"   # common lot
    assert pipeline.classify({**base, "prop_class": "2"}, CFG)[0] == "other"
    assert pipeline.classify({**base, "prop_class": "15F"}, CFG)[0] == "other"
    assert pipeline.classify({"prop_class": None, "defendant": "ACME LLC"}, CFG)[0] == "other"  # no parcel match


def test_multi_lot_notice_records_every_lot():
    class P(FakeParcels):
        def lookup(self, county, city, street, block, lot, qual):
            return ParcelMatch.from_attrs({"PAMS_PIN": "1324_86_2", "MUN_NAME": "KEYPORT BORO", "PCLBLOCK": "86",
                                           "PCLLOT": "2", "PROP_CLASS": "4A", "NET_VALUE": 447200}, "block/lot")

        def sibling_lots(self, pin, block, lots):
            assert pin == "1324_86_2" and block == "86"
            return [{"PAMS_PIN": f"1324_86_{l}", "PCLLOT": l, "NET_VALUE": 10000} for l in lots]

    rec = {"parcel_text": "Lot and Block: Lot 2 and Lots 3 through 7, Block 86 Tax Map of Borough Keyport",
           "street": "216 Atlantic Street", "city": "KEYPORT"}
    pipeline._enrich(rec, P(), "Monmouth")
    assert json.loads(rec["lots"]) == ["2", "3", "4", "5", "6", "7"]
    assert json.loads(rec["parcel_pins"])[0] == "1324_86_2" and rec["net_value_total"] == 497200
    # A lot list that doesn't include the matched lot isn't trusted.
    rec2 = {"parcel_text": "LOT(S): 6 & 2 BLOCK: 86", "street": "216 Atlantic Street"}

    class Q(P):
        def lookup(self, *a):
            return ParcelMatch.from_attrs({"PAMS_PIN": "1324_86_16", "PCLBLOCK": "86", "PCLLOT": "16",
                                           "PROP_CLASS": "4A"}, "block/lot")
    pipeline._enrich(rec2, Q(), "Monmouth")
    assert json.loads(rec2["lots"]) == ["16"] and rec2["net_value_total"] is None


def test_scripts_have_no_undefined_names():
    """Catch NameErrors in the entry-point scripts, which the unit tests don't execute."""
    import ast
    import builtins
    from pathlib import Path
    root = Path(__file__).parent.parent
    for script in ("run.py", "enrich_contacts.py"):
        tree = ast.parse((root / script).read_text())
        defined = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                defined.add(node.name)
                defined |= {a.arg for a in node.args.args + node.args.kwonlyargs} if isinstance(node, ast.FunctionDef) else set()
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.comprehension,)):
                for n in ast.walk(node.target):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        assert not (used - defined - {"__file__", "__name__"}), (script, used - defined)
