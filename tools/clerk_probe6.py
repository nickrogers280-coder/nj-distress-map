"""How NewVision serves a document image, and what Union's Viewer returns (run in Actions)."""
import json, re, sys
from datetime import date, timedelta
import requests
sys.path.insert(0, ".")
from tracker.press import BROWSER_UA

base = "https://records.mercercounty.org/publicsearch/"
s = requests.Session(); s.headers["User-Agent"] = BROWSER_UA
s.get(base, timeout=60)
for sc in ["Scripts/app/services.js", "Scripts/app/documentcontroller.js"]:
    js = s.get(base + sc, timeout=60).text
    for m in re.finditer(r"Encrypt\s*[:=]\s*function|function\s+Encrypt|api/document\"", js):
        print(sc, "CTX", re.sub(r"\s+", " ", js[max(0, m.start() - 100):m.end() + 900]))
end = date.today(); start = end - timedelta(days=30)
rows = s.post(base + "api/search", json={"MaxRows": 0, "RowsPerPage": 0, "StartRow": 0, "DocTypes": "NLP",
              "FromDate": start.strftime("%Y%m%d"), "ToDate": end.strftime("%Y%m%d")}, timeout=90).json()
d = rows[0]["doc_id"]
for body in [{"Convert": True, "ID": " " + str(d)}, {"Convert": True, "ID": " " + str(d), "Page": "1"}, {"ID": str(d)}]:
    r = s.post(base + "api/document", json=body, timeout=90)
    ct = r.headers.get("content-type", "")
    print("POSTDOC", body, r.status_code, ct, len(r.content), r.text[:600].replace("\n", " ") if "json" in ct or "text" in ct else "")

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []
    pg.on("response", lambda r: log.append(f"{r.request.method} {r.status} {r.headers.get('content-type','')} {r.url[:200]}"))
    pg.goto("https://clerk.ucnj.org/UCPA/DocIndex?s=date", wait_until="networkidle", timeout=60000)
    pg.select_option("select[name=dt]", value="7")
    pg.fill("input[name=from]", start.strftime("%m/%d/%Y")); pg.fill("input[name=to]", end.strftime("%m/%d/%Y")); pg.fill("input[name=rpp]", "50")
    with pg.expect_navigation(timeout=60000):
        pg.click("input[type=submit]")
    print("Union FORECLOSURE TEXT", re.sub(r"\s+", " ", pg.inner_text("body"))[300:1300])
    v = pg.eval_on_selector_all("a[href*='Viewer']", "els => els.map(e => e.href)")[:1]
    nd = pg.eval_on_selector_all("a[href*='NameDetails']", "els => els.map(e => e.href)")[:1]
    if nd:
        pg.goto(nd[0], wait_until="networkidle"); print("Union DETAILS", re.sub(r"\s+", " ", pg.inner_text("body"))[300:1300])
    if v:
        log.clear()
        pg.goto(v[0], wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
        print("Union VIEWER", pg.url[:120], re.sub(r"\s+", " ", pg.inner_text("body"))[:500])
        print("Union VIEWER frames", [f.url[:150] for f in pg.frames])
        print("Union VIEWER imgs", pg.eval_on_selector_all("img,embed,iframe,object", "els => els.map(e => e.tagName + ' ' + (e.src||e.data||'').slice(0,150))"))
        for l in log[-15:]:
            print("  ", l)
    b.close()
