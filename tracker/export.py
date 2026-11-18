"""Export the data the map website reads (data/map.json) plus a CSV of open leads."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .civilview import CivilViewClient
from .db import DB

TRACKED = ("target", "land")

PROPERTY_FIELDS = [
    "property_id", "category", "category_reason", "is_open", "closed_at", "first_seen", "county",
    "muni", "street", "city", "zip", "block", "lot", "qual", "pams_pin", "prop_class", "class_label",
    "net_value", "land_val", "imprvt_val", "last_yr_tax", "bldg_desc", "land_desc", "calc_acre",
    "yr_constr", "last_sale_price", "last_sale_date", "sale_date", "latest_status", "sheriff_number",
    "court_case", "plaintiff", "defendant", "judgment", "upset", "attorney", "attorney_phone",
    "description", "lat", "lon", "geo_source", "outcome", "outcome_date", "outcome_status",
    "net_value_total",
]

CSV_COLUMNS = [
    "category", "county", "muni", "street", "city", "zip", "block", "lot", "qual", "prop_class",
    "class_label", "net_value", "net_value_total", "calc_acre", "bldg_desc", "yr_constr", "sale_date", "latest_status",
    "sheriff_number", "court_case", "plaintiff", "defendant", "judgment", "upset", "attorney",
    "attorney_phone", "last_sale_price", "last_sale_date", "lat", "lon", "first_seen", "property_id",
    "outcome", "outcome_date",
]


LP_FIELDS = [
    "property_id", "category", "category_reason", "county", "muni", "street", "city", "zip", "block", "lot", "qual",
    "pams_pin", "prop_class", "class_label", "net_value", "land_val", "imprvt_val", "last_yr_tax", "bldg_desc",
    "land_desc", "calc_acre", "yr_constr", "last_sale_price", "last_sale_date", "plaintiff", "defendant",
    "attorney", "attorney_phone", "attorney_email", "attorney_firm", "attorney_address", "description", "lat", "lon",
    "geo_source", "instrument", "doc_type", "kind", "recorded", "first_seen", "mortgage_amount", "mortgage_date",
    "mortgage_ref", "complaint_filed", "docket", "status", "status_date", "sheriff_listing",
]
PRESS_HOME = {2: "https://press.essexregister.com/prodpress/clerk/ClerkHome.aspx?op=basic",
              17: "https://records.passaiccountynj.org/press/clerk/ClerkHome.aspx?op=basic"}


def pre_foreclosures(db: DB, closed_cutoff: str, enriched: set) -> list[dict]:
    """Commercial / land properties with a foreclosure lis pendens that hasn't reached a sheriff sale."""
    try:
        rows = db.conn.execute(
            """SELECT * FROM lis_pendens WHERE category IN ('target','land')
               AND (status = 'pending' OR (status = 'discharged' AND status_date >= ?))
               ORDER BY recorded DESC""", (closed_cutoff[:10],)).fetchall()
    except Exception:  # noqa: BLE001 - table not created yet
        return []
    out = []
    for r in rows:
        r = dict(r)
        p = {k: r.get(k) for k in LP_FIELDS}
        p.update({"stage": "pre", "is_open": 0, "lp_status": r.get("status"), "status_history": [],
                  "latest_status": "Lis pendens (foreclosure filed)" if r.get("status") == "pending" else "Lis pendens discharged",
                  "detail_url": PRESS_HOME.get(r.get("county_id")), "enriched": r["property_id"] in enriched})
        out.append(p)
    return out


REO_FIELDS = ["property_id", "pams_pin", "county", "muni", "street", "block", "lot", "qual", "prop_class",
              "class_label", "net_value", "land_val", "imprvt_val", "last_yr_tax", "bldg_desc", "land_desc",
              "calc_acre", "yr_constr", "last_sale_price", "last_sale_date", "category", "category_reason", "lat",
              "lon", "geo_source", "reo_price", "reo_kind", "resale_date", "resale_price", "first_seen"]


def reo_history_props(db: DB, have_pins: set) -> list[dict]:
    """Bank-owned properties since 2018 from the state's deed files, minus ones already on the map."""
    try:
        rows = db.conn.execute(
            """SELECT * FROM reo_history WHERE located = 1 AND category IN ('target','land')
               ORDER BY reo_date DESC""").fetchall()
    except Exception:  # noqa: BLE001 - table not created yet
        return []
    out = []
    for r in rows:
        r = dict(r)
        if r["pams_pin"] in have_pins:
            continue
        p = {k: r.get(k) for k in REO_FIELDS}
        p.update({"stage": "reo_hist", "is_open": 0, "outcome": "reo", "outcome_date": r["reo_date"],
                  "outcome_status": f"Lender took title ({r['reo_kind']})", "status_history": [],
                  "source": "NJ deed records (SR-1A)", "detail_url": None, "city": r.get("muni"),
                  "plaintiff": None, "defendant": None, "enriched": False})
        out.append(p)
    return out


def first_listed(r: dict, new_event: dict | None) -> tuple[str | None, bool]:
    """When a property hit the sheriff's sale list: the day a scan first saw it, or, for listings
    already on the list when tracking began, its first scheduled sale date (marked as estimated)."""
    if new_event and not new_event.get("baseline"):
        return (r.get("first_seen") or "")[:10] or None, False
    # Sheriff's sales are advertised for about four weeks before the first sale date, and the
    # listing can't have appeared after the day the tracker first saw it.
    hist = _hist(r.get("status_history"))
    dates = sorted(d for _, d in hist if d) or ([r["sale_date"]] if r.get("sale_date") else [])
    seen = (r.get("first_seen") or "")[:10] or None
    est = None
    if dates:
        try:
            est = (date.fromisoformat(dates[0][:10]) - timedelta(days=28)).isoformat()
        except ValueError:
            est = None
    cands = [d for d in (est, seen) if d]
    return (min(cands) if cands else None), True


def _hist(v) -> list:
    try:
        return [tuple(x) for x in json.loads(v or "[]")]
    except (json.JSONDecodeError, TypeError):
        return []


def build_map_data(db: DB, now: datetime | None = None, alert_days: int = 3650, closed_days: int = 45,
                   reo_days: int = 3650) -> dict:
    now = now or datetime.now(timezone.utc)
    alert_cutoff = (now - timedelta(days=alert_days)).isoformat(timespec="seconds")
    closed_cutoff = (now - timedelta(days=closed_days)).isoformat(timespec="seconds")
    reo_cutoff = (now - timedelta(days=reo_days)).date().isoformat()
    c = db.conn

    rows = c.execute(
        f"""SELECT * FROM listings WHERE category IN ('target','land')
            AND (is_open=1 OR closed_at >= ? OR (outcome='reo' AND outcome_date >= ?))
            ORDER BY sale_date""", (closed_cutoff, reo_cutoff)).fetchall()
    props = []
    ids = set()
    new_events = {e["property_id"]: dict(e) for e in c.execute(
        "SELECT property_id, ts, baseline FROM events WHERE kind='new' ORDER BY id")}
    try:
        enriched = {r[0] for r in db.conn.execute("SELECT property_id FROM enrichment")}
    except Exception:  # noqa: BLE001 - table not created yet
        enriched = set()
    for r in rows:
        r = dict(r)
        p = {k: r.get(k) for k in PROPERTY_FIELDS}
        try:
            p["status_history"] = json.loads(r.get("status_history") or "[]")
        except json.JSONDecodeError:
            p["status_history"] = []
        p["detail_url"] = CivilViewClient.listing_url(r)
        for k in ("parcel_pins", "lots"):
            try:
                p[k] = json.loads(r.get(k) or "null")
            except json.JSONDecodeError:
                p[k] = None
        p["enriched"] = r["property_id"] in enriched
        ne = new_events.get(r["property_id"])
        p["listed"], p["listed_estimated"] = first_listed(r, ne)
        props.append(p)
        ids.add(r["property_id"])

    # The lis pendens date (from the county's records) for listings already on a sheriff list.
    try:
        for pid, data in c.execute("SELECT property_id, data FROM enrichment"):
            hist = (json.loads(data) or {}).get("history") or []
            lp = [f["recorded"] for f in hist if f.get("recorded") and "LIS PENDENS" in (f.get("type") or "").upper()
                  and "DISCHARGE" not in (f.get("type") or "").upper()]
            if lp:
                for p in props:
                    if p["property_id"] == pid:
                        p["lp_recorded"] = max(lp)
    except Exception:  # noqa: BLE001 - no enrichment yet
        pass
    props += pre_foreclosures(db, closed_cutoff, enriched)
    props += reo_history_props(db, {p.get("pams_pin") for p in props if p.get("pams_pin")})
    ids |= {p["property_id"] for p in props}

    ev = c.execute(
        """SELECT id, property_id, ts, kind, old_value, new_value FROM events
           WHERE ts >= ? AND baseline=0 ORDER BY ts DESC, id DESC""", (alert_cutoff,)).fetchall()
    alerts = [dict(e) for e in ev if e["property_id"] in ids]

    last = c.execute("SELECT started, stats FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    run = {"at": last["started"], **json.loads(last["stats"])} if last else None
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "last_run": {k: run.get(k) for k in ("at", "counties", "county_errors", "baseline", "reo_errors",
                                             "reo_baseline", "reo_capped")} if run else None,
        "properties": props,
        "alerts": alerts,
    }


def export_all(db: DB, out_dir: Path, reo_days: int = 3300) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "map.json").write_text(json.dumps(build_map_data(db, reo_days=reo_days),
                                                 separators=(",", ":"), default=str))
    with open(out_dir / "leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(db.open_leads())
    from .enrich import export_contacts, export_history
    contacts = export_contacts(db)
    (out_dir / "contacts.json").write_text(json.dumps(contacts, separators=(",", ":"), default=str))
    (out_dir / "records.json").write_text(json.dumps(export_history(db), separators=(",", ":"), default=str))
    reo = [dict(r) for r in db.conn.execute(
        "SELECT * FROM listings WHERE outcome='reo' AND category IN ('target','land') ORDER BY outcome_date DESC")]
    with open(out_dir / "reo.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(reo)
