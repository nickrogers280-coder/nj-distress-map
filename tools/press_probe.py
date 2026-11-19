"""Open one clerk document the way the tracker does and report what happened (run in Actions)."""
import logging, sys
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker.press import BROWSER_UA, PRESS_SITES, PressClient, ocr_png

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cid, inst = int(sys.argv[1]), sys.argv[2]
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=BROWSER_UA, viewport={"width": 1400, "height": 1000})
    c = PressClient(PRESS_SITES[cid], ctx.new_page())
    doc = c.open_document(inst)
    print("RESULT", "ok" if doc else c.last_error, (doc or ("", 0))[1])
    if doc:
        png = c.page_image(doc[0], 0)
        print("PAGE0", len(png or b""), "bytes")
        if png:
            print("OCR", len(ocr_png(png)), "chars")
    b.close()
