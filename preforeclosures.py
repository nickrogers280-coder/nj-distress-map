"""Find new pre-foreclosures (foreclosure lis pendens) in county clerk records, then re-export the map.

    python preforeclosures.py
Needs: pip install playwright && python -m playwright install --with-deps chromium; apt install tesseract-ocr
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

from tracker.db import DB
from tracker.export import export_all
from tracker.geocode import Geocoder
from tracker.lispendens import run_lis_pendens
from tracker.parcels import ParcelClient

ROOT = Path(__file__).parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "tracker.db"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    db = DB(args.db)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            stats = run_lis_pendens(cfg, db, browser, ParcelClient(), Geocoder())
        finally:
            browser.close()
    logging.info("Pre-foreclosures: %s", json.dumps(stats))
    export_all(db, Path(args.db).parent, reo_days=int(cfg.get("reo_lookback_days", 365)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
