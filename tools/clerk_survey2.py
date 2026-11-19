"""Step into each open clerk site's search page; screenshot and list its fields (run in Actions)."""
import re, sys
from pathlib import Path
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA

OUT = Path("survey"); OUT.mkdir(exist_ok=True)
STEPS = {
    "Atlantic": ("https://atlantic.newvisionsystems.com/ClerkSearch/", ["[name$='btnLegacyApp']"]),
    "Middlesex": ("https://mcrecords.co.middlesex.nj.us/recordssearch/", ["[name$='btnLegacyApp']"]),
    "Ocean": ("http://sng.co.ocean.nj.us/searchapplication/", ["[name$='btnLegacyApp']"]),
    "OceanNG": ("http://sng.co.ocean.nj.us/searchapplication/", ["[name$='btnBasicApp']"]),
    "Mercer": ("https://records.mercercounty.org/publicsearch/", []),
    "Union": ("https://clerk.ucnj.org/UCPA/DocIndex", ["text=/accept|agree|search|continue/i"]),
    "Gloucester": ("https://i2e.uslandrecords.com/NJ/Gloucester/D/Default.aspx", []),
    "Camden": ("https://www.camdencounty.com/service/county-clerk/online-property-records/", []),
    "Salem": ("https://salemcountyclerk.org/public-records-search/", []),
    "Cape May": ("https://capemaycountynj.gov/1263/Land-Records-Public-Search", []),
}
want = sys.argv[1:] or list(STEPS)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    for name in want:
        url, clicks = STEPS[name]
        ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000}, ignore_https_errors=True)
        pg = ctx.new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(2500)
            for sel in clicks:
                try:
                    with ctx.expect_page(timeout=8000) as newp:
                        pg.locator(sel).first.click(timeout=8000)
                    pg = newp.value
                except Exception:
                    pass
                pg.wait_for_timeout(4000)
            html = pg.content()
            print(f"=== {name} | {pg.url.split('?')[0]} | {pg.title()} | captcha={bool(re.search(r'recaptcha|hcaptcha|turnstile|cf-challenge', html, re.I))}")
            print("  text:", re.sub(r"\s+", " ", pg.inner_text("body"))[:600])
            print("  links:", pg.eval_on_selector_all("a", "els => els.map(e => (e.innerText||'').trim().slice(0,40) + ' -> ' + (e.href||'').split('?')[0]).filter(s => /search|record|land|deed|clerk|guest|public|lis|document/i.test(s)).slice(0, 15)"))
            print("  fields:", pg.eval_on_selector_all("input,select,button", "els => els.filter(e => e.type !== 'hidden').map(e => (e.name||e.id||'') + ':' + (e.type||e.tagName) + ':' + (e.value||e.innerText||'').trim().slice(0,20)).slice(0, 40)"))
            sel = pg.eval_on_selector_all("select", "els => els.map(e => (e.name||e.id) + ' => ' + [...e.options].map(o => o.text.trim()).filter(t => /lis|pend|forecl/i.test(t)).join(' | ')).filter(s => s.includes('|') || /lis|pend/i.test(s))")
            print("  lp options:", sel)
            pg.screenshot(path=str(OUT / f"{name.replace(' ', '_')}.png"), full_page=False)
        except Exception as e:  # noqa: BLE001
            print(f"=== {name} | ERROR {e.__class__.__name__}: {str(e)[:200]}")
        ctx.close()
    b.close()
