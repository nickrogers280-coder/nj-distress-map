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
log = logging.getLogger("tracker")



def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    if cfg.get("counties") in (None, "all", []):
        cfg["counties"] = DEFAULT_ENABLED
    return cfg



def backfill_upset(db: DB) -> None:
    """Recover upset prices saved under labels the parser didn't know yet (e.g. 'Approx. Upset*')."""
    from tracker.civilview import DETAIL_LABELS, parse_money
    keys = [k for k, v in DETAIL_LABELS.items() if v == "upset"]
    rows = db.conn.execute("SELECT property_id, extra FROM listings WHERE upset IS NULL AND extra IS NOT NULL").fetchall()
    for pid, extra in rows:
        try:
            e = json.loads(extra)
        except json.JSONDecodeError:
            continue
        for k in keys:
            if e.get(k):
                val = parse_money(e[k])
                if val is not None:
                    db.conn.execute("UPDATE listings SET upset=? WHERE property_id=?", (val, pid))
                break
    db.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "tracker.db"))
    ap.add_argument("--counties", nargs="*", type=int, help="CivilView county IDs (default: config)")
    ap.add_argument("--max-new", type=int, help="Cap new-listing detail fetches (for testing)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-reo", action="store_true", help="Skip the bank-owned (REO) pass")
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

    backfill_upset(db)
    started = pipeline.now_iso()
    geocoder = Geocoder()
    if db.get_meta("parcel_audit_v1") != "1":
        log.info("Parcel audit: %s", pipeline.parcel_audit(cfg, parcels, db, geocoder))
        db.set_meta("parcel_audit_v1", "1")
        db.commit()
    log.info("Reclassified: %s", pipeline.reclassify_all(db, cfg))
    stats = pipeline.run(cfg, cv, parcels, db, ts=started, geocoder=geocoder)
    try:
        stats["parcel_check"] = pipeline.verify_parcels(cfg, parcels, db, geocoder)
        log.info("Parcel check: %s", stats["parcel_check"])
    except Exception as e:  # noqa: BLE001
        log.exception("Parcel check failed: %s", e)
    if cfg.get("reo_enabled", True) and not args.no_reo:
        stats.update(pipeline.run_reo(cfg, cv, parcels, db, ts=started, geocoder=geocoder))
        try:
            from tracker import reo_history
            h = reo_history.run(cfg, db, parcels)
            stats["reo_history"] = {k: h.get(k) for k in ("years", "missing_years", "events", "new", "located", "tracked")}
            log.info("REO history: %s", stats["reo_history"])
        except Exception as e:  # noqa: BLE001 - history is extra; never block the daily scan
            log.exception("REO history failed: %s", e)
            stats["reo_history"] = {"error": str(e)[:200]}
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
    export_all(db, Path(args.db).parent, reo_days=int(cfg.get("reo_lookback_days", 365)))
    db.close()
    logging.info("Done: %s", json.dumps({k: v for k, v in stats.items() if k != "counties"}))
    return 1 if stats["county_errors"] and not stats["counties"] else 0


if __name__ == "__main__":
    sys.exit(main())
