"""Run a real lis pendens search on Mercer/Salem (NewVision API), Union and Gloucester and
log request bodies, responses and the document-image requests (run in Actions)."""
import json, re, sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA

OUT = Path("survey"); OUT.mkdir(exist_ok=True)
end = date.today(); start = end - timedelta(days=21)
fmt = lambda d: d.strftime("%m/%d/%Y")


def watch(pg, log, keep_body=True):
    def on_resp(r):
        req = r.request
        if req.resource_type in ("xhr", "fetch", "document", "image") and not re.search(r"\.(png|gif|jpg|css|js)(\?|$)", r.url) or "image" in r.url.lower():
            body = ""
            try:
                ct = r.headers.get("content-type", "")
                if keep_body and ("json" in ct or "html" in ct or "text" in ct):
                    body = r.text()[:1200]
                else:
                    body = f"<{ct} {len(r.body())} bytes>"
            except Exception:
                pass
            log.append(f"{req.method} {r.status} {r.url[:260]} | POST {(req.post_data or '')[:500]} | RESP {re.sub(chr(10), ' ', body)[:900]}")
    pg.on("response", on_resp)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    for name, url in [("Mercer", "https://records.mercercounty.org/publicsearch/"),
                      ("Salem", "https://clerkrecordsng.salemcountynj.gov/publicsearch/")]:
        ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000}, ignore_https_errors=True)
        pg = ctx.new_page(); log = []; watch(pg, log)
        try:
            pg.goto(url, wait_until="networkidle", timeout=60000)
            pg.get_by_text("Document Type", exact=True).first.click(timeout=10000)
            pg.wait_for_timeout(1500)
            dt = [i for i in pg.query_selector_all("input[type=text]") if i.is_visible() and (i.get_attribute("placeholder") or "").startswith("Document") or (i.is_visible() and not i.get_attribute("name") and not (i.get_attribute("placeholder") or "").startswith("MM"))]
            if dt:
                dt[0].fill("L" if name == "Mercer" else "LP"); dt[0].press("Tab")
            dates = [i for i in pg.query_selector_all("input") if i.is_visible() and (i.get_attribute("placeholder") or "") == "MM/DD/YYYY"]
            if len(dates) >= 2:
                dates[0].fill(fmt(start)); dates[0].press("Tab"); dates[1].fill(fmt(end)); dates[1].press("Tab")
            print(name, "doc types sample:", [l for l in log if "doctypes" in l][:1])
            btn = [x for x in pg.query_selector_all("button, input[type=submit]") if x.is_visible() and re.fullmatch(r"\s*Search\s*", x.inner_text() or x.get_attribute("value") or "")]
            if btn:
                btn[-1].click()
            pg.wait_for_timeout(8000)
            rows = pg.query_selector_all("table tbody tr, .k-grid tr, tr[role=row]")
            print(name, "result rows:", len(rows))
            if len(rows) > 1:
                rows[1].click(); pg.wait_for_timeout(6000)
                imgs = [x for x in pg.query_selector_all("a,button") if x.is_visible() and re.search(r"image|view|pdf", (x.inner_text() or ""), re.I)]
                print(name, "image controls:", [x.inner_text().strip()[:30] for x in imgs][:8])
                if imgs:
                    try:
                        imgs[0].click(); pg.wait_for_timeout(8000)
                    except Exception as e:
                        print(name, "img click", repr(e)[:150])
        except Exception as e:  # noqa: BLE001
            print(name, "ERROR", repr(e)[:300])
        print(f"=== {name} | {pg.url[:200]}")
        for l in log:
            if "/api/" in l or "image" in l.lower() or "document" in l.lower():
                print("  ", l[:1600])
        pg.screenshot(path=str(OUT / f"{name}4.png"))
        ctx.close()
    # Union
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []; watch(pg, log)
    try:
        pg.goto("https://clerk.ucnj.org/UCPA/DocIndex?s=date", wait_until="networkidle", timeout=60000)
        pg.select_option("select[name=dt]", label="Lis Pendens Foreclosure")
        pg.fill("input[name=from]", fmt(start)); pg.fill("input[name=to]", fmt(end))
        pg.click("input[type=submit][value=Search], button:has-text('Search')")
        pg.wait_for_timeout(6000)
        print("Union result text:", re.sub(r"\s+", " ", pg.inner_text("body"))[:1500])
        links = pg.eval_on_selector_all("a", "els => els.map(e => e.innerText.trim().slice(0,30) + ' -> ' + e.href).filter(s => /doc|image|view|pdf|inst/i.test(s)).slice(0, 12)")
        print("Union links:", links)
        img = [a for a in pg.query_selector_all("a") if re.search(r"image|view|pdf", (a.inner_text() or "") + (a.get_attribute("href") or ""), re.I)]
        if img:
            img[0].click(); pg.wait_for_timeout(6000)
    except Exception as e:  # noqa: BLE001
        print("Union ERROR", repr(e)[:300])
    print(f"=== Union | {pg.url[:200]}")
    for l in log[-12:]:
        print("  ", l[:1400])
    pg.screenshot(path=str(OUT / "Union4.png"))
    ctx.close()
    # Gloucester
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []; watch(pg, log, keep_body=False)
    try:
        pg.goto("https://i2e.uslandrecords.com/NJ/Gloucester/D/Default.aspx", wait_until="networkidle", timeout=60000)
        pg.select_option("[name$='ACSDropDownList_DocumentType']", value="80")
        pg.fill("[name$='ACSTextBox_DateFrom']", fmt(start)); pg.fill("[name$='ACSTextBox_DateTo']", fmt(end))
        pg.click("[name$='btnSearch']"); pg.wait_for_timeout(8000)
        print("Gloucester results:", re.sub(r"\s+", " ", pg.inner_text("body"))[:1500])
        print("Gloucester frames:", [f.url[:120] for f in pg.frames])
    except Exception as e:  # noqa: BLE001
        print("Gloucester ERROR", repr(e)[:300])
    pg.screenshot(path=str(OUT / "Gloucester4.png"))
    ctx.close()
    b.close()
