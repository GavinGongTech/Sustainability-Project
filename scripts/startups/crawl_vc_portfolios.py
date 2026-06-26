"""
scripts/startups/crawl_vc_portfolios.py

Week 3: Crawl VC and accelerator portfolio pages to discover startups.
Outputs a CSV ready to use as the startup company list.

Key design decisions:
  - External domain filter: real portfolio companies have their own websites.
    Nav links and internal pages point back to the VC domain — those are dropped.
  - Playwright fallback: JS-heavy portfolio pages get rendered by a real browser.
  - Append mode: re-running adds new results without wiping previous ones.
  - Quality flag: each result is flagged needs_review until manually confirmed.

Run from project root:
    python scripts/startups/crawl_vc_portfolios.py
    python scripts/startups/crawl_vc_portfolios.py --vc "Lowercarbon Capital"
    python scripts/startups/crawl_vc_portfolios.py --vc "Y Combinator Climate"

Output: data/index/startup_candidates.csv
"""

import sys
import re
import time
import argparse
from pathlib import Path
from datetime import date
from urllib.parse import urlparse, urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger

import pandas as pd
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

logger = get_logger("crawl_vc_portfolios")


# ── Constants ─────────────────────────────────────────────────────────────────

# Name fragments that indicate a nav/UI link, not a company
NAV_KEYWORDS = {
    "accept", "decline", "close", "menu", "search", "login", "sign in",
    "sign up", "subscribe", "newsletter", "cookie", "privacy", "terms",
    "about", "contact", "team", "careers", "jobs", "press", "blog",
    "news", "insights", "events", "resources", "faq", "help", "support",
    "home", "back", "next", "previous", "more", "view all", "see all",
    "read more", "learn more", "get started", "apply", "apply now",
    "portfolio", "invest", "fund", "our work", "what we do",
    "for entrepreneurs", "eligibility", "how to apply", "programs",
    "partners", "mentors", "corporates", "cohort", "impact",
    "linkedin", "twitter", "facebook", "instagram", "youtube", "x.com",
}

# Social/utility domains to always drop
SKIP_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "youtube.com", "crunchbase.com", "pitchbook.com",
    "techcrunch.com", "forbes.com", "medium.com", "substack.com",
}


# ── URL helpers ───────────────────────────────────────────────────────────────

def abs_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def is_external(company_url: str, vc_url: str) -> bool:
    """
    Returns True if company_url points to a different domain than the VC site.
    This is the key filter — real portfolio companies have their own websites.
    Internal VC pages (cohort pages, program pages) share the VC domain.
    """
    vc_domain      = get_domain(vc_url)
    company_domain = get_domain(company_url)
    if not company_domain:
        return False
    # Drop if same domain as VC
    if company_domain == vc_domain:
        return False
    # Drop if base domain matches (e.g. sub.third-derivative.org)
    vc_base = ".".join(vc_domain.split(".")[-2:])
    co_base = ".".join(company_domain.split(".")[-2:])
    if vc_base and vc_base == co_base:
        return False
    return True


# ── Name filtering ────────────────────────────────────────────────────────────

def is_nav_name(name: str) -> bool:
    """Return True if name looks like a nav/UI element rather than a company."""
    name_lower = name.lower().strip()
    # Exact match against known nav keywords
    if name_lower in NAV_KEYWORDS:
        return True
    # Partial match for multi-word nav phrases
    if any(kw in name_lower for kw in NAV_KEYWORDS if len(kw) > 6):
        return True
    # Skip very short names (1-2 chars) and very long names (likely sentences)
    if len(name) < 3 or len(name) > 60:
        return True
    # Skip names that look like sentences (contain common sentence starters)
    if re.search(r'\b(the|our|we|you|your|this|that|these|those|how|what|why|where|when)\b',
                 name_lower):
        return True
    return False


def is_skip_domain(url: str) -> bool:
    domain = get_domain(url)
    return any(skip in domain for skip in SKIP_DOMAINS)


def clean_company_name(name: str) -> str:
    """
    Extract the actual company name from strings that mix a tagline with
    the company name. Many VC sites format their portfolio cards as:
      "Making chemicals with enzymes, not oil. Solugen"
      "Zero-carbon cement. Sublime"
      "Turning CO2 into jet fuel. Twelve"

    The company name is always the last sentence fragment after the final
    period, if the name contains a period and the fragment after it is short
    (1-4 words). Otherwise return the name unchanged.
    """
    name = name.strip()

    # If name contains a period, check if the last segment is a short company name
    if "." in name:
        parts = name.rsplit(".", 1)
        after_period = parts[1].strip()
        # If the part after the last period is 1-4 words and title-cased, it's
        # likely the company name
        words = after_period.split()
        if 1 <= len(words) <= 4 and after_period and after_period[0].isupper():
            return after_period

    # If name contains a dash separator pattern like "CompanyName — tagline"
    if " — " in name or " – " in name:
        sep = " — " if " — " in name else " – "
        parts = name.split(sep, 1)
        candidate = parts[0].strip()
        if 1 <= len(candidate.split()) <= 4:
            return candidate

    return name


# ── HTML extraction ───────────────────────────────────────────────────────────

def extract_company_links(html: str, vc_url: str) -> list[dict]:
    """
    Extract candidate portfolio company names and URLs from a VC page.

    Three strategies tried in order:
    1. Card/grid containers with class names suggesting portfolio items
    2. Image+link combinations (many VC sites use logo grids)
    3. Fallback: all external anchor links with short text

    The external domain filter is the primary quality gate — it drops
    all internal VC pages, nav links, and social media links.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    seen_urls = set()

    def add(name: str, url: str):
        name = clean_company_name(name.strip())
        url  = abs_url(url, vc_url)
        if not name or not url:
            return
        if url in seen_urls:
            return
        if is_nav_name(name):
            return
        if is_skip_domain(url):
            return
        if not is_external(url, vc_url):
            return
        seen_urls.add(url)
        candidates.append({"name": name, "url": url})

    # Strategy 1: card/grid containers
    card_pat = re.compile(
        r"(card|company|portfolio|startup|item|member|partner|venture|invest)",
        re.I
    )
    for tag in soup.find_all(["article", "li", "div"], class_=card_pat):
        a = tag.find("a", href=True)
        name_el = tag.find(["h1", "h2", "h3", "h4", "h5", "strong", "span", "p"])
        if a and name_el:
            add(name_el.get_text(" ", strip=True), a["href"])

    # Strategy 2: image + link combinations (logo grids)
    if len(candidates) < 3:
        for a in soup.find_all("a", href=True):
            img = a.find("img")
            if img:
                # Use alt text as company name
                alt = img.get("alt", "").strip()
                if alt and len(alt) > 2:
                    add(alt, a["href"])

    # Strategy 3: fallback — all short-text external links
    if len(candidates) < 3:
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = a["href"]
            if 3 <= len(text) <= 50:
                add(text, href)

    return candidates


# ── Page fetching ─────────────────────────────────────────────────────────────

def fetch_html(url: str, config: dict) -> str | None:
    """
    Fetch page HTML. Tries requests first, falls back to Playwright
    if content is thin (likely JS-rendered).
    """
    headers = {
        "User-Agent": f"{config['user']['name']} {config['user']['email']}"
    }
    html = None

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"  requests failed: {e}")

    if not html or len(html) < 3000:
        logger.info("  Content thin or failed — trying Playwright...")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()
            logger.info("  Playwright succeeded")
        except Exception as e:
            logger.error(f"  Playwright also failed: {e}")
            return None

    return html


# ── Deduplication ─────────────────────────────────────────────────────────────

def deduplicate(records: list[dict], threshold: int = 88) -> list[dict]:
    """Remove near-duplicate company names using rapidfuzz."""
    seen = []
    unique = []
    for r in records:
        name = r["name"]
        if not any(fuzz.ratio(name, s) >= threshold for s in seen):
            seen.append(name)
            unique.append(r)
    return unique


# ── Main crawler ──────────────────────────────────────────────────────────────

def crawl_vc(vc_name: str, url: str, config: dict) -> list[dict]:
    """Crawl one VC portfolio page. Returns list of company candidate dicts."""
    logger.info(f"\nCrawling: {vc_name}")
    logger.info(f"  URL: {url}")

    html = fetch_html(url, config)
    if not html:
        logger.error(f"  Failed to fetch {url} — skipping")
        return []

    time.sleep(config["rate_limits"]["general_delay_seconds"])

    candidates = extract_company_links(html, vc_url=url)
    logger.info(f"  Found {len(candidates)} external-domain candidates")

    for c in candidates:
        c["vc_source"]       = vc_name
        c["date_discovered"] = date.today().isoformat()
        c["segment"]         = "startup"
        c["quality_flag"]    = "needs_review"  # manual scan required before use

    return candidates


def run(filter_vc: str | None = None,
        config_path: str = "config/config.yaml"):

    cfg = load_config(config_path)
    vc_list = cfg.get("vc_portfolio_urls", [])

    if filter_vc:
        vc_list = [v for v in vc_list if filter_vc.lower() in v["name"].lower()]
        if not vc_list:
            logger.error(f"No VC found matching: {filter_vc}")
            return

    all_candidates = []
    for vc in vc_list:
        rows = crawl_vc(vc["name"], vc["url"], cfg)
        all_candidates.extend(rows)

    if not all_candidates:
        logger.warning("No candidates found across all VCs crawled.")
        return

    # Deduplicate by name across all VCs
    unique = deduplicate(all_candidates, threshold=88)
    logger.info(f"\nTotal unique candidates after dedup: {len(unique)}")

    out_path = Path(cfg["paths"]["index"]) / "startup_candidates.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Append mode: if file exists, merge with existing and re-dedup
    cols = ["name", "url", "segment", "vc_source", "date_discovered", "quality_flag"]
    df_new = pd.DataFrame(unique)
    df_new = df_new[[c for c in cols if c in df_new.columns]]

    if out_path.exists():
        df_existing = pd.read_csv(out_path)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined = df_combined.drop_duplicates(subset=["url"])
        df_combined.to_csv(out_path, index=False)
        logger.info(f"Appended to existing file — total rows: {len(df_combined)}")
    else:
        df_new.to_csv(out_path, index=False)
        logger.info(f"Saved {len(df_new)} candidates to {out_path}")

    # Preview
    df_preview = pd.read_csv(out_path)
    logger.info(f"\nFirst 20 results:\n{df_preview.head(20).to_string(index=False)}")
    logger.info(f"\nNOTE: All results flagged 'needs_review'.")
    logger.info(f"Scan {out_path} and remove junk before using as startup list.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl VC portfolio pages")
    parser.add_argument(
        "--vc", default=None,
        help="Filter to one VC by name substring, e.g. 'Lowercarbon Capital'"
    )
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    run(filter_vc=args.vc, config_path=args.config)
