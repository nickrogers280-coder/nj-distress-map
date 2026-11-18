"""Bank-owned (REO) history from New Jersey's statewide deed-sale files (SR-1A).

The Division of Taxation publishes every recorded deed each year as a fixed-width file. Names
are blanked (Daniel's Law), but each deed carries a non-usable code that says what kind of
transfer it was (N.J.A.C. 18:12-1.1):

    12  sheriff's sale            18  deed in lieu of foreclosure
    31  first sale after foreclosure by a bank (the lender selling its REO)

At a New Jersey sheriff's sale the foreclosing lender usually takes the property back with a
nominal bid (commonly $100), while an outside buyer pays a real price. So a code-12 deed at a
nominal price, or a code-18 deed, is the lender taking the property; a later code-31 deed on the
same parcel is the lender selling it on.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, asdict
from datetime import date

log = logging.getLogger(__name__)

STATE_URL = "https://www.nj.gov/treasury/taxation/lpt/statdata/Sales{year}.zip"
YTD_URL = "https://www.nj.gov/treasury/taxation/lpt/statdata/YTDSR1A{year}.zip"
# Older years are no longer on the state site; the Internet Archive kept some copies.
ARCHIVE_URL = "https://web.archive.org/web/2022id_/https://www.nj.gov/treasury/taxation/lpt/statdata/Sales{year}.zip"

NOMINAL_PRICE = 1000          # a sheriff's deed at or under this is the lender's credit bid
TRACKED_CLASSES = {"1", "4A", "4B", "4C"}
NU_SHERIFF, NU_IN_LIEU, NU_REO_RESALE = "12", "18", "31"

COUNTY_CODES = {"01": "Atlantic", "02": "Bergen", "03": "Burlington", "04": "Camden", "05": "Cape May",
                "06": "Cumberland", "07": "Essex", "08": "Gloucester", "09": "Hudson", "10": "Hunterdon",
                "11": "Mercer", "12": "Middlesex", "13": "Monmouth", "14": "Morris", "15": "Ocean",
                "16": "Passaic", "17": "Salem", "18": "Somerset", "19": "Sussex", "20": "Union", "21": "Warren"}


@dataclass
class Sale:
    district: str          # county + municipality code, e.g. "0706" (also the start of a PAMS_PIN)
    block: str
    lot: str
    qual: str
    prop_class: str
    nu: str                # state non-usable code ('' if usable)
    assessor_nu: str
    price: int
    deed_date: str | None  # ISO
    recorded: str | None
    location: str
    assessed: int | None

    @property
    def county(self) -> str:
        return COUNTY_CODES.get(self.district[:2], self.district[:2])

    @property
    def pin(self) -> str:
        return f"{self.district}_{self.block}_{self.lot}" + (f"_{self.qual}" if self.qual else "")

    def as_dict(self) -> dict:
        return asdict(self)


def _f(line: str, a: int, b: int) -> str:
    """Field by the layout's 1-based, inclusive positions."""
    return line[a - 1:b].strip()


def _num(s: str) -> int | None:
    s = re.sub(r"[^0-9]", "", s or "")
    return int(s) if s else None


def _ymd(mmddyy: str) -> str | None:
    if not re.fullmatch(r"\d{6}", mmddyy or ""):
        return None
    mm, dd, yy = int(mmddyy[:2]), int(mmddyy[2:4]), int(mmddyy[4:])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return date(year, mm, dd).isoformat()
    except ValueError:
        return None


def _part(main: str, suffix: str) -> str:
    """'00289' + '01' -> '289.01' (the PAMS_PIN spelling); '00014' + '' -> '14'."""
    m = (main or "").lstrip("0") or ("0" if main else "")
    suf = (suffix or "").strip().lstrip(".")
    if suf and re.fullmatch(r"\d+", suf):
        return f"{m}.{suf.zfill(2)}"
    return f"{m}{suf}" if suf else m


def parse_line(line: str) -> Sale | None:
    if len(line) < 640:
        return None
    district = _f(line, 1, 4)
    if not re.fullmatch(r"\d{4}", district):
        return None
    return Sale(
        district=district,
        block=_part(_f(line, 351, 355), _f(line, 356, 359)),
        lot=_part(_f(line, 360, 364), _f(line, 365, 368)),
        qual="",
        prop_class=_f(line, 627, 628).upper().lstrip("0") or _f(line, 627, 628),
        nu=_f(line, 35, 37).lstrip("0").zfill(2) if _f(line, 35, 37) else "",
        assessor_nu=_f(line, 638, 640).lstrip("0").zfill(2) if _f(line, 638, 640) else "",
        price=_num(_f(line, 38, 46)) or 0,
        deed_date=_ymd(_f(line, 339, 344)),
        recorded=_ymd(_f(line, 345, 350)),
        location=re.sub(r"\s+", " ", _f(line, 298, 322)),
        assessed=_num(_f(line, 74, 82)),
    )


def iter_sales(zip_bytes: bytes):
    """Note: the file named for a year mostly holds deeds from the year before (July to June)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".txt"):
                continue
            with z.open(name) as fh:
                for raw in io.TextIOWrapper(fh, encoding="latin-1", newline=""):
                    s = parse_line(raw.rstrip("\r\n"))
                    if s:
                        yield s


def classify_sale(s: Sale) -> str | None:
    """'reo' (lender took it), 'reo_resale' (lender sold it), or None."""
    codes = {s.nu, s.assessor_nu}
    if NU_IN_LIEU in codes:
        return "reo"
    if NU_SHERIFF in codes and s.price <= NOMINAL_PRICE:
        return "reo"
    if NU_REO_RESALE in codes:
        return "reo_resale"
    return None


def reo_events(sales, since: str = "2018-01-01") -> list[dict]:
    """Collapse a stream of sales into one record per lender take-back of a tracked parcel, with
    the lender's later resale attached when the files show it."""
    takes: dict[str, dict] = {}
    resales: dict[str, list[Sale]] = {}
    for s in sales:
        if s.prop_class not in TRACKED_CLASSES or not s.deed_date or s.deed_date < since:
            continue
        kind = classify_sale(s)
        if kind == "reo":
            cur = takes.get(s.pin)
            if cur is None or s.deed_date > cur["reo_date"]:      # keep the latest take-back per parcel
                takes[s.pin] = {"pin": s.pin, "county": s.county, "district": s.district, "block": s.block,
                                "lot": s.lot, "prop_class": s.prop_class, "reo_date": s.deed_date,
                                "reo_price": s.price, "reo_kind": "deed in lieu" if NU_IN_LIEU in (s.nu, s.assessor_nu)
                                else "sheriff's sale", "location": s.location, "assessed": s.assessed,
                                "recorded": s.recorded}
        elif kind == "reo_resale":
            resales.setdefault(s.pin, []).append(s)
    for pin, t in takes.items():
        later = sorted((r for r in resales.get(pin, []) if r.deed_date and r.deed_date >= t["reo_date"]),
                       key=lambda r: r.deed_date)
        if later:
            t["resale_date"], t["resale_price"] = later[0].deed_date, later[0].price
    return sorted(takes.values(), key=lambda t: t["reo_date"], reverse=True)


def download_years(years: list[int], session=None, include_ytd: bool = True) -> dict[int, bytes]:
    """Fetch the yearly files that exist (state site first, then the archive for old years)."""
    import requests
    s = session or requests.Session()
    out: dict[int, bytes] = {}
    for y in years:
        for url in (STATE_URL.format(year=y), ARCHIVE_URL.format(year=y)):
            try:
                r = s.get(url, timeout=180)
                if r.ok and r.content[:2] == b"PK":
                    out[y] = r.content
                    log.info("SR-1A %s: %s bytes from %s", y, len(r.content), url.split("/")[2])
                    break
            except Exception as e:  # noqa: BLE001
                log.info("SR-1A %s not at %s: %s", y, url, e)
        if y not in out:
            log.warning("SR-1A %s: no file found", y)
    if include_ytd:
        y = max(years) + 1
        try:
            r = s.get(YTD_URL.format(year=y), timeout=180)
            if r.ok and r.content[:2] == b"PK":
                out[y] = r.content
        except Exception as e:  # noqa: BLE001
            log.info("SR-1A year-to-date not fetched: %s", e)
    return out
