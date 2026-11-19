"""Load each county clerk's records site headless and report what it shows (run in Actions)."""
import re, sys
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA

SITES = {
    "Atlantic": "https://atlantic.newvisionsystems.com/ClerkSearch/",
    "Camden": "https://www.camdencounty.com/service/county-clerk/online-property-records/",
    "Camden2": "http://24.246.110.18/ClerkSearch/",
    "Cape May": "http://clerk.capemaycountynj.gov/landrecords/",
    "Cumberland": "https://countyfusion1.kofiletech.us/countyweb/loginDisplay.action?countyname=CumberlandNJ",
    "Gloucester": "https://i2e.uslandrecords.com/NJ/Gloucester/D/Default.aspx",
    "Hudson": "https://acclaim.hcnj.us/AcclaimWeb/",
    "Mercer": "https://records.mercercounty.org/RecordsNG_Search/",
    "Middlesex": "https://mcrecords.co.middlesex.nj.us/recordssearch/",
    "Morris": "http://mcclerksng.co.morris.nj.us/publicsearch/",
    "Ocean": "http://sng.co.ocean.nj.us/searchapplication/",
    "Salem": "https://salemcountyclerk.org/public-records-search/",
    "Somerset": "https://liveacclaim.co.somerset.nj.us/AcclaimWeb/",
    "Sussex": "https://cefile.sussex.nj.us/RecordsNG_Web/",
    "Union": "https://clerk.ucnj.org/UCPA/DocIndex",
    "Warren": "https://www.searchiqs.com/NJWAR/Login.aspx",
    "Hunterdon": "https://www.co.hunterdon.nj.us/countyclerk.htm",
    "Bergen": "http://bergensearch.co.bergen.nj.us/countyweb/login.do?countyname=BergenRegistry",
}
want = sys.argv[1:] or list(SITES)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    for name in want:
        url = SITES[name]
        ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000}, ignore_https_errors=True)
        pg = ctx.new_page()
        try:
            r = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(3000)
            html = pg.content()
            text = re.sub(r"\s+", " ", pg.inner_text("body"))[:400]
            links = [a for a in pg.eval_on_selector_all("a", "els => els.map(e => (e.innerText||'').trim() + ' -> ' + (e.getAttribute('href')||'').split('?')[0]).filter(s => /search|record|guest|public|land|deed|disclaim|accept|agree/i.test(s)).slice(0, 12)")]
            forms = pg.eval_on_selector_all("input,select,button", "els => els.filter(e => e.type !== 'hidden').map(e => (e.name||e.id||'') + ':' + (e.type||e.tagName) + ':' + (e.value||e.innerText||'').slice(0,25)).slice(0, 25)")
            print(f"=== {name} | {r.status if r else '?'} | {pg.url.split('?')[0]} | {pg.title()}")
            print("  captcha:", bool(re.search(r"captcha|recaptcha|hcaptcha|turnstile|cf-challenge", html, re.I)),
                  "| password field:", "type=\"password\"" in html.lower(), "| frames:", [f.url.split("?")[0] for f in pg.frames][:6])
            print("  text:", text)
            print("  links:", links)
            print("  inputs:", forms)
        except Exception as e:  # noqa: BLE001
            print(f"=== {name} | ERROR {e.__class__.__name__}: {str(e)[:200]}")
        ctx.close()
    b.close()
