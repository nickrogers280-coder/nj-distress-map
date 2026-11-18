"""County clerk records on Sunrise "PRESS" (Public Records Electronic Search System).

Essex and Passaic run PRESS. It's an ASP.NET site with an Atalasoft image viewer, so it's
driven with a headless browser (Playwright): search the index, open an instrument, then
download whole page images straight from the viewer at full resolution for OCR.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

PRESS_SITES = {
    2: "https://press.essexregister.com/prodpress/clerk/",       # Essex
    17: "https://records.passaiccountynj.org/press/clerk/",      # Passaic
}
F = ""   # fields are matched by the end of their name: Essex prefixes them "ctl00$ContentPlaceHolder1$", Passaic "_ctl0:PageContent:"


@dataclass
class IndexRow:
    doc_type: str
    direct: str
    indirect: str
    instrument: str
    recorded: str          # M/D/YYYY as shown
    town: str
    block: str
    lot: str
    view: str              # name of the row's VIEW button (valid on the results page that produced it)

    @property
    def recorded_iso(self) -> str | None:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", self.recorded or "")
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


_ROWS_JS = """() => {
  const out = [];
  const isView = (e) => (e.type === 'image' && !/imgMaster|imgTab/i.test(e.name || '')) ||
                        /^\\s*view\\s*$/i.test(e.value || e.textContent || '');
  for (const tr of document.querySelectorAll('tr')) {
    if (tr.querySelector('table')) continue;
    const btn = [...tr.querySelectorAll('input,a,button')].find(isView);
    if (!btn) continue;
    const c = [...tr.children].map(td => td.textContent.trim());
    if (c.length < 8 || /^Type$/i.test(c[0])) continue;
    btn.setAttribute('data-njdm', String(out.length));
    out.push({c, view: String(out.length)});
  }
  return out;
}"""

_SET_AND_SEARCH_JS = """([fields, button]) => {
  const by = (suffix) => document.querySelector(`[name$="${suffix}"]`);
  for (const [name, value] of Object.entries(fields)) {
    const el = by(name);
    if (el) el.value = value;
  }
  const b = by(button);
  if (!b) throw new Error('search button not found: ' + button);
  b.click();
}"""


class PressClient:
    def __init__(self, base: str, page, delay: float = 1.0):
        self.last_error: str | None = None
        self.debug_png: bytes | None = None
        self.base = base
        self.page = page          # a Playwright Page
        self.delay = delay

    # ------------------------------------------------------------------ index
    def _search(self, fields: dict, button: str) -> list[IndexRow]:
        self.page.goto(self.base + "ClerkHome.aspx?op=basic", wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.delay)
        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            self.page.evaluate(_SET_AND_SEARCH_JS, [fields, button])
        rows = []
        for r in self.page.evaluate(_ROWS_JS):
            c = r["c"] + [""] * 10
            rows.append(IndexRow(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], r["view"]))
        return rows

    def search_name(self, name: str) -> list[IndexRow]:
        return self._search({F + "txtLastNameTab1": name, F + "ddlShowRecTab1": "100",
                             F + "ddlTotalRecTab1": "500"}, F + "btnSearchTab1")

    def search_doc_type(self, label_re: "re.Pattern", start, end) -> list[IndexRow] | None:
        """Index rows for every document type whose label matches, recorded between two dates."""
        self.page.goto(self.base + "ClerkHome.aspx?op=basic", wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.delay)
        opts = self.page.evaluate("""(n) => [...(document.querySelector(`[name$="${n}"]`)?.options || [])]
                                     .map(o => [o.value, o.text.trim()])""", F + "ddlDocTypeTab2")
        values = [v for v, label in opts if v and label_re.search(label)]
        if not values:
            log.warning("No document type matches %s at %s", label_re.pattern, self.base)
            return None
        rows: list[IndexRow] = []
        for v in values:
            rows += self._search({F + "ddlDocTypeTab2": v, F + "txtFromTab2": start.strftime("%m/%d/%Y"),
                                  F + "txtToTab2": end.strftime("%m/%d/%Y"), F + "ddlShowRecTab2": "100",
                                  F + "ddlTotalRecTab2": "750"}, F + "btnSearchTab2")
        return rows

    def search_block_lot(self, muni: str, block: str, lot: str | None) -> list[IndexRow] | None:
        """Everything indexed against a block (and lot) in one town: the parcel's full record history."""
        self.page.goto(self.base + "ClerkHome.aspx?op=basic", wait_until="domcontentloaded", timeout=60000)
        time.sleep(self.delay)
        opts = self.page.evaluate("""(n) => [...(document.querySelector(`[name$="${n}"]`)?.options || [])]
                                     .map(o => [o.value, o.text.trim()])""", "ddlMunTab4")
        value = match_municipality(muni, opts)
        if value is None:
            log.info("No municipality in the records site matches %r", muni)
            return None
        fields = {"ddlMunTab4": value, "txtBlockTab4": block, "ddlShowRecTab4": "100", "ddlTotalRecTab4": "500"}
        if lot:
            fields["txtLotTab4"] = lot
        return self._search(fields, "btnSearchTab4")

    def search_instrument(self, instrument: str) -> list[IndexRow]:
        return self._search({F + "txtInstrumentTab3": instrument}, F + "btnSearchTab3")

    # ------------------------------------------------------------------ images
    def _fail(self, stage: str, instrument: str) -> None:
        self.last_error = stage
        log.warning("Viewer failed at '%s' for instrument %s; frames: %s", stage, instrument,
                    [f.url.split("?")[0] for f in self.page.frames])
        try:
            self.debug_png = self.page.screenshot(full_page=True)
        except Exception:  # noqa: BLE001
            self.debug_png = None

    def open_document(self, instrument: str) -> tuple[str, int] | None:
        """Open an instrument in the image viewer. Returns (tile URL template, page count).
        On failure, `last_error` names the step and `debug_png` holds a screenshot."""
        self.last_error, self.debug_png = None, None
        rows = self.search_instrument(instrument)
        if not rows:
            self._fail("instrument search found nothing", instrument)
            return None
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
                self.page.evaluate("(i) => document.querySelector(`[data-njdm=\"${i}\"]`).click()", rows[0].view)
        except Exception as e:  # noqa: BLE001
            self._fail(f"opening the record ({e.__class__.__name__})", instrument)
            return None
        # Essex puts the "Get Image" button in a frame; Passaic ("Display Image") on the page itself.
        clicked, deadline = False, time.time() + 45
        while not clicked and time.time() < deadline:
            for fr in self.page.frames:
                try:
                    clicked = fr.evaluate("""() => { const b = document.querySelector('[name$="btnImage"]');
                                                     if (b) { b.click(); return true; } return false; }""")
                except Exception:  # noqa: BLE001 - frame still loading
                    clicked = False
                if clicked:
                    break
            if not clicked:
                time.sleep(1)
        if not clicked:
            self._fail("no Get/Display Image button on the record page", instrument)
            return None
        deadline = time.time() + 90
        while time.time() < deadline:
            for fr in self.page.frames:
                if "ViewerNew" not in fr.url:
                    continue
                try:
                    tile, count = fr.evaluate("""() => {
                        let t = null;
                        const scan = (w) => { try { for (const i of w.document.images) if (/atalagettile/.test(i.src)) t = t || i.src;
                                                    for (let k = 0; k < w.frames.length; k++) scan(w.frames[k]); } catch (e) {} };
                        scan(window);
                        let n = null; try { n = window.WebThumbnailViewer1.getCount(); } catch (e) {}
                        return [t, n];
                    }""")
                except Exception:  # noqa: BLE001 - frame navigated away mid-call
                    continue
                if tile:
                    return tile, int(count or 1)
            time.sleep(1.5)
        self._fail("image viewer never loaded", instrument)
        return None

    def page_image(self, tile_url: str, index: int) -> bytes | None:
        """Full-resolution PNG of one page (0-based) of the open document."""
        parts = urlsplit(tile_url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q.update({"atala_z": "1", "atala_tx": "0", "atala_ty": "0", "atala_tw": "5000", "atala_th": "7000",
                  "atala_fi": str(index), "atala_rnd": str(int(time.time() * 1000) % 10**8)})
        url = urlunsplit(parts._replace(query=urlencode(q)))
        try:
            r = self.page.request.get(url, timeout=60000)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                return r.body()
        except Exception as e:  # noqa: BLE001
            log.warning("Page %d image failed: %s", index, e)
        return None

    def _wait_frame(self, pred, timeout: float = 45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for fr in self.page.frames:
                try:
                    if pred(fr):
                        fr.wait_for_load_state("domcontentloaded", timeout=30000)
                        return fr
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1)
        return None


_MUNI_WORDS = re.compile(r"\b(TWP|TOWNSHIP|BORO|BOROUGH|CITY|TOWN|VILLAGE|OF|THE)\b\.?", re.I)


def match_municipality(muni: str | None, options: list[list[str]]) -> str | None:
    """'Fairfield Twp' -> the dropdown value labelled 'FAIRFIELD'; 'Orange City' won't pick 'WEST ORANGE'."""
    core = lambda s: re.sub(r"\s+", " ", _MUNI_WORDS.sub(" ", re.sub(r"[^A-Za-z ]", " ", s or ""))).strip().upper()
    want = core(muni)
    if not want:
        return None
    cands = [(v, core(label)) for v, label in options if v and not re.search(r"select", label, re.I)]
    for v, c in cands:
        if c == want:
            return v
    for v, c in cands:
        if c.startswith(want) or want.startswith(c):
            return v
    return None


def ocr_png(png: bytes) -> str:
    import subprocess
    p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "4"], input=png, capture_output=True, timeout=120)
    return p.stdout.decode("utf-8", "replace")
