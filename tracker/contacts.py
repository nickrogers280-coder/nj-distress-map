"""Read owner- and lender-side contacts out of recorded documents.

Works on OCR text from county clerk images (mortgages, assignments, lis pendens) plus the
clerk's index rows. Everything here is pure text processing so it can be tested offline;
fetching and OCR live in tracker/press.py and enrich.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .parcels import looks_like_entity

TITLES = (
    "Managing Member|Sole Member|Authorized Member|Member|Managing Director|Manager|"
    "Chief Executive Officer|CEO|President|Vice President|Executive Vice President|Senior Vice President|"
    "Chief Financial Officer|CFO|Chief Operating Officer|COO|Secretary|Treasurer|"
    "General Partner|Managing Partner|Partner|Principal|Owner|Authorized Signatory|Authorized Representative|"
    "Officer|Director|Trustee"
)
_NAME = r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]*){1,3})"
# "John Adams, Member of 1221 Bloomfield LLC" / "John Adams, as Managing Member of ..."
_SIGNER_RE = re.compile(_NAME + r",?\s+(?:as\s+(?:its\s+)?|its\s+)?(" + TITLES + r")\b(?:\s+(?:of|for)\s+([^\n,;]{3,80}))?")
# "Name: John Adams\nTitle: Managing Member"
_NAME_TITLE_RE = re.compile(r"Name\s*[:.]\s*" + _NAME + r"[^\n]*\n\s*(?:Its|Title)\s*[:.]\s*(" + TITLES + r")", re.I)
_PREPARED_RE = re.compile(r"(?:instrument|mortgage|document|this)?\s*prepared\s+by\s*[:.]?\s*(.*)", re.I)
_RETURN_HDR_RE = re.compile(
    r"^\s*(?:RECORD(?:ING)?\s+(?:REQUESTED\s+BY|AND\s+RETURN\s+TO)|WHEN\s+RECORDED\s+(?:MAIL|RETURN)\s+TO|"
    r"AFTER\s+RECORDING\s+(?:RETURN|MAIL)\s+TO|RETURN\s+TO|PLEASE\s+RETURN\s+TO|MAIL\s+TO)[ \t]*:?[ \t]*(.*)$",
    re.I | re.M)
_AMOUNT_RE = re.compile(r"(?:Consideration|principal\s+(?:amount|sum)\s+of|in\s+the\s+amount\s+of|sum\s+of)\D{0,20}\$\s?([0-9][0-9,]{3,}(?:\.\d{2})?)", re.I)
_LENDER_DEF_RE = re.compile(r"[\"“”]?Lender[\"“”]?\s+means\s+([^\n]{3,120}?)(?:,\s*(?:its|whose|and)|\.\s)", re.I)
_ZIP_RE = re.compile(r"\b[A-Z]{2}\.?\s+\d{5}(?:-\d{4})?\b")
_NOT_A_NAME = re.compile(r"\b(Notary|Public|Witness|State|County|Commission|Expires|Lender|Grantor|Borrower|"
                         r"Mortgage|Section|Page|Recorder|Register|Deeds|Inst|Instrument|Limited|Liability|Company|"
                         r"Corporation|Bank|Trust|Society|Association|LLC|Inc)\b", re.I)


def clean_party(name: str | None) -> str:
    """First defendant/owner name from a sheriff listing, trimmed for an index search."""
    s = (name or "").split(";")[0]
    s = re.split(r",?\s+(?:ET\.?\s*AL\.?S?|AND\s+STATE\s+OF|A/K/A|AKA|F/K/A|D/B/A)\b", s, flags=re.I)[0]
    s = re.sub(r",\s*(?:A|AN)\s+(?:NEW\s+JERSEY|NJ|DELAWARE|NEW\s+YORK|[A-Z ]{3,20})\s+(?:LIMITED\s+LIABILITY\s+COMPANY|"
               r"CORPORATION|LIMITED\s+PARTNERSHIP|COMPANY).*$", "", s, flags=re.I)
    s = re.sub(r"[.,]", " ", s)
    return re.sub(r"\s+", " ", s).strip().upper()


def search_terms(name: str) -> list[str]:
    """Index searches to try, most specific first ('1221 BLOOMFIELD LLC', then '1221 BLOOMFIELD')."""
    base = clean_party(name)
    if not base:
        return []
    terms = [base]
    short = re.sub(r"\s+(?:L\s?L\s?C|INC|CORP|CORPORATION|CO|LP|L\s?P|LTD|LLP|COMPANY)$", "", base).strip()
    if short and short != base:
        terms.append(short)
    if not looks_like_entity(base):
        parts = base.split()
        if len(parts) >= 2:                      # index lists people as LAST FIRST
            terms.insert(0, f"{parts[-1]} {parts[0]}")
    return terms


def same_party(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper().replace("L.L.C", "LLC"))
    return bool(norm(a)) and (norm(a) == norm(b) or norm(a).startswith(norm(b)) or norm(b).startswith(norm(a)))


def person_from_index(name: str) -> str | None:
    """'ADAMS JOHN' -> 'John Adams'; None for companies, governments, etc."""
    n = (name or "").strip()
    if not n or looks_like_entity(n) or re.search(r"\b(STATE|TOWNSHIP|CITY|BOROUGH|TOWN|COUNTY|UNITED STATES|"
                                                   r"BANK|TRUST|SOCIETY|ASSOCIATION|FUND|CAPITAL|MORTGAGE|"
                                                   r"SECRETARY|INTERNAL REVENUE|COMMISSIONER|N\.?A\.?)\b|,", n, re.I):
        return None
    parts = n.split()
    if len(parts) < 2 or len(parts) > 4:
        return None
    last, rest = parts[0], parts[1:]
    return " ".join(p.title() for p in rest + [last])


@dataclass
class DocFacts:
    signers: list[dict] = field(default_factory=list)      # [{name, title, entity}]
    prepared_by: str | None = None
    return_to: list[str] = field(default_factory=list)     # address block lines
    lender_name: str | None = None
    amount: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _good_name(n: str) -> bool:
    n = n.strip()
    words = n.split()
    return 2 <= len(words) <= 4 and not _NOT_A_NAME.search(n) and all(len(w) > 1 or w.endswith(".") for w in words)


def parse_document(text: str) -> DocFacts:
    """Signers (with title), preparer, return-to address, lender and amount from OCR text."""
    t = text.replace("\r", "")
    t_flat = re.sub(r"[ \t]+", " ", t)
    f = DocFacts()

    seen = set()
    for m in list(_NAME_TITLE_RE.finditer(t_flat)) + list(_SIGNER_RE.finditer(t_flat)):
        name, title = m.group(1).strip(), m.group(2).strip()
        entity = (m.group(3) or "").strip(" .") if m.re is _SIGNER_RE and m.lastindex >= 3 else ""
        if not _good_name(name):
            continue
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        f.signers.append({"name": name, "title": title, "entity": re.sub(r"\s+", " ", entity)[:80] or None})

    for m in _PREPARED_RE.finditer(t):
        cand = m.group(1).strip()
        cand = re.sub(r"^[Xx%\W]+$", "", cand).strip()
        if len(cand) < 4:                         # name is on the next line(s)
            nxt = t[m.end():m.end() + 200].strip().split("\n")
            cand = next((ln for ln in nxt if len(ln.strip()) > 4), "")
        cand = re.sub(r"^\s*Name of Signer\s*:\s*", "", cand, flags=re.I)
        cand = re.sub(r"[“”\"|]+.*$", "", cand).strip(" ,.:")
        if len(cand) >= 5 and not re.match(r"(?i)^(the|by|and|or|of)\b", cand):
            f.prepared_by = cand[:140]
            break

    m = _RETURN_HDR_RE.search(t)
    if m:
        block, first = [], m.group(1).strip()
        if first:
            block.append(first)
        for ln in t[m.end():].split("\n")[1:]:
            ln = ln.strip()
            if not ln:
                if block:
                    break
                continue
            if re.match(r"(?i)^(SEND TAX|WHEN RECORDED|RECORD|SPACE ABOVE|MORTGAGE|THIS MORTGAGE|PREPARED)", ln):
                break
            block.append(re.sub(r"\s+SPACE ABOVE.*$", "", ln))
            if _ZIP_RE.search(ln) or len(block) >= 5:
                break
        f.return_to = [b for b in block if b][:5]

    m = _LENDER_DEF_RE.search(t_flat)
    if m:
        f.lender_name = m.group(1).strip(" ,.")
    elif f.return_to:
        f.lender_name = f.return_to[0]

    amts = [float(a.replace(",", "")) for a in _AMOUNT_RE.findall(t_flat)]
    amts = [a for a in amts if a >= 1000]
    if amts:
        f.amount = max(amts)
    return f


def merge_facts(parts: list[DocFacts]) -> DocFacts:
    out = DocFacts()
    seen = set()
    for p in parts:
        for s in p.signers:
            k = s["name"].lower()
            if k not in seen:
                seen.add(k)
                out.signers.append(s)
        out.prepared_by = out.prepared_by or p.prepared_by
        out.return_to = out.return_to or p.return_to
        out.lender_name = out.lender_name or p.lender_name
        out.amount = out.amount or p.amount
    return out


_SERVICER_RE = re.compile(r"(?:BY\s+AND\s+THROUGH|THROUGH)\s+ITS\s+(SPECIAL\s+SERVICER|SERVICER|ATTORNEY[- ]IN[- ]FACT)\s*,?\s*([^,;]+(?:,\s*(?:LLC|INC|L\.?P\.?|N\.?A\.?))?)", re.I)


def lender_profile(plaintiff: str | None) -> dict:
    """What kind of lender is foreclosing, and who actually handles the loan."""
    p = (plaintiff or "").upper()
    kind = "Private / non-bank lender"
    if re.search(r"MORTGAGE TRUST|PASS[- ]THROUGH CERTIFICATES|COMMERCIAL MORTGAGE|SERIES \d{4}-", p):
        kind = "CMBS / securitized loan"
    elif re.search(r"FANNIE MAE|FEDERAL NATIONAL MORTGAGE|FREDDIE MAC|FEDERAL HOME LOAN MORTGAGE", p):
        kind = "Agency (Fannie Mae / Freddie Mac)"
    elif re.search(r"\bBANK\b|SAVINGS|\bFSB\b|N\.?A\.?\b|CREDIT UNION|BANCORP", p):
        kind = "Bank"
    elif re.search(r"\bTRUST\b|\bREIT\b|\bFUND\b", p):
        kind = "Debt fund / trust"
    servicer = None
    m = _SERVICER_RE.search(plaintiff or "")
    if m:
        servicer = {"role": m.group(1).title(), "name": m.group(2).strip().title()}
    trustee = None
    m = re.match(r"\s*(.+?),?\s+(?:NOT\s+IN\s+ITS\s+INDIVIDUAL\s+CAPACITY\s+BUT\s+SOLELY\s+)?AS\s+(?:OWNER\s+)?TRUSTEE", p)
    if m:
        trustee = m.group(1).title()
    return {"kind": kind, "servicer": servicer, "trustee": trustee}
