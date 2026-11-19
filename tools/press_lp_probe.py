"""For one PRESS county: list doc types, count recent lis pendens, open one (run in Actions)."""
import logging, re, sys
from datetime import date, timedelta
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA, PRESS_SITES, PressClient, ocr_png
from tracker.lispendens import LP_FORECLOSURE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for cid in [int(x) for x in sys.argv[1].split(",")]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
        c = PressClient(PRESS_SITES[cid], ctx.new_page())
        try:
            c.page.goto(c.base + "ClerkHome.aspx?op=basic", wait_until="domcontentloaded", timeout=60000)
            opts = c.page.evaluate("""() => [...(document.querySelector('[name$="ddlDocTypeTab2"]')?.options || [])].map(o => o.text.trim())""")
            print(cid, "DOCTYPES", len(opts), [o for o in opts if re.search(r"LIS|PEND|FORECL", o, re.I)])
            rows = c.search_doc_type(LP_FORECLOSURE, date.today() - timedelta(days=10), date.today()) or []
            print(cid, "LP ROWS", len(rows), rows[:2])
            if rows:
                doc = c.open_document(rows[0].instrument)
                print(cid, "OPEN", "ok" if doc else c.last_error, (doc or ("", 0))[1])
                if doc:
                    png = c.page_image(doc[0], 0)
                    print(cid, "OCR", len(ocr_png(png)) if png else 0)
        except Exception as e:  # noqa: BLE001
            print(cid, "ERROR", repr(e)[:300])
            try:
                print(cid, "PAGE", c.page.url, c.page.inner_text("body")[:500].replace("\n", " "))
            except Exception:
                pass
        b.close()
