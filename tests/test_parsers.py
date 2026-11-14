from pathlib import Path

import pytest

from tracker.civilview import parse_detail, parse_list, split_address
from tracker.parcels import class_label, format_deed_date, looks_like_entity, parse_block_lot
from tracker.pipeline import first_defendant

FX = Path(__file__).parent / "fixtures"


def test_parse_list_hudson_column_order():
    rows = parse_list((FX / "list_hudson.html").read_text(), 10)
    assert len(rows) == 3
    r = rows[0]
    assert r.property_id == "2243981658"
    assert r.sheriff_number == "20052781"
    assert r.sale_date == "2026-11-19"
    assert r.address.startswith("1906 PATERSON PLANK ROAD")
    assert r.plaintiff.startswith("U.S. BANK TRUST")
    assert r.defendant.startswith("SYED KHABOOR")


def test_parse_list_monmouth_status_column():
    rows = parse_list((FX / "list_monmouth.html").read_text(), 8)
    assert len(rows) == 1
    assert rows[0].status == "Adjourned - Plaintiff"
    assert rows[0].sale_date == "2027-01-07"
    assert rows[0].address == "1200 HIGHWAY 35 MIDDLETOWN NJ 07748"


def test_parse_detail_hudson():
    d = parse_detail((FX / "detail_hudson.html").read_text(), "2243981658")
    assert d.county == "Hudson"
    assert d.court_case == "F-003702-23"
    assert d.sale_date == "2026-11-19"
    assert d.judgment == 43507.16
    assert d.upset == 55000.0
    assert (d.street, d.city, d.zip) == ("1906 PATERSON PLANK ROAD UNIT 3D", "NORTH BERGEN", "07047")
    assert d.attorney.startswith("ROBERTSON, ANSCHUTZ")
    assert d.attorney_phone == "973-575-0707"
    assert len(d.status_history) == 5
    assert d.status_history[1] == ("ADJOURNED SHERIFF - New Sale Date", "2023-11-16")
    assert d.latest_status == "POST BANKRUPTCY SALE DATE"


def test_parse_detail_label_variants_and_duplicate_history():
    d = parse_detail((FX / "detail_middlesex.html").read_text(), "1")
    assert d.county == "Middlesex"
    assert d.sale_date == "2025-02-05"
    assert d.judgment == 8412550.0
    assert d.upset == 9100000.0          # "Minimum Bid"
    assert d.city == "WOODBRIDGE"
    assert d.parcel_text.startswith("Tax Lot No. 4")
    assert d.attorney_file == "XX-1234"
    assert d.extra == {"occupancy": "Commercial"}
    assert d.status_history == [("Scheduled", "2025-01-08"), ("Adjourned - Plaintiff", "2025-02-05")]


@pytest.mark.parametrize("text,expected", [
    ("TAX LOT 3.04 QUALIFIER C0304 FKA C003D BLOCK 40 FKA BLOCK 40.01 DIMENSIONS OF LOT: TOGETHER",
     ("40", "3.04", "C0304")),
    ("TAX LOT: 24 F/K/A 8.A AND L TAX BLOCK: 21101 F/K/A 1978 APPROXIMATE DIMENSIONS: 100.00' X 26.94'",
     ("21101", "24", None)),
    ("LOT 5, BLOCK 112", ("112", "5", None)),
    ("LOT: 16 BLOCK: 5804 OCCUPANCY STATUS: BORROWER OCCUPIED", ("5804", "16", None)),
    ("Tax Lot No. 4 Block 233.03 fka 24D", ("233.03", "4", None)),
    ("Dimensions of Lot (Approximately) 55 feet wide by 92 feet long", (None, None, None)),
    ("DIMENSIONS OF LOT 100 X 25; BLOCK 7 LOT 12", ("7", "12", None)),
])
def test_parse_block_lot(text, expected):
    assert parse_block_lot(text) == expected


def test_parse_block_lot_prefers_parcel_field():
    assert parse_block_lot("Tax Lot No. 4 Block 233.03", "BLOCK 999 LOT 1") == ("233.03", "4", None)


def test_split_address_single_line():
    assert split_address("455 TONNELLE AVENUE JERSEY CITY NJ 07307") == (
        "455 TONNELLE AVENUE JERSEY CITY NJ 07307", "", "07307")


def test_first_defendant_and_entity_detection():
    assert first_defendant("SYED KHABOOR; SAIRA KHABOOR; HACKENSACK UNIVERSITY MED CTR") == "SYED KHABOOR"
    assert first_defendant("HARBOR DRIVE PARTNERS, LLC, ET AL.") == "HARBOR DRIVE PARTNERS, LLC"
    assert first_defendant("ROBERT RAMIREZ, JERSEY SHORE UNIVERSITY") == "ROBERT RAMIREZ"
    assert looks_like_entity("TONNELLE HOLDINGS LLC")
    assert not looks_like_entity("SYED KHABOOR")
    # junior creditor LLC later in the list must not make an individual look like an entity
    assert not looks_like_entity(first_defendant("JANE SMITH; MIDLAND FUNDING LLC"))


def test_class_and_deed_helpers():
    assert class_label("4A") == "Commercial"
    assert class_label("15F") == "Exempt"
    assert format_deed_date("880122") == "1988-01-22"
    assert format_deed_date("190305") == "2019-03-05"
    assert format_deed_date("") is None
