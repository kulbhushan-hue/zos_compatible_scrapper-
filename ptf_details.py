"""
Per-PTF detail table for Broadcom maintenance.

Output columns (the layout requested for the dashboard):
    #  |  Product / Release  |  PTF ID  |  FMID  |  PTF publish date  |  APAR ID
       |  Prerequisites  |  Superseding PTF  |  Full description  |  Detail Extraction Status

Where each value comes from
  * Product / Release, PTF publish date, short description, Feature/HIPER/PE/CARSID/SECINT flags:
      Broadcom's public "Mainframe Products Solutions List" (AllProducts.HTML, last three years).
  * FMID, APAR ID, Prerequisites, Superseding PTF, Full description:
      the individual solution page. Broadcom restricts those pages to signed-in support users, so they
      are read only from sources you are entitled to use:
        1. a detail export you upload / place at data/ptf_details.csv (any CSV with PTF ID + the fields), or
        2. an authorized Broadcom Support session: set BROADCOM_PTF_DETAIL_URL (a URL template containing
           {ptf}) and BROADCOM_SUPPORT_COOKIE (kept in your secret manager, never in the image or git), or
        3. SMP/E PTF text you already downloaded (++VER FMID/PRE/SUP statements) pasted or uploaded.
  * Nothing is guessed. Every row carries a status so missing data is never mistaken for "none":
        Verified | Partial | Not found in source | Access denied | Fetch failed | Not attempted
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd
from bs4 import BeautifulSoup

import broadcom_scraper as bs

CATALOG_URL = "https://ftpdocs.broadcom.com/WebInterface/phpdocs/0/MSPSaccount/COMPAT/AllProducts.HTML"

TABLE_COLUMNS = [
    "#", "Product / Release", "PTF ID", "FMID", "PTF publish date", "APAR ID",
    "Prerequisites", "Superseding PTF", "Full description", "Detail Extraction Status",
]
EXTRA_COLUMNS = ["z/OS release (matrix)", "Catalog description", "Feature", "HIPER", "PE", "CARSID", "SECINT", "Detail source"]
DETAIL_KEYS = ("fmid", "apar", "prerequisites", "superseded_by", "description", "published")
NOT_ACCESSIBLE = "Not accessible"
NOT_FOUND = "Not found in source"

PTF_ID = re.compile(r"\b[A-Z]{2}\d{5}\b")
FMID = re.compile(r"\b[A-Z][A-Z0-9]{6}\b")
DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
LOGIN_HINT = re.compile(r"\b(sign[\s-]?in|log[\s-]?in|authentication required|access denied|not authorized|session (?:has )?expired)\b", re.IGNORECASE)

# Plain-text catalog rows look like:
# "<product> <release> 2026-10-22 LU22137 <description> N N N CAR2611 N" (Feature HIPER PE CARSID SECINT)


# --------------------------------------------------------------------------- catalog
@dataclass
class CatalogEntry:
    ptf: str
    product: str
    release: str
    published: str
    description: str
    feature: str = ""
    hiper: str = ""
    pe: str = ""
    carsid: str = ""
    secint: str = ""

    @property
    def product_release(self) -> str:
        return f"{self.product} {self.release}".strip()


def parse_catalog(payload: bytes | str) -> dict[str, CatalogEntry]:
    """Parse the Broadcom Mainframe Products Solutions List. Returns {PTF id: entry}.

    Reads the HTML table when the columns can be identified by their headers, and falls back to the
    plain-text row layout otherwise.
    """
    entries: dict[str, CatalogEntry] = {}
    soup = BeautifulSoup(payload, "html.parser")
    for table in soup.find_all("table"):
        header = None
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True).lower() for c in tr.find_all(["th", "td"])]
            if any(c == "ptf" for c in cells) and any("publish" in c for c in cells):
                header = cells
                break
        if not header:
            continue
        idx = {name: i for i, name in enumerate(header)}
        def col(row, *names):
            for n in names:
                for key, i in idx.items():
                    if key == n or key.startswith(n):
                        return row[i] if i < len(row) else ""
            return ""
        started = False
        for tr in table.find_all("tr"):
            row = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if [c.lower() for c in row] == header:
                started = True
                continue
            if not started or len(row) < 4:
                continue
            ptf = col(row, "ptf").strip().upper()
            if not PTF_ID.fullmatch(ptf):
                continue
            name = col(row, "product")
            release = col(row, "release")
            if not release:
                name, release = bs.split_name_vrm(name)
            entries[ptf] = CatalogEntry(
                ptf=ptf, product=name, release=release, published=_iso(col(row, "published")),
                description=col(row, "description"), feature=col(row, "feature"), hiper=col(row, "hiper"),
                pe=col(row, "pe"), carsid=col(row, "carsid"), secint=col(row, "secint"),
            )
    if entries:
        return entries

    text = soup.get_text("\n") if soup.find() else (payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload))
    flat = re.sub(r"\s+", " ", text).strip()
    flat = re.sub(r"^.*?Product Name Release Published Date PTF Description Feature HIPER PE CARSID SECINT\s*", "", flat)
    anchors = list(re.finditer(r"(?:(?<=\s)|^)(20\d{2}-\d{2}-\d{2}) ([A-Z]{2}\d{5}) ", flat))
    trailer = re.compile(r"^(?P<desc>.*?)\s+(?P<f>[YN])\s+(?P<h>[YN])\s+(?P<p>[YN])(?:\s+(?P<c>CAR\d{4}))?\s+(?P<s>[YN])(?:\s+(?P<next>.*))?$")
    name = flat[: anchors[0].start()].strip() if anchors else ""
    for k, m in enumerate(anchors):
        end = anchors[k + 1].start() if k + 1 < len(anchors) else len(flat)
        seg = flat[m.end():end].strip()
        t = trailer.match(seg)
        if not t:
            name = ""
            continue
        product, release = bs.split_name_vrm(name)
        entries[m.group(2)] = CatalogEntry(
            ptf=m.group(2), product=product, release=release, published=m.group(1),
            description=t.group("desc").strip(), feature=t.group("f"), hiper=t.group("h"),
            pe=t.group("p"), carsid=t.group("c") or "", secint=t.group("s"),
        )
        name = (t.group("next") or "").strip()
    return entries


def _iso(value: str) -> str:
    m = DATE.search(value or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (value or "").strip()


def load_catalog(url: str = CATALOG_URL, html: bytes | str | None = None) -> tuple[dict[str, CatalogEntry], str]:
    """Fetch (or take supplied HTML) and parse the catalog. Returns (entries, status message)."""
    if html is None:
        payload, _, status = bs.fetch(url)
        if status != "Fetched":
            return {}, f"Catalog not reachable: {status}"
        html = payload
    entries = parse_catalog(html)
    return entries, (f"Catalog read: {len(entries)} PTF rows" if entries else "Catalog fetched but no PTF rows were recognised")


# --------------------------------------------------------------------------- detail parsing
@dataclass
class PtfDetail:
    ptf: str
    values: dict[str, str] = field(default_factory=dict)   # keys from DETAIL_KEYS, present only when found
    status: str = "Not attempted"
    source: str = ""
    note: str = ""


_LABELS = {
    "fmid": r"FMIDs?",
    "apar": r"APARs?(?:\s*(?:ID|number|no\.?))?|Problem\s*(?:ID|number)",
    "prerequisites": r"Pre-?requisites?(?:\s*PTFs?)?|Requisites?|PRE",
    "superseded_by": r"Superseded\s+by|Superseding(?:\s*PTFs?)?",
    "description": r"(?:Full\s+|Problem\s+)?Description|Abstract|Title|Problem\s+summary",
    "published": r"(?:PTF\s+)?(?:Publish(?:ed)?|Release|Creation)\s*Date|Published",
}
_ANY_LABEL = "|".join(f"(?:{p})" for p in _LABELS.values())


def parse_detail_text(text: str) -> dict[str, str]:
    """Pull labelled values out of a solution page's text. Returns only fields that are present."""
    out: dict[str, str] = {}
    flat = re.sub(r"[ \t]+", " ", text or "")
    for key, label in _LABELS.items():
        m = re.search(rf"(?im)^\s*(?:{label})\s*[:=\-]\s*(.+?)\s*$", flat) or re.search(rf"(?i)\b(?:{label})\s*[:=]\s*(.+?)(?:\n|$)", flat)
        if not m:
            m = re.search(rf"(?im)^\s*(?:{label})\s*\n\s*(.+?)\s*$", flat)
        if not m:
            continue
        value = m.group(1).strip()
        if key == "fmid":
            ids = [f for f in FMID.findall(value.upper()) if not PTF_ID.fullmatch(f)]
            value = ", ".join(dict.fromkeys(ids))
        elif key in ("prerequisites", "superseded_by"):
            ids = PTF_ID.findall(value.upper())
            value = ", ".join(dict.fromkeys(ids))
        elif key == "apar":
            ids = re.findall(r"\b[A-Z]{2}\d{5}\b|\b[A-Z]\d{6}\b", value.upper())
            value = ", ".join(dict.fromkeys(ids))
        elif key == "published":
            value = _iso(value)
            if not DATE.fullmatch(value):
                continue
        else:
            value = value[:1500]
        if value and not re.fullmatch(r"(?i)none|n/?a|-", value):
            out[key] = value
    return out


def parse_smpe_ver(text: str) -> dict[str, str]:
    """Read SMP/E PTF header statements (e.g. ++PTF(LU22137) / ++VER(Z038) FMID(CDBIC01) PRE(LU11111) SUP(LU00001)).

    PRE() lists prerequisite PTFs. SUP() lists the *older* PTFs this one supersedes; that is not the
    'Superseding PTF' column, so it is returned under 'supersedes' and reported separately.
    """
    out: dict[str, str] = {}
    blob = re.sub(r"\s+", " ", text or "")
    for key, kw in (("fmid", "FMID"), ("prerequisites", "PRE"), ("supersedes", "SUP")):
        m = re.search(rf"\b{kw}\s*\(([^)]*)\)", blob, re.IGNORECASE)
        if m:
            ids = [t for t in re.split(r"[\s,]+", m.group(1).upper()) if t]
            if ids:
                out[key] = ", ".join(dict.fromkeys(ids))
    return out


def classify(values: dict[str, str]) -> str:
    core = [k for k in DETAIL_KEYS if k not in ("published",)]
    present = [k for k in core if values.get(k)]
    if not present:
        return "Not found in source"
    return "Verified" if len(present) == len(core) else "Partial"


# --------------------------------------------------------------------------- detail providers
def load_detail_export(path_or_buffer) -> dict[str, PtfDetail]:
    """Read an export you are entitled to use: CSV with a PTF ID column and any of
    FMID, APAR ID, Prerequisites, Superseding PTF, Full description, PTF publish date."""
    df = pd.read_csv(path_or_buffer, dtype=str, keep_default_na=False)
    cols = {c.strip().lower(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_ptf = pick("ptf id", "ptf", "solution", "solution id")
    if not c_ptf:
        raise ValueError("The detail export needs a 'PTF ID' column")
    mapping = {
        "fmid": pick("fmid"), "apar": pick("apar id", "apar", "apars"),
        "prerequisites": pick("prerequisites", "prerequisite ptfs", "prereq", "pre"),
        "superseded_by": pick("superseding ptf", "superseded by"),
        "description": pick("full description", "description", "abstract"),
        "published": pick("ptf publish date", "published date", "publish date", "date"),
    }
    c_status = pick("detail extraction status", "status")
    valid_status = {"verified", "partial", "not found in source", "access denied", "fetch failed", "not attempted"}
    out: dict[str, PtfDetail] = {}
    for _, r in df.iterrows():
        ptf = str(r[c_ptf]).strip().upper()
        if not PTF_ID.fullmatch(ptf):
            continue
        values = {k: str(r[c]).strip() for k, c in mapping.items() if c and str(r[c]).strip()}
        if "published" in values:
            values["published"] = _iso(values["published"])
        given = str(r[c_status]).strip() if c_status else ""
        status = next((v for v in ("Verified", "Partial", "Not found in source", "Access denied", "Fetch failed", "Not attempted")
                       if v.lower() == given.lower()), None) if given.lower() in valid_status else None
        out[ptf] = PtfDetail(ptf=ptf, values=values, status=status or classify(values), source="uploaded detail export")
    return out


def _detail_url_template() -> str:
    return os.environ.get("BROADCOM_PTF_DETAIL_URL", "").strip()


def authorized_session_configured() -> bool:
    return bool(_detail_url_template() and os.environ.get("BROADCOM_SUPPORT_COOKIE", "").strip())


def fetch_detail(ptf: str) -> PtfDetail:
    """Read one PTF's solution page with the authorized session, if one is configured."""
    template = _detail_url_template()
    cookie = os.environ.get("BROADCOM_SUPPORT_COOKIE", "").strip()
    if not template or "{ptf}" not in template:
        return PtfDetail(ptf, status="Not attempted", note="no authorized detail source configured")
    url = template.format(ptf=ptf)
    if not bs.allowed_url(url):
        return PtfDetail(ptf, status="Not attempted", source=url, note="Detail URL is not on the approved host list")
    headers = {"Cookie": cookie} if cookie else None
    payload, ctype, status = bs.fetch(url, headers=headers)
    if status in ("HTTP 401", "HTTP 403") or status.startswith("Blocked"):
        return PtfDetail(ptf, status="Access denied", source=url, note=status)
    if status != "Fetched":
        return PtfDetail(ptf, status="Fetch failed", source=url, note=status)
    soup = BeautifulSoup(payload, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()
    text = bs._text_of(soup)
    values = parse_detail_text(text)
    if not values and (LOGIN_HINT.search(text) or not text.strip()) and ptf not in text:
        return PtfDetail(ptf, status="Access denied", source=url, note="The page asked for sign-in or returned no readable content")
    return PtfDetail(ptf, values=values, status=classify(values), source=url)


def gather_details(ptfs: list[str], overrides: dict[str, PtfDetail] | None = None, max_workers: int = 4) -> dict[str, PtfDetail]:
    overrides = overrides or {}
    result: dict[str, PtfDetail] = {}
    todo = []
    for p in ptfs:
        if p in overrides:
            result[p] = overrides[p]
        else:
            todo.append(p)
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for detail in pool.map(fetch_detail, todo):
                result[detail.ptf] = detail
    return result


# --------------------------------------------------------------------------- selecting PTFs
def ptfs_from_matrix(scrape: "bs.ScrapeResult", n: int = 10, zos: str | None = None) -> list[tuple[str, str, str]]:
    """First n distinct PTF IDs in matrix order: [(ptf, 'Product VRM', 'z/OS 3.2')]."""
    out, seen = [], set()
    for rec in scrape.records:
        for rel, res in rec.releases.items():
            if zos and rel != zos:
                continue
            for ptf in res.ptfs:
                if ptf not in seen:
                    seen.add(ptf)
                    out.append((ptf, rec.clean_name, f"z/OS {rel}"))
                    if len(out) >= n:
                        return out
    return out


def newest_in_catalog(catalog: dict[str, CatalogEntry], n: int = 10) -> list[tuple[str, str, str]]:
    ordered = sorted(catalog.values(), key=lambda e: (e.published, e.ptf), reverse=True)
    return [(e.ptf, e.product_release, "") for e in ordered[:n]]


# --------------------------------------------------------------------------- the table
def build_table(selection: list[tuple[str, str, str]], catalog: dict[str, CatalogEntry],
                details: dict[str, PtfDetail]) -> pd.DataFrame:
    rows = []
    for i, (ptf, matrix_name, zos) in enumerate(selection, 1):
        cat = catalog.get(ptf)
        det = details.get(ptf) or PtfDetail(ptf)
        accessible = det.status in ("Verified", "Partial", "Not found in source")
        missing = NOT_FOUND if accessible else NOT_ACCESSIBLE

        def val(key, fallback=""):
            return det.values.get(key) or fallback or missing

        published = det.values.get("published") or (cat.published if cat else "") or missing
        rows.append({
            "#": i,
            "Product / Release": cat.product_release if cat else matrix_name,
            "PTF ID": ptf,
            "FMID": val("fmid"),
            "PTF publish date": published,
            "APAR ID": val("apar"),
            "Prerequisites": val("prerequisites"),
            "Superseding PTF": val("superseded_by"),
            "Full description": val("description"),
            "Detail Extraction Status": det.status + (f" ({det.note})" if det.note and det.status not in ("Verified",) else ""),
            "z/OS release (matrix)": zos,
            "Catalog description": cat.description if cat else "Not in the public catalog (older than three years or not listed)",
            "Feature": cat.feature if cat else "", "HIPER": cat.hiper if cat else "", "PE": cat.pe if cat else "",
            "CARSID": cat.carsid if cat else "", "SECINT": cat.secint if cat else "",
            "Detail source": det.source,
        })
    return pd.DataFrame(rows, columns=TABLE_COLUMNS + EXTRA_COLUMNS)
