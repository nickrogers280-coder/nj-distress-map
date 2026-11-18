"""Print the shape of one SR-1A file so the parser's layout can be checked (run in Actions)."""
import collections, io, sys, zipfile
import requests
sys.path.insert(0, ".")
from tracker import sr1a

year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
r = requests.get(sr1a.STATE_URL.format(year=year), timeout=180)
print("status", r.status_code, r.headers.get("content-type"), len(r.content))
z = zipfile.ZipFile(io.BytesIO(r.content))
for info in z.infolist():
    print("member", info.filename, info.file_size)
name = z.namelist()[0]
raw = z.read(name)
lines = raw.decode("latin-1").splitlines()
print("lines", len(lines), "lengths", collections.Counter(len(l) for l in lines).most_common(5))
for l in lines[:3]:
    print("LINE", repr(l[:700]))
parsed = [sr1a.parse_line(l) for l in lines]
ok = [p for p in parsed if p]
print("parsed", len(ok))
print("classes", collections.Counter(p.prop_class for p in ok).most_common(12))
print("nu", collections.Counter(p.nu for p in ok).most_common(12))
print("assessor_nu", collections.Counter(p.assessor_nu for p in ok).most_common(12))
print("deed years", collections.Counter((p.deed_date or "none")[:4] for p in ok).most_common(8))
print("kinds", collections.Counter(sr1a.classify_sale(p) for p in ok if p.prop_class in sr1a.TRACKED_CLASSES))
ex = [p for p in ok if p.prop_class in ("4A", "4B") and "12" in (p.nu, p.assessor_nu)][:3]
for p in ex:
    print("EX", p.as_dict())
