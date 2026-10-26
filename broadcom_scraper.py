"""
Broadcom z/OS compatibility grid scraper.

Reads the public Broadcom grid (zos_compat.HTML), and for every product/release row:

  1. reads the z/OS V3R2 / V3R1 / V2R5 status cells of the grid table,
  2. follows the in-page "PTFs" and "Additional Information" anchors to the
     section they point to and extracts the PTF IDs and USERMOD/SYSMOD IDs,
  3. collects the extra hyperlinks found in those sections (and the product page
     link in the first column) and scrapes those pages too (depth 1),
  4. returns one structured record per product + release.

Design notes
  * Only https URLs on approved vendor hosts are fetched (no redirects off the allow-list).
  * Pure requests + BeautifulSoup. No Streamlit import, so it also runs from the command line.
  * Nothing is invented: an ID is reported only when it appears in the fetched text.
    "No ID found" never means "no maintenance required" - always confirm at the vendor page.
"""

from __future__ import annotations

import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

GRID_URL = "https://ftpdocs.broadcom.com/phpdocs/0/MSPSaccount/COMPAT/zos_compat.HTML"

ALLOWED_HOSTS = (
    "broadcom.com",
    "ca.com",          # legacy CA Technologies support links still used on Broadcom pages
    "ibm.com",
)
USER_AGENT = "zOS-Compatibility-Dashboard/1.1 (vendor support lookup; read-only)"
REQUEST_TIMEOUT = (6, 25)
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_REDIRECTS = 4
CACHE_TTL_SECONDS = 6 * 60 * 60
REQUEST_PAUSE_SECONDS = 0.15      # small politeness delay per worker request

# --------------------------------------------------------------------------- patterns
# Broadcom (ex-CA) PTF / solution IDs look like LU18187, RO94173, SO01425, QO91208 ...
FIX_ID = re.compile(r"\b[A-Z]{2}\d{5}\b")
# IBM z/OS service IDs that sometimes appear in Broadcom text (z/OS maintenance, not a Broadcom PTF)
IBM_SERVICE_PREFIXES = {"UA", "UJ", "UK", "UI", "UQ", "UR", "OA", "OW", "PH", "PI", "PJ", "PK", "PM", "PQ"}
USERMOD_WORD = re.compile(r"\b(usermods?|sysmods?)\b", re.IGNORECASE)
FIX_CONTEXT_WORD = re.compile(
    r"\b(ptfs?|solutions?|fix(?:es)?|apars?|usermods?|sysmods?|zaps?|apply|install(?:ed)?|requires?|required|need(?:ed)?)\b",
    re.IGNORECASE,
)
ZOS_RELEASE = re.compile(r"V(\d+)\s*R(\d+)", re.IGNORECASE)
FIX_CATEGORY = re.compile(r"Fix\s+Category:\s*([A-Za-z0-9_.\-/]+)", re.IGNORECASE)

STATUS_MAP = {
    "YES": "Yes", "NO": "No", "IN PROGRESS": "In Progress", "N/A": "N/A", "TBD": "In Progress",
}


# --------------------------------------------------------------------------- data classes
@dataclass
class CellLink:
    text: str
    href: str            # absolute URL (fragment kept)
    anchor_id: str = ""  # fragment, when the link points into the same page
    kind: str = "other"  # "ptf" | "info" | "other"


@dataclass
class ReleaseResult:
    zos: str                       # "3.2"
    status: str = ""
    grid_text: str = ""            # raw status cell text
    grid_links: str = ""           # link labels in the cell, e.g. "PTFs, Additional Information"
    ptfs: list[str] = field(default_factory=list)          # PTF IDs from the grid PTF/Info sections
    usermods: list[str] = field(default_factory=list)      # USERMOD / SYSMOD IDs
    linked_ptfs: list[str] = field(default_factory=list)   # extra IDs from hyperlinked pages
    other_ids: list[str] = field(default_factory=list)     # IBM z/OS service IDs mentioned (not Broadcom PTFs)
    notes: list[str] = field(default_factory=list)         # trimmed section text for context
    section_urls: list[str] = field(default_factory=list)  # URL#anchor used
    linked_urls: list[str] = field(default_factory=list)   # extra hyperlinks that were scraped


@dataclass
class ProductRecord:
    name: str                      # grid name as shown, e.g. "ACF2 for z/OS 16.0 (End Of Service)"
    clean_name: str                # name without lifecycle suffix
    vrm: str
    product_url: str
    releases: dict[str, ReleaseResult] = field(default_factory=dict)


@dataclass
class LinkedPageResult:
    url: str
    status: str
    ptfs: list[str] = field(default_factory=list)
    usermods: list[str] = field(default_factory=list)
    other_ids: list[str] = field(default_factory=list)
    title: str = ""


# --------------------------------------------------------------------------- HTTP
_cache: dict[str, tuple[float, tuple[bytes, str, str]]] = {}
_cache_lock = threading.Lock()


def allowed_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return False
    if p.port not in (None, 443):
        return False
    host = p.hostname.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def fetch(url: str, session: requests.Session | None = None, headers: dict | None = None) -> tuple[bytes, str, str]:
    """GET one approved URL. Returns (payload, content_type, status). Cached for 6 h.

    Requests that carry extra headers (for example an authorized support session cookie) are
    never cached, so one user's authenticated content cannot be served to another session.
    """
    url = urldefrag(url)[0]
    now = time.time()
    if headers:
        return _download(url, session or requests, headers)
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    result = _download(url, session or requests)
    with _cache_lock:
        _cache[url] = (now, result)
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _download(url: str, http, extra_headers: dict | None = None) -> tuple[bytes, str, str]:
    current = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5"}
    base_headers = dict(headers)
    origin_host = (urlparse(url).hostname or "").lower()
    if extra_headers:
        headers.update(extra_headers)
    for hop in range(MAX_REDIRECTS + 1):
        if not allowed_url(current):
            return b"", "", "Blocked: URL or redirect is not on the approved vendor host list"
        try:
            time.sleep(REQUEST_PAUSE_SECONDS)
            # credentials are only ever sent to the host they were meant for, never to a redirect target
            send = headers if (urlparse(current).hostname or "").lower() == origin_host else base_headers
            with http.get(current, headers=send, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=False) as r:
                if r.is_redirect or r.is_permanent_redirect:
                    loc = r.headers.get("Location", "")
                    if not loc or hop == MAX_REDIRECTS:
                        return b"", "", "Too many or invalid redirects"
                    current = urljoin(current, loc)
                    continue
                if r.status_code != 200:
                    return b"", "", f"HTTP {r.status_code}"
                chunks, size = [], 0
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        return b"", "", "Page larger than the 12 MB limit"
                    chunks.append(chunk)
                return b"".join(chunks), r.headers.get("Content-Type", "").lower(), "Fetched"
        except requests.RequestException as exc:
            return b"", "", f"Fetch failed: {exc.__class__.__name__}"
    return b"", "", "Too many redirects"


# --------------------------------------------------------------------------- small helpers
def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text or "").strip()


def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9.]+", " ", str(text).lower()).strip()
    t = re.sub(r"\bmgmt\b", "management", t)
    return t


def _strip_lifecycle(name: str) -> str:
    """'CA 7 12.0 (End Of Service)' / 'Bundl 5.0 (End Of Life' -> 'CA 7 12.0'."""
    return _clean(re.sub(r"\((?:End Of (?:Service|Life)|EOS|EOL)[^)]*\)?\s*$", "", name, flags=re.IGNORECASE))


def split_name_vrm(name: str) -> tuple[str, str]:
    """'ACF2 for z/OS 16.0' -> ('ACF2 for z/OS', '16.0')."""
    m = re.search(r"\s(\d+(?:\.\d+)*(?:\s*&\s*\d+(?:\.\d+)*)?)\s*$", name)
    return (name[:m.start()].strip(), m.group(1).strip()) if m else (name.strip(), "")


def release_of(text: str) -> str:
    """'Z/OS V3R2' -> '3.2' (empty string if not a z/OS release header)."""
    m = ZOS_RELEASE.search(text or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else ""


def normalize_status(text: str) -> str:
    t = _clean(text).upper()
    for key, val in STATUS_MAP.items():
        if t.startswith(key):
            return val
    return _clean(text).title() or "Unverified"


def extract_ids(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (ptf_ids, usermod_ids, ibm_service_ids) found in text, in order, de-duplicated.

    Any ID on a line that names a USERMOD/SYSMOD is reported as a usermod/sysmod ID.
    IBM z/OS service IDs (UA/OA/PH... prefixes) are reported separately because they
    are z/OS maintenance, not a Broadcom product PTF.
    """
    ptfs: list[str] = []
    usermods: list[str] = []
    other: list[str] = []
    for line in re.split(r"[\n\r]+|(?<=[.;])\s+(?=[A-Z])", text or ""):
        ids = FIX_ID.findall(line)
        explicit = [m.group(1) for m in re.finditer(r"(?:USERMOD|SYSMOD)S?\s*[:#-]?\s*([A-Z0-9]{7})\b", line, re.IGNORECASE)]
        is_usermod_line = bool(USERMOD_WORD.search(line))
        for fid in explicit:
            fid = fid.upper()
            if fid not in usermods and any(ch.isdigit() for ch in fid):
                usermods.append(fid)
        for fid in ids:
            if fid[:2] in IBM_SERVICE_PREFIXES:
                if fid not in other:
                    other.append(fid)
            elif is_usermod_line:
                if fid not in usermods:
                    usermods.append(fid)
            elif fid not in ptfs:
                ptfs.append(fid)
    ptfs = [p for p in ptfs if p not in usermods]
    return ptfs, usermods, other


def _text_of(node) -> str:
    """Readable text with line breaks preserved (<br>, <pre>, block elements)."""
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return "\n".join(_clean(line) for line in node.get_text("\n").splitlines() if _clean(line))


# --------------------------------------------------------------------------- grid parsing
def parse_grid(html: bytes | str, base_url: str = GRID_URL):
    """Parse the grid page. Returns (soup, records) where records is a list[ProductRecord]
    with ReleaseResult.status/grid_text set and the raw cell links stored in _cell_links."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[ProductRecord] = []
    for table in soup.find_all("table"):
        header_cells = None
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if cells and any("product" in c.get_text(" ", strip=True).lower() and "release" in c.get_text(" ", strip=True).lower() for c in cells[:1]):
                header_cells = cells
                break
        if not header_cells:
            continue
        columns = [release_of(c.get_text(" ", strip=True)) for c in header_cells]
        if not any(columns):
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) < 2 or cells is header_cells or cells[0] is header_cells[0]:
                continue
            name_cell = cells[0]
            name = _clean(name_cell.get_text(" ", strip=True))
            if not name or "product" in name.lower() and "release" in name.lower():
                continue
            anchor = name_cell.find("a", href=True)
            product_url = urljoin(base_url, anchor["href"]) if anchor else ""
            clean_name = _strip_lifecycle(name)
            prod, vrm = split_name_vrm(clean_name)
            rec = ProductRecord(name=name, clean_name=clean_name, vrm=vrm, product_url=product_url)
            for idx, cell in enumerate(cells[1:], start=1):
                rel = columns[idx] if idx < len(columns) else ""
                if not rel:
                    continue
                text_lines = _text_of(BeautifulSoup(str(cell), "html.parser")).splitlines()
                status = normalize_status(text_lines[0]) if text_lines else "Unverified"
                res = ReleaseResult(zos=rel, status=status, grid_text=" / ".join(text_lines))
                links: list[CellLink] = []
                for a in cell.find_all("a", href=True):
                    href = a["href"].strip()
                    if href.lower().startswith(("javascript:", "mailto:")):
                        continue
                    text = _clean(a.get_text(" ", strip=True))
                    abs_url = urljoin(base_url, href)
                    anchor_id = href[1:] if href.startswith("#") else (urlparse(abs_url).fragment if urldefrag(abs_url)[0] == urldefrag(base_url)[0] else "")
                    low = (text + " " + anchor_id).lower()
                    kind = "ptf" if ("ptf" in low) else ("info" if ("addinfo" in low or "additional" in low or "info" in low) else "other")
                    links.append(CellLink(text=text, href=abs_url, anchor_id=anchor_id, kind=kind))
                res._cell_links = links  # type: ignore[attr-defined]
                res.grid_links = ", ".join(dict.fromkeys(l.text for l in links if l.text))
                rec.releases[rel] = res
            records.append(rec)
        if records:
            break
    return soup, records


# --------------------------------------------------------------------------- section extraction
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _find_anchor(soup: BeautifulSoup, anchor_id: str):
    el = soup.find(attrs={"id": anchor_id})
    if el is None:
        el = soup.find("a", attrs={"name": anchor_id})
    return el


def section_for_anchor(soup: BeautifulSoup, anchor_id: str) -> tuple[str, list[str]]:
    """Text (and external hyperlinks) of the section an in-page anchor points to.

    * container element with content (li / div / section / td / p / pre): its whole text
    * heading: the heading plus following siblings up to the next heading
    * empty marker (<a id=...>): following siblings up to the next marker/heading
    """
    el = _find_anchor(soup, anchor_id)
    if el is None:
        return "", []
    nodes: list = []
    if el.name in _HEADING_TAGS:
        nodes.append(el)
        for sib in el.next_siblings:
            if isinstance(sib, Tag) and (sib.name in _HEADING_TAGS or sib.get("id")):
                break
            nodes.append(sib)
    elif len(el.get_text(strip=True)) > 0 and el.name not in ("a", "span"):
        nodes.append(el)
    else:
        parent_text = el.parent.get_text(" ", strip=True) if el.parent else ""
        if el.name in ("a", "span") and len(parent_text) > len(el.get_text(" ", strip=True)) + 3 and el.parent.name in ("li", "td", "p", "div", "pre"):
            nodes.append(el.parent)
        else:
            nodes.append(el)
            for sib in el.next_siblings:
                if isinstance(sib, Tag) and (sib.name in _HEADING_TAGS or sib.get("id") or (sib.name == "a" and sib.get("name"))):
                    break
                nodes.append(sib)
    texts, links = [], []
    for n in nodes:
        if isinstance(n, Tag):
            clone = BeautifulSoup(str(n), "html.parser")
            for a in clone.find_all("a", href=True):
                href = a["href"].strip()
                if href and not href.startswith("#") and not href.lower().startswith(("javascript:", "mailto:")):
                    links.append(href)
            texts.append(_text_of(clone))
        else:
            s = _clean(str(n))
            if s:
                texts.append(s)
    return "\n".join(t for t in texts if t)[:5000], links


def parse_fixcategory_blocks(soup: BeautifulSoup) -> dict[tuple[str, str], list[tuple[str, str, list[str]]]]:
    """Fallback index built from the page's heading + 'Fix Category:' blocks.

    key = (normalised product name without lifecycle suffix, "3.2") -> [(kind, text, links)]
    kind is "ptf" when the surrounding h2 mentions PTFs, otherwise "info".
    """
    index: dict[tuple[str, str], list[tuple[str, str, list[str]]]] = {}
    kind = "info"
    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        if h.name in ("h2", "h3"):
            kind = "ptf" if "ptf" in h.get_text(" ", strip=True).lower() else "info"
            continue
        product = _norm(_strip_lifecycle(h.get_text(" ", strip=True)))
        if not product:
            continue
        block: list = []
        for sib in h.next_siblings:
            if isinstance(sib, Tag) and sib.name in _HEADING_TAGS:
                break
            block.append(sib)
        parts, links = [], []
        for n in block:
            if isinstance(n, Tag):
                clone = BeautifulSoup(str(n), "html.parser")
                links += [a["href"] for a in clone.find_all("a", href=True) if not a["href"].startswith("#")]
                parts.append(_text_of(clone))
            else:
                parts.append(_clean(str(n)))
        text = "\n".join(p for p in parts if p)
        segments = re.split(r"(?=Fix\s+Category:)", text, flags=re.IGNORECASE)
        for seg in segments:
            m = FIX_CATEGORY.search(seg)
            if not m:
                continue
            rel = release_of(m.group(1)) if "z/os" in m.group(1).lower() else ""
            if rel:
                index.setdefault((product, rel), []).append((kind, seg, links))
    return index


def _shorten(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- linked pages
def scrape_linked_page(url: str, session: requests.Session | None = None) -> LinkedPageResult:
    """Fetch one hyperlinked page and pull PTF / USERMOD IDs from lines that talk about fixes."""
    payload, ctype, status = fetch(url, session)
    if status != "Fetched":
        return LinkedPageResult(url=url, status=status)
    text, title = "", ""
    try:
        if "pdf" in ctype or urlparse(url).path.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(payload), strict=False)
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
        else:
            soup = BeautifulSoup(payload, "html.parser")
            for el in soup(["script", "style", "noscript", "svg"]):
                el.decompose()
            title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
            text = _text_of(soup)
    except Exception as exc:  # parser problems must never stop the whole run
        return LinkedPageResult(url=url, status=f"Could not parse page: {exc.__class__.__name__}")
    if not text.strip():
        return LinkedPageResult(url=url, status="Fetched, but no readable static text (login or script-rendered page?)", title=title)
    relevant = "\n".join(line for line in text.splitlines() if FIX_CONTEXT_WORD.search(line) and (FIX_ID.search(line) or USERMOD_WORD.search(line)))
    ptfs, usermods, other = extract_ids(relevant)
    return LinkedPageResult(url=url, status="Scraped", ptfs=ptfs[:40], usermods=usermods[:40], other_ids=other[:20], title=title)


# --------------------------------------------------------------------------- main entry point
@dataclass
class ScrapeResult:
    records: list[ProductRecord]
    linked_pages: dict[str, LinkedPageResult]
    grid_url: str
    status: str
    scraped_at: str


def scrape_grid(
    grid_url: str = GRID_URL,
    html: bytes | str | None = None,
    follow_links: bool = True,
    follow_product_pages: bool = True,
    max_links_per_release: int = 4,
    max_workers: int = 6,
    limit: int | None = None,
    name_filter: str = "",
    progress=None,
) -> ScrapeResult:
    """Scrape the Broadcom grid. `html` lets you pass a saved copy of the page (offline/testing)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    status = "Fetched"
    if html is None:
        payload, _, status = fetch(grid_url)
        if status != "Fetched":
            return ScrapeResult([], {}, grid_url, f"Could not fetch the grid page: {status}", stamp)
        html = payload
    soup, records = parse_grid(html, grid_url)
    if not records:
        return ScrapeResult([], {}, grid_url, "The page was fetched but no 'Product/Release' table was found", stamp)

    if name_filter:
        records = [r for r in records if name_filter.lower() in r.name.lower()]
    if limit:
        records = records[:limit]
    fallback = parse_fixcategory_blocks(soup)

    # ---- step 1: resolve the in-page PTFs / Additional Information sections
    link_jobs: dict[str, str] = {}   # url -> reason
    for rec in records:
        prod_key = _norm(rec.clean_name)
        if follow_product_pages and rec.product_url and allowed_url(rec.product_url):
            link_jobs.setdefault(rec.product_url, "product page")
        for rel, res in rec.releases.items():
            cell_links: list[CellLink] = getattr(res, "_cell_links", [])
            section_texts: list[str] = []
            extra_links: list[str] = []
            for cl in cell_links:
                if cl.anchor_id:
                    text, links = section_for_anchor(soup, cl.anchor_id)
                    if text:
                        section_texts.append(text)
                        extra_links += links
                        res.section_urls.append(cl.href)
                elif cl.kind == "other" and cl.href:
                    extra_links.append(cl.href)
            if not section_texts and cell_links:     # anchors did not resolve: use heading + Fix Category index
                for kind, seg, links in fallback.get((prod_key, rel), []):
                    section_texts.append(seg)
                    extra_links += links
                    res.section_urls.append(f"{grid_url}#fixcategory:{rel}")
            blob = "\n".join(section_texts)
            ptfs, usermods, other = extract_ids(blob)
            res.ptfs, res.usermods, res.other_ids = ptfs, usermods, other
            res.notes = [_shorten(t) for t in section_texts]
            urls = []
            for href in extra_links:
                absolute = urldefrag(urljoin(grid_url, href))[0]
                if absolute != urldefrag(grid_url)[0] and allowed_url(absolute) and absolute not in urls:
                    urls.append(absolute)
            res.linked_urls = urls[:max_links_per_release]
            if follow_links:
                for u in res.linked_urls:
                    link_jobs.setdefault(u, "linked from grid section")

    # ---- step 2: scrape the additional hyperlinks (depth 1)
    pages: dict[str, LinkedPageResult] = {}
    if follow_links and link_jobs:
        with requests.Session() as session, ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(scrape_linked_page, u, session): u for u in link_jobs}
            done = 0
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    pages[u] = fut.result()
                except Exception as exc:
                    pages[u] = LinkedPageResult(url=u, status=f"Error: {exc.__class__.__name__}")
                done += 1
                if progress:
                    progress(done, len(futures))

        # ---- step 3: attach what the product pages and linked pages found to each release
        product_pages: dict[str, dict[str, tuple[list[str], list[str]]]] = {}
        for rec in records:
            by_release: dict[str, tuple[list[str], list[str]]] = {}
            pp = pages.get(rec.product_url)
            if follow_product_pages and pp and pp.status == "Scraped":
                payload, _, st = fetch(rec.product_url)
                if st == "Fetched":
                    psoup = BeautifulSoup(payload, "html.parser")
                    by_release = product_page_release_ids(psoup, rec.vrm)
            product_pages[rec.name] = by_release
            for rel, res in rec.releases.items():
                found: list[str] = []
                for u in res.linked_urls:
                    lp = pages.get(u)
                    if lp and lp.status == "Scraped":
                        found += lp.ptfs
                        for um in lp.usermods:
                            if um not in res.usermods:
                                res.usermods.append(um)
                pp_ptfs, pp_usermods = by_release.get(rel, ([], []))
                found += pp_ptfs
                for um in pp_usermods:
                    if um not in res.usermods:
                        res.usermods.append(um)
                if (pp_ptfs or pp_usermods) and rec.product_url not in res.linked_urls:
                    res.linked_urls.append(rec.product_url)
                res.linked_ptfs = [p for p in dict.fromkeys(found) if p not in res.ptfs]
    return ScrapeResult(records, pages, grid_url, status, stamp)


def _fixcategory_ids(psoup: BeautifulSoup) -> dict[str, list[str]]:
    """{'3.2': [PTF ids]} from 'Fix Category: ...z/OS.V3R2' blocks on a per-product page."""
    text = _text_of(psoup)
    out: dict[str, list[str]] = {}
    for seg in re.split(r"(?=Fix\s+Category:)", text, flags=re.IGNORECASE):
        m = FIX_CATEGORY.search(seg)
        if not m or "z/os" not in m.group(1).lower():
            continue
        rel = release_of(m.group(1))
        if not rel:
            continue
        body = re.split(r"This page last updated", seg[m.end():])[0]
        ptfs, _, _ = extract_ids(body)
        out.setdefault(rel, [])
        out[rel] += [p for p in ptfs if p not in out[rel]]
    return out


def product_page_release_ids(psoup: BeautifulSoup, vrm: str) -> dict[str, tuple[list[str], list[str]]]:
    """Per-product page: {'3.2': ([PTF ids], [USERMOD ids])} for the product release `vrm`.

    1. Find the 'z/OS Compatibility Matrix' table (rows 'Z/OS V3R2', columns = product releases),
       pick the column for this VRM and follow the PTFs / Info anchors in that cell.
    2. For z/OS releases whose cell has no resolvable anchor, fall back to the page's
       'Fix Category: ...z/OS.V3R2' blocks (these may cover several product releases, so
       the dashboard labels them 'verify the release').
    """
    result: dict[str, tuple[list[str], list[str]]] = {}
    want = _norm(vrm)
    for table in psoup.find_all("table"):
        rows = table.find_all("tr")
        header = next((r for r in rows if "compatibility" in r.get_text(" ", strip=True).lower()
                       and "release" in r.get_text(" ", strip=True).lower()), None)
        if header is None:
            continue
        head_cells = [c.get_text(" ", strip=True) for c in header.find_all(["th", "td"], recursive=False)]
        versions = head_cells[1:]
        col = next((i for i, v in enumerate(versions) if _norm(v) == want), None)
        if col is None and len(versions) == 1:
            col = 0
        if col is None:
            continue
        for r in rows:
            cells = r.find_all(["th", "td"], recursive=False)
            if len(cells) < col + 2:
                continue
            rel = release_of(cells[0].get_text(" ", strip=True))
            if not rel or "z/os" not in cells[0].get_text(" ", strip=True).lower():
                continue
            texts = []
            for a in cells[col + 1].find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("#"):
                    sec, _ = section_for_anchor(psoup, href[1:])
                    if sec:
                        texts.append(sec)
            if texts:
                ptfs, usermods, _ = extract_ids("\n".join(texts))
                result[rel] = (ptfs, usermods)
    for rel, ids in _fixcategory_ids(psoup).items():
        if rel not in result and ids:
            result[rel] = (ids, [])
    return result


# --------------------------------------------------------------------------- matching dashboard rows to grid rows
# Dashboard CSV product names that differ from the grid's wording (normalised with _norm()).
# value = normalised grid product name(s) (without version) the CSV row stands for.
NAME_ALIASES: dict[str, list[str]] = {
    "cross enterprise apm": ["cross enterprise application performance management"],
    "dras": ["distributed repository access system dras"],
    "file master plus for db2": ["file master plus for db2 for z/os"],
    "l serv": ["l serv common components and services"],
    "scheduler job mgmt smart console": ["scheduler job management smart console option"],
    "solve:operations automation": ["solve:operations automation non japan see addinfo"],
    "vision:results interface to db2 full function read": [
        "vision:results interface to db2 full function", "vision:results interface to db2 read"],
    "vision:results interface to idms db full read only": [
        "vision:results interface to idms db full function", "vision:results interface to idms db read only"],
    "vision:results interface to ims db full read": [
        "vision:results interface to ims db full", "vision:results interface to ims db read"],
}


NAME_ALIASES = {_norm(k).replace(",", ""): [_norm(v).replace(",", "") for v in vals] for k, vals in NAME_ALIASES.items()}


def _vrm_key(v: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^v\s*", "", str(v).strip().lower()))


def _vrm_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return None


def _vrm_in(csv_vrm: str, grid_vrm: str) -> bool:
    c, g = _vrm_key(csv_vrm), _vrm_key(grid_vrm)
    if c == g:
        return True
    m = re.fullmatch(r"(\S+)\s+to\s+(\S+)", c)               # '1.0 to 1.4'
    if m and _vrm_tuple(m.group(1)) and _vrm_tuple(m.group(2)) and _vrm_tuple(g):
        return _vrm_tuple(m.group(1)) <= _vrm_tuple(g) <= _vrm_tuple(m.group(2))
    return False


def match_records(product: str, vrm: str, records: list[ProductRecord]) -> list[ProductRecord]:
    """Grid rows that a dashboard CSV row (product + VRM) refers to. Usually one; can be several
    (e.g. 'VISION:Results Interface to Db2 (Full Function / Read)' or 'WatchTower z/IRIS-D 1.0 to 1.4')."""
    import difflib
    pn = _norm(product).replace(",", "")
    targets = NAME_ALIASES.get(pn)
    if targets is None:
        # drop punctuation differences like 'WatchTower Foundation, Alert ...'
        targets = [pn]
    out: list[ProductRecord] = []
    prepared = [(r, _norm(split_name_vrm(r.clean_name)[0]).replace(",", ""), r.vrm) for r in records]
    for t in targets:
        exact = [r for r, gp, gv in prepared if gp == t and _vrm_in(vrm, gv)]
        if not exact:
            starts = [r for r, gp, gv in prepared if (gp.startswith(t) or t.startswith(gp)) and _vrm_in(vrm, gv)]
            exact = starts
        if not exact:
            scored = sorted(((difflib.SequenceMatcher(None, t, gp).ratio(), r) for r, gp, gv in prepared if _vrm_in(vrm, gv)),
                            key=lambda x: -x[0])
            if scored and scored[0][0] >= 0.88:
                exact = [scored[0][1]]
        out += [r for r in exact if r not in out]
    return out


def merge_release(records: list[ProductRecord], zos: str) -> tuple[str, str, str, str]:
    """Combine several grid rows (rare) into (ptfs, usermods, linked_ptfs, source_urls) text for one z/OS release."""
    ptfs, mods, linked, urls = [], [], [], []
    for r in records:
        res = r.releases.get(zos)
        if not res:
            continue
        ptfs += res.ptfs; mods += res.usermods; linked += res.linked_ptfs
        urls += res.section_urls + res.linked_urls
    d = lambda xs: ", ".join(dict.fromkeys(xs))
    return d(ptfs), d(mods), d(linked), "\n".join(dict.fromkeys(urls))


# --------------------------------------------------------------------------- flat output
def flatten(result: ScrapeResult) -> list[dict]:
    """One dict per product + z/OS release, ready for pandas / Excel."""
    rows = []
    for rec in result.records:
        product, vrm = split_name_vrm(rec.clean_name)
        for rel, res in rec.releases.items():
            all_ptfs = list(dict.fromkeys(res.ptfs))
            rows.append({
                "Vendor": "Broadcom",
                "Product": product,
                "VRM": vrm,
                "Grid name": rec.name,
                "z/OS release": rel,
                "Status (grid)": res.status,
                "PTFs (grid sections)": ", ".join(all_ptfs),
                "USERMOD / SYSMOD IDs": ", ".join(res.usermods),
                "Additional PTFs (linked pages)": ", ".join(res.linked_ptfs),
                "IBM z/OS service IDs mentioned": ", ".join(res.other_ids),
                "Grid links": res.grid_links,
                "Section notes": "\n".join(res.notes),
                "Section source": "\n".join(dict.fromkeys(res.section_urls)),
                "Hyperlinks scraped": "\n".join(dict.fromkeys(res.linked_urls)),
                "Product page": rec.product_url,
            })
    return rows
