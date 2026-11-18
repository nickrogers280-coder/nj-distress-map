"""REO history from the statewide SR-1A deed files (layout checked against Sales2025.txt)."""
import io
import zipfile

from tracker import sr1a


def line(district="0714", block="0081401", lot="0002102", cls="4A", nu="12 ", anu="012", price=100,
         deed="240223", rec="240715", loc="847-849 MT PROSPECT AVE", assessed=845600):
    l = [" "] * 663

    def put(a, text):
        for i, ch in enumerate(text):
            l[a - 1 + i] = ch
    put(1, district)
    put(34, "N")
    put(35, nu)
    put(38, f"{price:09d}")
    put(74, f"{assessed:09d}")
    put(298, loc.ljust(25)[:25])
    put(339, deed)
    put(345, rec)
    put(351, block.ljust(9))
    put(360, lot.ljust(9))
    put(627, cls.ljust(2))
    put(638, anu)
    return "".join(l)


def test_parse_line_and_pin():
    s = sr1a.parse_line(line())
    assert s.pin == "0714_814.01_21.02" and s.county == "Essex"
    assert s.prop_class == "4A" and s.price == 100 and s.deed_date == "2024-02-23" and s.assessed == 845600
    assert sr1a.classify_sale(s) == "reo"
    assert sr1a.parse_line(line(block="00385", lot="00001")).pin == "0714_385_1"


def test_reo_events_with_resale_and_filters():
    sales = [sr1a.parse_line(x) for x in [
        line(),                                                         # lender takes it back for $100
        line(nu="31 ", anu="031", price=1_250_000, deed="250915"),     # lender sells it on
        line(block="00376", lot="00015", price=640_000),                # outside buyer at sheriff sale: not REO
        line(block="00377", lot="00001", nu="18 ", anu="018", price=1, cls="1"),   # deed in lieu, vacant land
        line(block="00378", lot="00002", cls="2"),                      # a house: not tracked
        line(block="00379", lot="00003", deed="170615"),                # before 2018
    ]]
    ev = sr1a.reo_events(sales)
    assert [e["pin"] for e in ev] == ["0714_814.01_21.02", "0714_377_1"] or \
        sorted(e["pin"] for e in ev) == ["0714_377_1", "0714_814.01_21.02"]
    big = next(e for e in ev if e["pin"] == "0714_814.01_21.02")
    assert big["reo_kind"] == "sheriff's sale" and big["resale_date"] == "2025-09-15" and big["resale_price"] == 1_250_000
    land = next(e for e in ev if e["pin"] == "0714_377_1")
    assert land["reo_kind"] == "deed in lieu" and "resale_date" not in land


def test_iter_sales_from_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Sales2025.txt", "\r\n".join([line(), line(block="00376"), "short line"]) + "\r\n")
    assert len(list(sr1a.iter_sales(buf.getvalue()))) == 2


def test_reo_history_run_and_map(tmp_path):
    from datetime import date, datetime, timezone
    from tracker import reo_history
    from tracker.db import DB
    from tracker.export import build_map_data

    def zipped(*lines):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Sales.txt", "\r\n".join(lines) + "\r\n")
        return buf.getvalue()

    files = {2019: zipped(line(deed="180306")), 2025: zipped(line(block="00376", lot="00015", nu="18 ", anu="018", price=1, deed="250102"))}
    asked = []

    def fetch(years):
        asked.append(list(years))
        return {y: b for y, b in files.items() if y in years}

    class Parcels:
        def query(self, where, limit=25):
            if "0714_814.01_21.02" in where:
                return [{"PAMS_PIN": "0714_814.01_21.02", "MUN_NAME": "NEWARK CITY", "PROP_CLASS": "4A", "NET_VALUE": 845600,
                         "PROP_LOC": "847-849 MT PROSPECT AVE"}]
            if "0714_376_15" in where:
                return [{"PAMS_PIN": "0714_376_15", "MUN_NAME": "NEWARK CITY", "PROP_CLASS": "2", "NET_VALUE": 300000}]
            return []

        def centroid(self, pin):
            return (40.76, -74.17)

    db = DB(tmp_path / "t.db")
    s = reo_history.run({}, db, Parcels(), today=date(2026, 11, 18), fetch=fetch)
    assert asked[0][0] == 2018 and s["events"] == 2 and s["located"] == 2 and s["tracked"] == 1
    m = build_map_data(db, now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    reo = [p for p in m["properties"] if p.get("stage") == "reo_hist"]
    assert len(reo) == 1 and reo[0]["outcome_date"] == "2018-03-06" and reo[0]["street"] == "847-849 MT PROSPECT AVE"
    assert reo[0]["category"] == "target" and m["alerts"] == []          # first load is history, not news
    # Next run only re-reads the latest two years.
    reo_history.run({}, db, Parcels(), today=date(2026, 11, 18), fetch=fetch)
    assert asked[-1] == [2025, 2026]


def test_history_events_keep_third_party_sheriff_sales():
    sales = [sr1a.parse_line(x) for x in [line(), line(block="00376", lot="00015", price=640_000)]]
    ev = sr1a.history_events(sales)
    sold = next(e for e in ev if e["outcome"] == "sold")
    assert sold["pin"] == "0714_376_15" and sold["reo_price"] == 640_000
    assert {e["outcome"] for e in ev} == {"reo", "sold"}
