"""
scripts/secondary/fetch_press_releases.py

Fetch press releases and news announcements for companies.
Tries common press/news URL patterns directly on company websites.
Works for both public companies and startups.

Run from project root:
    python scripts/secondary/fetch_press_releases.py --tickers ECL TILE
    python scripts/secondary/fetch_press_releases.py --companies "Solugen" "Twelve"
    python scripts/secondary/fetch_press_releases.py --from-startups
    python scripts/secondary/fetch_press_releases.py --from-startups --limit 20
    python scripts/secondary/fetch_press_releases.py  # all public companies
"""

import sys
import re
import time
import argparse
from pathlib import Path
from datetime import date
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import (
    load_config, get_logger, build_markdown,
    source_filepath, append_to_index
)

import requests
import pandas as pd
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

logger = get_logger("fetch_press_releases")

# URL patterns for press/news pages
PRESS_PATTERNS = [
    "{base}/news",
    "{base}/newsroom",
    "{base}/press",
    "{base}/press-releases",
    "{base}/press-room",
    "{base}/media",
    "{base}/media-center",
    "{base}/media-room",
    "{base}/about/news",
    "{base}/about/newsroom",
    "{base}/about/press",
    "{base}/investors/news",
    "{base}/investors/press-releases",
    "{base}/en/news",
    "{base}/en/newsroom",
    "{base}/company/news",
    "{base}/company/newsroom",
    "{base}/latest",
    "{base}/updates",
    "{base}/announcements",
]

# Keywords indicating real press release content
PRESS_KEYWORDS = [
    "press release", "announcement", "announces", "launches", "named",
    "awarded", "partnership", "funding", "raised", "appoints", "expands",
    "closes", "secures", "selected", "wins", "achieves", "reports",
    "milestone", "agreement", "collaboration", "investment",
]

MIN_KEYWORD_COUNT = 2
MIN_TEXT_LENGTH   = 300

# Reject pages that are navigation/error pages
REJECT_PHRASES = [
    "page not found", "404", "cookie consent", "we use cookies",
    "this website uses cookies", "access denied", "403 forbidden",
]


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }


def get_base_url(company_name: str, ticker: str,
                 known_url: str = "", cik: str = "") -> str | None:
    """Find company's base website URL."""

    # Use known URL from startup_candidates.csv if available
    if known_url and known_url.startswith("http"):
        parsed = urlparse(known_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    # EDGAR ticker lookup for public companies
    if ticker and not cik:
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": "Gavin Gong gyg2009@nyu.edu"},
                timeout=10
            )
            if resp.status_code == 200:
                for entry in resp.json().values():
                    if entry.get("ticker", "").upper() == ticker.upper():
                        cik = str(entry["cik_str"]).zfill(10)
                        break
        except Exception:
            pass

    if cik:
        try:
            resp = requests.get(
                f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                headers={"User-Agent": "Gavin Gong gyg2009@nyu.edu"},
                timeout=10
            )
            if resp.status_code == 200:
                website = resp.json().get("website", "")
                if website and website.startswith("http"):
                    parsed = urlparse(website)
                    return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

    # Construct from company name
    stop = {"inc", "corp", "ltd", "llc", "co", "the", "group", "company",
            "ag", "sa", "plc", "nv", "bv", "gmbh", "technologies",
            "technology", "systems", "solutions", "international",
            "industries", "products", "services"}
    words = company_name.lower().replace(",", "").replace(".", "").split()
    core  = [w for w in words if w not in stop and len(w) > 2]
    if not core:
        return None

    candidates = []
    if len(core) > 1:
        candidates.append(f"https://www.{''.join(core[:2])}.com")
        candidates.append(f"https://www.{'-'.join(core[:2])}.com")
    candidates.append(f"https://www.{core[0]}.com")

    from playwright.sync_api import sync_playwright
    for url in candidates:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-http2",
                          "--disable-blink-features=AutomationControlled"]
                )
                ctx  = browser.new_context(
                    user_agent=get_headers()["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=12000)
                final = page.url
                browser.close()
            if resp and resp.status < 400:
                parsed = urlparse(final)
                return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            continue
    return None


def try_press_patterns(base_url: str, company_name: str) -> tuple[str, str] | None:
    """
    Try press/news URL patterns using Playwright.
    Returns (url, text) if a real press page is found, else None.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-http2",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=get_headers()["User-Agent"],
            viewport={"width": 1280, "height": 800},
        )

        for pattern in PRESS_PATTERNS:
            url = pattern.format(base=base_url.rstrip("/"))
            try:
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=12000)
                if not resp or resp.status >= 400:
                    page.close()
                    continue

                time.sleep(1)
                text = page.inner_text("body")
                page.close()

                # Reject error/cookie pages
                text_lower = text.lower()
                if any(p in text_lower for p in REJECT_PHRASES):
                    continue

                # Check keyword count
                kw_count = sum(1 for kw in PRESS_KEYWORDS if kw in text_lower)
                if kw_count >= MIN_KEYWORD_COUNT and len(text) >= MIN_TEXT_LENGTH:
                    logger.info(f"  Found press page: {url} ({kw_count} keywords)")
                    browser.close()
                    return url, text

            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                continue

        browser.close()
    return None


def fetch_press_releases(ticker: str, company_name: str, meta: dict,
                         cfg: dict, known_url: str = ""):
    """Fetch press releases for one company."""
    cik     = str(meta.get("cik", ""))
    segment = meta.get("segment", "public")

    base_url = get_base_url(company_name, ticker, known_url, cik)
    if not base_url:
        logger.warning(f"  Could not find base URL for {company_name}")
        result = None
    else:
        logger.info(f"  Base URL: {base_url}")
        result = try_press_patterns(base_url, company_name)

    if result:
        source_url, text = result
        quality = "clean"
    else:
        source_url = ""
        text       = "(No press releases found)"
        quality    = "flagged_empty"

    folder_key = ticker if segment == "public" else company_name
    out_path   = source_filepath(
        company     = folder_key,
        source_type = "press release",
        year        = date.today().year,
        base_dir    = cfg["paths"]["normalized"],
    )
    out_path.write_text(
        build_markdown(
            text         = text[:50000],
            company      = company_name,
            segment      = segment,
            source_type  = "press release",
            source_year  = date.today().year,
            source_url   = source_url,
            ticker       = ticker,
            industry     = meta.get("industry", ""),
            sic          = str(meta.get("sic", "")),
            exchange     = meta.get("exchange", ""),
            hq_country   = meta.get("hq_country", ""),
            quality_flag = quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path} [{quality}]")

    append_to_index({
        "company":      company_name,
        "segment":      segment,
        "ticker":       ticker,
        "industry":     meta.get("industry", ""),
        "source_type":  "press release",
        "source_year":  date.today().year,
        "source_url":   source_url,
        "file_path":    str(out_path),
        "quality_flag": quality,
    }, index_path=f"{cfg['paths']['index']}/master_index.csv")

    return quality == "clean"


def run(tickers=None, companies=None, from_startups=False,
        limit=None, config_path="config/config.yaml"):
    cfg   = load_config(config_path)
    csv   = Path(cfg["paths"]["index"]) / "public_company_list.csv"
    s_csv = Path(cfg["paths"]["index"]) / "startup_candidates.csv"

    if from_startups:
        if not s_csv.exists():
            logger.error(f"Startup list not found: {s_csv}")
            return
        df = pd.read_csv(s_csv)
        if limit:
            df = df.head(limit)
        targets = []
        for _, r in df.iterrows():
            name = str(r.get("name", "")).strip()
            if not name or name == "nan" or len(name) > 60:
                continue
            targets.append({
                "ticker":    name.upper().replace(" ", "_")[:20],
                "company":   name,
                "segment":   "startup",
                "known_url": str(r.get("url", "")),
                "meta":      {"segment": "startup", "industry": "", "sic": "",
                              "exchange": "", "hq_country": "", "cik": ""},
            })
        logger.info(f"Loaded {len(targets):,} startups")

    elif tickers:
        name_map = {}
        if csv.exists():
            df = pd.read_csv(csv)
            name_map = {
                str(r["ticker"]).upper(): r.to_dict()
                for _, r in df.iterrows()
                if pd.notna(r.get("ticker"))
            }
        targets = [
            {"ticker": t.upper(), "company": name_map.get(t.upper(), {}).get("company", t),
             "segment": "public", "known_url": str(name_map.get(t.upper(), {}).get("website", "")),
             "meta": name_map.get(t.upper(), {"segment": "public"})}
            for t in tickers
        ]

    elif companies:
        targets = [
            {"ticker": c.upper().replace(" ", "_")[:20], "company": c,
             "segment": "public", "known_url": str(r.get("website", "")),
             "meta": {"segment": "startup", "industry": "", "sic": "",
                      "exchange": "", "hq_country": "", "cik": ""}}
            for c in companies
        ]

    else:
        if not csv.exists():
            logger.error(f"Run build_company_list.py first")
            return
        df = pd.read_csv(csv)
        if limit:
            df = df.head(limit)
        targets = [
            {"ticker": str(r["ticker"]).upper(),
             "company": r.get("company", ""),
             "segment": "public", "known_url": "",
             "meta": r.to_dict()}
            for _, r in df.iterrows()
            if pd.notna(r.get("ticker"))
        ]
        logger.info(f"Loaded {len(targets):,} public companies")

    found, not_found = 0, 0
    for t in targets:
        logger.info(f"\n── {t['ticker']} ({t['company']}) ──")
        try:
            success = fetch_press_releases(
                t["ticker"], t["company"], t["meta"], cfg, t["known_url"]
            )
            if success:
                found += 1
            else:
                not_found += 1
        except Exception as e:
            logger.error(f"  {t['ticker']}: {e}")
            not_found += 1
        time.sleep(2)

    logger.info(f"\nDone. Found: {found} | Not found: {not_found}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch press releases")
    p.add_argument("--tickers",       nargs="+")
    p.add_argument("--companies",     nargs="+")
    p.add_argument("--from-startups", action="store_true")
    p.add_argument("--limit",         type=int, default=None)
    p.add_argument("--config",        default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, companies=args.companies,
        from_startups=args.from_startups, limit=args.limit,
        config_path=args.config)
