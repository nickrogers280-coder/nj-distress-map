"""SQLite storage for listings, change events and run history.

SQLite keeps Phase 1 free and setup-free (the file is committed back to the repo by the
scheduled job). The schema maps one-to-one onto Postgres for the Phase 2 map CRM.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_listings_county ON listings(county_id, is_open);
CREATE INDEX IF NOT EXISTS ix_listings_category ON listings(category, is_open);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    property_id      TEXT PRIMARY KEY,
    county_id        INTEGER,
    county           TEXT,
    sheriff_number   TEXT,
    court_case       TEXT,
    sale_date        TEXT,
    list_status      TEXT,
    latest_status    TEXT,
    status_history   TEXT,      -- JSON [[status, date], ...]
    plaintiff        TEXT,
    defendant        TEXT,
    address          TEXT,
    street           TEXT,
    city             TEXT,
    zip              TEXT,
    description      TEXT,
    parcel_text      TEXT,
    judgment         REAL,
    upset            REAL,
    attorney         TEXT,
    attorney_phone   TEXT,
    extra            TEXT,      -- JSON of unmapped detail fields
    -- MOD-IV enrichment
    pams_pin         TEXT,
    muni             TEXT,
    block            TEXT,
    lot              TEXT,
    qual             TEXT,
    prop_class       TEXT,
    class_label      TEXT,
    prop_loc         TEXT,
    net_value        INTEGER,
    land_val         INTEGER,
    imprvt_val       INTEGER,
    last_yr_tax      REAL,
    bldg_desc        TEXT,
    land_desc        TEXT,
    calc_acre        REAL,
    yr_constr        INTEGER,
    last_sale_price  INTEGER,
    last_sale_date   TEXT,
    match_method     TEXT,
    lat              REAL,
    lon              REAL,
    geo_source       TEXT,      -- 'parcel' | 'address'
    -- sale outcome (from the sheriff's Sold/Cancelled list or the final status)
    outcome          TEXT,      -- 'reo' | 'third_party' | 'cancelled'
    outcome_date     TEXT,
    outcome_status   TEXT,
    -- classification
    category         TEXT,      -- 'target' | 'possible' | 'other'
    category_reason  TEXT,
    -- bookkeeping
    is_open          INTEGER DEFAULT 1,
    first_seen       TEXT,
    last_seen        TEXT,
    last_detail_at   TEXT,
    closed_at        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id  TEXT,
    ts           TEXT,
    kind         TEXT,          -- new | status | sale_date | upset | judgment | closed
    old_value    TEXT,
    new_value    TEXT,
    notified     INTEGER DEFAULT 0,
    baseline     INTEGER DEFAULT 0   -- 1 = recorded on the very first run (not a real alert)
);
CREATE INDEX IF NOT EXISTS ix_events_pending ON events(notified);

CREATE TABLE IF NOT EXISTS sold_checked (
    property_id  TEXT PRIMARY KEY,
    county_id    INTEGER,
    checked_at   TEXT,
    outcome      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   TEXT,
    finished  TEXT,
    stats     TEXT
);
"""

LISTING_COLUMNS = [
    "property_id", "county_id", "county", "sheriff_number", "court_case", "sale_date",
    "list_status", "latest_status", "status_history", "plaintiff", "defendant", "address",
    "street", "city", "zip", "description", "parcel_text", "judgment", "upset", "attorney",
    "attorney_phone", "extra", "pams_pin", "muni", "block", "lot", "qual", "prop_class",
    "class_label", "prop_loc", "net_value", "land_val", "imprvt_val", "last_yr_tax",
    "bldg_desc", "land_desc", "calc_acre", "yr_constr", "last_sale_price", "last_sale_date",
    "match_method", "lat", "lon", "geo_source", "outcome", "outcome_date", "outcome_status", "category", "category_reason", "is_open", "first_seen", "last_seen",
    "last_detail_at", "closed_at",
]


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(INDEXES)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(listings)")}
        for col in LISTING_COLUMNS:
            if col not in have:
                kind = "REAL" if col in ("lat", "lon", "judgment", "upset", "last_yr_tax", "calc_acre") else "TEXT"
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {kind}")
        have_ev = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "baseline" not in have_ev:
            self.conn.execute("ALTER TABLE events ADD COLUMN baseline INTEGER DEFAULT 0")
        self.conn.commit()

    # ---------------------------------------------------------------- meta / sold list
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    def sold_checked(self, property_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM sold_checked WHERE property_id=?", (property_id,)).fetchone()
        return dict(r) if r else None

    def mark_sold_checked(self, property_id: str, county_id: int, ts: str, outcome: str | None) -> None:
        self.conn.execute(
            "INSERT INTO sold_checked (property_id, county_id, checked_at, outcome) VALUES (?,?,?,?) "
            "ON CONFLICT(property_id) DO UPDATE SET checked_at=excluded.checked_at, outcome=excluded.outcome",
            (property_id, county_id, ts, outcome))

    # ---------------------------------------------------------------- listings
    def get(self, property_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM listings WHERE property_id=?", (property_id,)).fetchone()
        return dict(r) if r else None

    def upsert(self, rec: dict) -> None:
        rec = {k: rec.get(k) for k in LISTING_COLUMNS if k in rec}
        for k in ("status_history", "extra"):
            if k in rec and not isinstance(rec[k], (str, type(None))):
                rec[k] = json.dumps(rec[k])
        cols = list(rec)
        sql = (
            f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(property_id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "property_id")
        )
        self.conn.execute(sql, [rec[c] for c in cols])

    def open_ids(self, county_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT property_id FROM listings WHERE county_id=? AND is_open=1", (county_id,)
        ).fetchall()
        return {r[0] for r in rows}

    def count_listings(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def open_leads(self, categories=("target", "possible")) -> list[dict]:
        q = f"SELECT * FROM listings WHERE is_open=1 AND category IN ({','.join('?' * len(categories))}) ORDER BY sale_date"
        return [dict(r) for r in self.conn.execute(q, categories).fetchall()]

    # ---------------------------------------------------------------- events
    def add_event(self, property_id: str, ts: str, kind: str, old=None, new=None, baseline: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO events (property_id, ts, kind, old_value, new_value, baseline) VALUES (?,?,?,?,?,?)",
            (property_id, ts, kind, None if old is None else str(old), None if new is None else str(new),
             int(bool(baseline))),
        )

    def pending_events(self) -> list[dict]:
        q = """SELECT e.*, l.category FROM events e JOIN listings l USING(property_id)
               WHERE e.notified=0 ORDER BY e.id"""
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def mark_notified(self, event_ids: list[int]) -> None:
        self.conn.executemany("UPDATE events SET notified=1 WHERE id=?", [(i,) for i in event_ids])

    # ---------------------------------------------------------------- runs
    def log_run(self, started: str, finished: str, stats: dict) -> None:
        self.conn.execute("INSERT INTO runs (started, finished, stats) VALUES (?,?,?)",
                          (started, finished, json.dumps(stats)))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
