"""Fallback geocoding (US Census Bureau geocoder — free, no key) for tracked properties
that couldn't be placed from a tax-parcel match."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


class Geocoder:
    def __init__(self, timeout: int = 20, delay: float = 0.3, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.timeout = timeout
        self.delay = delay

    def locate(self, street: str, city: str = "", zip_code: str = "") -> tuple[float, float] | None:
        street = (street or "").strip()
        if not street:
            return None
        addr = ", ".join(x for x in (street, city, "NJ " + (zip_code or "")) if x and x.strip())
        try:
            time.sleep(self.delay)
            r = self.s.get(CENSUS_URL, params={"address": addr, "benchmark": "Public_AR_Current",
                                               "format": "json"}, timeout=self.timeout)
            r.raise_for_status()
            matches = r.json().get("result", {}).get("addressMatches", [])
            if not matches:
                return None
            c = matches[0]["coordinates"]
            lat, lon = float(c["y"]), float(c["x"])
            if not (38.8 <= lat <= 41.4 and -75.7 <= lon <= -73.8):  # outside New Jersey
                return None
            return round(lat, 6), round(lon, 6)
        except Exception as e:  # noqa: BLE001
            log.warning("Geocode failed for %s: %s", addr, e)
            return None
