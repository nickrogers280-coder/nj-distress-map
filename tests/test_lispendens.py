"""Pre-foreclosure (lis pendens) parsing and pipeline, from real OCR of Essex instrument 2026038724."""
import json
from datetime import date

from tracker.db import DB
from tracker import lispendens as LP
from tracker.parcels import ParcelMatch
from tracker.press import IndexRow

PAGE2 = """Essex County Recording Data Page Official Use Only
Date of Document: Type of Document:
06/03/2026 Notice of Lis Pendens (Foreclosure)
THE FOLLOWING SECTION IS FOR ORIGINAL MORTGAGE BOOKING & PAGING INFORMATION
Original Book: Original Page:
Instrument No. 2022079009
"""
PAGE3 = """DEMBO, BROWN & BURNS LLP
Michael E. Brown, Esquire
NJ ID# 028762000
1300 Route 73, Suite 205
Mt. Laurel, New Jersey 08054
(856) 354-8866
mbrown@dbblegal.com
Attorney for Plaintiff
File No. 16252
WILMINGTON SAVINGS FUND SUPERIOR COURT OF NEW JERSEY
SOCIETY, FSB, CHANCERY DIVISION
Plaintiff(s),
VS. Docket No. F-006677-26
1221 BLOOMFIELD LLC; ADAMS
INTERNATIONAL LLC; JOHN ADAMS;
STATE OF NEW JERSEY and UNKNOWN
OCCUPANTS and/or TENANTS 1 through NOTICE OF LIS PENDENS
TO WHOM IT MAY CONCERN:
NOTICE IS HEREBY GIVEN of the commencement and pendency of the above-entitled
action, the general objects of which are:
1. To foreclose following mortgage covering the premises hereinafter described,
to wit:
A mortgage made by 1221 Bloomfield LLC to Wilmington Savings Fund Society, FSB in
the amount of $1,490,000.00, dated August 15, 2022 and recorded in the office of the Clerk of
Essex County on August 24, 2022 as Instrument No. 2022079009.
2. To recover possession of the lands and premises hereinafter described. The lands
and premises to be affected by suit are annexed hereto as Schedules A.
"""
PAGE4 = """3. The foreclosure Complaint in the above-entitled matter was filed in the Office of
the Clerk of the Superior Court of New Jersey on the 8th day of April 2026.
DEMBO, BROWN & BURNS, LLP
DATED: June 3, 2026
"""
PAGE5 = """EXHIBIT A
1221 Bloomfield Avenue
Fairfield, NJ 07004
Tax Lot(s) 14, Tax Block 303, Township of Fairfield, Essex County
ALL that certain lot, piece of parcel of land, with the buildings and improvements thereon erected, situate, lying
and being in the Township of Fairfield, in the County of Essex, State of New Jerssy:
BEGINNING at a point in the norineasterly side of Bloomfield Avenue as laid out by Essex County at a point
therein distant westerly 326.15 fest from the intersection of the northeasterly line of Bloomfield Avenue with the
dividing line between Lots 1 & 2 in Block 0303 as set forth on the official tax map of the Township of Fairfield and
"""
TEXT = "\n".join([PAGE2, PAGE3, PAGE4, PAGE5])


def test_parse_real_lis_pendens():
    d = LP.parse_lis_pendens(TEXT)
    assert d["street"] == "1221 Bloomfield Avenue"
    assert d["city"] == "Fairfield" and d["zip"] == "07004"
    assert (d["block"], d["lot"]) == ("303", "14")
    assert d["muni_name"] == "Fairfield"
    assert d["mortgage_amount"] == 1_490_000.0 and d["mortgage_date"] == "2022-08-15"
    assert d["mortgage_ref"] == "Instrument 2022079009"
    assert d["complaint_filed"] == "2026-04-08"
    assert d["docket"] == "F-006677-26"
    assert d["attorney_firm"] == "DEMBO, BROWN & BURNS LLP"
    assert d["attorney"] == "Michael E. Brown"
    assert d["attorney_phone"] == "(856) 354-8866"
    assert d["attorney_email"] == "mbrown@dbblegal.com"
    assert d["attorney_address"] == "1300 Route 73, Suite 205, Mt. Laurel, New Jersey 08054" or \
        d["attorney_address"].startswith("1300 Route 73")


def test_parse_commonly_known_as_and_book_page():
    t = ("Plaintiff, VS. Docket No. F 012345 25\nA mortgage dated March 3, 2019 made by ACME LLC to Valley National Bank "
         "in the original amount of $2,250,000.00 recorded in Mortgage Book 12345 Page 67.\n"
         "The premises are commonly known as 55 Industrial Road, Totowa, New Jersey 07512, and designated as "
         "Block 170, Lot 3 on the tax map of the Borough of Totowa.")
    d = LP.parse_lis_pendens(t)
    assert d["street"] == "55 Industrial Road"
    assert (d["block"], d["lot"]) == ("170", "3")
    assert d["mortgage_amount"] == 2_250_000.0 and d["mortgage_date"] == "2019-03-03"
    assert d["mortgage_ref"] == "Book 12345 Page 67"
    assert d["docket"] == "F012345-25" or d["docket"].startswith("F")


def _rows(inst, recorded, defs, plaintiff, doc="LIS PENDENS FORECLOSURE AND RECOVERY"):
    return [IndexRow(doc, d, plaintiff, inst, recorded, "FAIRFIELD", "N/A", "", "v") for d in defs]


class FakeClient:
    base = "https://press.example/"

    def __init__(self):
        self.searches = []

    def doc_types(self):
        return ["LIS PENDENS FORECLOSURE", "LIS PENDENS DISCHARGE", "DEED"]

    def search_doc_type(self, label_re, a, b):
        self.searches.append((label_re.pattern, a, b))
        if "FORECLOS" in label_re.pattern:
            return (_rows("2026038724", "6/10/2026", ["1221 BLOOMFIELD LLC", "ADAMS INTERNATIONAL LLC", "ADAMS JOHN",
                                                     "NEW JERSEY, STATE OF"], "WILMINGTON SAVINGS FUND SOCIETY FSB")
                    + _rows("2026040001", "6/12/2026", ["SMITH JANE"], "BIG BANK NA")) if a <= date(2026, 6, 12) <= b else []
        return []

    def open_document(self, inst):
        return ("tile", 5) if inst == "2026038724" else ("tile2", 2)

    def page_image(self, tile, i):
        return f"{tile}:{i}".encode()


class FakeParcels:
    def lookup(self, county, city, street, block, lot, qual):
        if block == "303":
            return ParcelMatch.from_attrs({"PAMS_PIN": "0706_303_14", "MUN_NAME": "FAIRFIELD TWP", "PCLBLOCK": "303",
                                           "PCLLOT": "14", "PROP_CLASS": "4A", "NET_VALUE": 1_288_300,
                                           "PROP_LOC": "1221 BLOOMFIELD AVENUE"}, "block/lot")
        if block == "77":
            return ParcelMatch.from_attrs({"PAMS_PIN": "0714_77_1", "MUN_NAME": "NEWARK CITY", "PCLBLOCK": "77",
                                           "PCLLOT": "1", "PROP_CLASS": "2", "NET_VALUE": 300000}, "block/lot")
        return None

    def sibling_lots(self, *a):
        return []

    def centroid(self, pin):
        return (40.857, -74.312)


class FakeBrowser:
    def __init__(self, client):
        self.client = client

    def new_context(self, **kw):
        c = self.client

        class Ctx:
            def new_page(self):
                return c

            def close(self):
                pass
        return Ctx()


def test_run_lis_pendens(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(LP, "PressClient", lambda base, page: page)
    monkeypatch.setattr(LP, "ocr_png", lambda png: {b"tile:1": PAGE2, b"tile:2": PAGE3, b"tile:3": PAGE4,
                                                     b"tile:4": PAGE5}.get(png, "Block 77 Lot 1 City of Newark"
                                                     if png.startswith(b"tile2") else ""))
    db = DB(tmp_path / "t.db")
    cfg = {"lis_pendens_counties": [2], "lis_pendens_lookback_days": 200}
    s = LP.run_lis_pendens(cfg, db, FakeBrowser(client), FakeParcels(), today=date(2026, 11, 16))
    assert s["found"] == 2 and s["processed"] == 2 and s["tracked"] == 1
    r = dict(db.conn.execute("SELECT * FROM lis_pendens WHERE instrument='2026038724'").fetchone())
    assert r["category"] == "target" and r["prop_class"] == "4A" and r["pams_pin"] == "0706_303_14"
    assert r["defendant"].startswith("1221 BLOOMFIELD LLC") and r["street"] == "1221 Bloomfield Avenue"
    assert r["mortgage_amount"] == 1_490_000 and r["attorney_phone"] == "(856) 354-8866"
    assert r["lat"] == 40.857 and r["baseline"] == 1 and r["status"] == "pending"
    house = dict(db.conn.execute("SELECT * FROM lis_pendens WHERE instrument='2026040001'").fetchone())
    assert house["category"] == "other"                         # residential, kept only to avoid re-reading
    assert db.conn.execute("SELECT checked_through FROM lp_scan WHERE county_id=2").fetchone()[0] == "2026-11-16"

    # Next day: nothing new is re-read; the search window starts a few days back to catch late indexing.
    client.searches.clear()
    s2 = LP.run_lis_pendens(cfg, db, FakeBrowser(client), FakeParcels(), today=date(2026, 11, 17))
    assert s2["processed"] == 0 and client.searches[0][1] == date(2026, 11, 11)

    # Once the property shows up on a sheriff's sale list, the pre-foreclosure is linked to it.
    db.upsert({"property_id": "2:F-1", "county_id": 2, "pams_pin": "0706_303_14", "first_seen": "2026-12-01T00:00:00",
               "is_open": 1, "category": "target"})
    db.commit()
    assert LP.link_to_sheriff_sales(db) == 1
    assert db.conn.execute("SELECT status FROM lis_pendens WHERE instrument='2026038724'").fetchone()[0] == "sheriff_sale"


def test_discharge_marks_resolved(tmp_path):
    db = DB(tmp_path / "t.db")
    LP.ensure_tables(db)
    LP.save(db, {"property_id": "lp:2:1", "county_id": 2, "instrument": "1", "recorded": "2026-06-10", "status": "pending",
                 "parties": json.dumps({"defendants": ["1221 BLOOMFIELD LLC"], "plaintiffs": ["WILMINGTON SAVINGS FUND SOCIETY FSB"]})})
    db.commit()
    n = LP.apply_discharges(db, _rows("2", "10/30/2026", ["1221 BLOOMFIELD LLC"], "WILMINGTON SAVINGS FUND SOCIETY FSB",
                                       doc="LIS PENDENS DISCHARGE"))
    assert n == 1
    assert db.conn.execute("SELECT status, status_date FROM lis_pendens").fetchone()[:] == ("discharged", "2026-10-30")


def test_window_split_when_page_is_full():
    class C:
        def search_doc_type(self, rx, a, b):
            n = (b - a).days + 1
            return [IndexRow("LIS PENDENS FORECLOSURE", f"X{a}{i}", "B", f"{a}{i}", "1/1/2026", "", "", "", "v")
                    for i in range(100 if n > 2 else 10)]
    rows = LP.index_rows(C(), LP.LP_FORECLOSURE, date(2026, 1, 1), date(2026, 1, 7))
    assert len(rows) == 40          # 7 days split down to windows of <=2 days, 10 rows each


def test_pre_foreclosures_on_map(tmp_path):
    from datetime import datetime, timezone
    from tracker.export import build_map_data
    db = DB(tmp_path / "t.db")
    LP.ensure_tables(db)
    LP.save(db, {"property_id": "lp:2:9", "county_id": 2, "county": "Essex", "instrument": "9", "recorded": "2026-11-10",
                 "status": "pending", "category": "target", "street": "1 MAIN ST", "lat": 40.7, "lon": -74.2,
                 "plaintiff": "BANK", "defendant": "X LLC", "mortgage_amount": 500000})
    LP.save(db, {"property_id": "lp:2:8", "county_id": 2, "instrument": "8", "status": "pending", "category": "other"})
    db.add_event("lp:2:9", "2026-11-15T12:00:00+00:00", "lis_pendens", None, "BANK")
    db.commit()
    m = build_map_data(db, now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    pre = [p for p in m["properties"] if p.get("stage") == "pre"]
    assert [p["property_id"] for p in pre] == ["lp:2:9"] and pre[0]["mortgage_amount"] == 500000
    assert [a["kind"] for a in m["alerts"]] == ["lis_pendens"]


def test_lis_pendens_type_names_by_county():
    assert LP.LP_FORECLOSURE.search("FORECLOSURE LISPENDENS") and LP.LP_FORECLOSURE.search("LIS PENDENS FORECLOSURE AND RECOVERY")
    assert not LP.LP_FORECLOSURE.search("LIS PENDENS") and LP.LP_PLAIN.search("LIS PENDENS")
    assert not LP.LP_PLAIN.search("LIS PENDENS - AMENDED")
    assert LP.LP_DISCHARGE.search("DISCHARGE/LISPENDENS") and LP.LP_DISCHARGE.search("LIS PENDENS - RELEASED")
    assert LP.LP_IN_REM.search("LIS IN REM COMPLAINT")
