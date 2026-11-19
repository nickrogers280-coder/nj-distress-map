"""Lis pendens backfill: index first, read the promising ones, skip discharged and houses."""
import json
from datetime import date

from tracker import lispendens as LP
from tracker.db import DB


class Ad:
    county_id, county, system = 1100, "Mercer", "fake"

    def __init__(self):
        self.calls = []

    def lp_groups(self, a, b, *r):
        self.calls.append((a, b))
        out = []
        if a <= date(2021, 3, 1) <= b:
            out.append({"instrument": "A1", "doc_type": "NOTICE OF LIS PENDENS", "recorded": "2021-03-01",
                        "defendants": ["ACME REALTY LLC"], "plaintiffs": ["BIG BANK NA"], "town": "TRENTON",
                        "block": "1", "lot": "2", "street": "1 MAIN ST"})
            out.append({"instrument": "H1", "doc_type": "NOTICE OF LIS PENDENS", "recorded": "2021-03-01",
                        "defendants": ["SMITH JOHN"], "plaintiffs": ["BIG BANK NA"], "town": "TRENTON"})
            out.append({"instrument": "D1", "doc_type": "NOTICE OF LIS PENDENS", "recorded": "2021-02-01",
                        "defendants": ["OLD LLC"], "plaintiffs": ["BANK TWO"], "town": "TRENTON"})
        return out

    def discharge_groups(self, a, b, *r):
        if a <= date(2021, 6, 1) <= b:
            return [{"instrument": "X", "recorded": "2021-06-01", "defendants": ["OLD LLC"], "plaintiffs": ["BANK TWO"]}]
        return []

    def pages(self, g, n=8):
        return []

    def close(self):
        pass


def test_backfill_walks_back_and_prioritises(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    monkeypatch.setattr(LP, "process_instrument", lambda cfg, ad, parcels, cid, cname, g, geo: {
        "property_id": f"lp:{cid}:{g['instrument']}", "county_id": cid, "county": cname, "instrument": g["instrument"],
        "recorded": g["recorded"], "status": "pending", "category": "other", "category_reason": "x"})
    cfg = {"lis_pendens_since": "2020-01-01", "lis_pendens_backfill_days_per_run": 400, "max_lis_pendens_per_county": 1}
    ad = Ad()
    for day in range(16, 22):
        LP.run_lis_pendens(cfg, db, None, object(), today=date(2026, 11, day), adapters=[ad])
    back = db.conn.execute("SELECT back_through FROM lp_backfill").fetchone()[0]
    assert back == "2020-01-01"
    st = dict(db.conn.execute("SELECT instrument, status FROM lp_index").fetchall())
    assert st["D1"] == "discharged"              # discharged later: never read
    assert st["A1"] == "read"                    # company owner: read before the person
    order = [r[0] for r in db.conn.execute("SELECT instrument FROM lis_pendens ORDER BY rowid")]
    assert order[0] == "A1"
