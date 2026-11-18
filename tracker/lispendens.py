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
from .press import PRESS_SITES, PressClient, ocr_png

log = logging.getLogger(__name__)

LP_FORECLOSURE = re.compile(r"LIS PENDENS.*FORECLOS", re.I)
LP_IN_REM = re.compile(r"LIS PENDENS.*IN REM", re.I)        # municipal tax-lien foreclosure
LP_DISCHARGE = re.compile(r"LIS PENDENS.*DISCHARGE", re.I)

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
    pages_read INTEGER, ocr_ok INTEGER
);
CREATE TABLE IF NOT EXISTS lp_scan (county_id INTEGER PRIMARY KEY, checked_through TEXT);
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
        g = out.setdefault(r.instrument, {"instrument": r.instrument, "doc_type": r.doc_type, "recorded": r.recorded_iso,
                                          "defendants": [], "plaintiffs": [], "town": r.town})
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


def process_instrument(cfg: dict, client: PressClient, parcels: ParcelClient, cid: int, cname: str, g: dict,
                       geocoder=None, max_pages: int = 8) -> dict:
    from .pipeline import classify, locate, _enrich
    rec = {"property_id": f"lp:{cid}:{g['instrument']}", "county_id": cid, "county": cname,
           "instrument": g["instrument"], "doc_type": g["doc_type"],
           "kind": "in_rem" if LP_IN_REM.search(g["doc_type"]) else "foreclosure",
           "recorded": g["recorded"], "status": "pending"}
    defs = _owner_first(g["defendants"])
    rec["defendant"] = "; ".join(defs)
    rec["plaintiff"] = "; ".join(g["plaintiffs"])
    rec["parties"] = json.dumps({"defendants": g["defendants"], "plaintiffs": g["plaintiffs"]})
    doc = client.open_document(g["instrument"])
    if not doc:
        from .enrich import save_debug
        save_debug(client, f"lp-{cname}-{g['instrument']}")
    texts = []
    if doc:
        tile, n = doc
        for i in range(min(n, max_pages)):
            png = client.page_image(tile, i)
            if png:
                texts.append(ocr_png(png))
    rec["pages_read"] = len(texts)
    info = parse_lis_pendens("\n".join(texts)) if texts else {}
    rec["ocr_ok"] = int(bool(info.get("block") or info.get("street")))
    for k in ("street", "city", "zip", "block", "lot", "qual", "description", "mortgage_amount", "mortgage_date",
              "mortgage_ref", "complaint_filed", "docket", "attorney", "attorney_phone", "attorney_email",
              "attorney_firm", "attorney_address"):
        if info.get(k) is not None:
            rec[k] = info[k]
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
    if rec["category"] in ("target", "land"):
        locate(rec, parcels, geocoder)
    return rec


def save(db: DB, rec: dict) -> None:
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(lis_pendens)")]
    keys = [k for k in rec if k in cols]
    db.conn.execute(f"INSERT OR REPLACE INTO lis_pendens ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [rec[k] for k in keys])


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


def apply_discharges(db: DB, rows: list) -> int:
    """A lis pendens discharge names the same parties: mark matching pre-foreclosures resolved."""
    from .contacts import same_party
    n = 0
    pending = db.conn.execute("SELECT property_id, county_id, parties, recorded FROM lis_pendens WHERE status='pending'").fetchall()
    for g in group_instruments(rows).values():
        for p in pending:
            parties = json.loads(p["parties"] or "{}")
            if (p["recorded"] or "") > (g["recorded"] or "9999"):
                continue
            if any(same_party(a, b) for a in g["defendants"] for b in parties.get("defendants", [])) and \
               any(same_party(a, b) for a in g["plaintiffs"] for b in parties.get("plaintiffs", [])):
                db.conn.execute("UPDATE lis_pendens SET status='discharged', status_date=? WHERE property_id=?",
                                (g["recorded"], p["property_id"]))
                n += 1
    db.commit()
    return n


def run_lis_pendens(cfg: dict, db: DB, browser, parcels: ParcelClient, geocoder=None, today: date | None = None) -> dict:
    """Search each PRESS county for new foreclosure lis pendens and process them."""
    ensure_tables(db)
    today = today or datetime.now(timezone.utc).date()
    lookback = int(cfg.get("lis_pendens_lookback_days", 270))
    cap = int(cfg.get("max_lis_pendens_per_run", 80))
    overlap = 5                                     # clerks index documents a few days after recording
    stats = {"found": 0, "processed": 0, "tracked": 0, "discharged": 0, "linked": 0, "errors": {}, "capped": False}
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for cid, base in PRESS_SITES.items():
        if cid not in (cfg.get("lis_pendens_counties") or list(PRESS_SITES)):
            continue
        cname = {2: "Essex", 17: "Passaic"}.get(cid, str(cid))
        row = db.conn.execute("SELECT checked_through FROM lp_scan WHERE county_id=?", (cid,)).fetchone()
        baseline = row is None
        start = (date.fromisoformat(row[0]) - timedelta(days=overlap)) if row else today - timedelta(days=lookback)
        try:
            ctx = browser.new_context(user_agent="Mozilla/5.0 (NJ-Distress-Tracker; public records research)")
            client = PressClient(base, ctx.new_page())
            rows = index_rows(client, LP_FORECLOSURE, start, today)
            rows += index_rows(client, LP_IN_REM, start, today)
            stats["discharged"] += apply_discharges(db, index_rows(client, LP_DISCHARGE, start, today))
        except Exception as e:  # noqa: BLE001
            log.exception("%s: lis pendens search failed", cname)
            stats["errors"][cname] = str(e)[:200]
            continue
        groups = group_instruments(rows)
        stats["found"] += len(groups)
        known = {r[0] for r in db.conn.execute("SELECT instrument FROM lis_pendens WHERE county_id=?", (cid,))}
        todo = [g for g in groups.values() if g["instrument"] not in known]
        todo.sort(key=lambda g: g["recorded"] or "", reverse=True)        # newest first
        done_all = True
        for g in todo:
            if stats["processed"] >= cap:
                stats["capped"], done_all = True, False
                break
            try:
                rec = process_instrument(cfg, client, parcels, cid, cname, g, geocoder)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: lis pendens %s failed", cname, g["instrument"])
                stats["errors"][f"{cname} {g['instrument']}"] = str(e)[:200]
                continue
            rec.update({"first_seen": ts, "baseline": int(baseline)})
            save(db, rec)
            stats["processed"] += 1
            if rec["category"] in ("target", "land"):
                stats["tracked"] += 1
                db.add_event(rec["property_id"], ts, "lis_pendens", None, rec.get("plaintiff"), baseline=baseline)
            db.commit()
        if done_all:
            db.conn.execute("INSERT OR REPLACE INTO lp_scan (county_id, checked_through) VALUES (?, ?)",
                            (cid, today.isoformat()))
            db.commit()
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    stats["linked"] = link_to_sheriff_sales(db)
    return stats
