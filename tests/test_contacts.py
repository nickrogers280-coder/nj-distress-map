"""Contact extraction from real OCR output (Essex instrument 2022079009, read with Tesseract)."""
from tracker.contacts import (clean_party, lender_profile, merge_facts, parse_document, person_from_index,
                              same_party, search_terms)

PAGE3 = """Inst. # 2022079009 - Page 3 of 11
RECORDATION REQUESTED BY:
Wiimington Savings Fund Society, FSB
WSFS Bank Center
500 Delaware Avenue, 12th Floor
Wilmington, DE 19801
WHEN RECORDED MAIL TO:
Wilmington Savings Fund Society, FSB
WSFS Bank Center
500 Delaware Avenue, 12th Floor
Wilmington, DE 19801
SEND TAX NOTICES TO:
Wilmington Savings Fund Society, FSB
WSFS Bank Center
500 Delaware Avenue, 12th Floor
— Wilmington. OE 99801 SPACE ABOVE THIS LINE IS FOR RECORDER'S USE ONLY
This Mortgage prepared by: X %
Name of Signer: Jennifer Sandner, Small Business Lending Team Leader, “i
I i | i l
i
HHH HHHH0T 45089 5202201
MORTGAGE {
Lender. The word "Lender" means Wilmington Savings Fund Society, FSB, its successors and assigns.
Note. The word "Note" means the promissory note dated August 15, 2022, in the original principal amount of $1,490,000.00
"""

PAGE11 = """Inst. # 2022079009 - Page 11 of 11
MORTGAGE
(Continued) Page 8
GRANTOR ACKNOWLEDGES HAVING READ ALL THE PROVISIONS OF THIS MORTGAGE, AND GRANTOR AGREES TO ITS TERMS.
GRANTOR:
1221 BLOOMFIELD LLC
John Adams, Member of 1221 Bloomfield LLC dig,
Signed, acknowledged and delivered In the presence of: 0 hy
————————
Witness | |
LIMITED LIABILITY COMPANY ACI KNOWLEDGMENT
STATE OF A 2» Jerse . )
BE IT REMEMBERED that on this § Pa of / i od 20 7 A , before me, the
subscriber, personally appeared John Adams, Member of 1221 Bloomfleld LLC, who, | aps shed.” the person who signed the within
instrument, and he or she acknowledged that he or she wag aut horized to and signed the ght
, NotaryPiiblic [Notary] Pus]
"""


def test_real_mortgage_pages():
    f = merge_facts([parse_document(PAGE3), parse_document(PAGE11)])
    assert f.signers[0]["name"] == "John Adams" and f.signers[0]["title"] == "Member"
    assert f.signers[0]["entity"].upper().startswith("1221 BLOOMF")
    assert len(f.signers) == 1                                  # same person twice -> one entry
    assert f.prepared_by.startswith("Jennifer Sandner, Small Business Lending Team Leader")
    assert f.return_to[0].endswith("Savings Fund Society, FSB") and "500 Delaware Avenue, 12th Floor" in f.return_to
    assert f.return_to[-1] == "Wilmington, DE 19801"
    assert f.lender_name == "Wilmington Savings Fund Society, FSB"
    assert f.amount == 1_490_000.0


def test_other_signature_styles():
    t = """By: ______________________
Name: Moshe Klein
Title: Managing Member
Prepared by: Michael R. Smith, Esq.
RECORD AND RETURN TO:
Valley National Bank
1460 Valley Road
Wayne, NJ 07470
"""
    f = parse_document(t)
    assert f.signers == [{"name": "Moshe Klein", "title": "Managing Member", "entity": None}]
    assert f.prepared_by == "Michael R. Smith, Esq"
    assert f.return_to == ["Valley National Bank", "1460 Valley Road", "Wayne, NJ 07470"]
    # Notary and witness boilerplate isn't mistaken for a signer.
    assert parse_document("Notary Public, State of New Jersey\nWitness, Member of the Bar").signers == []


def test_party_names():
    assert clean_party("1221 BLOOMFIELD LLC, A NEW JERSEY LIMITED LIABILITY COMPANY; ET. AL.") == "1221 BLOOMFIELD LLC"
    assert clean_party("429 GETTY LLC AND STATE OF NEW JERSEY") == "429 GETTY LLC"
    assert clean_party("83-89 ROOSEVELT AVE LLC; ET AL.") == "83-89 ROOSEVELT AVE LLC"
    assert search_terms("1221 BLOOMFIELD LLC; ET. AL.") == ["1221 BLOOMFIELD LLC", "1221 BLOOMFIELD"]
    assert search_terms("JOHN DOE, ET ALS")[0] == "DOE JOHN"
    assert person_from_index("ADAMS JOHN") == "John Adams"
    assert person_from_index("ADAMS INTERNATIONAL LLC") is None
    assert person_from_index("NEW JERSEY, STATE OF") is None
    assert person_from_index("FAIRFIELD,TOWNSHIP OF") is None
    assert same_party("1221 BLOOMFIELD LLC", "1221 BLOOMFIELD L.L.C.")


def test_lender_profile():
    p = lender_profile("WILMINGTON TRUST, NATIONAL ASSOCIATION, AS TRUSTEE, FOR THE BENEFIT OF THE HOLDERS OF BENCHMARK "
                       "2018-B1 MORTGAGE TRUST COMMERCIAL MORTGAGE PASS-THROUGH CERTIFICATES, SERIES 2018-B1, BY AND "
                       "THROUGH ITS SPECIAL SERVICER, LNR PARTNERS, LLC")
    assert p["kind"] == "CMBS / securitized loan"
    assert p["servicer"] == {"role": "Special Servicer", "name": "Lnr Partners, Llc"}
    assert p["trustee"] == "Wilmington Trust, National Association"
    assert lender_profile("FANNIE MAE")["kind"].startswith("Agency")
    assert lender_profile("WILMINGTON SAVINGS FUND SOCIETY, FSB.")["kind"] == "Bank"
    assert lender_profile("AVATAR REIT I LLC")["kind"] == "Debt fund / trust"


def test_enrich_one_with_fake_records():
    from tracker.enrich import enrich_one
    from tracker.press import IndexRow

    rows = [
        IndexRow("DEED", "UCF FAIRFIELD LLC", "1221 BLOOMFIELD LLC", "2022079008", "8/24/2022", "FAIRFIELD", "303", "14", "v1"),
        IndexRow("MORTGAGE", "1221 BLOOMFIELD LLC", "WILMINGTON SAVINGS FUND SOCIETY", "2022079009", "8/24/2022", "FAIRFIELD", "N/A", "", "v2"),
        IndexRow("MORTGAGE", "ADAMS JOHN", "WILMINGTON SAVINGS FUND SOCIETY", "2022079009", "8/24/2022", "FAIRFIELD", "N/A", "", "v3"),
        IndexRow("LIS PENDENS FORECLOSURE AND RECOVERY", "1221 BLOOMFIELD LLC", "WILMINGTON SAVINGS FUND SOCIETY FSB", "2026038724", "6/10/2026", "FAIRFIELD", "N/A", "", "v4"),
        IndexRow("LIS PENDENS FORECLOSURE AND RECOVERY", "ADAMS INTERNATIONAL LLC", "WILMINGTON SAVINGS FUND SOCIETY FSB", "2026038724", "6/10/2026", "FAIRFIELD", "N/A", "", "v5"),
        IndexRow("LIS PENDENS FORECLOSURE AND RECOVERY", "NEW JERSEY, STATE OF", "WILMINGTON SAVINGS FUND SOCIETY FSB", "2026038724", "6/10/2026", "FAIRFIELD", "N/A", "", "v6"),
        IndexRow("NOTICE OF SETTLEMENT", "1221 BLOOMFIELD LLC", "VINLUCA, L.L.C.", "2026070276", "10/27/2026", "FAIRFIELD", "N/A", "", "v7"),
        IndexRow("MORTGAGE", "SOMEONE ELSE LLC", "OTHER BANK", "2019000001", "1/2/2019", "NEWARK", "1", "1", "v8"),
    ]

    class Client:
        base = "https://press.example/"
        def search_name(self, name):
            return rows if name == "1221 BLOOMFIELD LLC" else []
        def open_document(self, inst):
            assert inst == "2022079009"
            return ("tile", 11)
        def page_image(self, tile, i):
            return {2: b"P3", 10: b"P11"}.get(i, b"")

    import tracker.enrich as E
    orig = E.ocr_png
    E.ocr_png = lambda png: {b"P3": PAGE3, b"P11": PAGE11}.get(png, "")
    try:
        rec = {"property_id": "2:F-1", "county": "Essex", "defendant": "1221 BLOOMFIELD LLC; ET. AL.",
               "plaintiff": "WILMINGTON SAVINGS FUND SOCIETY, FSB.", "attorney": "X LAW", "attorney_phone": "(201) 555-0100"}
        out = enrich_one(rec, Client(), bank_lookup=lambda p: {"name": "WSFS"})
    finally:
        E.ocr_png = orig
    assert out["owner"]["name"] == "1221 BLOOMFIELD LLC"
    assert [p["name"] for p in out["owner"]["people"]] == ["John Adams"]
    assert out["owner"]["related_entities"] == ["ADAMS INTERNATIONAL LLC"]
    assert out["owner"]["signers"][0] == {"name": "John Adams", "title": "Member", "entity": "1221 Bloomfleld LLC"} \
        or out["owner"]["signers"][0]["name"] == "John Adams"
    assert "2019000001" not in [f["instrument"] for f in out["owner"]["filings"]]
    loan = out["lender"]["loan"]
    assert loan["amount"] == 1_490_000 and loan["prepared_by"].startswith("Jennifer Sandner")
    assert loan["notice_address"][-1] == "Wilmington, DE 19801"
    assert out["lender"]["kind"] == "Bank" and out["lender"]["bank"] == {"name": "WSFS"}
    assert out["lender"]["foreclosure_counsel"]["phone"] == "(201) 555-0100"
    assert any("Notice of settlement" in n for n in out["notes"]) or out["owner"]["filings"][0]["kind"].startswith("Notice")


def test_enrich_without_records_source():
    from tracker.enrich import enrich_one
    out = enrich_one({"property_id": "7:1", "county": "Bergen", "defendant": "X LLC", "plaintiff": "FANNIE MAE"}, None)
    assert out["lender"]["kind"].startswith("Agency") and "aren't searchable" in out["notes"][0]


def test_build_history_keeps_this_parcel_only():
    from tracker.enrich import build_history
    from tracker.press import IndexRow
    rec = {"block": "303", "lot": "14", "lots": None}
    parcel = [IndexRow("DEED", "UCF FAIRFIELD LLC", "1221 BLOOMFIELD LLC", "2022079008", "8/24/2022", "FAIRFIELD", "303", "14", "0"),
              IndexRow("EASEMENT", "1221 BLOOMFIELD LLC", "PSE&G", "2023000111", "3/1/2023", "FAIRFIELD", "303", "14", "1"),
              IndexRow("DEED", "OLD OWNER INC", "UCF FAIRFIELD LLC", "2009000123", "5/5/2009", "FAIRFIELD", "303", "14", "2")]
    owner = [IndexRow("MORTGAGE", "1221 BLOOMFIELD LLC", "WILMINGTON SAVINGS FUND SOCIETY", "2022079009", "8/24/2022", "FAIRFIELD", "N/A", "", "3"),
             IndexRow("DEED", "UCF FAIRFIELD LLC", "1221 BLOOMFIELD LLC", "2022079008", "8/24/2022", "FAIRFIELD", "303", "14", "4"),
             IndexRow("MORTGAGE", "1221 BLOOMFIELD LLC", "OTHER BANK", "2024000999", "2/2/2024", "NEWARK", "900", "1", "5")]
    h = build_history(rec, parcel, owner, "1221 BLOOMFIELD LLC")
    assert [f["instrument"] for f in h] == ["2023000111", "2022079009", "2022079008", "2009000123"]
    assert h[0]["kind"] == "Easement" and h[-1]["to"] == ["UCF FAIRFIELD LLC"]


def test_match_municipality():
    from tracker.press import match_municipality
    opts = [["", "Please Select a Municipality"], ["FAIRFIELD", "FAIRFIELD"], ["ORANGE", "ORANGE"],
            ["WEST ORANGE", "WEST ORANGE"], ["NEWARK", "NEWARK"], ["02", "CLIFTON CITY"]]
    assert match_municipality("Fairfield Twp", opts) == "FAIRFIELD"
    assert match_municipality("Orange City", opts) == "ORANGE"
    assert match_municipality("West Orange Twp", opts) == "WEST ORANGE"
    assert match_municipality("Clifton City", opts) == "02"
    assert match_municipality("Trenton", opts) is None
