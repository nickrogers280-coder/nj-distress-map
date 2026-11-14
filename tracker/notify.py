"""Build and send the alert email."""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from collections import OrderedDict
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .civilview import CivilViewClient
from .db import DB

log = logging.getLogger(__name__)
TRACKED = ("target", "possible")
TEMPLATES = Path(__file__).parent / "templates"

KIND_LABEL = {
    "status": "Status",
    "sale_date": "Sale date",
    "upset": "Upset price",
    "judgment": "Judgment",
    "reo": "Bank-owned (REO)",
}


def money(v) -> str:
    if v in (None, ""):
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def nice_date(v) -> str:
    if not v:
        return "—"
    try:
        return datetime.fromisoformat(str(v)[:10]).strftime("%b %-d, %Y")
    except ValueError:
        return str(v)


def card(l: dict) -> dict:
    hist = []
    try:
        hist = json.loads(l.get("status_history") or "[]")
    except json.JSONDecodeError:
        pass
    debt = max(l.get("judgment") or 0, l.get("upset") or 0)
    ratio = None
    if debt and l.get("net_value"):
        ratio = round(100 * debt / l["net_value"])
    town = l.get("muni") or (l.get("city") or "").title()
    if l.get("lat") and l.get("lon"):
        maps = f"https://www.google.com/maps/search/?api=1&query={l['lat']},{l['lon']}"
    else:
        maps = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
            f"{l.get('street') or l.get('address') or ''}, {l.get('city') or ''}, NJ")
    block_lot = ""
    if l.get("block") and l.get("lot"):
        block_lot = f"Block {l['block']} / Lot {l['lot']}" + (f" / {l['qual']}" if l.get("qual") else "")
    return {
        **l,
        "town": town,
        "title": (l.get("street") or l.get("address") or "Address not listed").title(),
        "block_lot": block_lot,
        "debt_to_value": ratio,
        "maps_url": maps,
        "detail_url": CivilViewClient.detail_url(l["property_id"]),
        "history_tail": [{"status": h[0], "date": nice_date(h[1] if len(h) > 1 else None)} for h in hist[-4:]][::-1],
    }


def build_report(db: DB, stats: dict, cfg: dict, ts: str) -> dict:
    events = db.pending_events()
    tracked = [e for e in events if e["category"] in TRACKED]

    new_target, new_possible = [], []
    changes: "OrderedDict[str, dict]" = OrderedDict()
    closed = []
    for e in tracked:
        l = db.get(e["property_id"])
        if not l:
            continue
        if e["kind"] == "new":
            (new_target if l["category"] == "target" else new_possible).append(card(l))
        elif e["kind"] == "closed":
            closed.append({**card(l), "final": e["new_value"]})
        else:
            c = changes.setdefault(l["property_id"], {**card(l), "lines": []})
            old, new = e["old_value"], e["new_value"]
            if e["kind"] in ("upset", "judgment"):
                old, new = money(old), money(new)
            elif e["kind"] == "sale_date":
                old, new = nice_date(old), nice_date(new)
            else:
                iso = re.compile(r"\d{4}-\d{2}-\d{2}")
                old = iso.sub(lambda m: nice_date(m.group()), old) if old else old
                new = iso.sub(lambda m: nice_date(m.group()), new) if new else new
            c["lines"].append({"what": KIND_LABEL.get(e["kind"], e["kind"]), "old": old, "new": new})

    by_value = lambda c: -(c.get("net_value") or c.get("upset") or 0)  # noqa: E731
    new_target.sort(key=by_value)
    new_possible.sort(key=by_value)
    return {
        "ts": ts,
        "date": nice_date(ts),
        "baseline": stats.get("baseline"),
        "new_target": new_target,
        "new_possible": new_possible,
        "changes": list(changes.values()),
        "closed": closed,
        "stats": stats,
        "event_ids": [e["id"] for e in events],
        "has_news": bool(new_target or new_possible or changes or closed),
    }


def subject(report: dict, cfg: dict) -> str:
    prefix = cfg.get("email", {}).get("subject_prefix", "NJ Distress Alert")
    nt, npos = len(report["new_target"]), len(report["new_possible"])
    if report["baseline"]:
        return f"{prefix}: baseline — {nt} commercial properties on NJ sheriff sale lists ({report['date']})"
    parts = []
    if nt:
        parts.append(f"{nt} new commercial sheriff sale{'s' if nt != 1 else ''}")
    if npos:
        parts.append(f"{npos} to review")
    upd = len(report["changes"]) + len(report["closed"])
    if upd:
        parts.append(f"{upd} update{'s' if upd != 1 else ''}")
    return f"{prefix}: {', '.join(parts) or 'no changes'} ({report['date']})"


def render(report: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = money
    env.filters["date"] = nice_date
    return env.get_template("email.html.j2").render(**report)


def send(html: str, subj: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    to = [a.strip() for a in os.environ.get("ALERT_TO", "").split(",") if a.strip()]
    user = os.environ.get("SMTP_USER")
    if not host or not to or not user:
        log.warning("Email not configured (SMTP_HOST / SMTP_USER / ALERT_TO); skipping send")
        return False
    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = os.environ.get("ALERT_FROM") or user
    msg["To"] = ", ".join(to)
    msg.set_content("This alert is HTML; open it in an email client that shows HTML.")
    msg.add_alternative(html, subtype="html")
    port = int(os.environ.get("SMTP_PORT") or "587")
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    log.info("Sent alert to %s", ", ".join(to))
    return True
