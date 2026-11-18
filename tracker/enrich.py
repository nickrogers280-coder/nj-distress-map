"""Enrich tracked properties with owner- and lender-side contacts from public records.

For each property the owner (first defendant) is searched in the county clerk's index. Every
filing it's a party to is listed; people named personally on those filings (co-borrowers,
guarantors, members) are pulled from the index; and the owner's most recent mortgages are
opened and OCR'd to read who signed for the LLC, who prepared the loan at the lender, and
where the lender wants mail sent.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .contacts import (DocFacts, lender_profile, merge_facts, parse_document, person_from_index, same_party,
                       search_terms)
from .db import DB
from .parcels import looks_like_entity
from .press import PRESS_SITES, PressClient, ocr_png

log = logging.getLogger(__name__)

MORTGAGE_RE = re.compile(r"^MORTGAGE\b(?!.*(?:CANCEL|DISCHARGE|SATISF|ASSIGN|RELEASE))", re.I)
NOTABLE = [
    (re.compile(r"NOTICE OF SETTLEMENT", re.I), "Notice of settlement (sale or refinance under contract)"),
    (re.compile(r"LIS PENDENS.*DISCHARGE|DISCHARGE.*LIS PENDENS", re.I), "Lis pendens discharged"),
    (re.compile(r"LIS PENDENS.*IN REM|IN REM", re.I), "Tax foreclosure (in rem lis pendens)"),
    (re.compile(r"SHERIFF", re.I), "Sheriff's deed"),
    (re.compile(r"EASEMENT|RIGHT OF WAY", re.I), "Easement"),
    (re.compile(r"TAX SALE CERT|TAX CERT", re.I), "Tax sale certificate"),
    (re.compile(r"REDEMPTION", re.I), "Tax certificate redeemed"),
    (re.compile(r"ASSIGNMENT OF (LEASES|RENTS)", re.I), "Assignment of leases and rents"),
    (re.compile(r"LEASE", re.I), "Lease / memorandum of lease"),
    (re.compile(r"JUDGMENT", re.I), "Judgment"),
    (re.compile(r"MASTER DEED|CONDOMINIUM", re.I), "Condominium master deed"),
    (re.compile(r"LIS PENDENS", re.I), "Lis pendens"),
    (re.compile(r"^DEED\b", re.I), "Deed"),
    (re.compile(r"ASSIGN", re.I), "Assignment"),
    (re.compile(r"CANCEL|DISCHARGE|SATISF", re.I), "Mortgage cancelled / discharged"),
    (re.compile(r"CONSTRUCTION LIEN|LIEN", re.I), "Lien"),
    (re.compile(r"UCC|FINANCING STATEMENT", re.I), "UCC financing statement"),
    (re.compile(r"MORTGAGE", re.I), "Mortgage"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_table(db: DB) -> None:
    db.conn.execute("""CREATE TABLE IF NOT EXISTS enrichment (
        property_id TEXT PRIMARY KEY, updated_at TEXT, status TEXT, data TEXT)""")
    db.commit()


def candidates(db: DB, limit: int, refresh_days: int = 30) -> list[dict]:
    ensure_table(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=refresh_days)).isoformat()
    rows = [dict(r) for r in db.conn.execute(
        """SELECT l.*, e.property_id AS done FROM listings l LEFT JOIN enrichment e ON e.property_id = l.property_id
           WHERE l.category IN ('target','land') AND (l.is_open = 1 OR l.outcome = 'reo')
             AND (e.property_id IS NULL OR e.updated_at < ?)""", (cutoff,))]
    try:
        rows += [dict(r) for r in db.conn.execute(
            """SELECT p.*, e.property_id AS done FROM lis_pendens p LEFT JOIN enrichment e ON e.property_id = p.property_id
               WHERE p.category IN ('target','land') AND p.status = 'pending'
                 AND (e.property_id IS NULL OR e.updated_at < ?)""", (cutoff,))]
    except Exception:  # noqa: BLE001 - no pre-foreclosure table yet
        pass
    rows.sort(key=lambda r: (r.get("done") is not None, -(r.get("net_value_total") or r.get("net_value") or 0)))
    return rows[:limit]


DEBUG_DIR = None   # set by enrich_contacts.py: where screenshots of failed document opens go


def save_debug(client, name: str, keep: int = 6) -> None:
    """Keep a few screenshots of failed document opens (committed with the data) for diagnosis."""
    png = getattr(client, "debug_png", None)
    if not png or DEBUG_DIR is None:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        if len(list(DEBUG_DIR.glob("*.png"))) < keep:
            (DEBUG_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.png").write_bytes(png)
    except OSError:
        pass


def _same_block_lot(r, rec: dict) -> bool | None:
    """True/False when the index row names a block (and lot); None when it doesn't (N/A)."""
    norm = lambda x: re.sub(r"\.0+$", "", (x or "").strip().upper())
    if not r.block or norm(r.block) in ("", "N/A", "NA"):
        return None
    if norm(r.block) != norm(rec.get("block")):
        return False
    lots = {norm(l) for l in (json.loads(rec["lots"]) if rec.get("lots") else [])} | {norm(rec.get("lot"))}
    return not r.lot or norm(r.lot) in lots


def build_history(rec: dict, parcel_rows: list, owner_rows: list, owner: str | None) -> list[dict]:
    """One timeline of recorded documents for the property, newest first.

    Everything indexed against the parcel's block/lot, plus the owner's filings that either name
    this block/lot or name no parcel at all (mortgages and lis pendens are often indexed without
    one). Filings for the owner's other properties are left out."""
    rows = list(parcel_rows)
    for r in owner_rows:
        if owner and not (same_party(r.direct, owner) or same_party(r.indirect, owner)):
            continue
        if _same_block_lot(r, rec) is False:
            continue
        rows.append(r)
    by_inst: dict[str, list] = defaultdict(list)
    for r in rows:
        by_inst[r.instrument].append(r)
    out = []
    for inst, rs in by_inst.items():
        r0 = rs[0]
        out.append({"recorded": r0.recorded_iso, "type": r0.doc_type, "kind": classify_filing(r0.doc_type),
                    "instrument": inst, "from": sorted({r.direct for r in rs if r.direct}),
                    "to": sorted({r.indirect for r in rs if r.indirect}),
                    "block": r0.block if r0.block not in ("", "N/A") else None, "lot": r0.lot or None})
    out.sort(key=lambda f: (f["recorded"] or "", f["instrument"]), reverse=True)
    return out[:150]


def lender_side(rec: dict) -> dict:
    prof = lender_profile(rec.get("plaintiff"))
    return {"plaintiff": rec.get("plaintiff"), **prof,
            "foreclosure_counsel": {"name": rec.get("attorney"), "phone": rec.get("attorney_phone")}
            if rec.get("attorney") else None}


def classify_filing(doc_type: str) -> str:
    for rx, label in NOTABLE:
        if rx.search(doc_type or ""):
            return label
    return (doc_type or "").title()


def enrich_one(rec: dict, client: PressClient | None, bank_lookup=None, max_docs: int = 2) -> dict:
    owner = rec.get("defendant") or ""
    out = {"property_id": rec["property_id"], "generated_at": now_iso(), "county": rec.get("county"),
           "owner": {"name": None, "people": [], "signers": [], "filings": [], "mailing": None},
           "lender": lender_side(rec), "documents": [], "source": None, "notes": []}
    if bank_lookup and out["lender"]["kind"] == "Bank":
        out["lender"]["bank"] = bank_lookup(rec.get("plaintiff"))
    if client is None:
        out["notes"].append(f"Recorded documents for {rec.get('county')} County aren't searchable online yet; "
                            "owner details come from the sheriff listing only.")
        return out
    out["source"] = client.base

    # The parcel's own record history: everything indexed against its block and lot.
    parcel_rows = []
    if rec.get("block") and hasattr(client, "search_block_lot"):
        try:
            parcel_rows = client.search_block_lot(rec.get("muni") or rec.get("city") or "", rec["block"], rec.get("lot")) or []
        except Exception as e:  # noqa: BLE001
            log.info("Block/lot search failed for %s: %s", rec["property_id"], e)

    rows, used = [], None
    for term in search_terms(owner):
        rows = client.search_name(term)
        if rows:
            used = term
            break
    out["owner"]["name"] = used or owner
    out["history"] = build_history(rec, parcel_rows, rows, used)
    if not rows:
        out["notes"].append(f"No recorded filings found under “{owner.split(';')[0].strip()}”.")
        return out

    # Group index rows by instrument; keep instruments the owner is a party to.
    by_inst: dict[str, list] = defaultdict(list)
    for r in rows:
        by_inst[r.instrument].append(r)
    owner_insts = {i: rs for i, rs in by_inst.items() if any(same_party(r.direct, used) or same_party(r.indirect, used) for r in rs)}

    people: dict[str, dict] = {}
    filings = []
    for inst, rs in sorted(owner_insts.items(), key=lambda kv: kv[1][0].recorded_iso or "", reverse=True):
        r0 = rs[0]
        parties_direct = sorted({r.direct for r in rs})
        parties_indirect = sorted({r.indirect for r in rs})
        filings.append({"type": r0.doc_type, "kind": classify_filing(r0.doc_type), "instrument": inst,
                        "recorded": r0.recorded_iso, "town": r0.town, "block": r0.block, "lot": r0.lot,
                        "from": parties_direct, "to": parties_indirect})
        if re.search(r"MORTGAGE|LIS PENDENS|NOTICE OF SETTLEMENT|DEED", r0.doc_type, re.I):
            for p in parties_direct + parties_indirect:
                nm = person_from_index(p)
                if nm and not same_party(p, used):
                    e = people.setdefault(nm, {"name": nm, "seen_on": []})
                    e["seen_on"].append(f"{r0.doc_type.title()} {r0.recorded_iso or ''}".strip())
        # Related companies named alongside the owner (e.g. guarantor entities in the lis pendens).
    related = sorted({p for f in filings if re.search("LIS PENDENS|MORTGAGE", f["type"], re.I)
                      for p in f["from"] if looks_like_entity(p) and not same_party(p, used)
                      and not re.search(r"STATE OF|TOWNSHIP|CITY OF|UNITED STATES", p)})
    out["owner"]["related_entities"] = related[:8]
    out["owner"]["filings"] = filings[:40]
    out["owner"]["people"] = list(people.values())

    # Read the owner's most recent mortgages.
    mortgages = [f for f in filings if MORTGAGE_RE.search(f["type"]) and any(same_party(p, used) for p in f["from"])][:max_docs]
    facts = []
    for f in mortgages:
        doc = client.open_document(f["instrument"])
        if not doc:
            out["notes"].append(f"Couldn't open mortgage {f['instrument']}"
                                f"{' (' + client.last_error + ')' if getattr(client, 'last_error', None) else ''}.")
            save_debug(client, f"{rec.get('county', 'x')}-{f['instrument']}")
            continue
        tile, n = doc
        pages = sorted({i for i in (0, 1, 2, 3, n - 2, n - 1) if 0 <= i < n})
        texts = []
        for i in pages:
            png = client.page_image(tile, i)
            if png:
                texts.append(ocr_png(png))
        df = merge_facts([parse_document(t) for t in texts])
        facts.append(df)
        lenders = f["to"]
        out["documents"].append({"type": f["type"], "instrument": f["instrument"], "recorded": f["recorded"],
                                 "lender": df.lender_name or (lenders[0] if lenders else None),
                                 "amount": df.amount, "signers": df.signers, "prepared_by": df.prepared_by,
                                 "return_to": df.return_to, "pages_read": len(texts), "page_count": n})
    if facts:
        m = merge_facts(facts)
        out["owner"]["signers"] = m.signers
        latest = out["documents"][0]
        out["lender"]["loan"] = {"lender": latest["lender"], "amount": latest["amount"], "date": latest["recorded"],
                                 "instrument": latest["instrument"], "prepared_by": latest["prepared_by"],
                                 "notice_address": latest["return_to"]}
    flags = [f for f in filings if f["kind"].startswith("Notice of settlement")
             and (f["recorded"] or "") >= (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()]
    if flags:
        f = flags[0]
        out["notes"].append(f"Notice of settlement recorded {f['recorded']} to {', '.join(f['to'])} "
                            "— a sale or refinance may be under contract.")
    return out


def bank_lookup_factory(session=None):
    """FDIC BankFind: headquarters address and website for an FDIC-insured bank."""
    import requests
    s = session or requests.Session()
    cache: dict[str, dict | None] = {}

    def lookup(plaintiff: str | None) -> dict | None:
        name = re.split(r",|\bAS\b|\bN\.?A\.?\b|\bF\.?S\.?B\.?\b", (plaintiff or "").upper())[0].strip()
        name = re.sub(r"[^A-Z0-9 &]", "", name)
        if len(name) < 4:
            return None
        if name in cache:
            return cache[name]
        res = None
        for url in ("https://api.fdic.gov/banks/institutions", "https://banks.data.fdic.gov/api/institutions"):
            try:
                r = s.get(url, params={"filters": "ACTIVE:1", "search": f"NAME:{name}",
                                       "fields": "NAME,ADDRESS,CITY,STALP,ZIP,WEBADDR,CERT", "limit": 3}, timeout=20)
                r.raise_for_status()
                data = [d["data"] for d in r.json().get("data", [])]
                if data:
                    d = data[0]
                    res = {"name": d.get("NAME"), "hq": f"{d.get('ADDRESS')}, {d.get('CITY')}, {d.get('STALP')} {d.get('ZIP')}",
                           "website": d.get("WEBADDR"), "fdic_cert": d.get("CERT")}
                break
            except Exception as e:  # noqa: BLE001
                log.info("FDIC lookup failed (%s): %s", url, e)
        cache[name] = res
        return res
    return lookup


def run_enrichment(db: DB, limit: int = 25, headless: bool = True) -> dict:
    """Enrich up to `limit` properties that haven't been done (or are >30 days old)."""
    ensure_table(db)
    todo = candidates(db, limit)
    stats = {"tried": 0, "ok": 0, "errors": 0, "no_source": 0}
    if not todo:
        return stats
    bank = bank_lookup_factory()
    pw = browser = None
    clients: dict[int, PressClient] = {}
    try:
        if any(r.get("county_id") in PRESS_SITES for r in todo):
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=headless)
        for rec in todo:
            stats["tried"] += 1
            cid = rec.get("county_id")
            client = None
            if cid in PRESS_SITES and browser is not None:
                if cid not in clients:
                    ctx = browser.new_context(user_agent="Mozilla/5.0 (NJ-Distress-Tracker; public records research)")
                    clients[cid] = PressClient(PRESS_SITES[cid], ctx.new_page())
                client = clients[cid]
            else:
                stats["no_source"] += 1
            try:
                data = enrich_one(rec, client, bank_lookup=bank)
                status = "ok"
                stats["ok"] += 1
            except Exception as e:  # noqa: BLE001 - one bad property must not stop the rest
                log.exception("Enrichment failed for %s", rec["property_id"])
                data = {"property_id": rec["property_id"], "generated_at": now_iso(), "error": str(e)[:300],
                        "lender": lender_side(rec)}
                status = "error"
                stats["errors"] += 1
            db.conn.execute("INSERT OR REPLACE INTO enrichment (property_id, updated_at, status, data) VALUES (?,?,?,?)",
                            (rec["property_id"], now_iso(), status, json.dumps(data)))
            db.commit()
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
    return stats


def export_contacts(db: DB) -> dict:
    """Contacts (the paid layer), without the record history, which is published separately."""
    ensure_table(db)
    out = {}
    for pid, updated, status, data in db.conn.execute("SELECT property_id, updated_at, status, data FROM enrichment"):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        d.pop("history", None)
        out[pid] = d
    return out


def export_history(db: DB) -> dict:
    """Recorded-document history per property, for the map's 'Public record history' section."""
    ensure_table(db)
    out = {}
    for pid, data in db.conn.execute("SELECT property_id, data FROM enrichment"):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        if d.get("history") is not None:
            out[pid] = {"generated_at": d.get("generated_at"), "county": d.get("county"), "source": d.get("source"),
                        "filings": d["history"]}
    return out
