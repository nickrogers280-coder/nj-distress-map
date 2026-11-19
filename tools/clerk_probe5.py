"""NewVision publicsearch API (Mercer, Salem): doc types, a lis pendens search, and the app's API
routes; Union's date-range search (run in Actions)."""
import json, re, sys
from datetime import date, timedelta
import requests
sys.path.insert(0, ".")
from tracker.press import BROWSER_UA

end = date.today(); start = end - timedelta(days=30)
for name, base in [("Mercer", "https://records.mercercounty.org/publicsearch/"),
                   ("Salem", "https://clerkrecordsng.salemcountynj.gov/publicsearch/")]:
    s = requests.Session(); s.headers["User-Agent"] = BROWSER_UA
    try:
        home = s.get(base, timeout=60).text
        scripts = re.findall(r'src="(Scripts/app/[^"]+)"', home)
        routes = set()
        for sc in scripts:
            js = s.get(base + sc, timeout=60).text
            routes |= set(re.findall(r"""['"]((?:\.\./)?api/[A-Za-z0-9_/{}\-]+)""", js))
        print(name, "SCRIPTS", scripts)
        print(name, "ROUTES", sorted(routes))
        dts = s.get(base + "api/document/doctypes", timeout=60).json()
        flat = []
        for d in dts:
            flat += d.get("children") or [d]
        lp = [d for d in flat if re.search(r"LIS ?PEND|FORECLOS", d.get("name") or "", re.I)]
        print(name, "LP TYPES", lp)
        codes = ",".join(d["value"] for d in lp if not re.search(r"DISCH|RELEAS|CANCEL", d["name"], re.I))
        body = {"MaxRows": 0, "RowsPerPage": 0, "StartRow": 0, "DocTypes": codes,
                "FromDate": start.strftime("%Y%m%d"), "ToDate": end.strftime("%Y%m%d")}
        r = s.post(base + "api/search", json=body, timeout=90)
        res = r.json()
        print(name, "SEARCH", r.status_code, "rows", len(res) if isinstance(res, list) else type(res))
        if isinstance(res, list) and res:
            print(name, "ROW0", json.dumps(res[0])[:1500])
            print(name, "ROW1", json.dumps(res[1])[:800] if len(res) > 1 else "")
    except Exception as e:  # noqa: BLE001
        print(name, "ERROR", repr(e)[:300])

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page()
    try:
        pg.goto("https://clerk.ucnj.org/UCPA/DocIndex?s=date", wait_until="networkidle", timeout=60000)
        opts = pg.eval_on_selector("select[name=dt]", "s => [...s.options].map(o => [o.value, o.text])")
        print("Union OPTIONS", opts)
        val = next(v for v, t in opts if re.search(r"lis pendens", t, re.I))
        pg.select_option("select[name=dt]", value=val)
        pg.fill("input[name=from]", start.strftime("%m/%d/%Y")); pg.fill("input[name=to]", end.strftime("%m/%d/%Y"))
        pg.fill("input[name=rpp]", "50")
        with pg.expect_navigation(timeout=60000):
            pg.click("input[type=submit]")
        pg.wait_for_timeout(2000)
        print("Union URL", pg.url)
        print("Union TEXT", re.sub(r"\s+", " ", pg.inner_text("body"))[:1800])
        print("Union LINKS", pg.eval_on_selector_all("a", "els => els.map(e => e.innerText.trim().slice(0,25) + ' -> ' + e.href).filter(s => !/ucnj.org\\/?$|#$/.test(s)).slice(0, 25)"))
        print("Union FORMS", pg.eval_on_selector_all("form", "fs => fs.map(f => f.action + ' ' + f.method)"))
    except Exception as e:  # noqa: BLE001
        print("Union ERROR", repr(e)[:300])
    b.close()
