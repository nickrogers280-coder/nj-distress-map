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
F = "ctl00$ContentPlaceHolder1$"


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
  for (const tr of document.querySelectorAll('tr')) {
    if (tr.querySelector('table')) continue;
    const btn = tr.querySelector('input[type=image]');
    if (!btn) continue;
    const c = [...tr.children].map(td => td.textContent.trim());
    if (c.length < 8) continue;
    out.push({c, view: btn.name});
  }
  return out;
}"""

_SET_AND_SEARCH_JS = """([fields, button]) => {
  for (const [name, value] of Object.entries(fields)) {
    const el = document.querySelector(`[name="${name}"]`);
    if (el) el.value = value;
  }
  document.querySelector(`[name="${button}"]`).click();
}"""


class PressClient:
    def __init__(self, base: str, page, delay: float = 1.0):
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

    def search_instrument(self, instrument: str) -> list[IndexRow]:
        return self._search({F + "txtInstrumentTab3": instrument}, F + "btnSearchTab3")

    # ------------------------------------------------------------------ images
    def open_document(self, instrument: str) -> tuple[str, int] | None:
        """Open an instrument in the image viewer. Returns (tile URL template, page count)."""
        rows = self.search_instrument(instrument)
        if not rows:
            return None
        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            self.page.click(f'input[name="{rows[0].view}"]')
        nav = self._wait_frame(lambda f: "NavigateRecords" in f.url)
        if nav is None:
            return None
        nav.click('input[name="btnImage"]')
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
        log.warning("Viewer never loaded for instrument %s", instrument)
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


def ocr_png(png: bytes) -> str:
    import subprocess
    p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "4"], input=png, capture_output=True, timeout=120)
    return p.stdout.decode("utf-8", "replace")
