"""
scripts/utils/web_scraper.py

Generic scraper for company websites: product pages, engineering blogs,
press releases, ESG reports (HTML), IR pages.

For JS-heavy pages, falls back to Playwright automatically.

Usage (as a module):
    from scripts.utils.web_scraper import scrape_url
    text = scrape_url("https://example.com/sustainability")
"""

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger, build_markdown, \
    source_filepath, append_to_index

import requests
from bs4 import BeautifulSoup

logger = get_logger("web_scraper")


# ── Tags to strip (navigation, ads, boilerplate) ──────────────────────────────
STRIP_TAGS = [
    "script", "style", "nav", "header", "footer",
    "aside", "form", "iframe", "noscript", "svg",
    "button", "input", "select", "textarea",
    "cookie", "banner", "popup",
]


def clean_html(html: str) -> str:
    """Strip boilerplate and return clean body text."""
    soup = BeautifulSoup(html, "lxml")

    # Remove junk tags
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    # Try to find the main content block
    main = (
        soup.find("main") or
        soup.find("article") or
        soup.find(id=lambda x: x and "content" in x.lower()) or
        soup.find(class_=lambda x: x and "content" in " ".join(x).lower()
                  if isinstance(x, list) else "content" in x.lower()) or
        soup.body or
        soup
    )

    lines = [l.strip() for l in main.get_text(separator="\n").splitlines()]
    lines = [l for l in lines if len(l) > 20]  # drop very short lines
    return "\n".join(lines)


def scrape_url_requests(url: str, headers: dict | None = None) -> str | None:
    """Fetch a URL with requests. Returns cleaned text or None on failure."""
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return clean_html(resp.text)
    except Exception as e:
        logger.warning(f"requests failed for {url}: {e}")
        return None


def scrape_url_playwright(url: str) -> str | None:
    """
    Fetch a JS-rendered page with Playwright.
    Only called as fallback when requests returns empty/thin content.
    Requires: playwright install chromium  (run once after pip install)
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            browser.close()
        return clean_html(html)
    except Exception as e:
        logger.warning(f"Playwright failed for {url}: {e}")
        return None


def scrape_url(url: str, config: dict | None = None,
               min_chars: int = 500) -> str | None:
    """
    Scrape a URL. Tries requests first; falls back to Playwright
    if content is too thin (likely JS-rendered).
    """
    default_headers = {"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"}
    if config:
        name = config["user"]["name"]
        email = config["user"]["email"]
        default_headers["User-Agent"] = f"{name}/{email} (NYU Research)"

    delay = config["rate_limits"]["general_delay_seconds"] if config else 1.5

    text = scrape_url_requests(url, headers=default_headers)
    time.sleep(delay)

    if not text or len(text) < min_chars:
        logger.info(f"Content thin ({len(text) if text else 0} chars), trying Playwright: {url}")
        text = scrape_url_playwright(url)
        time.sleep(delay)

    return text


# ── Convenience wrapper: scrape + save ────────────────────────────────────────

def scrape_and_save(
    url: str,
    company: str,
    segment: str,
    source_type: str,
    source_year: int,
    config: dict,
    ticker: str = "",
    industry: str = "",
    hq_country: str = "",
):
    """Scrape a URL and save the normalized markdown file + index row."""
    text = scrape_url(url, config=config)

    if not text or len(text) < 200:
        quality = "flagged_empty"
        text = "(scrape returned empty or very thin content — flag for review)"
        logger.warning(f"Empty scrape: {url}")
    elif len(text) < 800:
        quality = "flagged_short"
        logger.warning(f"Short scrape ({len(text)} chars): {url}")
    else:
        quality = "clean"
        logger.info(f"Scraped {len(text):,} chars from {url}")

    out_path = source_filepath(
        company=company,
        source_type=source_type,
        year=source_year,
        base_dir=config["paths"]["normalized"],
    )

    md = build_markdown(
        text=text,
        company=company,
        segment=segment,
        source_type=source_type,
        source_year=source_year,
        source_url=url,
        ticker=ticker,
        industry=industry,
        hq_country=hq_country,
        quality_flag=quality,
    )
    out_path.write_text(md, encoding="utf-8")
    logger.info(f"Saved: {out_path}")

    append_to_index({
        "company": company,
        "segment": segment,
        "ticker": ticker,
        "industry": industry,
        "source_type": source_type,
        "source_year": source_year,
        "source_url": url,
        "file_path": str(out_path),
        "quality_flag": quality,
    }, index_path=f"{config['paths']['index']}/master_index.csv")

    return out_path, quality


# ── CLI quick test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape a single URL (test mode)")
    parser.add_argument("url")
    parser.add_argument("--company", required=True)
    parser.add_argument("--segment", default="startup")
    parser.add_argument("--source-type", default="product page")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    path, flag = scrape_and_save(
        url=args.url,
        company=args.company,
        segment=args.segment,
        source_type=args.source_type,
        source_year=args.year,
        config=cfg,
    )
    print(f"\nResult: {path}  [quality={flag}]")
