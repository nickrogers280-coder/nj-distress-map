"""Enrich tracked properties with owner/lender contacts from county records, then re-export.

    python enrich_contacts.py              # up to 25 properties not yet enriched (or >30 days old)
    python enrich_contacts.py --limit 5
Needs: pip install playwright && python -m playwright install --with-deps chromium; apt install tesseract-ocr
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

from tracker.db import DB
from tracker.enrich import run_enrichment
from tracker.export import export_all

ROOT = Path(__file__).parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "tracker.db"))
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    db = DB(args.db)
    import tracker.enrich as enrich_mod
    enrich_mod.DEBUG_DIR = Path(args.db).parent / "debug"
    stats = run_enrichment(db, limit=args.limit)
    logging.info("Enrichment: %s", json.dumps(stats))
    export_all(db, Path(args.db).parent, reo_days=int(cfg.get("reo_lookback_days", 365)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
