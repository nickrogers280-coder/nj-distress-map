"""Run the NJ distressed-property tracker once.

    python run.py                 # scrape, update the database and the map data
    python run.py --no-email      # never send email, even if enabled in config.yaml
    python run.py --counties 10 7 --max-new 25   # quick test on Hudson + Bergen
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from tracker import notify, pipeline
from tracker.civilview import CivilViewClient
from tracker.counties import DEFAULT_ENABLED
from tracker.db import DB
from tracker.export import export_all
from tracker.geocode import Geocoder
from tracker.parcels import ParcelClient

ROOT = Path(__file__).parent



def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    if cfg.get("counties") in (None, "all", []):
        cfg["counties"] = DEFAULT_ENABLED
    return cfg



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "tracker.db"))
    ap.add_argument("--counties", nargs="*", type=int, help="CivilView county IDs (default: config)")
    ap.add_argument("--max-new", type=int, help="Cap new-listing detail fetches (for testing)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--preview", default=str(ROOT / "data" / "last_email.html"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    if args.counties:
        cfg["counties"] = args.counties
    if args.max_new is not None:
        cfg["max_new_details_per_run"] = args.max_new

    contact = os.environ.get("CONTACT_EMAIL") or cfg.get("contact_email")
    ua = "NJ-Distress-Tracker/1.0" + (f" (contact: {contact})" if contact else "")
    cv = CivilViewClient(delay=cfg.get("request_delay_seconds", 1.0), user_agent=ua)
    parcels = ParcelClient()
    db = DB(args.db)

    started = pipeline.now_iso()
    stats = pipeline.run(cfg, cv, parcels, db, ts=started, geocoder=Geocoder())
    report = notify.build_report(db, stats, cfg, started)
    html = notify.render(report)
    Path(args.preview).parent.mkdir(parents=True, exist_ok=True)
    Path(args.preview).write_text(html)

    email_cfg = cfg.get("email", {})
    want_email = email_cfg.get("enabled", False) and not args.no_email
    if want_email and (report["has_news"] or email_cfg.get("send_when_no_news") or stats["county_errors"]):
        try:
            if notify.send(html, notify.subject(report, cfg)):
                db.mark_notified(report["event_ids"])
        except Exception as e:  # noqa: BLE001
            logging.error("Email failed: %s", e)
    else:
        db.mark_notified(report["event_ids"])

    db.log_run(started, pipeline.now_iso(), stats)
    db.commit()
    export_all(db, Path(args.db).parent)
    db.close()
    logging.info("Done: %s", json.dumps({k: v for k, v in stats.items() if k != "counties"}))
    return 1 if stats["county_errors"] and not stats["counties"] else 0


if __name__ == "__main__":
    sys.exit(main())
