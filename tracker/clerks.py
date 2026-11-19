"""County clerk record sites, behind one small interface so every county's lis pendens go through
the same pipeline.

Each adapter returns lis pendens as "groups" (one per recorded document):
    {"instrument", "doc_type", "recorded" (ISO), "defendants", "plaintiffs", "town",
     optional "block", "lot", "street" when the index itself carries them}
and can return the document's pages as PNG images for OCR.

Systems covered:
  Sunrise PRESS      Essex, Passaic, Burlington, Monmouth   (headless browser)
  NewVision NG API   Mercer, Salem                          (plain JSON; the sites that add a
                                                             robot check are left alone)
  Union UCPA         Union                                  (headless browser; PDFs)
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

LENDERISH = re.compile(r"\b(BANK|TRUST|MORTGAGE|MTG|SAVINGS|FUND|CAPITAL|LOAN|LENDING|FINANC|CREDIT|SERVICING|"
                       r"ASSOCIATION|ASSN|N\.?A\.?|FSB|INS(URANCE)? CO|SBA|HOLDINGS|PARTNERS|INVESTOR|REIT|LP\b)", re.I)


def _iso(v) -> str | None:
    if not v:
        return None
    s = str(v)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


def _windows(start: date, end: date, days: int):
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)


class Adapter:
    county_id: int
    county: str
    system: str

    def lp_groups(self, start: date, end: date, fore_re, plain_re, in_rem_re) -> list[dict]:
        raise NotImplementedError

    def discharge_groups(self, start: date, end: date, discharge_re) -> list[dict]:
        return []

    def pages(self, g: dict, max_pages: int = 8) -> list[bytes]:
        return []

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------- Sunrise PRESS
class PressAdapter(Adapter):
    system = "Sunrise PRESS"

    def __init__(self, county_id: int, county: str, base: str, browser, client_factory=None):
        from .press import BROWSER_UA, PressClient
        self.county_id, self.county = county_id, county
        self.ctx = browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
        self.client = (client_factory or PressClient)(base, self.ctx.new_page())
        self.last_error = None

    def _groups(self, label_re, start, end):
        from .lispendens import group_instruments, index_rows
        return list(group_instruments(index_rows(self.client, label_re, start, end)).values())

    def lp_groups(self, start, end, fore_re, plain_re, in_rem_re):
        types = self.client.doc_types()
        fore = fore_re if any(fore_re.search(t) for t in types) else plain_re
        return self._groups(fore, start, end) + self._groups(in_rem_re, start, end)

    def discharge_groups(self, start, end, discharge_re):
        return self._groups(discharge_re, start, end)

    def pages(self, g, max_pages=8):
        doc = self.client.open_document(g["instrument"])
        self.last_error = getattr(self.client, "last_error", None)
        if not doc:
            from .enrich import save_debug
            save_debug(self.client, f"lp-{self.county}-{g['instrument']}")
            return []
        tile, n = doc
        out = []
        for i in range(min(n, max_pages)):
            png = self.client.page_image(tile, i)
            if png:
                out.append(png)
        return out

    def close(self):
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- NewVision NG
class NewVisionAdapter(Adapter):
    """NewVision 'publicsearch' sites expose the JSON API their own page uses:
    POST api/search {DocTypes, FromDate, ToDate}  and  POST api/document {ID, Page} -> base64 PNG."""
    system = "NewVision public search"

    def __init__(self, county_id: int, county: str, base: str, session=None):
        import requests
        from .press import BROWSER_UA
        self.county_id, self.county, self.base = county_id, county, base.rstrip("/") + "/"
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = BROWSER_UA
        self._types = None
        self._towns = None
        self.last_error = None

    def _get(self, path):
        r = self.s.get(self.base + path, timeout=90)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=120)
        r.raise_for_status()
        return r.json()

    def doc_types(self) -> list[dict]:
        if self._types is None:
            self.s.get(self.base, timeout=60)
            flat = []
            for d in self._get("api/document/doctypes"):
                flat += d.get("children") or [d]
            seen, self._types = set(), []
            for d in flat:
                if d.get("value") and d["value"] not in seen:
                    seen.add(d["value"])
                    self._types.append(d)
        return self._types

    def town_name(self, code: str | None) -> str:
        """Index rows give a town code ('TRE') or name; turn a code into the municipality name."""
        code = (code or "").strip().upper()
        if self._towns is None:
            try:
                self._towns = [t.get("common_town_value") or "" for t in self._get("api/document/commonTowns")]
            except Exception:  # noqa: BLE001
                self._towns = []
        if len(code) <= 4:
            hits = [t for t in self._towns if t.upper().startswith(code)]
            if len(hits) == 1:
                return hits[0]
        return code

    def _codes(self, label_re, exclude=re.compile(r"DISCH|RELEAS|CANCEL|AMEND|RERECORD|PARTIAL", re.I)):
        return [d["value"] for d in self.doc_types() if label_re.search(d.get("name") or "") and not exclude.search(d["name"])]

    def _search(self, codes, start, end) -> list[dict]:
        rows = []
        for a, b in _windows(start, end, 31):
            got = self._post("api/search", {"MaxRows": 0, "RowsPerPage": 0, "StartRow": 0, "DocTypes": ",".join(codes),
                                            "FromDate": a.strftime("%Y%m%d"), "ToDate": b.strftime("%Y%m%d")})
            rows += got if isinstance(got, list) else []
        return rows

    def _group(self, rows) -> list[dict]:
        names = {d["value"]: d["name"] for d in self.doc_types()}
        out: dict[str, dict] = {}
        for r in rows:
            key = str(r.get("file_num") or r.get("doc_id"))
            g = out.setdefault(key, {"instrument": key, "doc_id": r.get("doc_id"),
                                     "doc_type": names.get(r.get("doc_type"), r.get("doc_type") or ""),
                                     "recorded": _iso(r.get("rec_date")), "defendants": [], "plaintiffs": [],
                                     "town": self.town_name(r.get("town")), "block": (r.get("block") or "").strip() or None,
                                     "lot": (r.get("lot") or "").strip() or None,
                                     "street": (r.get("legal_1") or "").strip() or None})
            for name in (r.get("party_name"), r.get("cross_party_name")):
                name = (name or "").strip()
                if not name or name in g["defendants"] or name in g["plaintiffs"]:
                    continue
                # Party codes aren't consistent across these sites; lenders are easy to recognise.
                (g["plaintiffs"] if LENDERISH.search(name) else g["defendants"]).append(name)
        # An index "party" that is really the street (Mercer writes the address as a name) isn't a defendant.
        for g in out.values():
            g["defendants"] = [d for d in g["defendants"] if not (g.get("street") and d.split()[0:1] == g["street"].split()[0:1]
                                                                    and re.match(r"\d", d))]
        return list(out.values())

    def lp_groups(self, start, end, fore_re, plain_re, in_rem_re):
        lp_like = re.compile(r"LIS ?PEND|NOTICE OF FORECLOSURE|FORECLOS", re.I)
        codes = self._codes(lp_like)
        return self._group(self._search(codes, start, end)) if codes else []

    def discharge_groups(self, start, end, discharge_re):
        codes = [d["value"] for d in self.doc_types() if re.search(r"(DISCH|RELEAS|CANCEL).*(LIS ?PEND|FORECLOS)",
                                                                    d.get("name") or "", re.I)]
        return self._group(self._search(codes, start, end)) if codes else []

    def pages(self, g, max_pages=8):
        out = []
        try:
            meta = self._post("api/document", {"ID": " " + str(g["doc_id"])})
            n = int(meta.get("doc_pages") or 1)
            for i in range(1, min(n, max_pages) + 1):
                d = self._post("api/document", {"Convert": True, "ID": " " + str(g["doc_id"]), "Page": " " + str(i)})
                img = d.get("hi_res") or d.get("image")
                if img:
                    out.append(base64.b64decode(img))
                time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"document image ({e.__class__.__name__})"
            log.warning("%s: document %s image failed: %s", self.county, g.get("instrument"), e)
        return out


# ---------------------------------------------------------------------------- Union UCPA
class UnionAdapter(Adapter):
    system = "Union County UCPA"
    base = "https://clerk.ucnj.org/UCPA/"

    def __init__(self, county_id: int, county: str, browser):
        from .press import BROWSER_UA
        self.county_id, self.county = county_id, county
        self.ctx = browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
        self.page = self.ctx.new_page()
        self.last_error = None

    def _search(self, label_re, start, end) -> list[dict]:
        out = []
        for a, b in _windows(start, end, 14):
            pg = self.page
            pg.goto(self.base + "DocIndex?s=date", wait_until="domcontentloaded", timeout=60000)
            opts = pg.eval_on_selector("select[name=dt]", "s => [...s.options].map(o => [o.value, o.text.trim()])")
            vals = [v for v, t in opts if label_re.search(t)]
            for v in vals:
                pg.goto(self.base + "DocIndex?s=date", wait_until="domcontentloaded", timeout=60000)
                pg.select_option("select[name=dt]", value=v)
                pg.fill("input[name=from]", a.strftime("%m/%d/%Y"))
                pg.fill("input[name=to]", b.strftime("%m/%d/%Y"))
                pg.fill("input[name=rpp]", "500")
                with pg.expect_navigation(timeout=90000):
                    pg.click("input[type=submit]")
                text = pg.inner_text("body")
                viewers = pg.eval_on_selector_all("a[href*='Viewer']", "els => els.map(e => e.href)")
                out += self.parse_results(text, viewers)
        return out

    _REC = re.compile(r"(Lis Pendens(?: Discharge)?|Foreclosure)\t(Defendant|Complainant)\t([^\t\n]+)\t(\d{1,2}/\d{1,2}/\d{4})"
                      r"[^\n]*\n(Defendant|Complainant)\t([^\t\n]+)\t(\d+/\d+)")

    @classmethod
    def parse_results(cls, text: str, viewers: list[str]) -> list[dict]:
        """Date-search results: each document is two lines,
           'Lis Pendens <tab> Defendant <tab> NAME <tab> 10/23/2026'  /  'Complainant <tab> NAME <tab> 62/535'."""
        from urllib.parse import unquote
        by_bp = {}
        for v in viewers:
            m = re.search(r"bp=([^&]+)", v)
            if m:
                by_bp.setdefault(unquote(m.group(1)), v)
        out = []
        for m in cls._REC.finditer(text):
            dt, t1, n1, rec, t2, n2, bp = m.groups()
            parties = {t1: n1.strip(), t2: n2.strip()}
            out.append({"instrument": bp, "doc_type": dt, "recorded": _iso(rec),
                        "defendants": [parties["Defendant"]] if parties.get("Defendant") else [],
                        "plaintiffs": [parties["Complainant"]] if parties.get("Complainant") else [],
                        "town": "", "viewer": by_bp.get(bp)})
        return out

    def lp_groups(self, start, end, fore_re, plain_re, in_rem_re):
        rows = self._search(re.compile(r"^\s*Lis Pendens\s*$", re.I), start, end)
        merged: dict[str, dict] = {}
        for g in rows:
            if re.search(r"DISCHARGE", g["doc_type"], re.I) or not g.get("viewer"):
                continue
            m = merged.setdefault(g["instrument"], {**g, "defendants": [], "plaintiffs": []})
            m["defendants"] += [d for d in g["defendants"] if d not in m["defendants"]]
            m["plaintiffs"] += [p for p in g["plaintiffs"] if p not in m["plaintiffs"]]
        return list(merged.values())

    def discharge_groups(self, start, end, discharge_re):
        rows = self._search(re.compile(r"^\s*Lis Pendens\s*$", re.I), start, end)
        return [g for g in rows if re.search(r"DISCHARGE", g["doc_type"], re.I)]

    def pages(self, g, max_pages=8):
        from .press import to_pngs
        try:
            self.page.goto(g["viewer"], wait_until="domcontentloaded", timeout=60000)
            src = self.page.eval_on_selector_all("iframe[src*='.pdf'], embed[src*='.pdf'], object[data*='.pdf']",
                                                 "els => els.map(e => e.src || e.data)")
            if not src:
                self.last_error = "no PDF on the viewer page"
                return []
            r = self.page.request.get(src[0], timeout=90000)
            return to_pngs(r.body(), r.headers.get("content-type", ""))[:max_pages]
        except Exception as e:  # noqa: BLE001
            self.last_error = f"viewer ({e.__class__.__name__})"
            log.warning("Union: document %s failed: %s", g.get("instrument"), e)
            return []

    def close(self):
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- Acclaim (Hudson)
HUDSON_MUNIS = {"0901": "Bayonne", "0902": "East Newark", "0903": "Guttenberg", "0904": "Harrison", "0905": "Hoboken",
                "0906": "Jersey City", "0907": "Kearny", "0908": "North Bergen", "0909": "Secaucus",
                "0910": "Union City", "0911": "Weehawken", "0912": "West New York"}


class AcclaimAdapter(Adapter):
    """Harris Acclaim (Hudson). The site shows a disclaimer that the owner has OK'd accepting each run.
    Its index already carries the block, lot, municipality and address, so documents aren't read."""
    system = "Acclaim"

    def __init__(self, county_id: int, county: str, base: str, munis: dict | None = None, session=None):
        import requests
        from .press import BROWSER_UA
        self.county_id, self.county, self.base = county_id, county, base.rstrip("/") + "/"
        self.munis = munis or {}
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = BROWSER_UA
        self._ready = False
        self._types = None
        self.last_error = None

    def _start(self):
        if not self._ready:
            self.s.get(self.base, timeout=60)
            self.s.post(self.base + "search/Disclaimer", data={"Disclaimer": "true"}, timeout=60)
            page = self.s.get(self.base + "search/SearchTypeDocType", timeout=60).text
            # The page lists every type as 'NAME (CODE)' with its ids after a '|', e.g. 'LIS PENDENS (LIS PENDENS),...|130,131'.
            self._types = []
            for m in re.finditer(r'value="(\d+)"[^>]*>\s*(?:</?[^>]+>\s*)*([A-Z][^<]{2,80}?\([^)]+\))', page):
                self._types.append((m.group(1), m.group(2).strip()))
            self._page = page
            self._ready = True

    def _ids(self, rx) -> str | None:
        self._start()
        ids = [i for i, name in self._types if rx.search(name)]
        if not ids:
            m = re.search(r"LIS PENDENS \(LIS PENDENS\),LIS PENDENS BY MUNICIPALITY \(LIS PENDENS MUN\)\|([0-9,]+)", self._page)
            if m and rx.search("LIS PENDENS"):
                return m.group(1)
        return ",".join(ids) or None

    def _search(self, ids: str, start: date, end: date) -> list[dict]:
        rows = []
        for a, b in _windows(start, end, 31):
            form = {"DocTypes": ids, "DocTypesDisplay-input": "", "DocTypesDisplay": "", "DateRangeList": " ",
                    "RecordDateFrom": f"{a.month}/{a.day}/{a.year}", "RecordDateTo": f"{b.month}/{b.day}/{b.year}",
                    "X-Requested-With": "XMLHttpRequest"}
            self.s.post(self.base + "search/SearchTypeDocType?Length=6", data=form,
                        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=120)
            page = 1
            while True:
                g = self.s.post(self.base + "Search/GridResults", data={"page": page, "pageSize": 500},
                                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=120).json()
                got = g.get("data") or g.get("Data") or []
                rows += got
                total = g.get("total") or g.get("Total") or len(got)
                if not got or page * 500 >= total:
                    break
                page += 1
        return rows

    def _group(self, rows) -> list[dict]:
        out: dict[str, dict] = {}
        for r in rows:
            inst = str(r.get("InstrumentNumber") or r.get("TransactionId"))
            legal = r.get("DocLegalDescription") or ""
            lot = re.search(r"Lot:\s*([^\s]+)", legal)
            blk = re.search(r"Blk:\s*([^\s]+)", legal)
            mun = re.search(r"Municipality:\s*(\d{4})", legal)
            addr = re.search(r"Address:\s*(.+)$", legal)
            town = self.munis.get(mun.group(1)) if mun else ""
            street = addr.group(1).strip() if addr else None
            if street and town and street.upper().endswith(town.upper()):
                street = street[: -len(town)].strip()
            ms = re.search(r"/Date\((\d+)\)/", str(r.get("RecordDate") or ""))
            rec = datetime.utcfromtimestamp(int(ms.group(1)) / 1000).date().isoformat() if ms else None
            g = out.setdefault(inst, {"instrument": inst, "doc_type": f"{r.get('DocTypeDescription') or ''} {r.get('Comments') or ''}".strip(),
                                      "recorded": rec, "defendants": [], "plaintiffs": [], "town": town,
                                      "block": blk.group(1) if blk else None, "lot": lot.group(1) if lot else None,
                                      "street": street, "district": mun.group(1) if mun else None})
            for k, side in (("DirectName", "defendants"), ("IndirectName", "plaintiffs")):
                n = (r.get(k) or "").strip()
                if n and n not in g[side]:
                    g[side].append(n)
        return list(out.values())

    def lp_groups(self, start, end, fore_re, plain_re, in_rem_re):
        ids = self._ids(re.compile(r"^LIS PENDENS\b(?!.*DISCH)", re.I))
        return self._group(self._search(ids, start, end)) if ids else []

    def discharge_groups(self, start, end, discharge_re):
        ids = self._ids(re.compile(r"DISCH.*LIS ?PEND|LIS ?PEND.*(DISCH|CANCEL|RELEASE)", re.I))
        return self._group(self._search(ids, start, end)) if ids else []


# ---------------------------------------------------------------------------- registry
NEWVISION_SITES = {
    1100: ("Mercer", "https://records.mercercounty.org/publicsearch/"),
    20: ("Salem", "https://clerkrecordsng.salemcountynj.gov/publicsearch/"),
}
UNION_ID = 15
HUDSON_ID = 10


def build_adapters(cfg: dict, browser, include_press: bool = True) -> list[Adapter]:
    from .press import PRESS_COUNTY_NAMES, PRESS_SITES
    want = cfg.get("lis_pendens_counties")
    out: list[Adapter] = []
    for cid, base in (PRESS_SITES.items() if include_press else []):
        if not want or cid in want:
            out.append(PressAdapter(cid, PRESS_COUNTY_NAMES.get(cid, str(cid)), base, browser))
    for cid, (name, base) in NEWVISION_SITES.items():
        if not want or cid in want:
            out.append(NewVisionAdapter(cid, name, base))
    if not want or UNION_ID in want:
        out.append(UnionAdapter(UNION_ID, "Union", browser))
    if not want or HUDSON_ID in want:
        out.append(AcclaimAdapter(HUDSON_ID, "Hudson", "https://acclaim.hcnj.us/AcclaimWeb/", HUDSON_MUNIS))
    return out
