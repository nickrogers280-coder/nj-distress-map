"""County clerk adapters: index rows to lis pendens groups."""
import re
from datetime import date

from tracker import clerks


class FakeSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=0):
        class R:
            def __init__(self, data):
                self._d = data
            def raise_for_status(self):
                pass
            def json(self):
                return self._d
        if url.endswith("doctypes"):
            return R([{"name": "NOTICE OF LIS PENDENS", "value": "NLP"}, {"name": "DISCHARGE OF LIS PENDENS", "value": "DLP"},
                      {"name": "DEED", "value": "DEED"}])
        if url.endswith("commonTowns"):
            return R([{"common_town_value": "TRENTON"}, {"common_town_value": "EWING TOWNSHIP"}])
        return R({})

    def post(self, url, json=None, timeout=0):
        self.last = json
        class R:
            def raise_for_status(self):
                pass
            def json(self):
                if json.get("DocTypes") == "NLP":
                    return [{"doc_id": 1, "party_code": "R", "party_name": "162 BARNT", "cross_party_name": "METROPOLITAN LIFE INS CO",
                             "rec_date": "2026-10-22T00:00:00", "doc_type": "NLP", "town": "TRE", "lot": "16", "block": "18601",
                             "legal_1": "162 BARNT AVE", "file_num": "2026041056"},
                            {"doc_id": 1, "party_code": "D", "party_name": "ACME REALTY LLC", "cross_party_name": "METROPOLITAN LIFE INS CO",
                             "rec_date": "2026-10-22T00:00:00", "doc_type": "NLP", "town": "TRE", "lot": "16", "block": "18601",
                             "legal_1": "162 BARNT AVE", "file_num": "2026041056"}]
                return []
        return R()


def test_newvision_groups_carry_block_lot_and_parties():
    a = clerks.NewVisionAdapter(1100, "Mercer", "https://x/publicsearch/", session=FakeSession())
    gs = a.lp_groups(date(2026, 10, 1), date(2026, 10, 30), None, None, None)
    assert len(gs) == 1
    g = gs[0]
    assert g["instrument"] == "2026041056" and g["block"] == "18601" and g["lot"] == "16" and g["street"] == "162 BARNT AVE"
    assert g["town"] == "TRENTON" and g["plaintiffs"] == ["METROPOLITAN LIFE INS CO"] and g["defendants"] == ["ACME REALTY LLC"]
    assert g["doc_type"] == "NOTICE OF LIS PENDENS"


def test_union_results_parse():
    text = ("Search by Date Results\nLis Pendens\tDefendant\tCASTILLO, SILVIO J\t10/23/2026\t \nComplainant\tFCR TL TRUST\t62/535\n"
            "Lis Pendens Discharge\tComplainant\tFREEDOM MORTGAGE CORPORATION\t10/21/2026\t \nDefendant\tMUNOZ, JORGE\t49/614\n")
    viewers = ["https://clerk.ucnj.org/UCPA/Viewer?id=CAAB6GTS&dt=Lis%20Pendens&in=808&bp=62/535"]
    a, d = clerks.UnionAdapter.parse_results(text, viewers)
    assert a["instrument"] == "62/535" and a["recorded"] == "2026-10-23" and a["viewer"].endswith("bp=62/535")
    assert a["defendants"] == ["CASTILLO, SILVIO J"] and a["plaintiffs"] == ["FCR TL TRUST"]
    assert d["doc_type"] == "Lis Pendens Discharge" and d["defendants"] == ["MUNOZ, JORGE"] and d["viewer"] is None
