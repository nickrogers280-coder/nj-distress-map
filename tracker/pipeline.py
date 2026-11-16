"""One run of the tracker: scrape every county, enrich new listings, detect changes."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .civilview import CivilViewClient, ListingRow, SaleDetail, outcome_from_history
from .counties import CIVILVIEW_NJ
from .db import DB, norm_address
from .parcels import ParcelClient, looks_like_entity, parse_block_lot, parse_lot_list

log = logging.getLogger(__name__)

TRACKED = ("target", "land")

_CONDO_ADDR = re.compile(r"\b(UNIT|APT|APARTMENT|SUITE|STE)\b|#\s*\w", re.I)
_CONDO_TEXT = re.compile(r"CONDO|CNDO", re.I)


def listing_key(cid: int, sheriff_number: str | None, fallback: str) -> str:
    """Stable ID for a sheriff sale listing.

    CivilView hands out a new PropertyId every time a county refreshes its list, so the
    PropertyId can't identify a listing from one day to the next. The sheriff number is
    stable, so listings are keyed by county + sheriff number. `fallback` (normally the
    normalized address) is used only when the sheriff number is unknown.
    """
    sn = re.sub(r"\s+", "", (sheriff_number or "")).upper()
    return f"{cid}:{sn}" if sn else f"{cid}:@{fallback}"


def resolve_key(db: DB, cid: int, row: ListingRow, claimed: set[str] | None = None) -> str | None:
    """Key for a list row, or None if it can only be settled from the detail page.

    Some counties (Middlesex, Hunterdon, Salem) leave the sheriff number off their list
    pages; those rows are matched to what's already stored by address.
    """
    if row.sheriff_number:
        return listing_key(cid, row.sheriff_number, "")
    claimed = claimed or set()
    hit = db.find_by_cv_id(cid, row.property_id)      # same county refresh as last time
    if hit and hit not in claimed:
        return hit
    addr = norm_address(row.address)
    if addr:
        for k in db.find_by_address(cid, addr):      # two filings can share one address
            if k not in claimed:
                return k
    return None


def key_after_detail(cid: int, row: ListingRow, detail: SaleDetail) -> str:
    return listing_key(cid, row.sheriff_number or detail.sheriff_number,
                       norm_address(row.address or detail.address) or f"id{row.property_id}")


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


def condo_reason(rec: dict) -> str | None:
    """Why a listing looks like a condominium unit (residential or commercial), or None."""
    if (rec.get("qual") or "").upper().startswith("C"):
        return f"condo qualifier {rec['qual']}"
    for f in ("bldg_desc", "land_desc"):
        if _CONDO_TEXT.search(rec.get(f) or ""):
            return f"tax record says {rec[f].strip()}"
    if _CONDO_ADDR.search(rec.get("street") or rec.get("address") or ""):
        return "address is a unit"
    for f in ("description", "parcel_text"):
        if re.search(r"CONDOMINIUM", rec.get(f) or "", re.I):
            return "legal description is a condominium unit"
    return None


def classify(rec: dict, cfg: dict) -> tuple[str, str]:
    """target = tax class 4A/4B/4C, land = class 1 (vacant), other = everything else.

    Condominium units (residential or commercial) and listings that couldn't be matched to a
    tax parcel are never tracked.
    """
    targets = {c.upper() for c in cfg.get("target_classes", ["4A", "4B", "4C"])}
    land = {c.upper() for c in cfg.get("land_classes", ["1"])}
    pc = (rec.get("prop_class") or "").upper()
    if not pc:
        return "other", "No tax-record match"
    if pc not in targets and pc not in land:
        return "other", f"Tax class {pc} ({rec.get('class_label')})"
    why = condo_reason(rec)
    if why:
        return "other", f"Condominium unit excluded ({why})"
    if rec.get("net_value") == 0:
        return "other", "Tax parcel assessed at $0 (condo common area or exempt lot), not the collateral"
    if pc in land:
        return "land", f"Tax class {pc} ({rec.get('class_label')})"
    min_val = cfg.get("min_assessed_value") or 0
    if (rec.get("net_value") or 0) < min_val:
        return "other", f"Tax class {pc} below assessed-value threshold"
    return "target", f"Tax class {pc} ({rec.get('class_label')})"


def parcel_audit(cfg: dict, parcels: ParcelClient, db: DB, geocoder=None) -> dict:
    """One-time re-match of stored listings under the stricter parcel rules: address-only and
    unmatched listings are looked up again, and every tracked property's pin is re-placed with
    the improved parcel centre point."""
    rows = db.conn.execute(
        "SELECT * FROM listings WHERE (is_open=1 OR outcome='reo') AND "
        "(match_method IS NULL OR match_method IN ('address','unmatched') OR category IN ('target','land','possible'))"
    ).fetchall()
    s = {"checked": 0, "rematched": 0, "repinned": 0}
    for r in rows:
        rec = dict(r)
        before = (rec.get("pams_pin"), rec.get("match_method"))
        if rec.get("match_method") in (None, "address", "unmatched"):
            for k in ("pams_pin", "prop_class", "class_label", "prop_loc", "net_value", "land_val", "imprvt_val",
                      "last_yr_tax", "bldg_desc", "land_desc", "calc_acre", "yr_constr", "last_sale_price",
                      "last_sale_date", "muni"):
                rec[k] = None
            _enrich(rec, parcels, rec.get("county") or "")
            s["checked"] += 1
        rec["category"], rec["category_reason"] = classify(rec, cfg)
        if rec["category"] in TRACKED and rec.get("match_method") not in (None, "address", "unmatched") \
                and rec.get("pams_pin") and not rec.get("parcel_pins"):
            _enrich(rec, parcels, rec.get("county") or "")
            rec["category"], rec["category_reason"] = classify(rec, cfg)
        if rec["category"] in TRACKED:
            rec["lat"] = rec["lon"] = rec["geo_source"] = None
            locate(rec, parcels, geocoder)
            s["repinned"] += 1
        s["rematched"] += before != (rec.get("pams_pin"), rec.get("match_method"))
        db.upsert({k: rec.get(k) for k in rec if k in r.keys()})
    db.commit()
    return s


def reclassify_all(db: DB, cfg: dict) -> dict:
    """Re-apply the current rules to every stored listing (no network). Returns counts by change."""
    changed = {}
    for r in db.conn.execute("SELECT * FROM listings").fetchall():
        r = dict(r)
        cat, why = classify(r, cfg)
        if (cat, why) != (r.get("category"), r.get("category_reason")):
            db.conn.execute("UPDATE listings SET category=?, category_reason=? WHERE property_id=?",
                            (cat, why, r["property_id"]))
            k = f"{r.get('category')}->{cat}"
            changed[k] = changed.get(k, 0) + 1
    db.commit()
    return changed


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
        _add_sibling_lots(rec, parcels, m)
    else:
        rec["match_method"] = "unmatched"
        rec["parcel_pins"] = rec["lots"] = None
    return rec


def _add_sibling_lots(rec: dict, parcels: ParcelClient, m) -> None:
    """Record every lot the notice covers (so the map outlines all of the collateral) and
    their combined assessment."""
    norm = lambda l: (l or "").strip().upper().removesuffix(".00").removesuffix(".0")
    named = parse_lot_list(rec.get("parcel_text"), rec.get("description"))
    if norm(m.lot) not in {norm(l) for l in named}:
        named = []          # the notice's lot list doesn't include the matched lot; don't trust it
    others = [l for l in named if norm(l) != norm(m.lot)]
    pins, lots, total = [m.pams_pin], [m.lot], m.net_value
    if others and hasattr(parcels, "sibling_lots"):
        for a in parcels.sibling_lots(m.pams_pin, m.block, others):
            pin = a.get("PAMS_PIN")
            if pin and pin not in pins:
                pins.append(pin)
                lots.append(a.get("PCLLOT"))
                if a.get("NET_VALUE") is not None:
                    total = (total or 0) + a["NET_VALUE"]
    rec["parcel_pins"] = json.dumps(pins)
    rec["lots"] = json.dumps(lots)
    rec["net_value_total"] = total if len(pins) > 1 else None


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
            cvid = row.property_id
            pid = resolve_key(db, cid, row, seen)
            old = db.get(pid) if pid else None
            if pid:
                seen.add(pid)
            try:
                if old is None:
                    if max_new is not None and stats["details_fetched"] >= max_new:
                        continue
                    detail = cv.detail(cvid)
                    stats["details_fetched"] += 1
                    pid = key_after_detail(cid, row, detail)
                    seen.add(pid)
                    old = db.get(pid)
                if old is None:
                    rec = build_record(cfg, parcels, geocoder, cid, cname, row, detail, ts, is_open=True, key=pid)
                    db.upsert(rec)
                    db.add_event(pid, ts, "new", baseline=stats["baseline"])
                    stats["new"] += 1
                    stats["new_tracked"] += rec["category"] in TRACKED
                    continue

                upd = {"property_id": pid, "cv_id": cvid, "addr_norm": norm_address(row.address) or old.get("addr_norm"),
                       "last_seen": ts, "is_open": 1, "closed_at": None,
                       "list_status": row.status or old.get("list_status")}
                if not old.get("is_open"):
                    db.add_event(pid, ts, "status", old.get("latest_status"), "Back on the open sale list")
                    stats["changes"] += 1
                fresh = {"sale_date": row.sale_date} if row.sale_date else {}
                last = old.get("last_detail_at")
                due = not last or datetime.fromisoformat(ts) - datetime.fromisoformat(last) >= refresh_after
                if old.get("category") in TRACKED and due:
                    detail = cv.detail(cvid)
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
                    d = cv.detail(old.get("cv_id") or pid)
                    stats["details_fetched"] += 1
                    if d.sheriff_number and old.get("sheriff_number") and \
                            re.sub(r"\s+", "", d.sheriff_number) != re.sub(r"\s+", "", old["sheriff_number"]):
                        raise ValueError("detail page now belongs to a different listing")
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
                 detail: SaleDetail, ts: str, is_open: bool, key: str | None = None) -> dict:
    """A new listing record from a list row + its detail page, enriched and classified."""
    key = key or key_after_detail(cid, row, detail)
    rec = {"property_id": key, "cv_id": row.property_id, "addr_norm": norm_address(row.address) or None,
           "county_id": cid, "county": cname, "list_status": row.status,
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
            cvid = row.property_id
            if row.sale_date and row.sale_date < cutoff:
                continue
            pid = resolve_key(db, cid, row) or listing_key(cid, None, norm_address(row.address) or f"id{cvid}")
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
                detail = cv.detail(cvid)
                s["sold_details"] += 1
                outcome, odate, ostatus = outcome_from_history(detail.status_history)
                db.mark_sold_checked(pid, cid, ts, outcome)
                if not existing and not row.sheriff_number and detail.sheriff_number:
                    pid = listing_key(cid, detail.sheriff_number, "")
                    existing = db.get(pid)
                if outcome is None:
                    continue
                fields = {"outcome": outcome, "outcome_date": odate or row.sale_date, "outcome_status": ostatus,
                          "status_history": json.dumps(detail.status_history),
                          "latest_status": detail.latest_status}
                fields["cv_id"] = cvid
                if existing:
                    db.upsert({"property_id": pid, **fields})
                    rec = {**existing, **fields}
                elif outcome == "reo":
                    rec = build_record(cfg, parcels, geocoder, cid, cname, row, detail, ts, is_open=False, key=pid)
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
