"""Discover startup candidates from VC and accelerator portfolio pages.

This is the startup-discovery layer of the project. It visits VC websites,
extracts links that plausibly point to portfolio-company websites, filters out
navigation and junk links, and writes a curated startup candidate list to CSV.

The script is designed to be robust against common crawling mistakes: it keeps
internal portfolio-company detail links, strips marketing noise from names,
checks robots.txt, and keeps per-VC run summaries for debugging and QA.

The final output is a CSV used downstream by the startup website scraping stage.
"""

import argparse
import json
import re
import sys
import time
import urllib.robotparser as robotparser
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import get_logger, load_config  # noqa: E402

import pandas as pd  # noqa: E402
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

logger = get_logger("crawl_vc_portfolios")

# Output schema: v1 columns plus link_type and strategy.
CSV_COLUMNS = [
    "name", "url", "segment", "vc_source",
    "date_discovered", "quality_flag", "link_type", "strategy",
]

SUMMARY_COLUMNS = [
    "vc_source", "url", "http_status", "fetch_method", "rendered",
    "n_external", "n_detail", "n_total", "strategies", "status", "reason",
]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ── Name filtering ─────────────────────────────────────────────────────────────
# FIX 3: exact-match set. Never substring-scanned against company names.
NAV_EXACT = {
    "accept", "accept all", "decline", "close", "menu", "search", "login",
    "log in", "sign in", "sign up", "subscribe", "newsletter", "cookies",
    "cookie policy", "privacy", "privacy policy", "terms", "terms of use",
    "about", "about us", "contact", "contact us", "team", "our team",
    "careers", "jobs", "press", "blog", "news", "insights", "events",
    "resources", "faq", "help", "support", "home", "back", "next",
    "previous", "more", "view all", "see all", "read more", "learn more",
    "get started", "apply", "apply now", "portfolio", "our portfolio",
    "invest", "fund", "funds", "our work", "what we do", "who we are",
    "for entrepreneurs", "eligibility", "how to apply", "programs",
    "partners", "mentors", "corporates", "cohort", "impact", "approach",
    "strategy", "thesis", "people", "companies", "our companies",
    "linkedin", "twitter", "facebook", "instagram", "youtube", "x",
    "all", "filter", "filters", "reset", "clear", "load more", "show more",
    "skip to content", "skip to main content", "menu toggle", "share",
    "english", "deutsch", "français", "sitemap", "legal", "imprint",
    "disclosures", "disclaimer", "media", "podcast", "reports", "stories",
    "perspectives", "our approach", "join us", "open roles", "founders",
    "for founders", "community", "network", "platform", "overview",
}

# Anchored patterns — must match the WHOLE name, so real companies survive.
NAV_PATTERNS = [
    re.compile(r"^(read|learn|see|view|show|find|discover)\s+(more|all|less)$", re.I),
    re.compile(r"^\d+$"),
    re.compile(r"^[«»<>←→\-–—•|]+$"),
    re.compile(r"^page\s+\d+$", re.I),
    re.compile(r"^(previous|next)\s+(page|post)$", re.I),
    re.compile(r"^©.*$"),
    re.compile(r"^all rights reserved.*$", re.I),
    re.compile(r"^(copyright|©)\s", re.I),
    re.compile(r"^\W+$"),
]

SKIP_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "crunchbase.com", "pitchbook.com",
    "techcrunch.com", "forbes.com", "medium.com", "substack.com",
    "github.com", "t.me", "tiktok.com", "vimeo.com", "spotify.com",
    "apple.com", "google.com", "goo.gl", "bit.ly", "mailchimp.com",
    "eventbrite.com", "typeform.com", "airtable.com", "notion.so",
    # Domain-parking and consent/vendor hosts. A parked VC domain otherwise
    # yields rows like "Contrarian Ventures Llc" -> godaddy.com search links.
    "godaddy.com", "search-domainparking.com", "sedo.com", "afternic.com",
    "cookieyes.com", "onetrust.com", "cookiebot.com", "usercentrics.com",
    "getproven.com", "greenhouse.io", "lever.co", "workable.com",
    "fiscloudservices.com", "hs-sites.com", "hubspot.com",
}

# Internal paths that are navigation, never a portfolio company profile.
SKIP_PATH_PARTS = {
    "privacy", "terms", "legal", "cookie", "cookies", "careers", "jobs",
    "press", "contact", "about", "team", "login", "signin", "sign-in",
    "subscribe", "newsletter", "search", "sitemap", "imprint", "disclosures",
    "wp-content", "wp-admin", "feed", "rss", "tag", "category", "author",
    "events", "event", "insights", "perspectives", "media", "podcast",
}

# Path segments that mark an internal link as a portfolio company profile.
DETAIL_PATH_HINTS = (
    "portfolio", "companies", "company", "investments", "investment",
    "portfolio-company", "our-companies", "ventures", "startups", "startup",
)


# ── URL helpers ────────────────────────────────────────────────────────────────

def abs_url(href: str, base: str) -> str:
    """Normalize a relative URL against the VC page’s base URL."""
    href = (href or "").strip()
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base, href)


def get_domain(url: str) -> str:
    """FIX 4: strip a leading 'www.' prefix, not the character set {w,.}."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0]


def base_domain(domain: str) -> str:
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def is_external(company_url: str, vc_url: str) -> bool:
    vc_domain = get_domain(vc_url)
    co_domain = get_domain(company_url)
    if not co_domain:
        return False
    if co_domain == vc_domain:
        return False
    return base_domain(vc_domain) != base_domain(co_domain)


def is_skip_domain(url: str) -> bool:
    domain = get_domain(url)
    if not domain:
        return True
    return any(domain == s or domain.endswith("." + s) for s in SKIP_DOMAINS)


def path_parts(url: str) -> list[str]:
    try:
        return [p for p in urlparse(url).path.strip("/").split("/") if p]
    except Exception:
        return []


def is_nav_path(url: str) -> bool:
    return any(p.lower() in SKIP_PATH_PARTS for p in path_parts(url))


def looks_like_detail(url: str, vc_url: str, include_paths: list[str] | None = None) -> bool:
    """Decide whether an internal VC link likely points to a portfolio company page.

    This prevents the crawler from mistaking generic nav pages such as /about,
    /team, or /news for actual company profiles. The heuristic looks for
    portfolio-like path patterns and excludes the VC homepage itself.
    """
    if urlparse(url).scheme not in ("http", "https"):
        return False
    parts = path_parts(url)
    if not parts or is_nav_path(url):
        return False

    if include_paths:
        for inc in include_paths:
            inc_parts = [p for p in inc.strip("/").split("/") if p]
            if parts[:len(inc_parts)] == inc_parts and len(parts) > len(inc_parts):
                return True

    vc_parts = path_parts(vc_url)
    # Same path as the index page → it IS the index page, not a company.
    if parts == vc_parts:
        return False
    if len(parts) < 2:
        return False
    return any(h in parts[0].lower() or h in parts[-2].lower() for h in DETAIL_PATH_HINTS)


# ── Name handling ──────────────────────────────────────────────────────────────

def is_nav_name(name: str) -> bool:
    """Reject obvious navigation labels and marketing copy from company names.

    This is crucial because VC portfolio pages often contain lots of generic
    labels such as 'About', 'Team', 'Contact', 'Portfolio', or 'Read More'.
    Strings that match these patterns are dropped before deduplication.
    """
    n = " ".join((name or "").split()).strip()
    if not n:
        return True
    low = n.lower().strip(" .,:;!?-–—|")
    if low in NAV_EXACT:
        return True
    if any(p.match(n) for p in NAV_PATTERNS):
        return True
    if len(n) < 2 or len(n) > 80:
        return True
    # A long sentence is a blurb, not a company name.
    if len(n.split()) > 9:
        return True
    return False


def clean_company_name(name: str) -> str:
    """
    FIX 7: never returns the text after a period. Trims a trailing tagline
    that follows a separator, and strips wrapper noise.
    """
    n = " ".join((name or "").split()).strip()
    if not n:
        return ""
    # "Nitricity. Clean Fertilizer"  ->  "Nitricity"
    m = re.match(r"^([A-Z0-9][^.]{1,40}?)\.\s+[A-Z].*$", n)
    if m:
        n = m.group(1).strip()
    # "Watershed — carbon accounting" -> "Watershed"
    for sep in (" — ", " – ", " | ", " · ", " -- "):
        if sep in n:
            head = n.split(sep, 1)[0].strip()
            if head:
                n = head
                break
    # "Company: tagline" only when the head looks like a name, not a sentence.
    if ":" in n:
        head = n.split(":", 1)[0].strip()
        if head and len(head.split()) <= 5:
            n = head
    n = n.strip(" .,:;|-–—·")
    n = re.sub(r"\s*\b(logo|website|homepage|visit site|read more)\b\s*$", "", n, flags=re.I)
    return n.strip()


# ── robots.txt ─────────────────────────────────────────────────────────────────

class RobotsCache:
    """One RobotFileParser per host, fetched once per run."""

    def __init__(self, user_agent: str, enabled: bool = True):
        self.ua = user_agent
        self.enabled = enabled
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        try:
            parsed = urlparse(url)
            host = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return True
        if host not in self._cache:
            rp = robotparser.RobotFileParser()
            rp.set_url(urljoin(host, "/robots.txt"))
            try:
                resp = requests.get(
                    urljoin(host, "/robots.txt"),
                    headers={"User-Agent": self.ua},
                    timeout=10,
                )
                if resp.status_code >= 400:
                    self._cache[host] = None  # no robots.txt → allowed
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


# ── Rate limiting ──────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self._last: dict[str, float] = {}

    def wait(self, url: str):
        host = get_domain(url)
        now = time.monotonic()
        prev = self._last.get(host)
        if prev is not None:
            gap = self.delay - (now - prev)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()


# ── Fetching ───────────────────────────────────────────────────────────────────

def request_headers(cfg: dict) -> dict:
    """FIX 5: real browser UA; research contact goes in From, not User-Agent."""
    crawler = cfg.get("crawler", {})
    contact = crawler.get("contact_email") or cfg.get("user", {}).get("email", "")
    headers = {
        "User-Agent": crawler.get("user_agent", BROWSER_UA),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    if contact:
        headers["From"] = contact
    return headers


def fetch_static(url: str, cfg: dict, limiter: RateLimiter) -> tuple[str | None, int | None, str]:
    timeout = cfg.get("crawler", {}).get("timeout_seconds", 25)
    limiter.wait(url)
    try:
        resp = requests.get(url, headers=request_headers(cfg), timeout=timeout)
        return resp.text, resp.status_code, ""
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:160]}"


def fetch_rendered(url: str, cfg: dict, vc: dict) -> tuple[str | None, str]:
    """
    FIX 6: real UA + viewport, wait on a selector rather than networkidle,
    scroll for lazy loading, and click a load-more control when configured.

    Consent banners are deliberately NOT clicked.
    """
    crawler = cfg.get("crawler", {})
    ua = crawler.get("user_agent", BROWSER_UA)
    wait_selector = vc.get("wait_selector")
    load_more = vc.get("load_more_selector")
    max_clicks = int(vc.get("max_clicks", 12))
    scrolls = int(vc.get("scrolls", 8))
    settle_ms = int(vc.get("settle_ms", 2500))

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, f"playwright import failed: {e}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=ua,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=20000)
                except Exception:
                    logger.warning(f"  wait_selector never appeared: {wait_selector}")
            page.wait_for_timeout(settle_ms)

            if load_more:
                for i in range(max_clicks):
                    try:
                        btn = page.query_selector(load_more)
                        if not btn or not btn.is_visible():
                            break
                        btn.click(timeout=5000)
                        page.wait_for_timeout(1800)
                    except Exception:
                        break

            last_height = 0
            for _ in range(scrolls):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                try:
                    height = page.evaluate("document.body.scrollHeight")
                except Exception:
                    break
                if height == last_height:
                    break
                last_height = height

            html = page.content()
            ctx.close()
            browser.close()
        return html, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


# ── JSON endpoint discovery ────────────────────────────────────────────────────

NAME_KEYS = ("name", "title", "company", "company_name", "companyName")
URL_KEYS = ("website", "url", "link", "site", "website_url", "websiteUrl",
            "homepage", "company_url", "slug", "permalink")


def _walk_json_records(node, out: list, depth: int = 0):
    """Collect every list-of-dicts that carries a name-ish key."""
    if depth > 8:
        return
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if len(dicts) >= 3 and any(
            any(k in d for k in NAME_KEYS) for d in dicts[:5]
        ):
            out.append(dicts)
        for item in node[:200]:
            _walk_json_records(item, out, depth + 1)
    elif isinstance(node, dict):
        for value in node.values():
            _walk_json_records(value, out, depth + 1)


def _first_key(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("rendered", "url", "value"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return ""


def extract_from_json_blob(blob, base_url: str) -> list[dict]:
    buckets: list[list[dict]] = []
    _walk_json_records(blob, buckets)
    if not buckets:
        return []
    best = max(buckets, key=len)
    rows = []
    for rec in best:
        name = _first_key(rec, NAME_KEYS)
        url = _first_key(rec, URL_KEYS)
        if not name:
            continue
        if url and not url.startswith("http"):
            url = abs_url(url, base_url)
        rows.append({"name": name, "url": url})
    return rows


def find_next_data(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def try_wp_json(vc_url: str, html: str, cfg: dict, limiter: RateLimiter,
                robots: RobotsCache) -> list[dict]:
    """WordPress sites expose collections at /wp-json/wp/v2/<type>."""
    if "wp-json" not in html and "wp-content" not in html:
        return []
    parsed = urlparse(vc_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for ptype in ("portfolio", "companies", "company", "portfolio_company",
                  "portfolio-company", "investments"):
        endpoint = f"{origin}/wp-json/wp/v2/{ptype}?per_page=100"
        if not robots.allowed(endpoint):
            continue
        limiter.wait(endpoint)
        try:
            resp = requests.get(endpoint, headers=request_headers(cfg), timeout=20)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue
        rows = extract_from_json_blob(data, vc_url)
        if len(rows) >= 3:
            logger.info(f"  wp-json hit: {ptype} ({len(rows)} records)")
            return rows
    return []


def fetch_config_json(vc: dict, cfg: dict, limiter: RateLimiter,
                      robots: RobotsCache) -> tuple[list[dict], str]:
    """Pull an explicitly configured JSON endpoint, with optional pagination."""
    endpoint = vc.get("json_endpoint")
    if not endpoint:
        return [], ""
    name_field = vc.get("json_name_field")
    url_field = vc.get("json_url_field")
    records_path = vc.get("json_records_path")
    page_param = vc.get("json_page_param")
    page_size_param = vc.get("json_page_size_param")
    page_size = vc.get("json_page_size", 100)
    max_pages = int(vc.get("json_max_pages", 20))
    start_page = int(vc.get("json_start_page", 1))

    rows: list[dict] = []
    for i in range(max_pages):
        url = endpoint
        params = []
        if page_param:
            params.append(f"{page_param}={start_page + i}")
        if page_size_param:
            params.append(f"{page_size_param}={page_size}")
        if params:
            url += ("&" if "?" in url else "?") + "&".join(params)

        if not robots.allowed(url):
            return rows, "robots_disallow"
        limiter.wait(url)
        try:
            resp = requests.get(url, headers=request_headers(cfg), timeout=25)
            if resp.status_code != 200:
                return rows, f"http_{resp.status_code}"
            data = resp.json()
        except Exception as e:
            return rows, f"{type(e).__name__}: {str(e)[:120]}"

        node = data
        if records_path:
            for key in records_path.split("."):
                if isinstance(node, dict):
                    node = node.get(key)
                elif isinstance(node, list) and key.isdigit():
                    node = node[int(key)]
                else:
                    node = None
                if node is None:
                    break

        if name_field and isinstance(node, list):
            page_rows = []
            for rec in node:
                if not isinstance(rec, dict):
                    continue
                name = rec.get(name_field) or ""
                url_v = rec.get(url_field) if url_field else ""
                if isinstance(name, str) and name.strip():
                    page_rows.append({
                        "name": name.strip(),
                        "url": (url_v or "").strip() if isinstance(url_v, str) else "",
                    })
        else:
            page_rows = extract_from_json_blob(node if node is not None else data, endpoint)

        if not page_rows:
            break
        rows.extend(page_rows)
        if not page_param:
            break
    return rows, ""


# ── HTML extraction ────────────────────────────────────────────────────────────

CARD_PATTERN = re.compile(
    r"(card|company|portfolio|startup|item|member|partner|venture|invest|grid|tile|logo)",
    re.I,
)


class Collector:
    """Accumulates candidates, keyed by URL, remembering the winning strategy."""

    def __init__(self, vc_url: str, vc: dict):
        self.vc_url = vc_url
        self.vc = vc
        self.include_paths = vc.get("include_paths") or []
        self.allow_internal = vc.get("allow_internal", True)
        self.rows: dict[str, dict] = {}
        self.rejected = defaultdict(int)

    def add(self, name: str, href: str, strategy: str):
        name = clean_company_name(name)
        if not name:
            self.rejected["empty_name"] += 1
            return
        url = abs_url(href, self.vc_url)
        if not url or not url.startswith("http"):
            self.rejected["bad_url"] += 1
            return
        if is_nav_name(name):
            self.rejected["nav_name"] += 1
            return
        if is_skip_domain(url):
            self.rejected["skip_domain"] += 1
            return

        if is_external(url, self.vc_url):
            link_type = "external"
        elif self.allow_internal and looks_like_detail(url, self.vc_url, self.include_paths):
            link_type = "detail"
        else:
            self.rejected["internal_nav"] += 1
            return

        url = url.split("#")[0].rstrip("/")
        if url in self.rows:
            return
        self.rows[url] = {
            "name": name,
            "url": url,
            "link_type": link_type,
            "strategy": strategy,
        }

    def add_json(self, name: str, url: str, strategy: str):
        """JSON records may carry a name with no usable URL — keep them."""
        name = clean_company_name(name)
        if not name or is_nav_name(name):
            self.rejected["nav_name"] += 1
            return
        url = (url or "").strip()
        if url:
            url = abs_url(url, self.vc_url)
            if is_skip_domain(url):
                url = ""
        key = url.split("#")[0].rstrip("/") if url else f"name::{name.lower()}"
        if key in self.rows:
            return
        link_type = "external" if url and is_external(url, self.vc_url) else (
            "detail" if url else "name_only"
        )
        self.rows[key] = {
            "name": name,
            "url": url,
            "link_type": link_type,
            "strategy": strategy,
        }

    def result(self) -> list[dict]:
        return list(self.rows.values())


def extract_config_cards(soup: BeautifulSoup, coll: Collector, vc: dict) -> int:
    """Per-VC selector override — highest precedence strategy."""
    card_sel = vc.get("card_selector")
    if not card_sel:
        return 0
    name_sel = vc.get("name_selector")
    link_sel = vc.get("link_selector")
    before = len(coll.rows)
    for card in soup.select(card_sel):
        a = card.select_one(link_sel) if link_sel else (
            card if card.name == "a" and card.get("href") else card.find("a", href=True)
        )
        href = a.get("href") if a is not None else None
        if not href:
            continue
        if name_sel:
            name_el = card.select_one(name_sel)
            name = name_el.get_text(" ", strip=True) if name_el else ""
        else:
            name = card.get_text(" ", strip=True)
        if not name:
            img = card.find("img")
            if img:
                name = img.get("alt", "")
        coll.add(name, href, "config_selector")
    return len(coll.rows) - before


def extract_cards(soup: BeautifulSoup, coll: Collector) -> int:
    before = len(coll.rows)
    for tag in soup.find_all(["article", "li", "div"], class_=CARD_PATTERN):
        a = tag.find("a", href=True)
        if not a:
            continue
        name_el = tag.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong"])
        name = name_el.get_text(" ", strip=True) if name_el else ""
        if not name:
            img = tag.find("img")
            if img and img.get("alt", "").strip():
                name = img["alt"].strip()
        if not name:
            name = a.get_text(" ", strip=True)
        if name:
            coll.add(name, a["href"], "card")
    return len(coll.rows) - before


def extract_logo_alt(soup: BeautifulSoup, coll: Collector) -> int:
    before = len(coll.rows)
    for a in soup.find_all("a", href=True):
        img = a.find("img")
        if not img:
            continue
        alt = (img.get("alt") or "").strip()
        if not alt:
            alt = (img.get("title") or "").strip()
        if alt and len(alt) > 1:
            coll.add(alt, a["href"], "logo_alt")
    return len(coll.rows) - before


def extract_anchor_text(soup: BeautifulSoup, coll: Collector) -> int:
    before = len(coll.rows)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if text and 2 <= len(text) <= 60:
            coll.add(text, a["href"], "anchor_text")
    return len(coll.rows) - before


def extract_all(html: str, vc_url: str, vc: dict) -> tuple[list[dict], dict]:
    """FIX 2: every strategy runs; results merge instead of short-circuiting."""
    soup = BeautifulSoup(html, "lxml")
    coll = Collector(vc_url, vc)
    counts = {
        "config_selector": extract_config_cards(soup, coll, vc),
        "card": extract_cards(soup, coll),
        "logo_alt": extract_logo_alt(soup, coll),
        "anchor_text": extract_anchor_text(soup, coll),
    }
    return coll.result(), counts


# ── Deduplication ──────────────────────────────────────────────────────────────

def dedupe_within_vc(rows: list[dict], threshold: int) -> list[dict]:
    """
    FIX 8: fuzzy dedup is scoped to one VC and uses a high threshold, so
    distinct companies survive and cross-fund duplicates are preserved
    (a company backed by two funds is evidence, not noise).
    """
    kept: list[dict] = []
    seen_names: list[tuple[str, str]] = []
    for r in rows:
        name = r["name"]
        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        dup = False
        for prev, prev_norm in seen_names:
            if norm and norm == prev_norm:
                dup = True
                break
            if fuzz.ratio(name.lower(), prev.lower()) >= threshold:
                dup = True
                break
        if not dup:
            seen_names.append((name, norm))
            kept.append(r)
    return kept


# ── Crawl one VC ───────────────────────────────────────────────────────────────

def crawl_vc(vc: dict, cfg: dict, limiter: RateLimiter,
             robots: RobotsCache) -> tuple[list[dict], dict]:
    name = vc["name"]
    url = vc["url"]
    logger.info(f"\nCrawling: {name}")
    logger.info(f"  URL: {url}")

    summary = {
        "vc_source": name, "url": url, "http_status": "", "fetch_method": "",
        "rendered": False, "n_external": 0, "n_detail": 0, "n_total": 0,
        "strategies": "", "status": "ok", "reason": "",
    }

    # A configured JSON API wins outright — faster and complete.
    if vc.get("json_endpoint"):
        json_rows, err = fetch_config_json(vc, cfg, limiter, robots)
        if json_rows:
            coll = Collector(url, vc)
            for r in json_rows:
                coll.add_json(r["name"], r.get("url", ""), "json_config")
            rows = coll.result()
            rows = dedupe_within_vc(rows, cfg["crawler"]["fuzzy_dedup_threshold"])
            summary.update({
                "fetch_method": "json_api", "http_status": "200",
                "n_total": len(rows), "strategies": "json_config",
                "n_external": sum(1 for r in rows if r["link_type"] == "external"),
                "n_detail": sum(1 for r in rows if r["link_type"] == "detail"),
            })
            logger.info(f"  JSON API returned {len(rows)} companies")
            return _stamp(rows, name), summary
        summary["reason"] = f"json_endpoint empty ({err})" if err else "json_endpoint empty"
        logger.warning(f"  configured JSON endpoint returned nothing ({err})")

    if not robots.allowed(url):
        logger.error("  robots.txt disallows this path — skipping")
        summary.update({"status": "skipped", "reason": "robots_disallow"})
        return [], summary

    render_mode = (vc.get("render") or "auto").lower()
    html, status, err = None, None, ""

    if render_mode != "playwright":
        html, status, err = fetch_static(url, cfg, limiter)
        summary["http_status"] = str(status or "")
        summary["fetch_method"] = "requests"
        if err:
            logger.warning(f"  requests failed: {err}")

    needs_render = (
        render_mode == "playwright"
        or not html
        or len(html) < 3000
        or (status is not None and status >= 400)
    )

    if needs_render and render_mode != "static":
        logger.info("  rendering with Playwright...")
        rendered, rerr = fetch_rendered(url, cfg, vc)
        if rendered:
            html = rendered
            summary["rendered"] = True
            summary["fetch_method"] = "playwright"
            if not summary["http_status"]:
                summary["http_status"] = "200"
        else:
            logger.error(f"  Playwright failed: {rerr}")
            if not html:
                summary.update({"status": "failed", "reason": rerr or err or "fetch_failed"})
                return [], summary

    if not html:
        summary.update({"status": "failed", "reason": err or "no_html"})
        return [], summary

    rows, counts = extract_all(html, url, vc)

    # Auto-discovered JSON payloads are a FALLBACK only. The blob walker picks the
    # largest list of name-bearing dicts, which on a healthy page is often some
    # unrelated collection (nav, sectors, image manifests). When the DOM already
    # produced a real harvest, trust it. An explicitly configured json_endpoint is
    # handled above and always wins.
    coll_json = Collector(url, vc)
    if len(rows) < 5:
        nxt = find_next_data(html)
        if nxt:
            for r in extract_from_json_blob(nxt, url):
                coll_json.add_json(r["name"], r.get("url", ""), "json_next_data")
        for r in try_wp_json(url, html, cfg, limiter, robots):
            coll_json.add_json(r["name"], r.get("url", ""), "json_wp")

    json_rows = coll_json.result()
    if json_rows:
        have = {r["url"] for r in rows if r["url"]}
        rows.extend([r for r in json_rows if not r["url"] or r["url"] not in have])
        counts["json"] = len(json_rows)

    rows = dedupe_within_vc(rows, cfg["crawler"]["fuzzy_dedup_threshold"])

    summary.update({
        "n_total": len(rows),
        "n_external": sum(1 for r in rows if r["link_type"] == "external"),
        "n_detail": sum(1 for r in rows if r["link_type"] == "detail"),
        "strategies": ";".join(f"{k}={v}" for k, v in counts.items() if v),
    })
    if len(rows) == 0:
        summary["status"] = "empty"
        summary["reason"] = summary["reason"] or "no candidates extracted"

    logger.info(
        f"  Found {len(rows)} candidates "
        f"({summary['n_external']} external, {summary['n_detail']} detail) "
        f"[{summary['strategies']}]"
    )
    return _stamp(rows, name), summary


def _stamp(rows: list[dict], vc_name: str) -> list[dict]:
    today = date.today().isoformat()
    for r in rows:
        r["vc_source"] = vc_name
        r["date_discovered"] = today
        r["segment"] = "startup"
        r["quality_flag"] = "needs_review"  # never auto-promoted
    return rows


# ── Orchestration ──────────────────────────────────────────────────────────────

def run(filter_vc: str | None = None, config_path: str = "config/config.yaml",
        dry_run: bool = False, limit: int | None = None):
    cfg = load_config(config_path)
    vc_list = cfg.get("vc_portfolio_urls", [])

    if filter_vc:
        vc_list = [v for v in vc_list if filter_vc.lower() in v["name"].lower()]
        if not vc_list:
            logger.error(f"No VC found matching: {filter_vc}")
            return
    if limit:
        vc_list = vc_list[:limit]

    crawler_cfg = cfg.setdefault("crawler", {})
    crawler_cfg.setdefault("fuzzy_dedup_threshold", 95)
    delay = cfg.get("rate_limits", {}).get("general_delay_seconds", 1.5)
    limiter = RateLimiter(delay)
    robots = RobotsCache(
        crawler_cfg.get("user_agent", BROWSER_UA),
        enabled=crawler_cfg.get("respect_robots", True),
    )

    all_rows: list[dict] = []
    summaries: list[dict] = []
    for vc in vc_list:
        try:
            rows, summary = crawl_vc(vc, cfg, limiter, robots)
        except Exception as e:
            logger.error(f"  unhandled error: {type(e).__name__}: {e}")
            rows, summary = [], {
                "vc_source": vc["name"], "url": vc["url"], "http_status": "",
                "fetch_method": "", "rendered": False, "n_external": 0,
                "n_detail": 0, "n_total": 0, "strategies": "",
                "status": "failed", "reason": f"{type(e).__name__}: {str(e)[:120]}",
            }
        all_rows.extend(rows)
        summaries.append(summary)

    logger.info(f"\n{'=' * 62}")
    logger.info(f"Crawled {len(vc_list)} VC(s); {len(all_rows)} raw candidates")

    df_summary = pd.DataFrame(summaries, columns=SUMMARY_COLUMNS)
    logger.info("\n" + df_summary[
        ["vc_source", "n_total", "n_external", "n_detail", "status"]
    ].to_string(index=False))

    if not all_rows:
        logger.warning("No candidates found.")
    df_new = pd.DataFrame(all_rows)
    for col in CSV_COLUMNS:
        if col not in df_new.columns:
            df_new[col] = ""
    df_new = df_new[CSV_COLUMNS]

    if dry_run:
        logger.info("\n[DRY RUN] nothing written.")
        if len(df_new):
            logger.info("\nSample (up to 25):\n" + df_new[
                ["name", "url", "link_type", "strategy"]
            ].head(25).to_string(index=False))
        return df_new, df_summary

    out_dir = Path(cfg["paths"]["index"])
    if not out_dir.is_absolute():
        out_dir = Path(__file__).resolve().parents[2] / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "startup_candidates.csv"
    summary_path = out_dir / "crawl_run_summary.csv"

    if out_path.exists():
        df_existing = pd.read_csv(out_path)
        for col in CSV_COLUMNS:
            if col not in df_existing.columns:
                df_existing[col] = ""
        before = len(df_existing)
        df_combined = pd.concat([df_existing[CSV_COLUMNS], df_new], ignore_index=True)
        # Append-and-dedupe on (url, vc_source) only — existing rows are never dropped.
        df_combined = df_combined.drop_duplicates(subset=["url", "vc_source"], keep="first")
        df_combined.to_csv(out_path, index=False)
        logger.info(f"\nAppended: {before} -> {len(df_combined)} rows ({len(df_combined) - before} new)")
    else:
        df_new = df_new.drop_duplicates(subset=["url", "vc_source"], keep="first")
        df_new.to_csv(out_path, index=False)
        logger.info(f"\nSaved {len(df_new)} candidates to {out_path}")

    if summary_path.exists():
        prev = pd.read_csv(summary_path)
        df_summary_out = pd.concat([prev, df_summary], ignore_index=True)
    else:
        df_summary_out = df_summary
    df_summary_out.to_csv(summary_path, index=False)
    logger.info(f"Run summary -> {summary_path}")
    logger.info("All rows flagged quality_flag=needs_review.")
    return df_new, df_summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl VC portfolio pages (v2)")
    parser.add_argument("--vc", default=None, help="Filter to one VC by name")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Extract but write nothing")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N VCs")
    args = parser.parse_args()
    run(filter_vc=args.vc, config_path=args.config,
        dry_run=args.dry_run, limit=args.limit)