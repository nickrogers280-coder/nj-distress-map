"""ParcelClient.lookup against canned responses shaped like the live NJOGIS service
(values captured from real queries on 2026-11-13)."""
from tracker.parcels import ParcelClient

NB_BASE = {"PAMS_PIN": "0908_40_3.04", "COUNTY": "HUDSON", "MUN_NAME": "NORTH BERGEN TWP", "PCLBLOCK": "40",
           "PCLLOT": "3.04", "PCLQCODE": "", "PROP_CLASS": "15F", "PROP_LOC": "1906 PATERSON PLK", "NET_VALUE": 0}
NB_UNIT = {**NB_BASE, "PAMS_PIN": "0908_40_3.04_C0304", "PCLQCODE": "C0304", "PROP_CLASS": "2", "NET_VALUE": 252600}
JC = {"PAMS_PIN": "0906_21101_24", "COUNTY": "HUDSON", "MUN_NAME": "JERSEY CITY CITY", "PCLBLOCK": "21101",
      "PCLLOT": "24", "PCLQCODE": "", "PROP_CLASS": "2", "PROP_LOC": "333 FORREST ST.", "NET_VALUE": 405400}
WCM = {"PAMS_PIN": "0512_56_11", "COUNTY": "CAPE MAY", "MUN_NAME": "WEST CAPE MAY BORO", "PCLBLOCK": "56",
       "PCLLOT": "11", "PCLQCODE": " ", "PROP_CLASS": "4A", "PROP_LOC": "12 STIMPSON LANE", "NET_VALUE": 2058200}


def fake_client(responses):
    c = ParcelClient()
    c.calls = []

    def query(where, limit=25):
        c.calls.append(where)
        for needle, rows in responses:
            if all(n in where for n in needle):
                return rows
        return []
    c.query = query
    return c


def test_condo_unit_via_qualifier():
    c = fake_client([(("PCLQCODE='C0304'",), [NB_UNIT]), (("PCLBLOCK='40'",), [NB_BASE])])
    m = c.lookup("Hudson", "NORTH BERGEN", "1906 PATERSON PLANK ROAD UNIT 3D", "40", "3.04", "C0304")
    assert m.pams_pin == "0908_40_3.04_C0304" and m.prop_class == "2" and m.match_method == "block/lot+qual"


def test_block_lot_in_town():
    c = fake_client([(("PCLBLOCK='21101'", "MUN_NAME LIKE 'JERSEY CITY%'"), [JC])])
    m = c.lookup("Hudson", "JERSEY CITY", "333 FORREST STREET", "21101", "24", None)
    assert m.muni == "Jersey City City" and m.net_value == 405400 and m.match_method == "block/lot"


def test_postal_city_mismatch_falls_back_county_wide():
    # Address says "CAPE MAY" but the parcel is in West Cape May borough.
    c = fake_client([(("PCLBLOCK='56'", "MUN_NAME LIKE 'CAPE MAY%'"), []), (("PCLBLOCK='56'",), [WCM])])
    m = c.lookup("Cape May County", "CAPE MAY", "12 STIMPSON LANE", "56", "11", None)
    assert m.pams_pin == "0512_56_11" and m.class_label == "Commercial"
    assert m.match_method == "block/lot (county-wide)"
    assert "COUNTY='CAPE MAY'" in c.calls[0]


def test_address_fallback_when_no_block_lot():
    c = fake_client([(("PROP_LOC LIKE '333 FORRES%'",), [JC])])
    m = c.lookup("Hudson", "JERSEY CITY", "333 FORREST STREET", None, None, None)
    assert m.pams_pin == "0906_21101_24" and m.match_method == "address"


def test_no_match():
    c = fake_client([])
    assert c.lookup("Hudson", "BAYONNE", "88 HARBOR DRIVE", None, None, None) is None
