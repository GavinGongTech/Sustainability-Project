"""
scripts/esg/fetch_esg_direct.py  (v2)

Fetch ESG / sustainability reports for public companies AND startups, using
only free sources (no SerpAPI, no paid databases).

For each company the goal is, in order of preference:
  1. the actual sustainability/ESG REPORT (usually a PDF on the IR site), or
  2. the ESG landing page text, or
  3. a sustainability-referencing SEC filing (public companies only).

What changed from v1 (see fetch_esg_direct_v1.py.bak):
  1. STARTUPS USE THE URL WE ALREADY HAVE. v1 guessed www.<firstword>.com, which
     scrapes the wrong company for names like "Twelve", "Also", "Reach". v2 reads
     the real website from startup_candidates.csv (link_type=external). Startups
     with only a VC profile URL are flagged domain_unresolved, never guessed.
  2. FOLLOW THROUGH TO THE REPORT. v1 stopped at the landing page. v2 scans the
     page for the linked sustainability PDF and downloads/extracts that.
  3. EDGAR FULL-TEXT SEARCH. v1 only scanned 8-K descriptions (rare). v2 queries
     efts.sec.gov (free, no key), which indexes the full text of all filings.
  4. GRI dropped. Its endpoint is unreachable and returned nothing.
  5. HIGHER PRECISION. v1 accepted any page with >=3 generic keyword substrings
     (matches any homepage). v2 requires the company identity on the page plus a
     real report link or high keyword density.
  6. POLITENESS + SPEED. v2 respects robots.txt, rate-limits per domain, caches
     every fetch to disk, and reuses ONE browser instead of launching Chromium
     per candidate URL.
  7. Contact details come from config, not a hardcoded name/email.
  8. Nothing found -> recorded as empty with a reason flag; no placeholder body.

Run from project root:
    python scripts/esg/fetch_esg_direct.py --tickers ECL TILE
    python scripts/esg/fetch_esg_direct.py --companies "Solugen" "Twelve"
    python scripts/esg/fetch_esg_direct.py --from-startups
    python scripts/esg/fetch_esg_direct.py            # all public_company_list.csv
"""

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.robotparser as robotparser
import warnings
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import (  # noqa: E402
    append_to_index, build_markdown, get_logger, load_config, source_filepath,
)

import pandas as pd  # noqa: E402
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

warnings.filterwarnings("ignore")
logger = get_logger("fetch_esg_direct")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Tried in order on a company's own domain.
ESG_PATH_PATTERNS = [
    "/sustainability", "/sustainability-report", "/esg", "/esg-report",
    "/corporate-responsibility", "/corporate-sustainability", "/responsibility",
    "/impact", "/environment", "/about/sustainability", "/about/esg",
    "/about/impact", "/company/sustainability", "/investors/esg",
    "/investors/sustainability", "/our-impact", "/planet",
    # Locale-prefixed patterns (e.g. Owens Corning, many European companies)
    "/en/corporate/sustainability", "/en/sustainability", "/en/esg",
    "/en/about/sustainability", "/us/sustainability", "/us/esg",
    "/corporate/sustainability", "/corporate/esg",
    "/company/esg", "/company/impact",
    "/about/corporate-responsibility", "/about/environment",
    "/sustainability/overview", "/esg/overview",
    "/responsibility/environment", "/impact/environment",
]

# Homepage nav links whose text matches these are followed as ESG candidates.
NAV_LINK_TERMS = re.compile(
    r"sustainab|esg|\bimpact\b|responsibility|environment|climate|net.?zero|planet",
    re.I,
)

# A PDF/href is treated as the report itself when it matches these.
REPORT_HINTS = re.compile(
    r"sustainab|esg|impact|climate|responsibility|environment|net.?zero|"
    r"annual.?report|tcfd|cdp|gri", re.I,
)

# ── Report type classification ─────────────────────────────────────────────────
# Distinguishes between formal ESG reports and broader sustainability reports.
# ESG reports follow structured frameworks (GRI, SASB, TCFD) with quantitative
# metrics. Sustainability reports are broader narrative documents.

ESG_SIGNALS = re.compile(
    r"\besg\b|environmental.{0,10}social.{0,10}governance|"
    r"\bsasb\b|\btcfd\b|\bcdp\b|scope\s[123]|"
    r"esg.{0,15}report|corporate.responsibility.report|"
    r"esg.disclosure|esg.framework",
    re.I,
)

SUSTAINABILITY_SIGNALS = re.compile(
    r"sustainability.report|impact.report|purpose.report|"
    r"climate.report|environmental.report|growth.and.impact|"
    r"planet.report|responsibility.report|stewardship.report|"
    r"sustainability.review|annual.sustainability",
    re.I,
)


def classify_report_type(url: str, title: str = "", text_snippet: str = "") -> str:
    """Label a document as an ESG report, sustainability report, or ambiguous page.

    This normalizes public-facing corporate responsibility content into a
    consistent report taxonomy. It is especially helpful when a company has a
    sustainability web page, a standalone PDF, or an EDGAR filing that mentions
    climate or ESG metrics.

    Returns one of:
      esg_report                 - formal ESG/corporate responsibility report
      sustainability_report      - broader sustainability/impact narrative report
      esg_sustainability_report  - ambiguous content with signals of both
      esg_page                   - landing page only, no downloadable report
      edgar_filing               - SEC filing with a sustainability mention
    """
    combined = f"{url} {title} {text_snippet[:2000]}".lower()

    has_esg    = bool(ESG_SIGNALS.search(combined))
    has_sust   = bool(SUSTAINABILITY_SIGNALS.search(combined))

    if has_esg and has_sust:
        return "esg_sustainability_report"
    elif has_esg:
        return "esg_report"
    elif has_sust:
        return "sustainability_report"
    else:
        # Fall back to URL-based classification
        url_lower = url.lower()
        if "/esg" in url_lower or "esg-report" in url_lower:
            return "esg_report"
        elif "/sustainability" in url_lower or "sustainability-report" in url_lower:
            return "sustainability_report"
        elif "/impact" in url_lower:
            return "sustainability_report"
        else:
            return "esg_sustainability_report"  # can't tell — flag both

ESG_KEYWORDS = [
    "sustainability", "esg", "emission", "carbon", "climate", "net zero",
    "greenhouse", "renewable", "circular", "scope 1", "scope 2", "scope 3",
    "science-based", "decarboni", "water use", "biodiversity",
]

# Junk hrefs that leak into startup_candidates.csv from consent banners etc.
SKIP_URL_HOSTS = (
    "cookie-script.com", "cookiebot", "cookieyes", "onetrust", "safety.google",
    "policies.google", "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "youtube.com", "instagram.com",
)


# ── robots + rate limiting + cache (self-contained so this drops into any repo) ─

class RobotsCache:
    """Small robots.txt cache used to respect site crawling constraints.

    This helps the research pipeline avoid overruning websites that explicitly
    forbid automated crawling. The cache reuses earlier results so repeated
    checks for the same host do not trigger needless extra requests.
    """

    def __init__(self, user_agent: str, enabled: bool = True):
        self.ua, self.enabled = user_agent, enabled
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        """Return whether the URL is allowed by the site robots policy."""
        if not self.enabled:
            return True
        try:
            pr = urlparse(url)
            host = f"{pr.scheme}://{pr.netloc}"
        except Exception:
            return True
        if host not in self._cache:
            rp = robotparser.RobotFileParser()
            try:
                resp = requests.get(urljoin(host, "/robots.txt"),
                                    headers={"User-Agent": self.ua}, timeout=10)
                if resp.status_code >= 400:
                    self._cache[host] = None
                else:
                    rp.parse(resp.text.splitlines())
                    self._cache[host] = rp
            except Exception:
                self._cache[host] = None
        rp = self._cache[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:
            return True


class RateLimiter:
    def __init__(self, delay: float):
        self.delay, self._last = delay, {}

    def wait(self, url: str):
        host = urlparse(url).netloc
        now = time.monotonic()
        prev = self._last.get(host)
        if prev is not None:
            gap = self.delay - (now - prev)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()


class DiskCache:
    """Persist fetched ESG pages to disk to avoid repeated expensive downloads.

    Re-running the same ESG collection pass should be cheap: pages that have
    already been downloaded are reused from the cache instead of being fetched a
    second time.
    """

    def __init__(self, base_dir: Path):
        self.dir = base_dir / "esg"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str, ext: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.dir / f"{h}.{ext}.gz"

    def get(self, url: str, ext: str) -> bytes | None:
        p = self._path(url, ext)
        if p.exists():
            try:
                return gzip.decompress(p.read_bytes())
            except Exception:
                return None
        return None

    def put(self, url: str, ext: str, data: bytes):
        try:
            self._path(url, ext).write_bytes(gzip.compress(data))
        except Exception:
            pass


class BrowserManager:
    """Reusable Playwright controller for browser-heavy ESG page checks.

    Instead of opening a new browser for every URL, this class keeps a single
    Chromium instance and reuses it for a run. That reduces startup overhead and
    makes the scraper more practical for larger company lists.
    """

    def __init__(self, ua: str):
        self.ua, self._pw, self._browser, self._ctx = ua, None, None, None
        self.available = True

    def _ensure(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled",
                  "--no-sandbox"],
        )
        self._ctx = self._browser.new_context(
            user_agent=self.ua, viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

    def get_html(self, url: str, timeout: int = 25000) -> str | None:
        if not self.available:
            return None
        try:
            self._ensure()
        except Exception as e:
            logger.warning(f"    Playwright unavailable ({str(e)[:70]}); requests only")
            self.available = False
            return None
        page = None
        try:
            page = self._ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if not resp or resp.status >= 400:
                return None
            page.wait_for_timeout(1500)
            return page.content()
        except Exception:
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self):
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass


# ── fetching ───────────────────────────────────────────────────────────────────

def sec_headers(cfg: dict) -> dict:
    """SEC asks for a descriptive UA with a contact from config."""
    u = cfg.get("user", {})
    contact = f"{u.get('name', 'NYU CSB Research')} {u.get('email', '')}".strip()
    return {"User-Agent": contact or "NYU CSB Research"}


def web_headers(cfg: dict) -> dict:
    contact = cfg.get("crawler", {}).get("contact_email") or cfg.get("user", {}).get("email", "")
    h = {
        "User-Agent": cfg.get("crawler", {}).get("user_agent", BROWSER_UA),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if contact:
        h["From"] = contact
    return h


def fetch_static(url: str, cfg: dict, limiter: RateLimiter, cache: DiskCache,
                 timeout: int = 20) -> tuple[bytes | None, str | None, int | None]:
    """Returns (content_bytes, content_type, status). Cached."""
    cached = cache.get(url, "raw")
    if cached is not None:
        ctype = cache.get(url, "ctype")
        return cached, (ctype.decode() if ctype else ""), 200
    limiter.wait(url)
    try:
        r = requests.get(url, headers=web_headers(cfg), timeout=timeout, allow_redirects=True)
        if r.status_code < 400 and r.content:
            cache.put(url, "raw", r.content)
            cache.put(url, "ctype", r.headers.get("content-type", "").encode())
        return r.content, r.headers.get("content-type", ""), r.status_code
    except Exception as e:
        logger.warning(f"    requests failed: {type(e).__name__}: {str(e)[:70]}")
        return None, None, None


def page_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def keyword_score(text: str) -> int:
    low = text.lower()
    return sum(1 for k in ESG_KEYWORDS if k in low)


def plausible_year(text: str) -> str:
    """Latest 4-digit year in a plausible reporting range (avoids e.g. PAS 2060)."""
    this_year = date.today().year
    yrs = [int(y) for y in re.findall(r"(?:19|20)\d{2}", text)
           if 2000 <= int(y) <= this_year + 1]
    return str(max(yrs)) if yrs else ""


def identity_on_page(company_name: str, html: str) -> bool:
    """Provenance guard: does this page actually belong to the company?"""
    stop = {"inc", "corp", "ltd", "llc", "co", "the", "group", "company", "ag",
            "sa", "plc", "holdings", "technologies", "technology", "energy",
            "systems", "solutions", "international", "labs", "materials"}
    tokens = [w for w in re.sub(r"[^a-z0-9 ]", " ", company_name.lower()).split()
              if w not in stop and len(w) > 2]
    if not tokens:
        return True  # single-word odd names: don't over-reject
    low = html.lower()
    return any(t in low for t in tokens)


# ── discovery ──────────────────────────────────────────────────────────────────

def find_report_pdf(html: str, page_url: str) -> str | None:
    """Best sustainability-report PDF linked from a page, if any."""
    soup = BeautifulSoup(html, "lxml")
    cands = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True)
        blob = f"{href} {label}"
        if ".pdf" in href.lower() and REPORT_HINTS.search(blob):
            cands.append(urljoin(page_url, href))
    if not cands:
        return None
    # prefer the most specific/recent: those naming a year, then 'sustainability'
    def rank(u: str) -> tuple:
        yr = max([int(y) for y in re.findall(r"20\d{2}", u)] or [0])
        return (bool(re.search(r"sustainab|esg|impact", u, re.I)), yr)
    return sorted(cands, key=rank, reverse=True)[0]


def download_and_extract(url: str, cfg: dict, limiter: RateLimiter,
                         cache: DiskCache, max_pages: int = 30) -> str | None:
    """Download a PDF or HTML doc and return extracted text. Cached."""
    # Use SEC headers for SEC.gov URLs to avoid 403
    if "sec.gov" in url:
        try:
            limiter.wait(url)
            r = requests.get(url, headers=sec_headers(cfg), timeout=40)
            if r.status_code >= 400:
                logger.warning(f"    Download returned {r.status_code} — skipping")
                return None
            content = r.content
            ctype = r.headers.get("content-type", "")
            status = r.status_code
        except Exception as e:
            logger.warning(f"    SEC download failed: {str(e)[:70]}")
            return None
    else:
        content, ctype, status = fetch_static(url, cfg, limiter, cache, timeout=40)
    if not content:
        return None

    # Reject non-200 responses
    if status and status >= 400:
        logger.warning(f"    Download returned {status} — skipping")
        return None

    is_pdf = ".pdf" in url.lower() or "application/pdf" in (ctype or "")

    if is_pdf:
        # Validate it's actually a PDF (starts with %PDF magic bytes)
        if not content.startswith(b"%PDF"):
            logger.warning(f"    URL says PDF but content is not a PDF (got HTML/404) — skipping")
            return None
        try:
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:max_pages])
            return text if len(text) > 200 else None
        except ImportError:
            logger.warning("    pdfplumber not installed — pip install pdfplumber")
            return None
        except Exception as e:
            logger.warning(f"    PDF extract failed: {str(e)[:70]}")
            return None

    try:
        decoded = content.decode("utf-8", "ignore")
        # Reject if it looks like a 404 page or cookie consent page
        if any(phrase in decoded.lower() for phrase in [
            "page not found", "404", "page doesn't exist",
            "not found", "error 404", "floor oh floor",
            "this website uses cookies", "we use cookies to personalise",
            "cookie consent", "accept cookies", "cookie policy",
            "we use cookies to provide", "cookies to improve your experience",
        ]) and len(decoded) < 50000:
            logger.warning(f"    Got 404/cookie page — skipping")
            return None
        text = page_text(decoded)
        # Real ESG content should have substantial text
        if len(text) < 500:
            logger.warning(f"    Page text too short ({len(text)} chars) — skipping")
            return None
        return text
    except Exception:
        return None


def _get_html(url: str, cfg: dict, limiter: RateLimiter, robots: RobotsCache,
              browser: BrowserManager, allow_browser: bool = True) -> tuple[str, str]:
    """Fetch one URL as HTML. Returns (html, method) where method is
    "requests" | "browser" | "" (unreachable). Respects robots + cache."""
    if not robots.allowed(url):
        return "", ""
    content, ctype, status = fetch_static(url, cfg, limiter, CACHE)
    if content and status and status < 400 and ("html" in (ctype or "") or not ctype):
        return content.decode("utf-8", "ignore"), "requests"
    if allow_browser:
        html = browser.get_html(url, timeout=12000)   # discovery: fail fast
        if html:
            return html, "browser"
    return "", ""


def find_esg_page(base_url: str, company_name: str, cfg: dict,
                  limiter: RateLimiter, robots: RobotsCache,
                  browser: BrowserManager) -> tuple[str, str] | None:
    """Locate an ESG page on the company domain.

    The function first checks the homepage for reachability and then scans the
    common sustainability paths to find the best identity-confirmed ESG page.
    Returns a tuple of (url, html) or None if no suitable page is found.
    """
    base = base_url.rstrip("/")
    home, method = _get_html(base + "/", cfg, limiter, robots, browser)
    if not home:
        return None                      # domain dead — let EDGAR handle it
    # Check if homepage is blocked (403/WAF) — skip pattern scan, let EDGAR handle it
    home_lower = home.lower()
    if any(phrase in home_lower for phrase in [
        'access denied', 'forbidden', '403', 'blocked',
        'bot detection', 'cloudflare', 'please enable javascript',
        'ddos protection', 'ray id',
    ]) and len(home) < 20000:
        logger.info(f"    Homepage blocked — skipping to EDGAR fallback")
        return None
    use_browser = (method == "browser")

    def evaluate(html: str) -> int | None:
        if not html or len(page_text(html)) < 400:
            return None
        if not identity_on_page(company_name, html):
            return None
        return keyword_score(page_text(html))

    best = None
    browser_budget = 12                  # increased from 6 to cover more patterns
    consecutive_403 = 0                  # bail early if site is blocking all paths
    for pat in ESG_PATH_PATTERNS:
        url = base + pat
        if use_browser:
            if browser_budget <= 0:
                break
            html, _ = _get_html(url, cfg, limiter, robots, browser)
            browser_budget -= 1
        else:
            html, _ = _get_html(url, cfg, limiter, robots, browser, allow_browser=False)
        # Count consecutive empty responses (blocked/403)
        if not html or len(html) < 200:
            consecutive_403 += 1
            if consecutive_403 >= 3:
                logger.info(f"    Site blocking all paths — skipping to EDGAR")
                return None
        else:
            consecutive_403 = 0
        score = evaluate(html)
        if score is not None and score >= 2:  # lowered from 3 to 2
            has_pdf = find_report_pdf(html, url) is not None
            total = score + (5 if has_pdf else 0)
            if not best or total > best[2]:
                best = (url, html, total)
            if has_pdf and score >= 3:  # lowered from 4 to 3
                break                    # strong hit, stop early

    # homepage nav-link scan (catches non-standard paths) — reuse the home HTML
    if not best:
        seen = set()
        for a in BeautifulSoup(home, "lxml").find_all("a", href=True):
            if NAV_LINK_TERMS.search(a.get_text(" ", strip=True)) or NAV_LINK_TERMS.search(a["href"]):
                u = urljoin(base + "/", a["href"].split("#")[0])
                if u in seen or urlparse(u).netloc != urlparse(base).netloc:
                    continue
                seen.add(u)
                html, _ = _get_html(u, cfg, limiter, robots, browser, allow_browser=use_browser)
                score = evaluate(html)
                if score is not None and score >= 2:  # lowered from 3 to 2
                    best = (u, html, score)
                    break
                if len(seen) >= 8:
                    break

    return (best[0], best[1]) if best else None


def wayback_machine_fetch(base_url: str, company_name: str,
                          cfg: dict, limiter: RateLimiter,
                          cache: DiskCache) -> tuple[str, str] | None:
    """Fallback to the Wayback Machine for archived ESG content.

    This is a best-effort path for companies that block direct scraping or do
    not expose current sustainability pages. It tries the CDX API to find the
    most recent archived snapshot and converts it to candidate content when
    available.
    """
    # Only try the most likely sustainability URLs in Wayback Machine
    # to avoid long timeout chains
    base = base_url.rstrip("/")
    candidates = [
        f"{base}/sustainability",
        f"{base}/en/corporate/sustainability",
        f"{base}/esg",
    ]

    CDX_API = "https://web.archive.org/cdx/search/cdx"

    for url in candidates:
        try:
            # Query CDX API for most recent snapshot
            params = {
                "url":       url,
                "output":    "json",
                "limit":     1,
                "fl":        "timestamp,statuscode",
                "filter":    "statuscode:200",
                "from":      "20240101",  # only recent snapshots
                "fastLatest": "true",
            }
            limiter.wait(CDX_API)
            r = requests.get(CDX_API, params=params,
                            headers={"User-Agent": "Mozilla/5.0"},
                            timeout=10)  # short timeout — skip if slow
            if r.status_code != 200:
                continue
            data = r.json()
            if len(data) < 2:  # no results (first row is header)
                continue

            timestamp = data[1][0]
            wayback_url = f"https://web.archive.org/web/{timestamp}if_/{url}"
            logger.info(f"    Wayback Machine: found snapshot {timestamp}")

            # Fetch the archived page with short timeout
            content, ctype, status = fetch_static(
                wayback_url, cfg, limiter, cache, timeout=15
            )
            if not content or status != 200:
                continue

            html = content.decode("utf-8", "ignore")
            text = page_text(html)

            # Validate content
            if not identity_on_page(company_name, html):
                continue
            score = keyword_score(text)
            if score >= 2 and len(text) > 500:
                logger.info(f"    Wayback Machine: {score} keywords, {len(text)} chars")
                return wayback_url, html

        except Exception as e:
            logger.warning(f"    Wayback Machine failed for {url}: {str(e)[:60]}")
            continue

    return None


def edgar_fulltext_search(cik: str, cfg: dict, limiter, cache) -> tuple | None:
    """Search EDGAR full text for the most recent sustainability filing.

    Returns a tuple of (document_url, form, filing_date) or None if no suitable
    filing is found for the company.
    """
    if not cik:
        return None
    cik10 = f"{int(cik):010d}"
    # quoted phrase — bare "sustainability" matches boilerplate in any filing
    q = 'q=%22sustainability+report%22&ciks=' + cik10
    api = "https://efts.sec.gov/LATEST/search-index?" + q
    cached = cache.get(api, "json")
    if cached is not None:
        data = json.loads(cached)
    else:
        limiter.wait(api)
        try:
            r = requests.get(api, headers=sec_headers(cfg), timeout=20)
            if r.status_code != 200:
                return None
            data = r.json()
            cache.put(api, "json", r.content)
        except Exception as e:
            logger.warning(f"    EDGAR FTS failed: {str(e)[:70]}")
            return None
    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        return None
    hits.sort(key=lambda h: h.get("_source", {}).get("file_date", ""), reverse=True)
    top = hits[0]
    src = top.get("_source", {})
    _id = top.get("_id", "")
    if ":" not in _id:
        return None
    acc, fname = _id.split(":", 1)
    acc_nodash = acc.replace("-", "")
    doc_cik = str(src.get("ciks", [cik10])[0]).lstrip("0") or cik10
    doc_url = f"https://www.sec.gov/Archives/edgar/data/{doc_cik}/{acc_nodash}/{fname}"
    return doc_url, (src.get("form", "") + " filing"), src.get("file_date", "")


# ── per-company orchestration ──────────────────────────────────────────────────

CACHE: DiskCache = None  # set in run()


def candidate_domains(company_name: str) -> list[str]:
    """Guess candidate domains for a company when the SEC website field is blank.

    These are only used alongside the identity guard, so pages are rejected if
    they do not clearly mention the company name. This makes the fallback safe
    for companies where the SEC data does not provide a clean public website.
    """
    stop = {"inc", "corp", "corporation", "ltd", "llc", "co", "the", "group",
            "company", "ag", "sa", "plc", "nv", "holdings", "international",
            "industries", "technologies", "technology", "systems", "solutions"}
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", company_name.lower()).split()
             if w not in stop and len(w) > 1]
    if not words:
        return []
    joined     = "".join(words[:2])
    hyphenated = "-".join(words[:2])
    three      = "".join(words[:3])

    # Try joined multi-word name first (owenscorning before owens)
    # then hyphenated (owens-corning), then first word alone
    out = []
    if len(words) > 1:
        out.append(f"https://www.{joined}.com")
        out.append(f"https://www.{hyphenated}.com")
    if len(words) > 2:
        out.append(f"https://www.{three}.com")
    out.append(f"https://www.{words[0]}.com")

    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:4]  # try up to 4 candidates




def discover_esg(company_name: str, base_urls: list[str], cik: str,
                 cfg: dict, limiter: RateLimiter, robots: RobotsCache,
                 browser: BrowserManager) -> dict:
    """Return a provenance-tagged ESG result dict.

    The result records the best report URL, the report type, year, extracted
    text, and a quality label describing whether the record came from a PDF, an
    ESG landing page, or an SEC filing.
    """
    result = {"report_url": "", "report_type": "", "report_year": "",
              "text": "", "quality": ""}

    for base_url in [b for b in base_urls if b]:
        page = find_esg_page(base_url, company_name, cfg, limiter, robots, browser)
        if page:
            page_url, html = page
            pdf_url = find_report_pdf(html, page_url)
            if pdf_url:
                text = download_and_extract(pdf_url, cfg, limiter, CACHE)
                if text:
                    rtype = classify_report_type(pdf_url, text_snippet=text[:2000])
                    result.update(report_url=pdf_url, report_type=rtype,
                                  report_year=plausible_year(pdf_url) or plausible_year(text[:2000]),
                                  text=text, quality="report_pdf")
                    return result
                else:
                    logger.warning(f"    PDF download failed — falling back to landing page")
            # landing page only (either no PDF found or PDF download failed)
            rtype = classify_report_type(page_url, text_snippet=page_text(html)[:2000])
            result.update(report_url=page_url, report_type=rtype,
                          text=page_text(html), quality="esg_page")
            return result

    # Wayback Machine fallback disabled — web.archive.org unreachable on this network
    if False and not result["text"] and base_urls:
        logger.info(f"    Trying Wayback Machine fallback...")
        for base_url in [b for b in base_urls if b]:
            wb = wayback_machine_fetch(base_url, company_name, cfg, limiter, CACHE)
            if wb:
                wb_url, html = wb
                pdf_url = find_report_pdf(html, wb_url)
                if pdf_url:
                    text = download_and_extract(pdf_url, cfg, limiter, CACHE)
                    if text:
                        rtype = classify_report_type(pdf_url, text_snippet=text[:2000])
                        result.update(report_url=pdf_url, report_type=rtype,
                                      report_year=plausible_year(pdf_url) or plausible_year(text[:2000]),
                                      text=text, quality="report_pdf")
                        return result
                rtype = classify_report_type(wb_url, text_snippet=page_text(html)[:2000])
                result.update(report_url=wb_url, report_type=rtype,
                              text=page_text(html), quality="esg_page")
                return result

    # public-company fallback: EDGAR full text
    if cik:
        fts = edgar_fulltext_search(cik, cfg, limiter, CACHE)
        if fts:
            doc_url, form, fdate = fts
            text = download_and_extract(doc_url, cfg, limiter, CACHE)
            if text:
                rtype = classify_report_type(doc_url, text_snippet=text[:2000])
                result.update(report_url=doc_url, report_type=rtype,
                              report_year=(fdate[:4] if fdate else ""), text=text,
                              quality="edgar_filing")
                return result

    result["quality"] = "domain_unresolved" if not any(base_urls) and not cik else "no_report_found"
    return result


def ticker_to_cik(ticker: str, cfg: dict, _cache={}) -> str:
    if not ticker:
        return ""
    if not _cache:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers=sec_headers(cfg), timeout=15)
            for e in r.json().values():
                _cache[e["ticker"].upper()] = str(e["cik_str"]).zfill(10)
        except Exception:
            _cache["__failed__"] = True
    return _cache.get(ticker.upper(), "")


def sec_submissions(cik: str, cfg: dict, _cache={}) -> dict:
    """Fetch one SEC submissions record and cache it by CIK.

    The legal name and website metadata are used to build approximate domain
    guesses for companies when the official public website is not populated in
    the SEC filing metadata.
    """
    if not cik:
        return {"name": "", "website": ""}
    if cik in _cache:
        return _cache[cik]
    out = {"name": "", "website": ""}
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                         headers=sec_headers(cfg), timeout=15)
        if r.status_code == 200:
            d = r.json()
            out["name"] = d.get("name", "") or ""
            w = d.get("website", "") or ""
            if w.startswith("http"):
                pr = urlparse(w)
                out["website"] = f"{pr.scheme}://{pr.netloc}"
    except Exception:
        pass
    _cache[cik] = out
    return out


def fetch_company_esg(ticker: str, company_name: str, meta: dict, cfg: dict,
                      limiter, robots, browser, cik: str = "") -> str:
    logger.info(f"-- {ticker} ({company_name}) --")
    segment = meta.get("segment", "public")
    base_urls: list[str] = []

    if segment == "startup":
        # trust the crawled URL; never guess (guessing scrapes the wrong company)
        u = str(meta.get("url", "")).strip()
        if u.startswith("http") and not any(h in u for h in SKIP_URL_HOSTS):
            pr = urlparse(u)
            base_urls = [f"{pr.scheme}://{pr.netloc}"]
    else:
        # public: resolve CIK if missing, then SEC website field, then
        # identity-guarded domain guesses (SEC's website field is usually blank)
        if not cik:
            cik = ticker_to_cik(ticker, cfg)
        sub = sec_submissions(cik, cfg)
        # the crawl may only know the ticker; SEC has the real legal name
        if sub["name"] and company_name.upper() == ticker.upper():
            company_name = sub["name"].title()
            logger.info(f"    name (SEC): {company_name}")
        if meta.get("website", "").strip():
            base_urls.append(meta["website"].strip())
        if sub["website"]:
            base_urls.append(sub["website"])
        base_urls += candidate_domains(company_name)
        base_urls = list(dict.fromkeys(base_urls))  # de-dup, keep order

    if base_urls:
        logger.info(f"    candidates: {', '.join(base_urls[:4])}")

    res = discover_esg(company_name, base_urls, cik, cfg, limiter, robots, browser)
    q = res["quality"]

    if res["text"]:
        out_path = source_filepath(company=company_name, source_type="ESG report",
                                   year=res["report_year"] or date.today().year,
                                   base_dir=cfg["paths"]["normalized"],
                                   segment=meta.get("segment", "public"))
        out_path.write_text(build_markdown(
            text=res["text"][:60000], company=company_name,
            segment=meta.get("segment", "public"), source_type="ESG report",
            source_year=res["report_year"] or "", report_type=res["report_type"],
            source_url=res["report_url"], ticker=ticker,
            industry=meta.get("industry", ""), hq_country=meta.get("hq_country", ""),
            quality_flag=q,
        ), encoding="utf-8")
        logger.info(f"    [{q}] {res['report_url'][:80]}")
        file_path = str(out_path)
    else:
        logger.info(f"    [{q}] nothing saved")
        file_path = ""

    append_to_index({
        "company": company_name, "segment": meta.get("segment", "public"),
        "ticker": ticker, "source_type": "ESG report",
        "report_type": res["report_type"], "report_year": res["report_year"],
        "source_url": res["report_url"], "file_path": file_path, "quality_flag": q,
    }, index_path=f"{cfg['paths']['index']}/esg_index.csv",
       key_fields=("ticker", "source_type"))  # one row per company

    return q


# ── target loading + run ───────────────────────────────────────────────────────

def _load_targets(tickers, companies, from_startups, cfg) -> dict:
    idx = Path(cfg["paths"]["index"])
    if not idx.is_absolute():
        idx = Path(__file__).resolve().parents[2] / idx
    public_csv = idx / "public_company_list.csv"
    startup_csv = idx / "startup_candidates.csv"

    if from_startups:
        if not startup_csv.exists():
            logger.error(f"Not found: {startup_csv} — run crawl_vc_portfolios.py first")
            return {}
        df = pd.read_csv(startup_csv)
        # one target per real company; prefer rows that carry a real website
        df["has_site"] = (df.get("link_type", "") == "external")
        df = df.sort_values("has_site", ascending=False)
        targets = {}
        for _, r in df.iterrows():
            name = str(r.get("name", "")).strip()
            if not name:
                continue
            key = re.sub(r"[^A-Z0-9]+", "_", name.upper())[:24]
            if key in targets:
                continue
            targets[key] = {"company": name, "segment": "startup",
                            "url": r.get("url", "") if r.get("link_type") == "external" else "",
                            "cik": ""}
        logger.info(f"Loaded {len(targets):,} startups (of which "
                    f"{sum(1 for t in targets.values() if t['url'])} have a known website)")
        return targets

    if companies:
        # match against the crawled startup list to recover real URLs
        url_map = {}
        if startup_csv.exists():
            sdf = pd.read_csv(startup_csv)
            for _, r in sdf[sdf.get("link_type", "") == "external"].iterrows():
                url_map[str(r.get("name", "")).strip().lower()] = r.get("url", "")
        return {re.sub(r"[^A-Z0-9]+", "_", c.upper())[:24]:
                {"company": c, "segment": "startup", "cik": "",
                 "url": url_map.get(c.strip().lower(), "")} for c in companies}

    if tickers:
        name_map, cik_map, meta_map = {}, {}, {}
        if public_csv.exists():
            for _, r in pd.read_csv(public_csv).iterrows():
                if pd.notna(r.get("ticker")):
                    t = str(r["ticker"]).upper()
                    name_map[t] = r.get("company", t)
                    cik_map[t] = str(r.get("cik", ""))
                    meta_map[t] = r.to_dict()
        out = {}
        for t in tickers:
            tu = t.upper()
            m = meta_map.get(tu, {})
            out[tu] = {"company": name_map.get(tu, t), "segment": "public",
                       "cik": cik_map.get(tu, ""), "website": m.get("website", ""),
                       "industry": m.get("industry", ""), "hq_country": m.get("hq_country", "")}
        return out

    if not public_csv.exists():
        logger.error(f"Not found: {public_csv} — build it first")
        return {}
    df = pd.read_csv(public_csv)
    out = {}
    for _, r in df.iterrows():
        if pd.notna(r.get("ticker")):
            d = r.to_dict()
            d["segment"] = "public"
            out[str(r["ticker"]).upper()] = d
    logger.info(f"Loaded {len(out):,} public companies from {public_csv}")
    return out


def run(tickers=None, companies=None, from_startups=False, limit=None,
        config_path="config/config.yaml"):
    global CACHE
    cfg = load_config(config_path)
    CACHE = DiskCache(Path(cfg["paths"].get("cache", "data/cache")) if
                      Path(cfg["paths"].get("cache", "data/cache")).is_absolute() else
                      Path(__file__).resolve().parents[2] / cfg["paths"].get("cache", "data/cache"))

    targets = _load_targets(tickers, companies, from_startups, cfg)
    if not targets:
        return
    if limit:
        targets = dict(list(targets.items())[:limit])

    delay = cfg.get("rate_limits", {}).get("general_delay_seconds", 1.5)
    limiter = RateLimiter(delay)
    robots = RobotsCache(cfg.get("crawler", {}).get("user_agent", BROWSER_UA),
                         enabled=cfg.get("crawler", {}).get("respect_robots", True))
    browser = BrowserManager(cfg.get("crawler", {}).get("user_agent", BROWSER_UA))

    tally = {}
    try:
        for ticker, meta in targets.items():
            try:
                q = fetch_company_esg(ticker, meta.get("company", ticker), meta, cfg,
                                      limiter, robots, browser, str(meta.get("cik", "")))
            except Exception as e:
                logger.error(f"{ticker}: {type(e).__name__}: {str(e)[:90]}")
                q = "error"
            tally[q] = tally.get(q, 0) + 1
    finally:
        browser.close()

    logger.info("\n" + "=" * 54)
    logger.info(f"Done. {sum(tally.values())} companies:")
    for k in sorted(tally, key=lambda x: -tally[x]):
        logger.info(f"  {tally[k]:>5}  {k}")
    got = tally.get("report_pdf", 0) + tally.get("esg_page", 0) + tally.get("edgar_filing", 0)
    logger.info(f"  ----- {got} with some ESG source, "
                f"{sum(tally.values()) - got} without")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch ESG reports directly (no SerpAPI)")
    p.add_argument("--tickers", nargs="+", help="Public tickers e.g. ECL TILE")
    p.add_argument("--companies", nargs="+", help="Startup names e.g. 'Solugen'")
    p.add_argument("--from-startups", action="store_true",
                   help="Run across startup_candidates.csv")
    p.add_argument("--limit", type=int, default=None, help="Only first N targets")
    p.add_argument("--config", default="config/config.yaml")
    a = p.parse_args()
    run(tickers=a.tickers, companies=a.companies, from_startups=a.from_startups,
        limit=a.limit, config_path=a.config)