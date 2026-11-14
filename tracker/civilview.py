"""Scraper and parsers for CivilView sheriff sale listings.

The site has two page types:
  * List page   /Sales/SalesSearch?countyId=N   -> table of open sales (column order varies by county)
  * Detail page /Sales/SaleDetails?PropertyId=N -> label/value pairs + a status history table

Parsing is kept in pure functions (parse_list, parse_detail) so it can be tested against
saved HTML without network access.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://salesweb.civilview.com"

# Header text on the list page -> our field name. Matched after lower-casing and stripping.
LIST_HEADERS = {
    "sheriff #": "sheriff_number",
    "sales date": "sale_date",
    "sale date": "sale_date",
    "status date": "sale_date",  # Morris labels the sale date column "Status Date"
    "status": "status",
    "address": "address",
    "property address": "address",
    "plaintiff": "plaintiff",
    "defendant": "defendant",
}

# Detail page labels -> our field name. Labels are normalised (lower-case, no ':' or '*').
DETAIL_LABELS = {
    "sheriff #": "sheriff_number",
    "court case #": "court_case",
    "sales date": "sale_date",
    "sale date": "sale_date",
    "plaintiff": "plaintiff",
    "defendant": "defendant",
    "address": "address",
    "property address": "address",
    "description": "description",
    "judgment": "judgment",
    "approx. judgment": "judgment",
    "approx judgment": "judgment",
    "approximate judgment": "judgment",
    "good faith upset": "upset",
    "upset amount": "upset",
    "upset": "upset",
    "minimum bid": "upset",
    "attorney": "attorney",
    "attorney phone": "attorney_phone",
    "attorney file #": "attorney_file",
    "parcel #": "parcel_text",
    "property note": "property_note",
}


@dataclass
class ListingRow:
    property_id: str
    county_id: int
    sheriff_number: str = ""
    sale_date: str | None = None
    status: str = ""
    address: str = ""
    plaintiff: str = ""
    defendant: str = ""


@dataclass
class SaleDetail:
    property_id: str
    county: str = ""
    sheriff_number: str = ""
    court_case: str = ""
    sale_date: str | None = None
    plaintiff: str = ""
    defendant: str = ""
    address: str = ""
    street: str = ""
    city: str = ""
    zip: str = ""
    description: str = ""
    judgment: float | None = None
    upset: float | None = None
    attorney: str = ""
    attorney_phone: str = ""
    attorney_file: str = ""
    parcel_text: str = ""
    property_note: str = ""
    status_history: list[tuple[str, str | None]] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def latest_status(self) -> str:
        return self.status_history[-1][0] if self.status_history else ""


# ---------------------------------------------------------------- helpers

def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(text: str | None) -> str | None:
    """'11/19/2026' -> '2026-11-19'. Returns None if no date is found."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not m:
        return None
    mo, d, y = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def parse_money(text: str | None) -> float | None:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _norm_label(text: str) -> str:
    return clean(text).lower().rstrip(":").replace("*", "").rstrip(":").strip()


def split_address(text: str) -> tuple[str, str, str]:
    """Split a CivilView address into (street, city, zip).

    Detail pages put the street and 'CITY NJ 07047' on separate lines; list pages run them
    together, in which case city may come back empty.
    """
    lines = [clean(x) for x in (text or "").split("\n") if clean(x)]
    if len(lines) >= 2:
        street = lines[0]
        m = re.match(r"^(.*?)[,\s]+NJ\.?\s*(\d{5})?", lines[-1], re.I)
        if m:
            return street, clean(m.group(1)).upper(), m.group(2) or ""
        return street, lines[-1].upper(), ""
    one = lines[0] if lines else ""
    m = re.search(r"\bNJ\.?\s*(\d{5})\s*$", one, re.I)
    return one, "", (m.group(1) if m else "")


def property_id_from_href(href: str) -> str | None:
    qs = parse_qs(urlparse(href).query)
    for key, vals in qs.items():
        if key.lower() == "propertyid" and vals:
            return vals[0]
    return None


# ---------------------------------------------------------------- parsers

def parse_list(html: str, county_id: int) -> list[ListingRow]:
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        first = t.find("tr")
        if first and "sheriff #" in clean(first.get_text(" ")).lower():
            table = t
    if table is None:
        return []
    trs = table.find_all("tr")
    headers = [_norm_label(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
    fields = [LIST_HEADERS.get(h) for h in headers]

    rows: list[ListingRow] = []
    for tr in trs[1:]:
        link = tr.find("a", href=True)
        pid = property_id_from_href(link["href"]) if link else None
        if not pid:
            continue
        row = ListingRow(property_id=pid, county_id=county_id)
        for cell, fname in zip(tr.find_all(["td", "th"]), fields):
            if not fname:
                continue
            value = clean(cell.get_text(" "))
            if fname == "sale_date":
                row.sale_date = parse_date(value)
            else:
                setattr(row, fname, value)
        rows.append(row)
    return rows


def parse_detail(html: str, property_id: str) -> SaleDetail:
    soup = BeautifulSoup(html, "lxml")
    d = SaleDetail(property_id=property_id)

    h1 = soup.find("h1")
    if h1:
        m = re.search(r"\(([^)]*?)\s+County", h1.get_text())
        if m:
            d.county = clean(m.group(1))

    for item in soup.select(".sale-detail-item"):
        label_el = item.select_one(".sale-detail-label")
        value_el = item.select_one(".sale-detail-value")
        if not label_el or not value_el:
            continue
        label = _norm_label(label_el.get_text())
        raw = value_el.get_text("\n")
        value = clean(raw)
        fname = DETAIL_LABELS.get(label)
        if fname is None:
            if label:
                d.extra[label] = value
            continue
        if fname == "sale_date":
            d.sale_date = parse_date(value)
        elif fname in ("judgment", "upset"):
            setattr(d, fname, parse_money(value))
        elif fname == "address":
            d.address = value
            d.street, d.city, d.zip = split_address(raw)
        else:
            setattr(d, fname, value)

    # Status history: the table whose first header is "Status". Some pages render it twice
    # (long/short views), so de-duplicate while keeping order.
    seen = set()
    for t in soup.find_all("table"):
        first = t.find("tr")
        if not first or not clean(first.get_text(" ")).lower().startswith("status"):
            continue
        for tr in t.find_all("tr")[1:]:
            cells = [clean(c.get_text(" ")) for c in tr.find_all("td")]
            if not cells or not cells[0]:
                continue
            entry = (cells[0].rstrip(":").strip(), parse_date(cells[1]) if len(cells) > 1 else None)
            if entry not in seen:
                seen.add(entry)
                d.status_history.append(entry)
    return d


# ---------------------------------------------------------------- HTTP client

class CivilViewClient:
    def __init__(self, delay: float = 1.0, timeout: int = 30, user_agent: str | None = None,
                 session: requests.Session | None = None, retries: int = 3):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = user_agent or "NJ-Distress-Tracker/1.0"
        self._last = 0.0

    def _get(self, path: str) -> str:
        url = urljoin(BASE, path)
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=self.timeout)
                self._last = time.monotonic()
                r.raise_for_status()
                return r.text
            except requests.RequestException as e:
                self._last = time.monotonic()
                if attempt == self.retries:
                    raise
                log.warning("GET %s failed (%s); retry %d", url, e, attempt)
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def list_open(self, county_id: int) -> list[ListingRow]:
        return parse_list(self._get(f"/Sales/SalesSearch?countyId={county_id}"), county_id)

    def detail(self, property_id: str) -> SaleDetail:
        return parse_detail(self._get(f"/Sales/SaleDetails?PropertyId={property_id}"), property_id)

    @staticmethod
    def detail_url(property_id: str) -> str:
        return f"{BASE}/Sales/SaleDetails?PropertyId={property_id}"
