"""One run of the tracker: scrape every county, enrich new listings, detect changes."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .civilview import CivilViewClient, ListingRow, SaleDetail, outcome_from_history
from .counties import CIVILVIEW_NJ
from .db import DB
from .parcels import ParcelClient, looks_like_entity, parse_block_lot

log = logging.getLogger(__name__)

TRACKED = ("target", "possible")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def first_defendant(defendant: str | None) -> str:
    """The borrower/owner is listed first; later names are usually junior creditors."""
    if not defendant:
        return ""
    first = re.split(r";|\bET\.?\s*AL\b|\bET\s+ALS\b|\bA/K/A\b|\bAND\b", defendant, flags=re.I)[0]
    parts = first.split(",")
    seg = parts[0]
    if len(parts) > 1 and re.match(r"\s*(L\.?L\.?C|INC|CORP|L\.?P|LTD|CO)\b", parts[1], re.I):
        seg += "," + parts[1]
    return seg.strip()


def classify(rec: dict, cfg: dict) -> tuple[str, str]:
    targets = {c.upper() for c in cfg.get("target_classes", ["1", "4A", "4B", "4C"])}
    pc = (rec.get("prop_class") or "").upper()
    if pc:
        if pc in targets:
            min_val = cfg.get("min_assessed_value") or 0
            if (rec.get("net_value") or 0) >= min_val:
                return "target", f"Tax class {pc} ({rec.get('class_label')})"
            return "other", f"Tax class {pc} below assessed-value threshold"
        return "other", f"Tax class {pc} ({rec.get('class_label')})"
    if cfg.get("include_possible", True):
        owner = first_defendant(rec.get("defendant"))
        unit = re.search(r"\b(UNIT|APT|APARTMENT)\b", rec.get("street") or "", re.I)
        amount = max(rec.get("judgment") or 0, rec.get("upset") or 0)
        if looks_like_entity(owner) and not unit and amount >= (cfg.get("possible_min_amount") or 0):
            return "possible", f"No tax-record match; owner '{owner}' is a business entity"
    return "other", "No tax-record match"


def _detail_fields(d: SaleDetail) -> dict:
    return {
        "county": d.county or None,
        "sheriff_number": d.sheriff_number,
        "court_case": d.court_case,
        "sale_date": d.sale_date,
        "latest_status": d.latest_status,
        "status_history": json.dumps(d.status_history),
        "plaintiff": d.plaintiff,
        "defendant": d.defendant,
        "address": d.address,
        "street": d.street,
        "city": d.city,
        "zip": d.zip,
        "description": d.description,
        "parcel_text": d.parcel_text,
        "judgment": d.judgment,
        "upset": d.upset,
        "attorney": d.attorney,
        "attorney_phone": d.attorney_phone,
        "extra": json.dumps(d.extra) if d.extra else None,
    }


def _enrich(rec: dict, parcels: ParcelClient, county_name: str) -> dict:
    block, lot, qual = parse_block_lot(rec.get("parcel_text"), rec.get("description"))
    rec.update({"block": block, "lot": lot, "qual": qual})
    m = parcels.lookup(county_name, rec.get("city") or "", rec.get("street") or rec.get("address") or "",
                       block, lot, qual)
    if m:
        rec.update({
            "pams_pin": m.pams_pin, "muni": m.muni, "block": m.block, "lot": m.lot, "qual": m.qual,
            "prop_class": m.prop_class, "class_label": m.class_label, "prop_loc": m.prop_loc,
            "net_value": m.net_value, "land_val": m.land_val, "imprvt_val": m.imprvt_val,
            "last_yr_tax": m.last_yr_tax, "bldg_desc": m.bldg_desc, "land_desc": m.land_desc,
            "calc_acre": m.calc_acre, "yr_constr": m.yr_constr, "last_sale_price": m.sale_price,
            "last_sale_date": m.deed_date, "match_method": m.match_method,
        })
    else:
        rec["match_method"] = "unmatched"
    return rec


def _history(value) -> list[list]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            return [list(x) for x in json.loads(value)]
        except json.JSONDecodeError:
            return []
    return [list(x) for x in value]


def _fmt_status(entry: list) -> str:
    status, date = entry[0], (entry[1] if len(entry) > 1 else None)
    return f"{status} ({date})" if date else status


def diff_and_log(db: DB, ts: str, old: dict, new: dict) -> int:
    """Record events for meaningful changes between a stored listing and fresh data."""
    pid = old["property_id"]
    n = 0
    if new.get("sale_date") and new["sale_date"] != old.get("sale_date"):
        db.add_event(pid, ts, "sale_date", old.get("sale_date"), new["sale_date"])
        n += 1
    if "status_history" in new:
        before = _history(old.get("status_history"))
        after = _history(new["status_history"])
        added = after[len(before):] if after[:len(before)] == before else [e for e in after if e not in before]
        for e in added:
            db.add_event(pid, ts, "status", _fmt_status(before[-1]) if before else None, _fmt_status(e))
            n += 1
    for key in ("upset", "judgment"):
        if key in new and new[key] is not None and old.get(key) is not None and abs(new[key] - old[key]) >= 1:
            db.add_event(pid, ts, key, old[key], new[key])
            n += 1
    return n


def locate(rec: dict, parcels: ParcelClient, geocoder=None) -> None:
    """Set lat/lon: parcel centroid when matched to a tax parcel, else geocode the address."""
    ll, source = None, None
    if rec.get("pams_pin"):
        ll, source = parcels.centroid(rec["pams_pin"]), "parcel"
    if not ll and geocoder is not None:
        street = rec.get("street") or rec.get("address") or ""
        ll, source = geocoder.locate(street, rec.get("city") or "", rec.get("zip") or ""), "address"
    if ll:
        rec["lat"], rec["lon"], rec["geo_source"] = ll[0], ll[1], source


def run(cfg: dict, cv: CivilViewClient, parcels: ParcelClient, db: DB, ts: str | None = None,
        geocoder=None) -> dict:
    ts = ts or now_iso()
    counties = cfg.get("counties") or list(CIVILVIEW_NJ)
    refresh_after = timedelta(hours=cfg.get("refresh_tracked_details_hours", 20))
    max_new = cfg.get("max_new_details_per_run")
    stats = {"baseline": db.count_listings() == 0, "counties": {}, "county_errors": {},
             "new": 0, "new_tracked": 0, "details_fetched": 0, "changes": 0, "closed": 0}

    for cid in counties:
        cname = CIVILVIEW_NJ.get(cid, f"County {cid}")
        try:
            rows: list[ListingRow] = cv.list_open(cid)
        except Exception as e:  # noqa: BLE001
            log.error("%s: list fetch failed: %s", cname, e)
            stats["county_errors"][cname] = str(e)
            continue
        stats["counties"][cname] = len(rows)
        if not rows:
            # An empty list is more likely an outage than zero sales; don't close anything.
            stats["county_errors"][cname] = "List page returned no rows"
            continue

        prev_open = db.open_ids(cid)
        seen: set[str] = set()
        for row in rows:
            pid = row.property_id
            seen.add(pid)
            old = db.get(pid)
            try:
                if old is None:
                    if max_new is not None and stats["details_fetched"] >= max_new:
                        continue
                    detail = cv.detail(pid)
                    stats["details_fetched"] += 1
                    rec = build_record(cfg, parcels, geocoder, cid, cname, row, detail, ts, is_open=True)
                    db.upsert(rec)
                    db.add_event(pid, ts, "new", baseline=stats["baseline"])
                    stats["new"] += 1
                    stats["new_tracked"] += rec["category"] in TRACKED
                    continue

                upd = {"property_id": pid, "last_seen": ts, "is_open": 1, "closed_at": None,
                       "list_status": row.status or old.get("list_status")}
                if not old.get("is_open"):
                    db.add_event(pid, ts, "status", old.get("latest_status"), "Back on the open sale list")
                    stats["changes"] += 1
                fresh = {"sale_date": row.sale_date} if row.sale_date else {}
                last = old.get("last_detail_at")
                due = not last or datetime.fromisoformat(ts) - datetime.fromisoformat(last) >= refresh_after
                if old.get("category") in TRACKED and due:
                    detail = cv.detail(pid)
                    stats["details_fetched"] += 1
                    fresh = {k: v for k, v in _detail_fields(detail).items() if v not in (None, "")}
                    fresh.pop("county", None)
                    upd["last_detail_at"] = ts
                if old.get("category") in TRACKED:
                    stats["changes"] += diff_and_log(db, ts, old, fresh)
                upd.update(fresh)
                db.upsert(upd)
            except Exception as e:  # noqa: BLE001 - one bad record must not stop the run
                log.exception("%s: failed on property %s: %s", cname, pid, e)

        for pid in prev_open - seen:
            old = db.get(pid) or {}
            final = None
            if old.get("category") in TRACKED:
                try:
                    d = cv.detail(pid)
                    stats["details_fetched"] += 1
                    final = d.latest_status or None
                    outcome, odate, ostatus = outcome_from_history(d.status_history)
                    db.upsert({"property_id": pid, "status_history": json.dumps(d.status_history),
                               "latest_status": final or old.get("latest_status"),
                               "outcome": outcome, "outcome_date": odate, "outcome_status": ostatus})
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not fetch final status for %s: %s", pid, e)
                db.add_event(pid, ts, "closed", old.get("latest_status"), final or "Removed from the open sale list")
                if (db.get(pid) or {}).get("outcome") == "reo":
                    db.add_event(pid, ts, "reo", None, (db.get(pid) or {}).get("outcome_status"))
            db.upsert({"property_id": pid, "is_open": 0, "closed_at": ts})
            stats["closed"] += 1
        db.commit()
        log.info("%s: %d open, %d new so far", cname, len(rows), stats["new"])

    return stats


def build_record(cfg: dict, parcels: ParcelClient, geocoder, cid: int, cname: str, row: ListingRow,
                 detail: SaleDetail, ts: str, is_open: bool) -> dict:
    """A new listing record from a list row + its detail page, enriched and classified."""
    rec = {"property_id": row.property_id, "county_id": cid, "county": cname, "list_status": row.status,
           "is_open": 1 if is_open else 0, "first_seen": ts, "last_seen": ts, "last_detail_at": ts}
    rec.update({k: v for k, v in _detail_fields(detail).items() if v not in (None, "")})
    rec["county"] = cname
    rec.setdefault("sale_date", row.sale_date)
    rec.setdefault("address", row.address)
    rec.setdefault("street", row.address)
    rec.setdefault("plaintiff", row.plaintiff)
    rec.setdefault("defendant", row.defendant)
    _enrich(rec, parcels, cname)
    rec["category"], rec["category_reason"] = classify(rec, cfg)
    if rec["category"] in TRACKED:
        locate(rec, parcels, geocoder)
    return rec


def run_reo(cfg: dict, cv: CivilViewClient, parcels: ParcelClient, db: DB, ts: str | None = None,
            geocoder=None) -> dict:
    """Find properties the foreclosing lender bought back at sheriff sale (REO).

    Reads each county's Sold/Cancelled list, opens the detail page of every sale in the
    look-back window that hasn't been checked yet, and records REO outcomes. Properties
    already tracked from the open list just get their outcome filled in. The first pass is
    spread over several runs (max_sold_details_per_run) and raises no alerts.
    """
    ts = ts or now_iso()
    counties = cfg.get("counties") or list(CIVILVIEW_NJ)
    lookback = int(cfg.get("reo_lookback_days", 365))
    cap = cfg.get("max_sold_details_per_run", 1200)
    recheck = timedelta(days=int(cfg.get("sold_recheck_days", 7)))
    cutoff = (datetime.fromisoformat(ts) - timedelta(days=lookback)).date().isoformat()
    baseline = db.get_meta("reo_baseline_done") != "1"
    s = {"reo_baseline": baseline, "sold_rows": 0, "sold_details": 0, "reo_found": 0,
         "reo_tracked": 0, "reo_errors": {}, "reo_capped": False}

    for cid in counties:
        cname = CIVILVIEW_NJ.get(cid, f"County {cid}")
        try:
            rows = cv.list_sold(cid)
        except Exception as e:  # noqa: BLE001
            log.error("%s: sold list fetch failed: %s", cname, e)
            s["reo_errors"][cname] = str(e)
            continue
        s["sold_rows"] += len(rows)
        for row in rows:
            pid = row.property_id
            if row.sale_date and row.sale_date < cutoff:
                continue
            existing = db.get(pid)
            if existing and existing.get("outcome") in ("reo", "third_party"):
                continue
            chk = db.sold_checked(pid)
            if chk and (chk.get("outcome") or
                        datetime.fromisoformat(ts) - datetime.fromisoformat(chk["checked_at"]) < recheck):
                continue
            if cap is not None and s["sold_details"] >= cap:
                s["reo_capped"] = True
                break
            try:
                detail = cv.detail(pid)
                s["sold_details"] += 1
                outcome, odate, ostatus = outcome_from_history(detail.status_history)
                db.mark_sold_checked(pid, cid, ts, outcome)
                if outcome is None:
                    continue
                fields = {"outcome": outcome, "outcome_date": odate or row.sale_date, "outcome_status": ostatus,
                          "status_history": json.dumps(detail.status_history),
                          "latest_status": detail.latest_status}
                if existing:
                    db.upsert({"property_id": pid, **fields})
                    rec = {**existing, **fields}
                elif outcome == "reo":
                    rec = build_record(cfg, parcels, geocoder, cid, cname, row, detail, ts, is_open=False)
                    rec.update(fields)
                    rec["closed_at"] = ts
                    db.upsert(rec)
                else:
                    continue
                if outcome == "reo":
                    s["reo_found"] += 1
                    if rec.get("category") in TRACKED:
                        s["reo_tracked"] += 1
                        db.add_event(pid, ts, "reo", None, ostatus, baseline=baseline)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: failed on sold property %s: %s", cname, pid, e)
        db.commit()
        log.info("%s: %d sold/cancelled rows; %d REO so far", cname, len(rows), s["reo_found"])
        if s["reo_capped"]:
            break

    if baseline and not s["reo_capped"] and not s["reo_errors"]:
        db.set_meta("reo_baseline_done", "1")
    db.commit()
    return s
