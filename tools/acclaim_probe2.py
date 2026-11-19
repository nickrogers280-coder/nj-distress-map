"""Hudson Acclaim over plain HTTP: disclaimer, doc-type search, grid data, document details (run in Actions)."""
import json, re, sys
from datetime import date, timedelta
import requests
sys.path.insert(0, ".")
from tracker.press import BROWSER_UA

B = "https://acclaim.hcnj.us/AcclaimWeb/"
s = requests.Session(); s.headers["User-Agent"] = BROWSER_UA
s.get(B, timeout=60)
r = s.post(B + "search/Disclaimer", data={"Disclaimer": "true"}, timeout=60)
print("DISCLAIMER", r.status_code, r.url)
page = s.get(B + "search/SearchTypeDocType", timeout=60).text
end = date.today(); start = end - timedelta(days=14)
form = {"DocTypes": "130,131", "DocTypesDisplay-input": "LIS PENDENS", "DocTypesDisplay": "LIS PENDENS", "DateRangeList": " ",
        "RecordDateFrom": start.strftime("%-m/%-d/%Y"), "RecordDateTo": end.strftime("%-m/%-d/%Y"), "X-Requested-With": "XMLHttpRequest"}
r = s.post(B + "search/SearchTypeDocType?Length=6", data=form, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=90)
print("SEARCH", r.status_code, len(r.text))
urls = sorted(set(re.findall(r"""["'](/?AcclaimWeb/[A-Za-z]+/[A-Za-z0-9]+[^"']*)["']""", r.text + page)))
print("URLS", urls[:40])
print("SNIP", re.sub(r"\s+", " ", r.text)[:1500])
for path, body in [("Search/GridResults", {"page": 1, "pageSize": 100}), ("search/GridResults", {"page": 1, "size": 100})]:
    try:
        g = s.post(B + path, data=body, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=90)
        print("GRID", path, g.status_code, g.headers.get("content-type"), g.text[:1500].replace("\n", " "))
        if "json" in g.headers.get("content-type", ""):
            d = g.json()
            rows = d.get("Data") or d.get("data") or []
            print("GRID ROWS", len(rows), json.dumps(rows[:2])[:1500])
            break
    except Exception as e:
        print("GRID", path, "ERR", e)
