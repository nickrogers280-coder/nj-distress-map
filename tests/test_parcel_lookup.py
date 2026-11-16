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


def test_address_match_must_sit_on_the_noticed_block():
    """113 E Main St, block 106 per the notice: a 113 Main St on block 5060 elsewhere is rejected."""
    from tracker.parcels import ParcelClient

    class PC(ParcelClient):
        def __init__(self):
            pass

        def query(self, where, limit=25):
            if "PROP_LOC LIKE '113 MAIN" in where:
                return [{"PAMS_PIN": "1608_5060_28", "MUN_NAME": "PATERSON CITY", "PCLBLOCK": "5060",
                         "PCLLOT": "28", "PROP_CLASS": "1", "PROP_LOC": "113 MAIN STREET"}]
            return []

    assert PC().lookup("Passaic", "PATERSON", "113 E Main Street", "106", None, None) is None
    assert PC().lookup("Passaic", "PATERSON", "113 E Main Street", None, None, None).block == "5060"


def test_label_point_stays_inside_l_shaped_lot():
    from tracker.parcels import _inside, polygon_label_point
    # An L: wide bottom bar plus a tall left arm. The vertex average falls outside it.
    ring = [(0, 0), (10, 0), (10, 2), (2, 2), (2, 10), (0, 10), (0, 0)]
    x, y = polygon_label_point([ring])
    assert _inside(x, y, ring)
    # A plain rectangle with lots of survey points on one side still centres correctly.
    rect = [(0, 0), (4, 0), (4, 2)] + [(4 - i * 0.01, 2) for i in range(1, 300)] + [(0, 2), (0, 0)]
    x, y = polygon_label_point([rect])
    assert abs(x - 2) < 1e-6 and abs(y - 1) < 1e-6
