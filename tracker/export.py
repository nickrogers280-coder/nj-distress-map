"""Export the data the map website reads (data/map.json) plus a CSV of open leads."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .civilview import CivilViewClient
from .db import DB

TRACKED = ("target", "possible")

PROPERTY_FIELDS = [
    "property_id", "category", "category_reason", "is_open", "closed_at", "first_seen", "county",
    "muni", "street", "city", "zip", "block", "lot", "qual", "pams_pin", "prop_class", "class_label",
    "net_value", "land_val", "imprvt_val", "last_yr_tax", "bldg_desc", "land_desc", "calc_acre",
    "yr_constr", "last_sale_price", "last_sale_date", "sale_date", "latest_status", "sheriff_number",
    "court_case", "plaintiff", "defendant", "judgment", "upset", "attorney", "attorney_phone",
    "description", "lat", "lon", "geo_source",
]

CSV_COLUMNS = [
    "category", "county", "muni", "street", "city", "zip", "block", "lot", "qual", "prop_class",
    "class_label", "net_value", "calc_acre", "bldg_desc", "yr_constr", "sale_date", "latest_status",
    "sheriff_number", "court_case", "plaintiff", "defendant", "judgment", "upset", "attorney",
    "attorney_phone", "last_sale_price", "last_sale_date", "lat", "lon", "first_seen", "property_id",
]


def build_map_data(db: DB, now: datetime | None = None, alert_days: int = 30, closed_days: int = 45) -> dict:
    now = now or datetime.now(timezone.utc)
    alert_cutoff = (now - timedelta(days=alert_days)).isoformat(timespec="seconds")
    closed_cutoff = (now - timedelta(days=closed_days)).isoformat(timespec="seconds")
    c = db.conn

    rows = c.execute(
        f"""SELECT * FROM listings WHERE category IN ('target','possible')
            AND (is_open=1 OR closed_at >= ?) ORDER BY sale_date""", (closed_cutoff,)).fetchall()
    props = []
    ids = set()
    for r in rows:
        r = dict(r)
        p = {k: r.get(k) for k in PROPERTY_FIELDS}
        try:
            p["status_history"] = json.loads(r.get("status_history") or "[]")
        except json.JSONDecodeError:
            p["status_history"] = []
        p["detail_url"] = CivilViewClient.detail_url(r["property_id"])
        props.append(p)
        ids.add(r["property_id"])

    ev = c.execute(
        """SELECT id, property_id, ts, kind, old_value, new_value FROM events
           WHERE ts >= ? AND baseline=0 ORDER BY ts DESC, id DESC""", (alert_cutoff,)).fetchall()
    alerts = [dict(e) for e in ev if e["property_id"] in ids]

    last = c.execute("SELECT started, stats FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    run = {"at": last["started"], **json.loads(last["stats"])} if last else None
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "last_run": {k: run[k] for k in ("at", "counties", "county_errors", "baseline")} if run else None,
        "properties": props,
        "alerts": alerts,
    }


def export_all(db: DB, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "map.json").write_text(json.dumps(build_map_data(db), separators=(",", ":"), default=str))
    with open(out_dir / "leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(db.open_leads())
