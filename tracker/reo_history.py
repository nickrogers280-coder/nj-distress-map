"""Load bank-owned (REO) history since 2018 from the state's SR-1A deed files and place it on the map."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from .db import DB
from .parcels import ParcelClient, ParcelMatch
from .sr1a import download_years, history_events, iter_sales

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reo_history (
    property_id TEXT PRIMARY KEY,      -- reo:<PAMS_PIN>
    pams_pin TEXT, county TEXT, district TEXT, block TEXT, lot TEXT, sr_class TEXT,
    reo_date TEXT, reo_price REAL, reo_kind TEXT, location TEXT, sr_assessed REAL, recorded TEXT,
    resale_date TEXT, resale_price REAL,
    muni TEXT, street TEXT, prop_class TEXT, class_label TEXT, prop_loc TEXT, net_value REAL, land_val REAL,
    imprvt_val REAL, last_yr_tax REAL, bldg_desc TEXT, land_desc TEXT, calc_acre REAL, yr_constr TEXT,
    last_sale_price REAL, last_sale_date TEXT, qual TEXT,
    category TEXT, category_reason TEXT, lat REAL, lon REAL, geo_source TEXT, located INTEGER DEFAULT 0,
    first_seen TEXT, baseline INTEGER DEFAULT 0, outcome TEXT DEFAULT 'reo'
);
"""


def ensure_tables(db: DB) -> None:
    db.conn.executescript(SCHEMA)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(reo_history)")}
    if "outcome" not in cols:
        db.conn.execute("ALTER TABLE reo_history ADD COLUMN outcome TEXT DEFAULT 'reo'")
    db.commit()


def load_years(db: DB, years: list[int], fetch=download_years, since: str = "2018-01-01") -> dict:
    """Read the yearly files and upsert one row per parcel the lender took back."""
    ensure_tables(db)
    files = fetch(years)
    baseline = db.conn.execute("SELECT COUNT(*) FROM reo_history").fetchone()[0] == 0
    # Third-party sheriff's sales were added later: their first load is history too, not news.
    sold_baseline = db.conn.execute("SELECT COUNT(*) FROM reo_history WHERE outcome='sold'").fetchone()[0] == 0
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sales = (s for y in sorted(files) for s in iter_sales(files[y]))
    stats = {"years": sorted(files), "missing_years": [y for y in years if y not in files], "events": 0, "new": 0,
             "sold": 0}
    for e in history_events(sales, since=since):
        stats["events"] += 1
        is_sold = e["outcome"] == "sold"
        stats["sold"] += is_sold
        pid = f"{'sold' if is_sold else 'reo'}:{e['pin']}"
        old = db.conn.execute("SELECT reo_date FROM reo_history WHERE property_id=?", (pid,)).fetchone()
        row = {"property_id": pid, "pams_pin": e["pin"], "county": e["county"], "district": e["district"],
               "block": e["block"], "lot": e["lot"], "sr_class": e["prop_class"], "reo_date": e["reo_date"],
               "reo_price": e["reo_price"], "reo_kind": e["reo_kind"], "location": e["location"],
               "sr_assessed": e.get("assessed"), "recorded": e.get("recorded"),
               "resale_date": e.get("resale_date"), "resale_price": e.get("resale_price"),
               "outcome": e["outcome"]}
        if old is None:
            quiet = sold_baseline if is_sold else baseline
            row.update({"first_seen": ts, "baseline": int(quiet)})
            stats["new"] += 1
            if not quiet and not is_sold:
                db.add_event(pid, ts, "reo", None, f"Bank took it back ({e['reo_kind']}, {e['reo_date']})")
        elif old[0] and old[0] > e["reo_date"]:
            continue
        cols = list(row)
        db.conn.execute(
            f"INSERT INTO reo_history ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(property_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c not in ('first_seen', 'baseline'))}",
            [row[c] for c in cols])
    db.commit()
    return stats


def locate_pending(cfg: dict, db: DB, parcels: ParcelClient, cap: int = 400) -> dict:
    """Match REO parcels to the tax map (current class, value, address) and pin them."""
    from .pipeline import classify
    ensure_tables(db)
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM reo_history WHERE located = 0 ORDER BY reo_date DESC LIMIT ?", (cap,))]
    s = {"tried": len(rows), "located": 0, "tracked": 0}
    for r in rows:
        attrs = parcels.query(f"PAMS_PIN='{r['pams_pin']}'", limit=1)
        if not attrs:   # pins of condo units carry a qualifier; take the lot's own record if there is one
            attrs = [a for a in parcels.query(f"PAMS_PIN LIKE '{r['pams_pin']}\\_%' ESCAPE '\\'", limit=5)]
        upd = {"located": 1}
        if attrs:
            m = ParcelMatch.from_attrs(attrs[0], "pin")
            upd.update({"muni": m.muni, "prop_class": m.prop_class, "class_label": m.class_label, "prop_loc": m.prop_loc,
                        "street": m.prop_loc or r["location"], "net_value": m.net_value, "land_val": m.land_val,
                        "imprvt_val": m.imprvt_val, "last_yr_tax": m.last_yr_tax, "bldg_desc": m.bldg_desc,
                        "land_desc": m.land_desc, "calc_acre": m.calc_acre, "yr_constr": m.yr_constr,
                        "last_sale_price": m.sale_price, "last_sale_date": m.deed_date, "qual": m.qual})
            ll = parcels.centroid(attrs[0].get("PAMS_PIN") or r["pams_pin"])
            if ll:
                upd.update({"lat": ll[0], "lon": ll[1], "geo_source": "parcel"})
                s["located"] += 1
        else:
            upd.update({"street": r["location"], "prop_class": r["sr_class"]})
        rec = {**r, **upd}
        rec.setdefault("street", r["location"])
        upd["category"], upd["category_reason"] = classify(rec, cfg)
        s["tracked"] += upd["category"] in ("target", "land")
        sets = ",".join(f"{k}=?" for k in upd)
        db.conn.execute(f"UPDATE reo_history SET {sets} WHERE property_id=?", [*upd.values(), r["property_id"]])
        db.commit()
    return s


def run(cfg: dict, db: DB, parcels: ParcelClient, today: date | None = None, fetch=download_years) -> dict:
    """First run: every year since 2018. Later runs: only the latest year and the year-to-date file."""
    ensure_tables(db)
    today = today or datetime.now(timezone.utc).date()
    first_year = int(str(cfg.get("reo_history_since", "2018-01-01"))[:4])
    done = set(json.loads(db.get_meta("sr1a_years_done") or "[]"))
    if db.get_meta("sr1a_has_sold") != "1":       # years read before third-party sales were kept
        done = set()
    # A file named for year Y holds deeds from about mid Y-1 to mid Y; 2018 deeds need the 2018 and 2019 files.
    wanted = [y for y in range(first_year, today.year + 1) if y not in done or y >= today.year - 1]
    stats = load_years(db, wanted, fetch=fetch, since=str(cfg.get("reo_history_since", "2018-01-01")))
    # Older years don't change; one that isn't published anywhere isn't retried every day either.
    done |= {y for y in wanted if y < today.year - 1}
    if stats["missing_years"]:
        db.set_meta("sr1a_years_missing", json.dumps(stats["missing_years"]))
    db.set_meta("sr1a_years_done", json.dumps(sorted(done)))
    db.set_meta("sr1a_has_sold", "1")
    db.commit()
    stats.update(locate_pending(cfg, db, parcels, cap=int(cfg.get("reo_history_locate_per_run", 400))))
    return stats
