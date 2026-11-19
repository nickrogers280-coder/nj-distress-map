"""Match sheriff sale properties to New Jersey MOD-IV tax records and classify them.

Data source: NJOGIS statewide "Parcels and MOD-IV Composite" ArcGIS feature service.
It carries property class, assessed values, building/land descriptions, acreage, year built
and last sale. OWNER_NAME is redacted in this service under Daniel's Law, so ownership comes
from the sheriff sale defendant instead.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

PARCEL_SERVICE = (
    "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/ArcGIS/rest/services/"
    "Parcels_Composite_NJ_WM/FeatureServer/0/query"
)

OUT_FIELDS = [
    "PAMS_PIN", "COUNTY", "MUN_NAME", "PCL_MUN", "PCLBLOCK", "PCLLOT", "PCLQCODE",
    "PROP_CLASS", "PROP_LOC", "ST_ADDRESS", "LAND_VAL", "IMPRVT_VAL", "NET_VALUE",
    "LAST_YR_TX", "BLDG_DESC", "LAND_DESC", "CALC_ACRE", "BLDG_CLASS", "YR_CONSTR",
    "SALE_PRICE", "DEED_DATE", "DEED_BOOK", "DEED_PAGE", "ADD_LOTS1", "COMM_DWELL", "FAC_NAME",
]

CLASS_LABELS = {
    "1": "Vacant land",
    "2": "Residential (1-4 family)",
    "3A": "Farm (regular)",
    "3B": "Farm (qualified)",
    "4A": "Commercial",
    "4B": "Industrial",
    "4C": "Apartment (5+ units)",
    "5A": "Railroad (class I)",
    "5B": "Railroad (class II)",
    "6A": "Telephone/telegraph",
    "6B": "Petroleum refinery",
}

ENTITY_RE = re.compile(
    r"\b(L\.?L\.?C|INC|CORP(ORATION)?|L\.?P|LTD|COMPANY|CO\b|ASSOCIATES|PARTNERS(HIP)?|HOLDINGS|"
    r"REALTY|PROPERTIES|ENTERPRISES|DEVELOPMENT|MANAGEMENT|INVESTMENTS?|GROUP|TRUST\b)",
    re.I,
)


def class_label(prop_class: str | None) -> str:
    if not prop_class:
        return "Unknown"
    pc = prop_class.strip().upper()
    if pc.startswith("15"):
        return "Exempt"
    return CLASS_LABELS.get(pc, pc)


def looks_like_entity(name: str | None) -> bool:
    return bool(name and ENTITY_RE.search(name))


# ---------------------------------------------------------------- block / lot extraction

_NUM = r"([0-9]+(?:\.[0-9]+)?[A-Z]?)"
_BLOCK_RE = re.compile(r"\bBLOCKS?\b(?:\(S\))?\.?\s*(?:NO\.?|NUMBER|#)?\s*:?\s*" + _NUM, re.I)
_LOT_RE = re.compile(r"\bLOTS?\b(?:\(S\))?\.?\s*(?:NO\.?|NUMBER|#)?\s*:?\s*" + _NUM, re.I)
_QUAL_RE = re.compile(r"\bQUAL(?:IFIER)?\.?\s*(?:NO\.?|#)?\s*:?\s*(C[0-9A-Z]{3,6}|[0-9A-Z]{4,6})\b", re.I)


def _first_good(regex: re.Pattern, text: str) -> str | None:
    for m in regex.finditer(text):
        before = text[max(0, m.start() - 18):m.start()].upper()
        after = text[m.end():m.end() + 8].upper()
        if "DIMENSION" in before or "SIZE OF" in before or "F/K/A" in before[-6:] or "FKA" in before[-5:]:
            continue
        if re.match(r"\s*(X\b|'|FT\b|FEET\b|FOOT\b)", after):
            continue
        return m.group(1).upper()
    return None


def parse_block_lot(*texts: str | None) -> tuple[str | None, str | None, str | None]:
    """Find (block, lot, qualifier) in the parcel field and/or legal description.

    Handles the formats seen on CivilView, e.g.
      'TAX LOT 3.04 QUALIFIER C0304 FKA C003D BLOCK 40 FKA BLOCK 40.01'
      'TAX LOT: 24 F/K/A 8.A AND L TAX BLOCK: 21101 F/K/A 1978'
      'LOT 5, BLOCK 112'      'Tax Lot No. 4 Block 233.03 fka 24D'
    """
    block = lot = qual = None
    for text in texts:
        if not text:
            continue
        block = block or _first_good(_BLOCK_RE, text)
        lot = lot or _first_good(_LOT_RE, text)
        if qual is None:
            m = _QUAL_RE.search(text)
            if m:
                qual = m.group(1).upper()
        if block and lot:
            break
    return block, lot, qual


def _good_matches(regex: re.Pattern, text: str) -> list[tuple[int, str]]:
    out = []
    for m in regex.finditer(text):
        before = text[max(0, m.start() - 18):m.start()].upper()
        after = text[m.end():m.end() + 8].upper()
        if "DIMENSION" in before or "SIZE OF" in before or "F/K/A" in before[-6:] or "FKA" in before[-5:]:
            continue
        if re.match(r"\s*(X\b|'|FT\b|FEET\b|FOOT\b)", after):
            continue
        out.append((m.start(), m.group(1).upper()))
    return out


def block_lot_pairs(text: str | None) -> list[tuple[int, str, str]]:
    """Every (position, block, lot) a notice names, pairing each block with the nearest lot."""
    if not text:
        return []
    blocks, lots = _good_matches(_BLOCK_RE, text), _good_matches(_LOT_RE, text)
    out = []
    for pos, b in blocks:
        near = [(abs(lp - pos), l) for lp, l in lots if abs(lp - pos) <= 120]
        if near:
            out.append((pos, b, min(near)[1]))
    return out


def stated_block_lot(street: str | None, *texts: str | None) -> tuple[str | None, str | None, str | None]:
    """The block/lot the filing gives for this property. A notice that sells several properties
    names several; take the one written next to this property's own street address."""
    block, lot, qual = parse_block_lot(*texts)
    house = re.match(r"\s*(\d+)", street or "")
    word = re.sub(r"^\s*[\d\-A-Z]*\d[\d\-A-Z]*\s+", "", (street or "").upper()).split(" ")[0] if street else ""
    for text in texts:
        pairs = block_lot_pairs(text)
        if len({(b, l) for _, b, l in pairs}) <= 1 or not house:
            continue
        up = text.upper()
        hits = [m.start() for m in re.finditer(rf"\b{house.group(1)}\s+{re.escape(word[:5])}" if word else rf"\b{house.group(1)}\b", up)]
        if not hits:
            continue
        after = [(pos - hits[0], b, l) for pos, b, l in pairs if pos >= hits[0]]
        if after:
            _, b, l = min(after)
            return b, l, qual
    return block, lot, qual


def lot_named(lot: str | None, *texts: str | None) -> bool:
    """True when a text names this lot number in a lot list ('Lots 7.01 & 8', 'Tax lot 7.01 X Lot 8')."""
    if not lot:
        return False
    num = re.escape(str(lot).upper().removesuffix(".00").removesuffix(".0"))
    pat = re.compile(rf"\bLOTS?\b[^;:]{{0,40}}?(?<![\d.]){num}(?:\.0+)?(?![\d.]*\d)|(?<![\d.]){num}(?:\.0+)?\s+(?:X|&|AND)\s+LOTS?\b", re.I)
    return any(t and pat.search(t) for t in texts)


_LOT_SEG_RE = re.compile(
    r"\bLOT(?:\(S\)|S)?\b\.?\s*(?:NOS?\.?|NUMBERS?|#)?\s*:?\s*"
    r"(.{1,90}?)(?=\bTAX\s+BLOCK|\bBLOCKS?\b|\bIN\s+BLOCK|;|\bTAX\s+MAP|\bDIMENSION|\bQUAL|\bF/?K/?A\b|\bCITY\b|\bTOWN|\bBOROUGH|$)",
    re.I)
_LOT_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9.*X])L?([0-9]+(?:\.[0-9]+)?)(?:\s*(?:-|THROUGH|THRU|TO)\s*([0-9]+(?:\.[0-9]+)?))?(?![0-9]*\s*(?:\*|X\b|'|FT\b|FEET))(?![0-9.]*[A-Z])",
    re.I)


def parse_lot_list(*texts: str | None) -> list[str]:
    """Every tax lot a sheriff's notice names, in order, e.g.
       'Lot 2 and Lots 3 through 7, Block 86'      -> 2, 3, 4, 5, 6, 7
       'LOTS 27, 28, 29, 30 AND 31 BLOCK 186'      -> 27 … 31
       'Tax Lot(s): 11&12 in Block: 2818'          -> 11, 12
       'LOT: 2.03 ADTL LOTS: L2.04 BLOCK: 87.01'   -> 2.03, 2.04
    Lots after 'f/k/a' (former numbers) and dimensions are ignored. Uses the first text that
    names a lot, so a lot list and a legal description aren't mixed."""
    for text in texts:
        if not text or re.search(r"\bTHENCE\b", text, re.I):   # metes and bounds name neighbours' lots
            continue
        lots: list[str] = []
        for m in _LOT_SEG_RE.finditer(text):
            before = text[max(0, m.start() - 8):m.start()].upper()
            if "FKA" in before or "F/K/A" in before or "DIMENSION" in text[max(0, m.start() - 20):m.start()].upper():
                continue
            for t in _LOT_TOKEN_RE.finditer(m.group(1).upper()):
                a, b = t.group(1), t.group(2)
                if b and a.isdigit() and b.isdigit() and 0 < int(b) - int(a) <= 30:
                    lots += [str(i) for i in range(int(a), int(b) + 1)]
                else:
                    lots += [a] + ([b] if b else [])
        if lots:
            return list(dict.fromkeys(lots))
    return []


# ---------------------------------------------------------------- matching

@dataclass
class ParcelMatch:
    pams_pin: str
    muni: str
    block: str
    lot: str
    qual: str
    prop_class: str
    class_label: str
    prop_loc: str
    net_value: int | None
    land_val: int | None
    imprvt_val: int | None
    last_yr_tax: float | None
    bldg_desc: str
    land_desc: str
    calc_acre: float | None
    yr_constr: int | None
    sale_price: int | None
    deed_date: str | None
    match_method: str

    @classmethod
    def from_attrs(cls, a: dict, method: str) -> "ParcelMatch":
        return cls(
            pams_pin=a.get("PAMS_PIN") or "",
            muni=(a.get("MUN_NAME") or "").title(),
            block=a.get("PCLBLOCK") or "",
            lot=a.get("PCLLOT") or "",
            qual=a.get("PCLQCODE") or "",
            prop_class=(a.get("PROP_CLASS") or "").strip().upper(),
            class_label=class_label(a.get("PROP_CLASS")),
            prop_loc=a.get("PROP_LOC") or "",
            net_value=a.get("NET_VALUE"),
            land_val=a.get("LAND_VAL"),
            imprvt_val=a.get("IMPRVT_VAL"),
            last_yr_tax=a.get("LAST_YR_TX"),
            bldg_desc=a.get("BLDG_DESC") or "",
            land_desc=a.get("LAND_DESC") or "",
            calc_acre=a.get("CALC_ACRE"),
            yr_constr=a.get("YR_CONSTR"),
            sale_price=a.get("SALE_PRICE"),
            deed_date=format_deed_date(a.get("DEED_DATE")),
            match_method=method,
        )


def format_deed_date(v: str | None) -> str | None:
    """MOD-IV DEED_DATE is YYMMDD ('880122'). Returns ISO date or None."""
    if not v or not re.fullmatch(r"\d{6}", v.strip()):
        return None
    yy, mm, dd = int(v[:2]), int(v[2:4]), int(v[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    year = 2000 + yy if yy <= 50 else 1900 + yy
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _q(s: str) -> str:
    return s.replace("'", "''")


def _house_and_street(street: str) -> tuple[str | None, str | None]:
    s = re.sub(r"\b(UNIT|APT|SUITE|STE|#)\s*\S+.*$", "", street.upper()).strip()
    m = re.match(r"^\s*([0-9]+[A-Z]?(?:-[0-9]+[A-Z]?)?)\s+(.+)$", s)
    if not m:
        return None, None
    words = [w for w in re.split(r"\s+", m.group(2)) if w not in {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}]
    return m.group(1), (words[0] if words else None)


def _ring_area_centroid(ring: list) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    a /= 2
    if abs(a) < 1e-14:
        n = len(ring)
        return 0.0, sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n
    return abs(a), cx / (6 * a), cy / (6 * a)


def _inside(x: float, y: float, ring: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def polygon_label_point(rings: list) -> tuple[float, float] | None:
    """A point (x, y) inside the parcel's largest piece: its area centroid, or — for L- or
    U-shaped lots where the centroid falls outside — the middle of the widest stretch of the
    lot along the centroid's latitude. (Averaging the vertices, as before, drifts toward
    whichever side of the lot has the most survey points.)"""
    rings = [[tuple(p[:2]) for p in r] for r in rings if len(r) >= 3]
    if not rings:
        return None
    area, cx, cy = max((_ring_area_centroid(r) + (r,) for r in rings), key=lambda t: t[0])[:3]
    ring = max(rings, key=lambda r: _ring_area_centroid(r)[0])
    if _inside(cx, cy, ring):
        return cx, cy
    xs = sorted((x2 - x1) * (cy - y1) / (y2 - y1) + x1
                for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]) if (y1 > cy) != (y2 > cy))
    spans = [(xs[i + 1] - xs[i], (xs[i] + xs[i + 1]) / 2) for i in range(0, len(xs) - 1, 2)]
    return (max(spans)[1], cy) if spans else (cx, cy)


def _same_block(a: str | None, b: str | None) -> bool:
    """Blocks match ignoring padding and trailing '.00' (e.g. '106' vs '106.00')."""
    norm = lambda x: re.sub(r"\.0+$", "", (x or "").strip().upper())
    return norm(a) == norm(b) and norm(a) != ""


class ParcelClient:
    def __init__(self, timeout: int = 30, delay: float = 0.3, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.timeout = timeout
        self.delay = delay

    def query(self, where: str, limit: int = 25) -> list[dict]:
        params = {
            "where": where,
            "outFields": ",".join(OUT_FIELDS),
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": str(limit),
        }
        for attempt in range(1, 4):
            try:
                time.sleep(self.delay)
                r = self.s.get(PARCEL_SERVICE, params=params, timeout=self.timeout)
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(j["error"])
                return [f["attributes"] for f in j.get("features", [])]
            except Exception as e:  # noqa: BLE001 - network/service errors are retried
                if attempt == 3:
                    log.warning("Parcel query failed (%s): %s", where, e)
                    return []
                time.sleep(2 ** attempt)
        return []

    def centroid(self, pams_pin: str) -> tuple[float, float] | None:
        """Approximate parcel centroid (lat, lon) from its polygon, for maps."""
        params = {
            "where": f"PAMS_PIN='{_q(pams_pin)}'",
            "outFields": "PAMS_PIN",
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": "6",
            "f": "json",
            "resultRecordCount": "1",
        }
        try:
            time.sleep(self.delay)
            r = self.s.get(PARCEL_SERVICE, params=params, timeout=self.timeout)
            r.raise_for_status()
            feats = r.json().get("features", [])
            rings = feats[0]["geometry"]["rings"] if feats else []
            pt = polygon_label_point(rings)
            return (round(pt[1], 6), round(pt[0], 6)) if pt else None
        except Exception as e:  # noqa: BLE001
            log.warning("Centroid lookup failed for %s: %s", pams_pin, e)
            return None

    def sibling_lots(self, primary_pin: str, block: str, lots: list[str]) -> list[dict]:
        """Tax parcels for the other lots a notice names, in the same town and block as the
        matched parcel (the PAMS_PIN starts with the municipality code)."""
        mun = primary_pin.split("_")[0]
        want = [l for l in lots if l]
        if not mun or not block or not want:
            return []
        in_list = ",".join(f"'{_q(l)}'" for l in want[:40])
        where = f"PAMS_PIN LIKE '{_q(mun)}\\_%' ESCAPE '\\' AND PCLBLOCK='{_q(block)}' AND PCLLOT IN ({in_list})"
        rows = self.query(where, limit=100)
        if not rows:   # some services don't honour ESCAPE; the block filter keeps it to one town anyway
            rows = [r for r in self.query(f"PAMS_PIN LIKE '{_q(mun)}%' AND PCLBLOCK='{_q(block)}' "
                                          f"AND PCLLOT IN ({in_list})", limit=100)
                    if (r.get("PAMS_PIN") or "").startswith(mun + "_")]
        # Whole lots only: a condo building's unit records share the lot number.
        base = [r for r in rows if not (r.get("PCLQCODE") or "").strip()]
        return base or rows

    def lookup(self, county: str, city: str, street: str, block: str | None,
               lot: str | None, qual: str | None) -> ParcelMatch | None:
        county_u = _q(county.upper().replace(" COUNTY", "").strip())
        city_u = _q((city or "").upper().strip())
        house, word = _house_and_street(street or "")

        def pick(cands: list[dict], method: str) -> ParcelMatch | None:
            if not cands:
                return None
            if qual:
                for c in cands:
                    if (c.get("PCLQCODE") or "").strip().upper() == qual:
                        return ParcelMatch.from_attrs(c, method + "+qual")
            if house:
                hits = [c for c in cands if (c.get("PROP_LOC") or "").upper().startswith(house + " ")]
                if len(hits) >= 1:
                    cands = hits
            # Prefer the main parcel (no condo qualifier) over individual units.
            base = [c for c in cands if not (c.get("PCLQCODE") or "").strip()]
            return ParcelMatch.from_attrs((base or cands)[0], method)

        # 1) Block + lot within the town named in the address.
        if block and lot:
            where = f"COUNTY='{county_u}' AND PCLBLOCK='{_q(block)}' AND PCLLOT='{_q(lot)}'"
            if qual:
                # Condo buildings can have hundreds of units on one block/lot; ask for the unit directly.
                hit = self.query(where + f" AND PCLQCODE='{_q(qual)}'", limit=5)
                if city_u:
                    hit = [c for c in hit if (c.get("MUN_NAME") or "").upper().startswith(city_u)] or hit
                if len({c.get("MUN_NAME") for c in hit}) == 1:
                    return ParcelMatch.from_attrs(hit[0], "block/lot+qual")
            if city_u:
                m = pick(self.query(where + f" AND MUN_NAME LIKE '{city_u}%'"), "block/lot")
                if m:
                    return m
            # Postal city often differs from the tax municipality; fall back county-wide and
            # require the house number to agree.
            cands = self.query(where)
            if house:
                cands = [c for c in cands if (c.get("PROP_LOC") or "").upper().startswith(house + " ")]
            m = pick(cands, "block/lot (county-wide)")
            if m:
                return m

        # 1b) Block only (lot missing from the notice) + house number.
        if block and not lot and house:
            where = (f"COUNTY='{county_u}' AND PCLBLOCK='{_q(block)}' "
                     f"AND PROP_LOC LIKE '{_q(house)} %'")
            cands = self.query(where)
            if city_u:
                cands = [c for c in cands if (c.get("MUN_NAME") or "").upper().startswith(city_u)] or \
                    ([] if len({c.get("MUN_NAME") for c in cands}) > 1 else cands)
            if word:
                cands = [c for c in cands if word[:4] in (c.get("PROP_LOC") or "").upper()]
            m = pick(cands, "block+address")
            if m:
                return m

        # 2) House number + first street word. If the notice gives a block, the parcel must be
        #    on that block — otherwise the same address in the wrong place could match.
        if house and word:
            where = f"COUNTY='{county_u}' AND PROP_LOC LIKE '{_q(house)} {_q(word[:6])}%'"
            cands = self.query(where)
            if block:
                cands = [c for c in cands if _same_block(c.get("PCLBLOCK"), block)]
            if city_u:
                same = [c for c in cands if (c.get("MUN_NAME") or "").upper().startswith(city_u)]
                if same:
                    return pick(same, "address")
            if len({c.get("MUN_NAME") for c in cands}) == 1:
                return pick(cands, "address")
        return None
