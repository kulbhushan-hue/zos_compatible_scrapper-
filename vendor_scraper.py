"""
Multi-vendor PTF / USERMOD / SYSMOD scraper (IBM, BMC, Rocket, Precisely, ASG, Broadcom pages).

What it does for every vendor
  1. Opens each source URL listed in the vendor's compatibility CSV.
  2. If the site wants a login it says so (status "Login required") and, when you supply a user name and
     password (or a cookie / basic-auth pair), signs in and retries. Credentials are used only for hosts that
     belong to that vendor, are held in memory for the run, and are never written to disk, logs or the Excel file.
  3. If the source has more than one page it keeps going: "next" links, numbered pagination and ?page=N style
     URLs for HTML, page by page for PDFs. At least 10 pages are searched whenever that many exist.
  4. Finds the passages that mention each product (and version), pulls PTF, USERMOD/SYSMOD and APAR IDs out of
     them, assigns them to z/OS 3.2 / 3.1 / 2.5 when the passage names a release, and follows hyperlinks in
     those passages one level deep.

Nothing is invented: an ID is reported only if it appears in fetched text. "No ID found" never means
"no maintenance required", and IDs whose passage does not name a z/OS release are kept apart as
"release not stated" so they are never silently attributed to the wrong release.
"""

from __future__ import annotations

import io
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

import broadcom_scraper as bs

USER_AGENT = "zOS-Compatibility-Dashboard/1.2 (vendor support lookup; read-only)"
REQUEST_TIMEOUT = (6, 25)
MAX_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
MIN_PAGES = 10                 # "at least 10 pages" whenever the source has that many
DEFAULT_MAX_PAGES = 10
DEFAULT_PDF_PAGES = 60
PAUSE = 0.15
ZOS_RELEASES = ("3.2", "3.1", "2.5")
UNSPECIFIED = "not stated"

# --------------------------------------------------------------------------- vendor profiles
@dataclass(frozen=True)
class VendorProfile:
    key: str
    label: str
    hosts: tuple[str, ...]              # credentials and requests never leave these hosts
    login_note: str = ""


PROFILES: dict[str, VendorProfile] = {
    "IBM": VendorProfile("IBM", "IBM", ("ibm.com",),
                         "IBMid sign-in often uses SSO / multi-factor authentication, which a script cannot complete. "
                         "If form login fails, paste the Cookie header of a signed-in browser session instead."),
    "BMC": VendorProfile("BMC", "BMC", ("bmc.com",),
                         "BMC support content (docs.bmc.com, support.bmc.com) usually needs a BMC Support login."),
    "Rocket": VendorProfile("Rocket", "Rocket Software", ("rocketsoftware.com",),
                            "Rocket Support Central content needs a Rocket Support login."),
    "Precisely": VendorProfile("Precisely", "Precisely", ("precisely.com",),
                               "Precisely community and support-portal pages may need a Precisely login."),
    "ASG": VendorProfile("ASG", "ASG (now Rocket)", ("asg.com", "rocketsoftware.com"),
                         "ASG products are supported through Rocket; use a Rocket Support login."),
    "Broadcom": VendorProfile("Broadcom", "Broadcom", ("broadcom.com", "ca.com"),
                              "Broadcom Support (support.broadcom.com) needs a Broadcom Support login."),
}


def profile_for(vendor: str) -> VendorProfile:
    key = next((k for k in PROFILES if k.lower() == str(vendor).strip().lower()), None)
    if key:
        return PROFILES[key]
    return VendorProfile(str(vendor), str(vendor), ())


def host_allowed(url: str, profile: VendorProfile) -> bool:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return False
    host = p.hostname.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in profile.hosts)


# --------------------------------------------------------------------------- ID extraction
# IBM: PTFs start with U (UJ, UI, UK, UA, UQ, UR, UN ...); APARs are PH/PI/PJ/PK/PM/PQ/OA/OW/II + 5 digits.
IBM_PTF = re.compile(r"\bU[A-Z]\d{5}\b")
IBM_APAR = re.compile(r"\b(?:PH|PI|PJ|PK|PM|PQ|OA|OW|II)\d{5}\b")
# Other vendors: 2-3 letters + 4-5 digits, or 1 letter + 6 digits (SMP/E style IDs).
GENERIC_FIX = re.compile(r"\b(?:[A-Z]{2}\d{5}|[A-Z]{3}\d{4,5}|[A-Z]\d{6})\b")
FIX_KEYWORD = re.compile(r"\b(ptfs?|apars?|fix(?:es|pack)?|hotfix(?:es)?|patch(?:es)?|maintenance|usermods?|sysmods?|zaps?|"
                         r"service\s+level|requires?|required|apply|install(?:ed)?|prerequisites?|corequisites?|solution)\b", re.IGNORECASE)
USERMOD_WORD = re.compile(r"\b(usermods?|sysmods?)\b", re.IGNORECASE)
EXPLICIT_MOD = re.compile(r"(?:USERMOD|SYSMOD)S?\s*[:#-]?\s*([A-Z0-9]{7})\b", re.IGNORECASE)
ZOS_MENTION = [
    re.compile(r"(?:z/?OS|zOS)\s*(?:version\s*)?V?\s*(\d{1,2})\s*[.R]\s*(\d)", re.IGNORECASE),
    re.compile(r"\bV(\d{1,2})R(\d)\b", re.IGNORECASE),
]
LOGIN_WORDS = re.compile(r"\b(sign[\s-]?in|log[\s-]?in|authentication required|please authenticate|session (?:has )?expired|"
                         r"access denied|not authori[sz]ed|forbidden)\b", re.IGNORECASE)
LOGIN_URL_HINT = re.compile(r"(login|signin|sign-in|logon|sso|/auth|oauth|idp|saml|cas/|ibmid|okta)", re.IGNORECASE)


def extract_ids(text: str, vendor: str = "") -> tuple[list[str], list[str], list[str]]:
    """Return (ptf_ids, usermod_ids, apar_ids) from lines that talk about fixes. De-duplicated, in order."""
    ibm = vendor.strip().lower() == "ibm"
    ptfs: list[str] = []
    mods: list[str] = []
    apars: list[str] = []

    def add(lst, v):
        if v not in lst:
            lst.append(v)

    for line in re.split(r"[\n\r]+|(?<=[.;])\s+(?=[A-Z])", text or ""):
        if not FIX_KEYWORD.search(line):
            continue
        is_mod_line = bool(USERMOD_WORD.search(line))
        for m in EXPLICIT_MOD.finditer(line):
            tok = m.group(1).upper()
            if any(c.isdigit() for c in tok):
                add(mods, tok)
        if ibm:
            for t in IBM_APAR.findall(line):
                add(apars, t)
            for t in IBM_PTF.findall(line):
                add(mods if is_mod_line else ptfs, t)
        else:
            for t in GENERIC_FIX.findall(line):
                if re.fullmatch(r"(?:ISO|RFC|CVE|SHA|AES|TLS|SSL|TCP|UDP)\d+", t):
                    continue
                add(mods if is_mod_line else ptfs, t)
            for t in re.findall(r"\bAPARs?\s*[:#]?\s*([A-Z]{2}\d{5})\b", line):
                add(apars, t)
    ptfs = [p for p in ptfs if p not in mods and p not in apars]
    return ptfs, mods, apars


def zos_releases_in(text: str) -> list[str]:
    found: list[str] = []
    for rx in ZOS_MENTION:
        for m in rx.finditer(text or ""):
            rel = f"{int(m.group(1))}.{int(m.group(2))}"
            if rel in ZOS_RELEASES and rel not in found:
                found.append(rel)
    return found


# --------------------------------------------------------------------------- credentials and session
@dataclass
class Credentials:
    """Everything is optional. method: 'auto' (form login), 'basic' (HTTP Basic), 'cookie' (paste a session cookie)."""
    username: str = ""
    password: str = ""
    method: str = "auto"
    login_url: str = ""
    cookie: str = ""

    def present(self) -> bool:
        return bool((self.username and self.password) or self.cookie)

    def __repr__(self) -> str:                      # never print secrets
        return f"Credentials(method={self.method!r}, user={'set' if self.username else 'unset'}, secret=hidden)"


class Fetcher:
    """HTTP client for one vendor: host-scoped, redirect-checked, optional login, polite delay."""

    def __init__(self, profile: VendorProfile, creds: Credentials | None = None, http=None):
        self.profile = profile
        self.creds = creds or Credentials()
        self.http = http or requests.Session()
        self.logged_in = False
        self.login_message = ""

    # ---- low level
    def _headers(self, url: str) -> dict:
        h = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5"}
        if self.creds.cookie and host_allowed(url, self.profile):
            h["Cookie"] = self.creds.cookie.strip()
        return h

    def _auth(self, url: str):
        if self.creds.method == "basic" and self.creds.username and host_allowed(url, self.profile):
            return (self.creds.username, self.creds.password)
        return None

    def get(self, url: str) -> tuple[bytes, str, str, str]:
        """Returns (payload, content_type, status, final_url). status is 'Fetched', 'Login required', or a failure text."""
        current = urldefrag(url)[0]
        for hop in range(MAX_REDIRECTS + 1):
            if not host_allowed(current, self.profile):
                login_like = bool(LOGIN_URL_HINT.search(current))
                return b"", "", ("Login required (redirected to a sign-in host outside the approved list)" if login_like
                                 else "Blocked: URL is not on this vendor's approved host list"), current
            try:
                time.sleep(PAUSE)
                kw = {"headers": self._headers(current), "timeout": REQUEST_TIMEOUT, "stream": True, "allow_redirects": False}
                auth = self._auth(current)
                if auth:
                    kw["auth"] = auth
                with self.http.get(current, **kw) as r:
                    if r.is_redirect or r.is_permanent_redirect:
                        loc = r.headers.get("Location", "")
                        if not loc or hop == MAX_REDIRECTS:
                            return b"", "", "Too many or invalid redirects", current
                        current = urldefrag(urljoin(current, loc))[0]
                        continue
                    if r.status_code in (401, 403):
                        return b"", "", "Login required", current
                    if r.status_code != 200:
                        return b"", "", f"HTTP {r.status_code}", current
                    size, chunks = 0, []
                    for chunk in r.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            return b"", "", "Page larger than the 15 MB limit", current
                        chunks.append(chunk)
                    return b"".join(chunks), r.headers.get("Content-Type", "").lower(), "Fetched", current
            except requests.RequestException as exc:
                return b"", "", f"Fetch failed: {exc.__class__.__name__}", current
        return b"", "", "Too many redirects", current

    # ---- login
    def needs_login(self, payload: bytes, ctype: str, final_url: str) -> bool:
        if is_pdf(payload):
            return False
        soup = BeautifulSoup(payload, "html.parser")
        if soup.find("input", attrs={"type": "password"}):
            return True
        text = soup.get_text(" ", strip=True)
        if LOGIN_URL_HINT.search(urlparse(final_url).path) and LOGIN_WORDS.search(text) and len(text) < 4000:
            return True
        return bool(len(text) < 600 and LOGIN_WORDS.search(text))

    def login(self, page_payload: bytes | None = None, page_url: str = "") -> bool:
        """Sign in with the supplied credentials. Returns True when the sign-in looks successful."""
        c = self.creds
        if c.cookie or (c.method == "basic" and c.username):
            self.logged_in = True
            self.login_message = "Using supplied " + ("cookie" if c.cookie else "HTTP Basic credentials")
            return True
        if not (c.username and c.password):
            self.login_message = "No user name / password supplied"
            return False
        form_html, form_url = page_payload, page_url
        if c.login_url:
            if not host_allowed(c.login_url, self.profile):
                self.login_message = "Login URL is not on this vendor's approved host list"
                return False
            form_html, _, st, form_url = self.get(c.login_url)
            if st != "Fetched":
                self.login_message = f"Could not open the login page: {st}"
                return False
        if not form_html:
            self.login_message = "No login form available"
            return False
        soup = BeautifulSoup(form_html, "html.parser")
        form = next((f for f in soup.find_all("form") if f.find("input", attrs={"type": "password"})), None)
        if form is None:
            self.login_message = "No login form (user name / password fields) was found on the page"
            return False
        data = {}
        user_field = pass_field = None
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            t = (inp.get("type") or "text").lower()
            if t == "password" and pass_field is None:
                pass_field = name
            elif t in ("text", "email") and user_field is None and re.search(r"user|email|login|id|name", name, re.IGNORECASE):
                user_field = name
            elif t in ("hidden", "submit") or inp.name != "input":
                data[name] = inp.get("value", "")
            elif t in ("text", "email") and user_field is None:
                user_field = name
        if not (user_field and pass_field):
            self.login_message = "The login form could not be understood (no user name / password field pair)"
            return False
        data[user_field] = c.username
        data[pass_field] = c.password
        action = urljoin(form_url or page_url, form.get("action") or form_url or page_url)
        if not host_allowed(action, self.profile):
            self.login_message = f"The login form posts to {urlparse(action).hostname}, which is outside this vendor's approved hosts"
            return False
        try:
            r = self.http.post(action, data=data, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            self.login_message = f"Login request failed: {exc.__class__.__name__}"
            return False
        body = BeautifulSoup(getattr(r, "content", b"") or b"", "html.parser")
        if r.status_code in (401, 403) or body.find("input", attrs={"type": "password"}):
            self.login_message = "Sign-in was not accepted (wrong credentials, or the site needs SSO / multi-factor authentication)"
            return False
        self.logged_in = True
        self.login_message = "Signed in"
        return True


# --------------------------------------------------------------------------- pages and pagination
@dataclass
class PageBlock:
    text: str
    links: list[str]
    page_no: int
    url: str
    kind: str = "html"          # html | pdf


@dataclass
class PageLog:
    vendor: str
    source_url: str
    page_no: int
    url: str
    kind: str
    status: str
    blocks: int = 0
    note: str = ""


NEXT_WORDS = {"next", "next page", "next ›", "›", "»", ">", ">>", "older", "more", "next »", "weiter", "suivant", "siguiente"}
PAGE_PARAMS = ("page", "p", "pg", "pagenumber", "pageno", "page_no")


def find_next_url(soup: BeautifulSoup, url: str) -> str:
    """URL of the next page of a paginated listing, or '' when there is none."""
    link = soup.find(["link", "a"], attrs={"rel": lambda v: v and "next" in (v if isinstance(v, list) else str(v).split())}, href=True)
    if link:
        return urldefrag(urljoin(url, link["href"]))[0]
    for a in soup.find_all("a", href=True):
        label = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("aria-label", ""), a.get("title", "")])).strip().lower()
        if label in NEXT_WORDS or re.fullmatch(r"(?:go to )?next( page)?", label) or label.startswith("next page"):
            if a.get("aria-disabled") == "true" or "disabled" in " ".join(a.get("class", [])).lower():
                continue
            href = a["href"].strip()
            if href and not href.lower().startswith(("javascript:", "#", "mailto:")):
                return urldefrag(urljoin(url, href))[0]
    # numbered pagination: find the active number and link the next one
    cur = None
    for el in soup.find_all(attrs={"aria-current": True}) + soup.find_all(class_=re.compile(r"\b(active|current|selected)\b", re.I)):
        t = el.get_text(strip=True)
        if t.isdigit():
            cur = int(t)
            break
    if cur is not None:
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True) == str(cur + 1):
                return urldefrag(urljoin(url, a["href"]))[0]
    # explicit page parameter in the URL: bump it (only when the page really looks like a listing)
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    for i, (k, v) in enumerate(q):
        if k.lower() in PAGE_PARAMS and v.isdigit():
            q[i] = (k, str(int(v) + 1))
            return urlunparse(p._replace(query=urlencode(q)))
    return ""


def has_pagination(soup: BeautifulSoup) -> bool:
    if soup.find(["link", "a"], attrs={"rel": lambda v: v and "next" in (v if isinstance(v, list) else str(v).split())}):
        return True
    nums = [a for a in soup.find_all("a", href=True) if a.get_text(strip=True).isdigit()]
    parents = {id(a.parent) for a in nums}
    return len(nums) >= 2 and len(parents) <= 3


def html_blocks(soup: BeautifulSoup, url: str, page_no: int) -> list[PageBlock]:
    for el in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        el.decompose()
    blocks: list[PageBlock] = []
    seen: set[str] = set()

    def add(el):
        text = " | ".join(t for t in (c.get_text(" ", strip=True) for c in el.find_all(["td", "th"], recursive=False)) if t) if el.name == "tr" \
            else bs._text_of(BeautifulSoup(str(el), "html.parser"))
        text = re.sub(r"\s+", " ", text).strip() if el.name == "tr" else text.strip()
        if len(text) < 3 or text in seen:
            return
        seen.add(text)
        links = [urldefrag(urljoin(url, a["href"]))[0] for a in el.find_all("a", href=True)
                 if not a["href"].lower().startswith(("javascript:", "mailto:", "#"))]
        blocks.append(PageBlock(text, links, page_no, url))

    for el in soup.find_all(["tr", "li", "p", "pre", "dd"]):
        if el.name in ("li", "p", "dd") and el.find(["tr", "li", "p"]):
            continue                                   # use the innermost block only
        add(el)
    # heading sections: heading + following siblings up to the next heading (narrative pages)
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        parts, links = [h.get_text(" ", strip=True)], []
        for sib in h.next_siblings:
            if isinstance(sib, Tag):
                if re.fullmatch(r"h[1-6]", sib.name or ""):
                    break
                parts.append(bs._text_of(BeautifulSoup(str(sib), "html.parser")))
                links += [urldefrag(urljoin(url, a["href"]))[0] for a in sib.find_all("a", href=True)
                          if not a["href"].lower().startswith(("javascript:", "mailto:", "#"))]
        text = "\n".join(p for p in parts if p).strip()
        if len(text) > 3 and text not in seen and len(text) < 6000:
            seen.add(text)
            blocks.append(PageBlock(text, links, page_no, url))
    return blocks


def is_pdf(payload: bytes, ctype: str = "", url: str = "") -> bool:
    """Decide by the file's own bytes, not the URL ending: some servers send HTML for *.pdf links (and the reverse)."""
    return payload[:1024].lstrip()[:5] == b"%PDF-"


def pdf_blocks(payload: bytes, url: str, max_pages: int) -> tuple[list[PageBlock], int, int]:
    """Text windows from a PDF. Returns (blocks, pages_read, total_pages)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(payload), strict=False)
    total = len(reader.pages)
    limit = min(total, max(max_pages, MIN_PAGES))
    blocks: list[PageBlock] = []
    for i in range(limit):
        text = reader.pages[i].extract_text() or ""
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
        step = 3
        for s in range(0, max(len(lines), 1), step):
            window = "\n".join(lines[s:s + 6])
            if window.strip():
                blocks.append(PageBlock(window, [], i + 1, url, "pdf"))
    return blocks, limit, total


# --------------------------------------------------------------------------- matching products to passages
STOP = {"for", "the", "and", "of", "z", "os", "zos", "ibm", "now", "current", "your", "release", "with", "on", "to", "a", "in",
        "rocket", "bmc", "asg", "precisely", "software", "version", "support", "supported"}


def product_tokens(product: str) -> list[str]:
    outside = re.sub(r"\([^)]*\)", " ", product)
    norm = re.sub(r"[^a-z0-9.+]+", " ", outside.lower()).split()
    toks = [t for t in norm if t not in STOP and len(t) >= 2]
    return toks


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9.+]+", " ", s.lower())


def block_matches(block_text: str, tokens: list[str]) -> bool:
    if not tokens:
        return False
    n = " " + _norm_text(block_text) + " "
    return all(f" {t} " in n or t in n for t in tokens)


def version_in(block_text: str, vrm: str) -> bool:
    v = str(vrm).strip()
    if not v or not any(ch.isdigit() for ch in v):
        return False
    return re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", block_text) is not None


@dataclass
class ReleaseIds:
    ptfs: list[str] = field(default_factory=list)
    usermods: list[str] = field(default_factory=list)
    apars: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def add(self, ptfs, mods, apars, note, source):
        for lst, vals in ((self.ptfs, ptfs), (self.usermods, mods), (self.apars, apars)):
            for v in vals:
                if v not in lst:
                    lst.append(v)
        if note and note not in self.notes and len(self.notes) < 3:
            self.notes.append(note)
        if source and source not in self.sources:
            self.sources.append(source)

    def empty(self) -> bool:
        return not (self.ptfs or self.usermods or self.apars)


@dataclass
class RowResult:
    vendor: str
    product: str
    vrm: str
    link: str
    status: dict = field(default_factory=dict)                 # {'3.2': 'Yes', ...} from the dashboard data
    releases: dict = field(default_factory=dict)                # '3.2' | ... | UNSPECIFIED -> ReleaseIds
    linked: dict = field(default_factory=dict)                  # same keys, IDs found on hyperlinked pages (verify)
    linked_urls: list[str] = field(default_factory=list)
    evidence: int = 0                                           # passages that matched the product
    version_confirmed: bool = False
    pages: list[int] = field(default_factory=list)
    note: str = ""


@dataclass
class VendorScrape:
    vendor: str
    rows: list[RowResult]
    pages: list[PageLog]
    linked_pages: dict                                          # url -> dict(status, title, ptfs, usermods, apars)
    login_required: list[str]                                   # source URLs that need a login
    login_message: str
    scraped_at: str
    max_pages: int


def _rel_ids(d: dict, key: str) -> ReleaseIds:
    return d.setdefault(key, ReleaseIds())


def scan_blocks(blocks: list[PageBlock], row: RowResult, vendor: str) -> None:
    toks = product_tokens(row.product)
    matched = [b for b in blocks if block_matches(b.text, toks)]
    if not matched:
        return
    confirmed = [b for b in matched if version_in(b.text, row.vrm)]
    use = confirmed or matched
    row.version_confirmed = row.version_confirmed or bool(confirmed)
    row.evidence += len(use)
    for b in use:
        if b.page_no not in row.pages:
            row.pages.append(b.page_no)
        for u in b.links:
            if u not in row.linked_urls and len(row.linked_urls) < 12:
                row.linked_urls.append(u)
        block_rels = zos_releases_in(b.text)
        # clause by clause, so "z/OS 3.2 needs X; z/OS 2.5 needs Y" keeps X with 3.2 and Y with 2.5
        for clause in [c for c in re.split(r"[;\n]|(?<=\.)\s+|\s\|\s", b.text) if c.strip()]:
            ptfs, mods, apars = extract_ids(clause, vendor)
            if not (ptfs or mods or apars):
                continue
            rels = zos_releases_in(clause) or (block_rels if len(block_rels) == 1 else []) or [UNSPECIFIED]
            note = re.sub(r"\s+", " ", b.text)[:400]
            for rel in rels:
                _rel_ids(row.releases, rel).add(ptfs, mods, apars, note, f"{b.url}" + (f" (page {b.page_no})" if b.kind == "pdf" or b.page_no > 1 else ""))


# --------------------------------------------------------------------------- the crawl
def _crawl_source(fetcher: Fetcher, source_url: str, max_pages: int, pdf_pages: int, vendor: str,
                  log: list[PageLog], login_required: list[str]) -> list[PageBlock]:
    """Fetch one source URL and keep following its pagination (at least 10 pages when they exist)."""
    blocks: list[PageBlock] = []
    url = urldefrag(source_url)[0]
    visited: set[str] = set()
    page_no = 0
    tried_login = False
    while url and url not in visited and page_no < max(max_pages, MIN_PAGES):
        visited.add(url)
        page_no += 1
        payload, ctype, status, final = fetcher.get(url)
        if status == "Fetched" and not is_pdf(payload) and fetcher.needs_login(payload, ctype, final):
            status = "Login required"
        if status.startswith("Login required"):
            if fetcher.creds.present() and not tried_login:
                tried_login = True
                if fetcher.login(payload if payload else None, final):
                    visited.discard(url)
                    page_no -= 1
                    continue
            log.append(PageLog(vendor, source_url, page_no, url, "html", status,
                               note=fetcher.login_message if tried_login else "Supply a user name and password to read this page"))
            if source_url not in login_required:
                login_required.append(source_url)
            break
        if status != "Fetched":
            log.append(PageLog(vendor, source_url, page_no, url, "html", status))
            break
        if is_pdf(payload):
            try:
                pb, read, total = pdf_blocks(payload, final, pdf_pages)
            except Exception as exc:
                log.append(PageLog(vendor, source_url, page_no, url, "pdf", f"Could not read PDF: {exc.__class__.__name__}"))
                break
            blocks += pb
            log.append(PageLog(vendor, source_url, 1, final, "pdf", "Scraped", len(pb), f"PDF pages searched: {read} of {total}"))
            break
        soup = BeautifulSoup(payload, "html.parser")
        nxt = find_next_url(soup, final)
        paginated = bool(nxt) or has_pagination(soup)
        pb = html_blocks(soup, final, page_no)
        blocks += pb
        log.append(PageLog(vendor, source_url, page_no, final, "html", "Scraped", len(pb),
                           "more pages follow" if nxt else ("last page" if paginated else "single page")))
        url = nxt
    return blocks


def scrape_vendor(vendor: str, rows, creds: Credentials | None = None, max_pages: int = DEFAULT_MAX_PAGES,
                  pdf_pages: int = DEFAULT_PDF_PAGES, follow_links: bool = True, max_links_per_row: int = 4,
                  http=None, progress=None) -> VendorScrape:
    """Scrape every distinct source URL of one vendor and attach IDs to each dashboard row.

    rows: iterable of dicts/Series with product, vrm, link, zos32, zos31, zos25.
    """
    profile = profile_for(vendor)
    fetcher = Fetcher(profile, creds, http)
    max_pages = max(int(max_pages or DEFAULT_MAX_PAGES), MIN_PAGES)
    results = [RowResult(vendor, str(r["product"]), str(r["vrm"]), str(r["link"]).strip(),
                         {"3.2": r.get("zos32", ""), "3.1": r.get("zos31", ""), "2.5": r.get("zos25", "")}) for r in rows]
    log: list[PageLog] = []
    login_required: list[str] = []
    by_source: dict[str, list[RowResult]] = {}
    for r in results:
        if r.link:
            by_source.setdefault(r.link, []).append(r)
        else:
            r.note = "No vendor URL supplied"
    sources = list(by_source)
    for i, src in enumerate(sources, 1):
        blocks = _crawl_source(fetcher, src, max_pages, pdf_pages, vendor, log, login_required)
        for r in by_source[src]:
            scan_blocks(blocks, r, vendor)
            if not blocks:
                r.note = ("Login required to read the source page (enter the vendor user name and password)"
                          if src in login_required else "Source page could not be read")
            elif r.evidence == 0:
                r.note = "Product not found in the pages searched"
            elif not r.version_confirmed and any(ch.isdigit() for ch in r.vrm):
                r.note = "Product found but this version was not named in the passage"
        if progress:
            progress(i, len(sources))

    linked_pages: dict = {}
    if follow_links:
        wanted: dict[str, None] = {}
        for r in results:
            r.linked_urls = [u for u in r.linked_urls if host_allowed(u, profile) and u not in by_source][:max_links_per_row]
            for u in r.linked_urls:
                wanted[u] = None

        def one(u):
            return u, _scan_linked(fetcher, u, pdf_pages, vendor)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for u, res in pool.map(one, list(wanted)):
                linked_pages[u] = res
                if res["status"].startswith("Login required") and u not in login_required:
                    login_required.append(u)
        for r in results:
            for u in r.linked_urls:
                res = linked_pages.get(u)
                if res and res["status"] == "Scraped":
                    rels = res["releases"] or [UNSPECIFIED]
                    for rel in rels:
                        _rel_ids(r.linked, rel).add(res["ptfs"], res["usermods"], res["apars"], "", u)
    return VendorScrape(vendor, results, log, linked_pages, login_required, fetcher.login_message,
                        time.strftime("%Y-%m-%d %H:%M:%S"), max_pages)


def _scan_linked(fetcher: Fetcher, url: str, pdf_pages: int, vendor: str) -> dict:
    payload, ctype, status, final = fetcher.get(url)
    out = {"status": status, "title": "", "ptfs": [], "usermods": [], "apars": [], "releases": []}
    if status == "Fetched" and not is_pdf(payload) and fetcher.needs_login(payload, ctype, final):
        out["status"] = "Login required"
        return out
    if status != "Fetched":
        return out
    try:
        if is_pdf(payload):
            blocks, _, _ = pdf_blocks(payload, final, pdf_pages)
            text = "\n".join(b.text for b in blocks)
        else:
            soup = BeautifulSoup(payload, "html.parser")
            out["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
            for el in soup(["script", "style", "noscript"]):
                el.decompose()
            text = bs._text_of(soup)
    except Exception as exc:
        out["status"] = f"Could not parse page: {exc.__class__.__name__}"
        return out
    if not text.strip():
        out["status"] = "Fetched, but no readable static text (script-rendered page?)"
        return out
    ptfs, mods, apars = extract_ids(text, vendor)
    out.update(status="Scraped", ptfs=ptfs[:40], usermods=mods[:40], apars=apars[:40])
    return out


# --------------------------------------------------------------------------- flat rows for Excel / dashboard
def flatten(scrape: VendorScrape) -> list[dict]:
    """One dict per product + z/OS release (+ one 'release not stated' row when IDs had no release)."""
    rows = []
    for r in scrape.rows:
        for rel in list(ZOS_RELEASES) + [UNSPECIFIED]:
            ids = r.releases.get(rel)
            lk = r.linked.get(rel)
            if rel == UNSPECIFIED and not (ids or lk):
                continue
            ids = ids or ReleaseIds()
            lk = lk or ReleaseIds()
            rows.append({
                "Vendor": scrape.vendor, "Product": r.product, "VRM": r.vrm,
                "z/OS release": rel if rel != UNSPECIFIED else "Release not stated",
                "Status (dashboard data)": r.status.get(rel, "") if rel != UNSPECIFIED else "",
                "PTFs": ", ".join(ids.ptfs), "USERMOD / SYSMOD IDs": ", ".join(ids.usermods), "APAR IDs": ", ".join(ids.apars),
                "Additional PTFs (linked pages)": ", ".join(p for p in lk.ptfs if p not in ids.ptfs),
                "Passage": "\n".join(ids.notes), "Section source": "\n".join(ids.sources),
                "Hyperlinks scraped": "\n".join(r.linked_urls), "Source page": r.link,
                "Note": r.note,
            })
    return rows
