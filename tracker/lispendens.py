"""Pre-foreclosures: foreclosure lis pendens recorded with the county clerk.

A lender records a lis pendens when it files a foreclosure complaint, months before the
property reaches the sheriff's sale list. Each day the clerk's index is searched for new
foreclosure lis pendens; each one is opened and OCR'd to find the property (address and
block/lot are in the notice, not the index), the mortgage being foreclosed and the lender's
attorney. The property is matched to its tax parcel and kept only if it's commercial
(4A/4B/4C) or vacant land, by the same rules as the sheriff-sale map.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from .db import DB
from .parcels import ParcelClient, parse_block_lot
from .press import BROWSER_UA, PRESS_COUNTY_NAMES, PRESS_SITES, PressClient, ocr_png

log = logging.getLogger(__name__)

# Document-type names differ by county: Essex "LIS PENDENS FORECLOSURE", Monmouth "FORECLOSURE
# LISPENDENS", Burlington just "LIS PENDENS" (then the document itself has to say foreclosure).
LP_FORECLOSURE = re.compile(r"LIS ?PENDENS.*FORECLOS|FORECLOS\w*\W+LIS ?PENDENS", re.I)
LP_PLAIN = re.compile(r"^\s*LIS ?PENDENS\s*$", re.I)
LP_IN_REM = re.compile(r"LIS ?PENDENS.*IN REM|\bLIS IN REM\b", re.I)        # municipal tax-lien foreclosure
LP_DISCHARGE = re.compile(r"LIS ?PENDENS.*(DISCHARGE|RELEASE)|DISCHARGE\W*LIS ?PENDENS", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lis_pendens (
    property_id      TEXT PRIMARY KEY,   -- lp:<county_id>:<instrument>
    county_id        INTEGER, county TEXT, instrument TEXT, doc_type TEXT, kind TEXT,
    recorded         TEXT, first_seen TEXT, baseline INTEGER DEFAULT 0,
    plaintiff        TEXT, defendant TEXT, parties TEXT,
    status           TEXT,               -- pending | discharged | sheriff_sale
    status_date      TEXT, sheriff_listing TEXT,
    street TEXT, city TEXT, zip TEXT, block TEXT, lot TEXT, qual TEXT, description TEXT,
    pams_pin TEXT, muni TEXT, prop_class TEXT, class_label TEXT, prop_loc TEXT, net_value REAL,
    land_val REAL, imprvt_val REAL, last_yr_tax REAL, bldg_desc TEXT, land_desc TEXT, calc_acre REAL,
    yr_constr TEXT, last_sale_price REAL, last_sale_date TEXT, match_method TEXT,
    category TEXT, category_reason TEXT, lat REAL, lon REAL, geo_source TEXT,
    mortgage_amount REAL, mortgage_date TEXT, mortgage_ref TEXT, complaint_filed TEXT, docket TEXT,
    attorney TEXT, attorney_phone TEXT, attorney_email TEXT, attorney_firm TEXT, attorney_address TEXT,
    pages_read INTEGER, ocr_ok INTEGER, stated_block TEXT, stated_lot TEXT
);
CREATE TABLE IF NOT EXISTS lp_scan (county_id INTEGER PRIMARY KEY, checked_through TEXT);
-- Every lis pendens in a county's index since lis_pendens_since, read or not. Documents are read
-- from this queue most-promising first (owner is a company, the index names a commercial lot).
CREATE TABLE IF NOT EXISTS lp_index (
    county_id INTEGER, instrument TEXT, doc_type TEXT, recorded TEXT, defendants TEXT, plaintiffs TEXT,
    town TEXT, block TEXT, lot TEXT, street TEXT, extra TEXT, priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued',      -- queued | read | skipped | discharged
    status_note TEXT, discharged_on TEXT, seen TEXT,
    PRIMARY KEY (county_id, instrument)
);
CREATE TABLE IF NOT EXISTS lp_backfill (county_id INTEGER PRIMARY KEY, back_through TEXT);
CREATE TABLE IF NOT EXISTS lp_discharge (county_id INTEGER, instrument TEXT, recorded TEXT, defendants TEXT,
    plaintiffs TEXT, PRIMARY KEY (county_id, instrument));
"""

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = rf"(?:(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})"
_STREET_SUFFIX = (r"(?:Avenue|Ave|Street|St|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Place|Pl|Court|Ct|Parkway|Pkwy|"
                  r"Highway|Hwy|Route|Rte|Turnpike|Tpke|Pike|Terrace|Ter|Way|Plaza|Circle|Square|Sq)")
_ADDR_LINE = re.compile(rf"^\s*(\d{{1,6}}[A-Z]?(?:\s*[-&]\s*\d{{1,6}}[A-Z]?)?\s+[A-Za-z0-9 .'\-]{{2,40}}?\b{_STREET_SUFFIX}\b\.?(?:\s+(?:North|South|East|West|N|S|E|W))?)\s*,?\s*(.*)$", re.I | re.M)
_CITY_LINE = re.compile(r"^\s*([A-Za-z .'\-]{3,40}),?\s+(?:NJ|N\.J\.|New Jersey)\.?\s+(0\d{4})", re.I | re.M)
_KNOWN_AS = re.compile(rf"(?:commonly\s+known\s+as|street\s+address\s*(?:is|of)?|premises\s+known\s+as|a/k/a)\s*:?\s*"
                       rf"(\d{{1,6}}[^\n,;]{{3,60}}?\b{_STREET_SUFFIX}\b\.?)\s*,?\s*([A-Za-z .'\-]{{3,40}})?,?\s*(?:NJ|New Jersey)?\s*(0\d{{4}})?",
                       re.I)
_MUNI = re.compile(r"\b(Township|Borough|City|Town|Village)\s+of\s+([A-Z][A-Za-z .'\-]{2,30}?)(?=[,.;\n]|\s+(?:in|County|Essex|Passaic|State)\b)", re.I)
_MORTGAGE = re.compile(rf"amount\s+of\s+\$\s?([0-9][0-9,]+(?:\.\d{{2}})?)\s*,?\s*dated\s+({_DATE})", re.I)
_MORTGAGE2 = re.compile(rf"dated\s+({_DATE})[^$]{{0,200}}?amount\s+of\s+\$\s?([0-9][0-9,]+(?:\.\d{{2}})?)", re.I | re.S)
_INST = re.compile(r"Instrument\s+(?:No\.?|Number|#)\s*:?\s*(\d{6,12})", re.I)
_BOOKPAGE = re.compile(r"(?:Mortgage\s+)?Book\s+(\d{2,6})\s*,?\s*(?:at\s+)?Page\s+(\d{1,5})", re.I)
_COMPLAINT = re.compile(rf"Complaint[^.]{{0,120}}?filed[^.]{{0,80}}?(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+day\s+of\s+({_MONTHS})[,]?\s+(\d{{4}})", re.I)
_COMPLAINT2 = re.compile(rf"Complaint[^.]{{0,120}}?filed[^.]{{0,80}}?(?:on\s+)?({_DATE})", re.I)
_DOCKET = re.compile(r"Docket\s+No\.?\s*:?\s*(F\s*-?\s*\d{3,6}\s*-?\s*\d{2})", re.I)
_PHONE = re.compile(r"\(?\b(\d{3})\)?[\s.\-]+(\d{3})[\s.\-]+(\d{4})\b")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
_FIRM = re.compile(r"^([A-Z][A-Za-z,.&' ]{3,60}?(?:LLP|LLC|L\.L\.C\.|P\.C\.|PC|P\.A\.|PA|Law Group|Law Firm|& Associates|Associates))\b", re.M)
_ESQ = re.compile(r"^\s*([A-Z][A-Za-z.'\- ]{3,40}?),?\s+Esq(?:uire|\.)?", re.M)


def _iso(d: str | None) -> str | None:
    if not d:
        return None
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(re.sub(r"\s+", " ", d.replace(".", "")).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_lis_pendens(text: str) -> dict:
    """Property, loan and attorney details from the OCR text of a foreclosure lis pendens."""
    t = text.replace("\r", "")
    flat = re.sub(r"\s+", " ", t)
    out: dict = {}

    # Property: prefer the Schedule A / exhibit address block, else "commonly known as".
    sched = re.search(r"(SCHEDULE\s+A|EXHIBIT\s+A|LEGAL\s+DESCRIPTION)", t, re.I)
    region = t[sched.start():] if sched else t
    for chunk in (region, t):
        m = _ADDR_LINE.search(chunk)
        if m:
            out["street"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
            rest = m.group(2) or ""
            c = _CITY_LINE.search(rest) or _CITY_LINE.search(chunk[m.end():m.end() + 120])
            if c:
                out["city"], out["zip"] = c.group(1).strip().title(), c.group(2)
            break
    if "street" not in out:
        m = _KNOWN_AS.search(flat)
        if m:
            out["street"] = m.group(1).strip()
            if m.group(2):
                out["city"] = m.group(2).strip().title()
            if m.group(3):
                out["zip"] = m.group(3)
    block, lot, qual = parse_block_lot(region[:1500], t)
    out.update({"block": block, "lot": lot, "qual": qual})
    m = _MUNI.search(region) or _MUNI.search(t)
    if m:
        out["municipality"] = f"{m.group(1).title()} of {m.group(2).strip().title()}"
        out["muni_name"] = m.group(2).strip()
    out["description"] = re.sub(r"\s+", " ", region[:900]).strip()

    m = _MORTGAGE.search(flat)
    if m:
        out["mortgage_amount"], out["mortgage_date"] = float(m.group(1).replace(",", "")), _iso(m.group(2))
    else:
        m = _MORTGAGE2.search(flat)
        if m:
            out["mortgage_date"], out["mortgage_amount"] = _iso(m.group(1)), float(m.group(2).replace(",", ""))
    m = _INST.search(flat)
    if m:
        out["mortgage_ref"] = f"Instrument {m.group(1)}"
    else:
        m = _BOOKPAGE.search(flat)
        if m:
            out["mortgage_ref"] = f"Book {m.group(1)} Page {m.group(2)}"
    m = _COMPLAINT.search(flat)
    if m:
        out["complaint_filed"] = _iso(f"{m.group(2)} {m.group(1)}, {m.group(3)}")
    else:
        m = _COMPLAINT2.search(flat)
        if m:
            out["complaint_filed"] = _iso(m.group(1))
    m = _DOCKET.search(flat)
    if m:
        out["docket"] = re.sub(r"\s+", "", m.group(1)).upper()

    # Lender's attorney: the caption block at the top of the notice.
    head = t[:1800]
    m = _FIRM.search(head)
    if m:
        out["attorney_firm"] = m.group(1).strip(" ,")
    m = _ESQ.search(head)
    if m:
        out["attorney"] = m.group(1).strip()
    m = _PHONE.search(head)
    if m:
        out["attorney_phone"] = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    m = _EMAIL.search(head)
    if m:
        out["attorney_email"] = m.group(0).strip(".,;")
    a = re.search(r"^\s*(\d{1,6}\s+[^\n]{3,50})\n(?:\s*Suite[^\n]*\n)?\s*([A-Za-z .'\-]{3,30},?\s+(?:NJ|New Jersey|NY|PA|New York|Pennsylvania)\s+\d{5})",
                  head, re.M)
    if a:
        out["attorney_address"] = f"{a.group(1).strip()}, {a.group(2).strip()}"
    return out


# ---------------------------------------------------------------------------- pipeline

def ensure_tables(db: DB) -> None:
    db.conn.executescript(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(lis_pendens)")}
    for col in ("stated_block", "stated_lot"):
        if col not in have:
            db.conn.execute(f"ALTER TABLE lis_pendens ADD COLUMN {col} TEXT")
    db.commit()


def _windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        out.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return out


def index_rows(client: PressClient, label_re: re.Pattern, start: date, end: date) -> list:
    """All index rows of a document type between two dates; windows are split so no single
    search hits the site's 100-row page."""
    rows = []
    stack = _windows(start, end, 7)
    while stack:
        a, b = stack.pop(0)
        got = client.search_doc_type(label_re, a, b)
        if got is None:
            continue
        if len(got) >= 100 and a < b:
            mid = a + (b - a) // 2
            stack[:0] = [(a, mid), (mid + timedelta(days=1), b)]
            continue
        rows += got
    return rows


def group_instruments(rows: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        bl = lambda v: None if (v or "").strip().upper() in ("", "N/A", "NA") else v.strip()
        g = out.setdefault(r.instrument, {"instrument": r.instrument, "doc_type": r.doc_type, "recorded": r.recorded_iso,
                                          "defendants": [], "plaintiffs": [], "town": r.town,
                                          "block": bl(r.block), "lot": bl(r.lot)})
        if r.direct and r.direct not in g["defendants"]:
            g["defendants"].append(r.direct)
        if r.indirect and r.indirect not in g["plaintiffs"]:
            g["plaintiffs"].append(r.indirect)
    return out


def _owner_first(defs: list[str]) -> list[str]:
    """Put the probable owner first: businesses before people, governments last."""
    from .parcels import looks_like_entity
    gov = re.compile(r"STATE OF|UNITED STATES|TOWNSHIP|CITY OF|BOROUGH|COUNTY OF|SECRETARY|INTERNAL REVENUE|TENANT|OCCUPANT", re.I)
    return sorted(defs, key=lambda d: (bool(gov.search(d)), not looks_like_entity(d)))


def process_instrument(cfg: dict, client, parcels: ParcelClient, cid: int, cname: str, g: dict,
                       geocoder=None, max_pages: int = 8) -> dict:
    """`client` is a county adapter (tracker.clerks) or, in older callers, a PressClient."""
    from .pipeline import classify, locate, _enrich
    rec = {"property_id": f"lp:{cid}:{g['instrument']}", "county_id": cid, "county": cname,
           "instrument": g["instrument"], "doc_type": g["doc_type"],
           "kind": "in_rem" if LP_IN_REM.search(g["doc_type"]) else "foreclosure",
           "recorded": g["recorded"], "status": "pending"}
    defs = _owner_first(g["defendants"])
    rec["defendant"] = "; ".join(defs)
    rec["plaintiff"] = "; ".join(g["plaintiffs"])
    rec["parties"] = json.dumps({"defendants": g["defendants"], "plaintiffs": g["plaintiffs"]})
    if hasattr(client, "pages"):
        pngs = client.pages(g, max_pages)
    else:                                   # a bare PressClient (tests, older callers)
        doc = client.open_document(g["instrument"])
        if not doc:
            from .enrich import save_debug
            save_debug(client, f"lp-{cname}-{g['instrument']}")
        pngs = [x for x in (client.page_image(doc[0], i) for i in range(min(doc[1], max_pages))) if x] if doc else []
    texts = [ocr_png(png) for png in pngs]
    rec["pages_read"] = len(texts)
    if not (LP_FORECLOSURE.search(g["doc_type"]) or LP_IN_REM.search(g["doc_type"])):
        joined = "\n".join(texts)
        if texts and not re.search(r"FORECLOS|MORTGAGE", joined, re.I):
            rec["kind"] = "other"          # a lis pendens over something else (a contract, a lien, a partition)
        elif re.search(r"\bIN REM\b|TAX SALE CERTIFICATE", joined, re.I):
            rec["kind"] = "in_rem"
    info = parse_lis_pendens("\n".join(texts)) if texts else {}
    rec["ocr_ok"] = int(bool(info.get("block") or info.get("street")))
    for k in ("street", "city", "zip", "block", "lot", "qual", "description", "mortgage_amount", "mortgage_date",
              "mortgage_ref", "complaint_filed", "docket", "attorney", "attorney_phone", "attorney_email",
              "attorney_firm", "attorney_address"):
        if info.get(k) is not None:
            rec[k] = info[k]
    for k in ("block", "lot", "street"):
        if not rec.get(k) and g.get(k):
            rec[k] = g[k].title() if k == "street" else g[k]
    city = info.get("muni_name") or info.get("city") or (g.get("town") or "").title()
    rec["city"] = rec.get("city") or city
    if rec.get("block") or rec.get("street"):
        # _enrich re-parses block/lot from parcel_text/description; give it the notice's values.
        rec["parcel_text"] = f"Block {rec['block']} Lot {rec['lot']}" if rec.get("block") and rec.get("lot") else None
        saved = {k: rec.get(k) for k in ("street", "city")}
        rec["city"] = city
        _enrich(rec, parcels, cname)
        rec.update({k: v for k, v in saved.items() if v})
        rec.pop("parcel_text", None)
        for k in ("parcel_pins", "lots", "net_value_total"):
            rec.pop(k, None)
    else:
        rec["match_method"] = "unmatched"
    rec["category"], rec["category_reason"] = classify(rec, cfg)
    if rec["kind"] == "other":
        rec["category"], rec["category_reason"] = "other", "Lis pendens that isn't a foreclosure"
    if rec["category"] in ("target", "land"):
        locate(rec, parcels, geocoder)
    return rec


def save(db: DB, rec: dict) -> None:
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(lis_pendens)")]
    keys = [k for k in rec if k in cols]
    db.conn.execute(f"INSERT OR REPLACE INTO lis_pendens ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [rec[k] for k in keys])


def link_to_deeds(db: DB) -> int:
    """A pending lis pendens whose lot later shows a sheriff's deed, a lender take-back or a sale in the
    state's deed records is resolved; it stays on the map's history but isn't an open pre-foreclosure."""
    n = 0
    try:
        rows = db.conn.execute(
            """SELECT lp.property_id, lp.recorded, d.deed_date, d.kind, d.price FROM lis_pendens lp
               JOIN sr1a_sales d ON d.pin = lp.pams_pin
               WHERE lp.status='pending' AND lp.pams_pin IS NOT NULL AND d.deed_date > lp.recorded
               ORDER BY d.deed_date""").fetchall()
    except Exception:  # noqa: BLE001 - no deed data yet
        return 0
    for r in rows:
        k = r["kind"] or ""
        status = "reo" if "Lender took" in k or "lieu" in k else "sheriff_sold" if "Sheriff" in k else \
            "sold" if "sale" in k.lower() and (r["price"] or 0) > 1000 else None
        if status:
            hit = db.conn.execute("UPDATE lis_pendens SET status=?, status_date=? WHERE property_id=? AND status='pending'",
                                  (status, r["deed_date"], r["property_id"])).rowcount
            if hit:
                label = {"reo": "Bank took title", "sheriff_sold": "Sold at sheriff sale", "sold": "Property sold"}[status]
                db.add_event(r["property_id"], datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                             "lp_closed", "pending", f"{label} ({r['deed_date']})")
            n += hit
    db.commit()
    return n


def link_to_sheriff_sales(db: DB) -> int:
    """Mark pre-foreclosures whose property is now on a sheriff's sale list."""
    n = 0
    rows = db.conn.execute(
        """SELECT lp.property_id, l.property_id AS sid, l.first_seen FROM lis_pendens lp
           JOIN listings l ON l.pams_pin = lp.pams_pin AND l.pams_pin IS NOT NULL
           WHERE lp.status = 'pending'""").fetchall()
    for r in rows:
        db.conn.execute("UPDATE lis_pendens SET status='sheriff_sale', status_date=?, sheriff_listing=? WHERE property_id=?",
                        ((r["first_seen"] or "")[:10], r["sid"], r["property_id"]))
        n += 1
    db.commit()
    return n


def apply_discharges(db: DB, rows: list, county_id: int | None = None) -> int:
    """A lis pendens discharge names the same parties: mark matching pre-foreclosures resolved.
    `rows` are index rows (PRESS) or already-grouped documents (other adapters)."""
    from .contacts import same_party
    n = 0
    pending = db.conn.execute("SELECT property_id, county_id, parties, recorded FROM lis_pendens WHERE status='pending'"
                              + (" AND county_id=?" if county_id is not None else ""),
                              (county_id,) if county_id is not None else ()).fetchall()
    groups = [r for r in rows if isinstance(r, dict)] or list(group_instruments(rows).values())
    for g in groups:
        for p in pending:
            parties = json.loads(p["parties"] or "{}")
            if (p["recorded"] or "") > (g["recorded"] or "9999"):
                continue
            if any(same_party(a, b) for a in g["defendants"] for b in parties.get("defendants", [])) and \
               any(same_party(a, b) for a in g["plaintiffs"] for b in parties.get("plaintiffs", [])):
                db.conn.execute("UPDATE lis_pendens SET status='discharged', status_date=? WHERE property_id=?",
                                (g["recorded"], p["property_id"]))
                db.add_event(p["property_id"], datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                             "lp_closed", "pending", f"Lis pendens discharged ({g['recorded']})")
                n += 1
    db.commit()
    return n


_GOV = re.compile(r"STATE OF|UNITED STATES|TOWNSHIP|CITY OF|BOROUGH|COUNTY OF|SECRETARY|INTERNAL REVENUE|TENANT|"
                  r"OCCUPANT|UNKNOWN|JOHN DOE|JANE DOE|SPOUSE|HEIRS|DEVISEES|MIDLAND|CAPITAL ONE|DISCOVER|PORTFOLIO RECOVERY", re.I)
RESIDENTIAL_CLASSES = {"2", "3A", "3B", "15A", "15B", "15C", "15D", "15E", "15F", "5A", "5B", "6A", "6B"}


def priority(g: dict) -> int:
    """Which documents to read first: an owner that's a company, a lot named in the index, a tax foreclosure."""
    from .parcels import looks_like_entity
    defs = [d for d in g.get("defendants") or [] if not _GOV.search(d)]
    p = 0
    if any(looks_like_entity(d) for d in defs):
        p += 4
    if g.get("block") and g.get("lot"):
        p += 1
    if LP_IN_REM.search(g.get("doc_type") or ""):
        p += 1
    return p


def _store_index(db: DB, cid: int, groups: list[dict], ts: str) -> int:
    n = 0
    for g in groups:
        extra = {k: g[k] for k in ("doc_id", "viewer") if g.get(k)}
        cur = db.conn.execute(
            """INSERT OR IGNORE INTO lp_index (county_id, instrument, doc_type, recorded, defendants, plaintiffs, town,
               block, lot, street, extra, priority, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, g["instrument"], g.get("doc_type"), g.get("recorded"), json.dumps(g.get("defendants") or []),
             json.dumps(g.get("plaintiffs") or []), g.get("town"), g.get("block"), g.get("lot"), g.get("street"),
             json.dumps(extra), priority(g), ts))
        n += cur.rowcount
    return n


def _index_group(r) -> dict:
    g = {k: r[k] for k in ("instrument", "doc_type", "recorded", "town", "block", "lot", "street")}
    g["defendants"], g["plaintiffs"] = json.loads(r["defendants"] or "[]"), json.loads(r["plaintiffs"] or "[]")
    g.update(json.loads(r["extra"] or "{}"))
    return g


def discharge_index(db: DB, cid: int, discharges: list[dict]) -> int:
    """Queued lis pendens that a later discharge names (same defendant and plaintiff) aren't worth reading."""
    from .contacts import same_party
    key = lambda name: re.sub(r"[^A-Z0-9]", "", (name or "").upper())[:6]
    queued: dict[str, list] = {}
    for r in db.conn.execute("SELECT instrument, recorded, defendants, plaintiffs FROM lp_index "
                             "WHERE county_id=? AND status='queued'", (cid,)):
        for d in json.loads(r["defendants"] or "[]"):
            queued.setdefault(key(d), []).append(r)
    n = 0
    for g in discharges:
        for d in g.get("defendants") or []:
            for r in queued.get(key(d), []):
                if (r["recorded"] or "") > (g.get("recorded") or "9999"):
                    continue
                if any(same_party(a, b) for a in g["defendants"] for b in json.loads(r["defendants"])) and \
                   (not g.get("plaintiffs") or any(same_party(a, b) for a in g["plaintiffs"] for b in json.loads(r["plaintiffs"]))):
                    n += db.conn.execute("UPDATE lp_index SET status='discharged', discharged_on=? WHERE county_id=? "
                                         "AND instrument=? AND status='queued'", (g.get("recorded"), cid, r["instrument"])).rowcount
    return n


def _residential_by_index(cfg, parcels, cname: str, g: dict) -> str | None:
    """When the index names the lot, check its tax class before reading the document: a house is skipped."""
    if not (g.get("block") and g.get("lot")) or not hasattr(parcels, "lookup"):
        return None
    try:
        m = parcels.lookup(cname, g.get("town") or "", g.get("street") or "", g["block"], g["lot"], None)
    except Exception:  # noqa: BLE001
        return None
    if m and (m.prop_class or "").upper() in RESIDENTIAL_CLASSES:
        return f"Tax class {m.prop_class} ({m.class_label}) at Block {m.block} Lot {m.lot}"
    return None


def run_lis_pendens(cfg: dict, db: DB, browser, parcels: ParcelClient, geocoder=None, today: date | None = None,
                    adapters: list | None = None) -> dict:
    """For each connected county clerk: index new lis pendens (and, a few months per run, older ones
    back to `lis_pendens_since`), drop the ones already discharged, then read the most promising
    unread documents."""
    from . import clerks
    ensure_tables(db)
    today = today or datetime.now(timezone.utc).date()
    since = date.fromisoformat(str(cfg.get("lis_pendens_since") or
                                   (today - timedelta(days=int(cfg.get("lis_pendens_lookback_days", 270)))).isoformat()))
    back_days = int(cfg.get("lis_pendens_backfill_days_per_run", 180))
    cap = int(cfg.get("max_lis_pendens_per_county", cfg.get("max_lis_pendens_per_run", 40)))
    overlap = 30                                    # clerks index documents days to weeks after recording (Passaic lags)
    stats = {"found": 0, "indexed": 0, "processed": 0, "tracked": 0, "discharged": 0, "linked": 0, "errors": {},
             "capped": False, "by_county": {}}
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if adapters is None:
        want = cfg.get("lis_pendens_counties")
        adapters = []
        for cid, base in PRESS_SITES.items():
            if not want or cid in want:
                try:
                    adapters.append(clerks.PressAdapter(cid, PRESS_COUNTY_NAMES.get(cid, str(cid)), base, browser,
                                                        client_factory=PressClient))
                except Exception as e:  # noqa: BLE001
                    stats["errors"][PRESS_COUNTY_NAMES.get(cid, str(cid))] = str(e)[:200]
        adapters += clerks.build_adapters(cfg, browser, include_press=False)
    for ad in adapters:
        cid, cname = ad.county_id, ad.county
        cs = {"system": ad.system, "new": 0, "processed": 0, "tracked": 0, "skipped": 0}
        stats["by_county"][cname] = cs
        row = db.conn.execute("SELECT checked_through FROM lp_scan WHERE county_id=?", (cid,)).fetchone()
        baseline = row is None
        fwd_start = (date.fromisoformat(row[0]) - timedelta(days=overlap)) if row else max(since, today - timedelta(days=back_days))
        back = db.conn.execute("SELECT back_through FROM lp_backfill WHERE county_id=?", (cid,)).fetchone()
        back_to = date.fromisoformat(back[0]) if back else fwd_start
        windows = [(fwd_start, today)]
        if back_to > since:
            windows.append((max(since, back_to - timedelta(days=back_days)), back_to - timedelta(days=1)))
        try:
            for a, b in windows:
                groups = ad.lp_groups(a, b, LP_FORECLOSURE, LP_PLAIN, LP_IN_REM)
                stats["found"] += len(groups)
                cs["new"] += _store_index(db, cid, groups, ts)
                dis = ad.discharge_groups(a, b, LP_DISCHARGE)
                for d in dis:
                    db.conn.execute("INSERT OR IGNORE INTO lp_discharge VALUES (?,?,?,?,?)",
                                    (cid, d.get("instrument"), d.get("recorded"), json.dumps(d.get("defendants") or []),
                                     json.dumps(d.get("plaintiffs") or [])))
                stats["discharged"] += apply_discharges(db, dis, county_id=cid)
                db.commit()
            # The index is read backwards in time, so a discharge can be seen before its lis pendens.
            alld = [{"recorded": r[0], "defendants": json.loads(r[1]), "plaintiffs": json.loads(r[2])} for r in
                    db.conn.execute("SELECT recorded, defendants, plaintiffs FROM lp_discharge WHERE county_id=?", (cid,))]
            stats["discharged"] += discharge_index(db, cid, alld)
            db.conn.execute("INSERT OR REPLACE INTO lp_scan (county_id, checked_through) VALUES (?, ?)", (cid, today.isoformat()))
            if len(windows) > 1:
                db.conn.execute("INSERT OR REPLACE INTO lp_backfill (county_id, back_through) VALUES (?, ?)",
                                (cid, windows[1][0].isoformat()))
            elif not back:
                db.conn.execute("INSERT OR REPLACE INTO lp_backfill (county_id, back_through) VALUES (?, ?)",
                                (cid, fwd_start.isoformat()))
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("%s: lis pendens search failed", cname)
            stats["errors"][cname] = str(e)[:200]
        stats["indexed"] += cs["new"]
        known = {r[0] for r in db.conn.execute("SELECT instrument FROM lis_pendens WHERE county_id=?", (cid,))}
        # Open filings first; then ones already closed out (discharged), so a lot's history shows them too.
        queue = db.conn.execute("SELECT * FROM lp_index WHERE county_id=? AND status='queued' "
                                "ORDER BY priority DESC, recorded DESC", (cid,)).fetchall() + \
            db.conn.execute("SELECT * FROM lp_index WHERE county_id=? AND status='discharged' AND priority >= 4 "
                            "ORDER BY recorded DESC", (cid,)).fetchall()
        # Hudson's index already has the lot, so nothing is opened or read: many more fit in a run.
        county_cap = cap * 6 if getattr(ad, "system", "") == "Acclaim" else cap
        for r in queue:
            if cs["processed"] >= county_cap:
                stats["capped"] = True
                break
            g = _index_group(r)
            if g["instrument"] in known:
                db.conn.execute("UPDATE lp_index SET status='read' WHERE county_id=? AND instrument=?", (cid, g["instrument"]))
                continue
            why = _residential_by_index(cfg, parcels, cname, g)
            if why:
                db.conn.execute("UPDATE lp_index SET status='skipped', status_note=? WHERE county_id=? AND instrument=?",
                                (why, cid, g["instrument"]))
                cs["skipped"] += 1
                continue
            try:
                rec = process_instrument(cfg, ad, parcels, cid, cname, g, geocoder)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: lis pendens %s failed", cname, g["instrument"])
                stats["errors"][f"{cname} {g['instrument']}"] = str(e)[:200]
                continue
            # Older filings were history before tracking began: on the map, but not new alerts.
            old = (g.get("recorded") or "") < (today - timedelta(days=30)).isoformat()
            rec.update({"first_seen": ts, "baseline": int(baseline or old)})
            if r["status"] == "discharged":
                rec.update({"status": "discharged", "status_date": r["discharged_on"], "baseline": 1})
            save(db, rec)
            db.conn.execute("UPDATE lp_index SET status='read' WHERE county_id=? AND instrument=?", (cid, g["instrument"]))
            stats["processed"] += 1
            cs["processed"] += 1
            if rec["category"] in ("target", "land"):
                stats["tracked"] += 1
                cs["tracked"] += 1
                if rec.get("status") == "pending":
                    db.add_event(rec["property_id"], ts, "lis_pendens", None, rec.get("plaintiff"), baseline=bool(baseline or old))
            db.commit()
        cs["queued"] = db.conn.execute("SELECT COUNT(*) FROM lp_index WHERE county_id=? AND status='queued'", (cid,)).fetchone()[0]
        cs["indexed_back_to"] = (db.conn.execute("SELECT back_through FROM lp_backfill WHERE county_id=?", (cid,)).fetchone() or [None])[0]
        ad.close()
    stats["outcomes"] = link_to_deeds(db)
    stats["linked"] = link_to_sheriff_sales(db)
    return stats
