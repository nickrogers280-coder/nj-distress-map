"""Hudson (Acclaim): accept the disclaimer, find the lis pendens document types, run a date search,
and log the requests the page makes (run in Actions)."""
import re, sys
from datetime import date, timedelta
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA

base = "https://acclaim.hcnj.us/AcclaimWeb/"
end = date.today(); start = end - timedelta(days=14)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []
    def on_resp(r):
        if r.request.resource_type in ("xhr", "fetch", "document"):
            body = ""
            try:
                ct = r.headers.get("content-type", "")
                body = r.text()[:700] if ("json" in ct or "text" in ct) and "html" not in ct else f"<{ct}>"
            except Exception:
                pass
            log.append(f"{r.request.method} {r.status} {r.url[:220]} | POST {(r.request.post_data or '')[:500]} | {body.replace(chr(10),' ')}")
    pg.on("response", on_resp)
    pg.goto(base, wait_until="networkidle", timeout=60000)
    pg.click("text=I accept the conditions", timeout=15000)
    pg.wait_for_load_state("networkidle")
    print("AFTER ACCEPT", pg.url, re.sub(r"\s+", " ", pg.inner_text("body"))[:400])
    links = pg.eval_on_selector_all("a", "els => els.map(e => e.innerText.trim() + ' -> ' + e.href).filter(s => /search|type/i.test(s)).slice(0, 20)")
    print("LINKS", links)
    pg.goto(base + "search/SearchTypeDocType", wait_until="networkidle", timeout=60000)
    print("DOCTYPE PAGE", pg.url, re.sub(r"\s+", " ", pg.inner_text("body"))[:800])
    print("FIELDS", pg.eval_on_selector_all("input,select,button", "els => els.filter(e => e.type !== 'hidden').map(e => (e.name||e.id) + ':' + e.type).slice(0, 40)"))
    boxes = pg.query_selector_all("input[name=DocTypeInfoCheckBox]")
    picked = []
    for bx in boxes:
        lab = pg.evaluate("(e) => (e.closest('tr,li,div,label')||e.parentElement).innerText", bx)
        if re.search(r"LIS PENDENS", lab or "", re.I):
            pg.evaluate("(e) => { if (!e.checked) e.click(); }", bx); picked.append((bx.get_attribute("value"), lab.strip()[:60]))
    print("PICKED", picked)
    print("DOCTYPES FIELD", pg.evaluate("() => [document.querySelector('[name=DocTypesDisplay]')?.value, document.querySelector('#DocTypes, [name=DocTypes]')?.value]"))
    for n, v in (("RecordDateFrom", start.strftime("%m/%d/%Y")), ("RecordDateTo", end.strftime("%m/%d/%Y"))):
        pg.evaluate("([n, v]) => { const e = document.querySelector(`[name=${n}]`); e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); }", [n, v])
    log.clear()
    pg.click("#btnSearch, [name=btnSearch]")
    pg.wait_for_timeout(10000)
    print("RESULTS PAGE", pg.url, re.sub(r"\s+", " ", pg.inner_text("body"))[:1500])
    for l in log:
        print("  S", l[:1600])
    rows = pg.query_selector_all("tr.k-master-row, .k-grid-content tr")
    print("ROWS", len(rows))
    if rows:
        log.clear()
        rows[0].dblclick(); pg.wait_for_timeout(8000)
        for p2 in ctx.pages:
            print("PAGE", p2.url[:200])
        for l in log:
            print("  D", l[:900])
    html = pg.content()
    lp = re.findall(r'[^<>"]{0,60}LIS PEND[^<>"]{0,60}', html, re.I)[:10]
    print("LP MENTIONS", lp)
    for l in log[-20:]:
        print("  ", l[:1300])
    b.close()
