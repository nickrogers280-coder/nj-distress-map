"""Export the data the map website reads (data/map.json) plus a CSV of open leads."""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .civilview import CivilViewClient
from .db import DB
from .parcels import parse_block_lot

TRACKED = ("target", "land")

PROPERTY_FIELDS = [
    "property_id", "category", "category_reason", "is_open", "closed_at", "first_seen", "county",
    "muni", "street", "city", "zip", "block", "lot", "qual", "pams_pin", "prop_class", "class_label",
    "net_value", "land_val", "imprvt_val", "last_yr_tax", "bldg_desc", "land_desc", "calc_acre",
    "yr_constr", "last_sale_price", "last_sale_date", "sale_date", "latest_status", "sheriff_number",
    "court_case", "plaintiff", "defendant", "judgment", "upset", "attorney", "attorney_phone",
    "description", "lat", "lon", "geo_source", "outcome", "outcome_date", "outcome_status",
    "net_value_total", "match_method", "stated_block", "stated_lot", "parcel_text",
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
    "match_method", "stated_block", "stated_lot",
]
PRESS_HOME = {2: "https://press.essexregister.com/prodpress/clerk/ClerkHome.aspx?op=basic",
              17: "https://records.passaiccountynj.org/press/clerk/ClerkHome.aspx?op=basic",
              3: "https://press.co.burlington.nj.us/press/clerk/ClerkHome.aspx?op=basic",
              8: "https://oprs.co.monmouth.nj.us/Oprs/clerk/ClerkHome.aspx?op=basic",
              1100: "https://records.mercercounty.org/publicsearch/",
              20: "https://clerkrecordsng.salemcountynj.gov/publicsearch/",
              15: "https://clerk.ucnj.org/UCPA/DocIndex"}


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
    """Past sheriff's sale outcomes from the state's deed files (bank take-backs and third-party
    sales), minus parcels already on the map."""
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
        if r.get("outcome") == "sold":
            p.update({"stage": "sale_hist", "is_open": 0, "outcome": "sold", "outcome_date": r["reo_date"],
                      "sale_date": r["reo_date"], "sold_price": r["reo_price"],
                      "latest_status": "Sold at sheriff's sale",
                      "outcome_status": "Sold at sheriff's sale to a third party",
                      "status_history": [], "source": "NJ deed records (SR-1A)", "detail_url": None,
                      "city": r.get("muni"), "plaintiff": None, "defendant": None, "enriched": False})
            out.append(p)
            continue
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


def _norm_part(v) -> str:
    """'0014.00' / '14' / '14.0' -> '14'; '1.01' stays '1.01'; letters upper-cased."""
    v = str(v or "").strip().upper()
    if re.fullmatch(r"0*\d+(\.\d+)?", v):
        whole, _, frac = v.partition(".")
        whole = whole.lstrip("0") or "0"
        frac = frac.rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return v


def parcel_check(p: dict) -> tuple[str, str]:
    """Does the outlined tax parcel match the block/lot the distress filing itself names?
    verified  - the filing's block and lot are the outlined parcel (or the record is keyed by parcel)
    address   - matched by street address; the filing gives no block/lot to check against
    mismatch  - the filing names a different block/lot than the parcel found; nothing is outlined
    none      - no tax parcel found"""
    if not p.get("pams_pin"):
        return "none", "No tax parcel found for this filing."
    if p.get("stage") in ("reo_hist", "sale_hist"):
        return "verified", "Keyed by block and lot in the state's deed records."
    from .parcels import stated_block_lot
    from .pipeline import parcel_matches_filing
    rec = dict(p)
    if isinstance(rec.get("lots"), list):
        rec["lots"] = json.dumps(rec["lots"])
    ok = parcel_matches_filing(rec)
    lots = ", ".join(str(x) for x in (p.get("lots") or [p.get("lot")]) if x)
    if ok is None:
        return "address", "Matched by street address; the filing doesn't state a block and lot."
    if ok:
        return "verified", f"Block {p.get('block')}, Lot {lots} is the block and lot the filing names."
    sb, sl, _ = stated_block_lot(p.get("street"), p.get("parcel_text"), p.get("description"))
    sb, sl = sb or p.get("stated_block"), sl or p.get("stated_lot")
    return "mismatch", (f"The filing names Block {sb}{' Lot ' + sl if sl else ''}, but the closest tax-map parcel "
                        f"found is Block {p.get('block')} Lot {lots}. The lot isn't outlined until this is resolved.")


_LEAD_ORDER = {"open": 0, "pre": 1, "reo": 2, "closed": 3, "sold": 4}


def _rank(p: dict) -> tuple:
    if p.get("stage") == "pre":
        k = "pre" if p.get("lp_status", "pending") == "pending" else "closed"
    elif p.get("is_open"):
        k = "open"
    elif p.get("outcome") == "reo":
        k = "reo"
    elif p.get("stage") == "sale_hist":
        k = "sold"
    else:
        k = "closed"
    d = p.get("sale_date") or p.get("recorded") or p.get("outcome_date") or ""
    return (_LEAD_ORDER[k], "" if not d else "".join(chr(255 - ord(ch)) for ch in str(d)))


def _summary(p: dict) -> dict:
    if p.get("stage") == "pre":
        kind, date, label = "lis_pendens", p.get("recorded"), "Lis pendens (foreclosure filed)"
    elif p.get("stage") == "reo_hist":
        kind, date, label = "reo", p.get("outcome_date"), f"Bank took title ({p.get('reo_kind') or 'sheriff sale'})"
    elif p.get("stage") == "sale_hist":
        kind, date, label = "sold", p.get("outcome_date"), "Sold at sheriff sale to an outside buyer"
    elif p.get("is_open"):
        kind, date, label = "sheriff", p.get("sale_date"), "On the sheriff sale list"
    else:
        kind, date, label = "sheriff_closed", p.get("outcome_date") or p.get("sale_date"), p.get("outcome_status") or p.get("latest_status") or "Off the sheriff sale list"
    return {"property_id": p["property_id"], "kind": kind, "date": date, "label": label,
            "lender": p.get("plaintiff"), "amount": p.get("mortgage_amount") or p.get("judgment") or p.get("upset") or p.get("reo_price"),
            "docket": p.get("court_case") or p.get("docket"), "instrument": p.get("instrument"),
            "sheriff_number": p.get("sheriff_number")}


def group_by_parcel(props: list[dict]) -> list[dict]:
    """Attach every distress record on the same tax parcel to each of them, pick one 'lead'
    record per parcel for the map pin, and check each outline against the filing's block/lot."""
    for p in props:
        p["parcel_check"], p["parcel_check_note"] = parcel_check(p)
    groups: dict[str, list[dict]] = {}
    for p in props:
        pins = p.get("parcel_pins") or ([p["pams_pin"]] if p.get("pams_pin") else [])
        if p["parcel_check"] in ("verified", "address") and pins:
            groups.setdefault(pins[0], []).append(p)
    out_ids = set()
    for pin, ps in groups.items():
        # The state's deed record of a lender take-back the tracker already saw happen at the sheriff sale is the same event.
        seen_reo = [x for x in ps if x.get("outcome") == "reo" and x.get("stage") != "reo_hist"]
        for x in [x for x in ps if x.get("stage") == "reo_hist"]:
            if any(abs((_d(x.get("outcome_date")) - _d(y.get("outcome_date"))).days) <= 90
                   for y in seen_reo if _d(y.get("outcome_date")) and _d(x.get("outcome_date"))):
                out_ids.add(x["property_id"])
        ps = [x for x in ps if x["property_id"] not in out_ids]
        ps.sort(key=_rank)
        group = [_summary(x) for x in ps]
        for i, x in enumerate(ps):
            x["parcel_group"] = group if len(group) > 1 else None
            x["parcel_lead"] = i == 0
    for p in props:
        p.setdefault("parcel_lead", True)
        p.setdefault("parcel_group", None)
    return [p for p in props if p["property_id"] not in out_ids]


def _d(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


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
    props += reo_history_props(db, set())
    props = group_by_parcel(props)
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


def add_state_deeds(db: DB, records: dict, props: list[dict]) -> None:
    """Every county: the state's deed history (2020 on; names withheld by law) for each property's parcel."""
    try:
        rows = db.conn.execute("SELECT pin, deed_date, recorded, price, nu, kind, location FROM sr1a_sales "
                               "ORDER BY deed_date DESC").fetchall()
    except Exception:  # noqa: BLE001 - deeds not loaded yet
        return
    by_pin: dict[str, list] = {}
    for r in rows:
        by_pin.setdefault(r[0], []).append({"date": r[1], "recorded": r[2], "price": r[3], "code": r[4],
                                            "kind": r[5], "location": r[6], "pin": r[0]})
    for p in props:
        pins = p.get("parcel_pins") or ([p["pams_pin"]] if p.get("pams_pin") else [])
        deeds = [d for pin in pins for d in by_pin.get(pin, [])]
        if deeds:
            deeds.sort(key=lambda d: d["date"] or "", reverse=True)
            records.setdefault(p["property_id"], {})["state_deeds"] = deeds[:40]


def export_all(db: DB, out_dir: Path, reo_days: int = 3300) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    map_data = build_map_data(db, reo_days=reo_days)
    (out_dir / "map.json").write_text(json.dumps(map_data, separators=(",", ":"), default=str))
    with open(out_dir / "leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(db.open_leads())
    from .enrich import export_contacts, export_history
    contacts = export_contacts(db)
    (out_dir / "contacts.json").write_text(json.dumps(contacts, separators=(",", ":"), default=str))
    records = export_history(db)
    add_state_deeds(db, records, map_data["properties"])
    (out_dir / "records.json").write_text(json.dumps(records, separators=(",", ":"), default=str))
    reo = [dict(r) for r in db.conn.execute(
        "SELECT * FROM listings WHERE outcome='reo' AND category IN ('target','land') ORDER BY outcome_date DESC")]
    with open(out_dir / "reo.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(reo)
