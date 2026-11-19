"""Run each non-PRESS clerk adapter for two weeks and read one document (run in Actions)."""
import logging, sys
from datetime import date, timedelta
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tracker import clerks
from tracker.lispendens import LP_DISCHARGE, LP_FORECLOSURE, LP_IN_REM, LP_PLAIN
from tracker.press import ocr_png

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
end = date.today(); start = end - timedelta(days=14)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    for ad in clerks.build_adapters({"lis_pendens_counties": [1100, 20, 15]}, b, include_press=False):
        try:
            gs = ad.lp_groups(start, end, LP_FORECLOSURE, LP_PLAIN, LP_IN_REM)
            ds = ad.discharge_groups(start, end, LP_DISCHARGE)
            print(ad.county, "GROUPS", len(gs), "DISCHARGES", len(ds))
            for g in gs[:3]:
                print(ad.county, "G", {k: g.get(k) for k in ("instrument", "doc_type", "recorded", "defendants", "plaintiffs", "town", "block", "lot", "street")})
            if gs:
                pngs = ad.pages(gs[0], 3)
                print(ad.county, "PAGES", len(pngs), [len(p) for p in pngs], "err", ad.last_error)
                if pngs:
                    t = ocr_png(pngs[0])
                    print(ad.county, "OCR", len(t), t[:300].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print(ad.county, "ERROR", repr(e)[:400])
        ad.close()
    b.close()
