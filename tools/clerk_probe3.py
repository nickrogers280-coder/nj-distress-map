"""Try a lis pendens search on Mercer (BrowserView), Union (UCPA) and Gloucester (i2e); log the
requests the page makes so an adapter can call the same endpoints (run in Actions)."""
import json, re, sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA

OUT = Path("survey"); OUT.mkdir(exist_ok=True)
end = date.today(); start = end - timedelta(days=14)
fmt = lambda d: d.strftime("%m/%d/%Y")


def watch(pg, log):
    def on_req(r):
        if r.resource_type in ("xhr", "fetch", "document"):
            log.append({"m": r.method, "u": r.url[:300], "post": (r.post_data or "")[:600]})
    pg.on("request", on_req)


def dump(name, pg, log):
    print(f"=== {name} | {pg.url[:200]}")
    for e in log[-25:]:
        print("  REQ", e["m"], e["u"], "| POST", e["post"][:400])
    print("  TEXT", re.sub(r"\s+", " ", pg.inner_text("body"))[:1500])
    pg.screenshot(path=str(OUT / f"{name}.png"))


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    # ---- Mercer
    for name, url in [("Mercer", "https://records.mercercounty.org/publicsearch/")] + [(n, u) for n, u in [("Salem", "https://clerkrecordsng.salemcountynj.gov/publicsearch/")] ]:
        ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000}, ignore_https_errors=True)
        pg = ctx.new_page(); log = []; watch(pg, log)
        try:
            pg.goto(url, wait_until="networkidle", timeout=60000)
            print(name, "captcha on page:", bool(re.search(r"recaptcha|turnstile", pg.content(), re.I)))
            pg.get_by_text("Document Type", exact=True).first.click(timeout=10000)
            pg.wait_for_timeout(1500)
            vis = [i for i in pg.query_selector_all("input[type=text]") if i.is_visible()]
            print(name, "visible text inputs:", [(i.get_attribute("name"), i.get_attribute("placeholder")) for i in vis])
            if vis:
                vis[0].fill("L")
            for n, v in (("fromdate", fmt(start)), ("todate", fmt(end))):
                for i in pg.query_selector_all(f"input[name={n}]"):
                    if i.is_visible():
                        i.fill(v)
            btns = [x for x in pg.query_selector_all("button[type=submit], input[type=submit]") if x.is_visible() and re.search("search", (x.inner_text() or x.get_attribute("value") or ""), re.I)]
            if btns:
                btns[0].click()
            pg.wait_for_timeout(8000)
        except Exception as e:  # noqa: BLE001
            print(name, "ERROR", repr(e)[:300])
        dump(name, pg, log)
        ctx.close()
    # ---- Union
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []; watch(pg, log)
    try:
        pg.goto("https://clerk.ucnj.org/UCPA/DocIndex", wait_until="networkidle", timeout=60000)
        pg.get_by_text("Date Range Search").first.click(timeout=10000)
        pg.wait_for_timeout(3000)
        print("Union fields:", pg.eval_on_selector_all("input,select,button", "els => els.filter(e => e.type !== 'hidden').map(e => (e.name||e.id) + ':' + e.type + ':' + (e.tagName=='SELECT' ? [...e.options].map(o=>o.text).filter(t=>/lis|pend|forecl/i.test(t)).join('|') : (e.value||e.innerText||'').slice(0,20)))"))
    except Exception as e:  # noqa: BLE001
        print("Union ERROR", repr(e)[:300])
    dump("Union", pg, log)
    ctx.close()
    # ---- Gloucester
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page(); log = []; watch(pg, log)
    try:
        pg.goto("https://i2e.uslandrecords.com/NJ/Gloucester/D/Default.aspx", wait_until="networkidle", timeout=60000)
        print("Gloucester search fields:", pg.eval_on_selector_all("[name*='SearchFormEx1']", "els => els.map(e => e.name + ':' + e.type + ':' + (e.tagName=='SELECT' ? e.options.length + ' opts' : (e.value||'').slice(0,20)))"))
        sel = pg.query_selector("[name$='ACSDropDownList_DocumentType']")
        if sel:
            val = pg.evaluate("(s) => [...s.options].find(o => /LIS PENDENS\\/FORCLOS/i.test(o.text))?.value", sel)
            print("Gloucester LP option value:", val)
    except Exception as e:  # noqa: BLE001
        print("Gloucester ERROR", repr(e)[:300])
    dump("Gloucester", pg, log)
    ctx.close()
    b.close()
