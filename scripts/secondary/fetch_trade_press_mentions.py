"""
scripts/secondary/fetch_trade_press_mentions.py

Searches DuckDuckGo's plain-HTML search endpoint (html.duckduckgo.com),
restricted via site: operators to a curated list of climate-tech / industry
trade publications. This endpoint is specifically maintained by DuckDuckGo
for lightweight, no-JavaScript tools and has much lighter bot detection
than Google's main search page — it's a real, general-purpose search
engine, just a different one.

Does NOT require SerpAPI. DuckDuckGo's index is smaller than Google's, so
expect a lower hit rate than a true Google/SerpAPI search would give — but
it costs nothing and searches many publications at once, unlike scraping
each site's own on-site search individually.

Run from project root:
    python scripts/secondary/fetch_trade_press_mentions.py --tickers ECL TILE
    python scripts/secondary/fetch_trade_press_mentions.py --companies "Solugen"
    python scripts/secondary/fetch_trade_press_mentions.py --from-startups --limit 20
    python scripts/secondary/fetch_trade_press_mentions.py --from-startups
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import date
from urllib.parse import urlparse, parse_qs, unquote

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

logger = get_logger("fetch_trade_press_mentions")

SEARCH_URL = "https://html.duckduckgo.com/html/"

TRADE_PRESS_SITES = [
    "greenbiz.com", "canarymedia.com", "utilitydive.com", "waste360.com",
    "agfundernews.com", "esgtoday.com", "chemweek.com", "plasticsnews.com",
    "sustainablebrands.com", "triplepundit.com", "chemistryworld.com",
    "environmentalleader.com", "constructiondive.com",
]

MAX_RESULTS = 5


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }


def unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo sometimes wraps result links in a redirect — unwrap it."""
    if "duckduckgo.com/l/" in href or "uddg=" in href:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def search_duckduckgo(company_name: str) -> list[dict]:
    """
    Search DuckDuckGo's HTML endpoint restricted to known trade publications.
    Returns [] if nothing found or the request fails.
    """
    site_filter = " OR ".join(f"site:{s}" for s in TRADE_PRESS_SITES)
    query = f'"{company_name}" ({site_filter})'

    try:
        resp = requests.post(
            SEARCH_URL, data={"q": query},
            headers=get_headers(), timeout=20
        )
        if resp.status_code != 200:
            return []
    except Exception as e:
        logger.warning(f"    DuckDuckGo request failed: {str(e)[:60]}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    seen_urls = set()

    for a in soup.find_all("a", class_="result__a", href=True):
        title = a.get_text(strip=True)
        href  = unwrap_ddg_redirect(a["href"])
        if not title or len(title) < 10:
            continue
        if not href.startswith("http"):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        domain = urlparse(href).netloc.lower().lstrip("www.")
        results.append({"title": title[:200], "url": href, "domain": domain})
        if len(results) >= MAX_RESULTS:
            break

    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "(No trade press mentions found)"
    lines = ["# Trade Press Mentions", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r['title']}")
        lines.append(f"**Source:** {r['domain']}")
        lines.append(f"**Link:** {r['url']}")
        lines.append("")
    return "\n".join(lines)


def fetch_one(ticker: str, company_name: str, meta: dict, cfg: dict):
    logger.info(f"  Searching trade press (DuckDuckGo) for '{company_name}'")
    results = search_duckduckgo(company_name)

    quality = "clean" if results else "flagged_empty"
    text    = format_results(results)
    segment = meta.get("segment", "public")
    folder_key = ticker if segment == "public" else company_name

    out_path = source_filepath(
        company=folder_key, source_type="trade press mention",
        year=date.today().year, base_dir=cfg["paths"]["normalized"],
    )
    out_path.write_text(
        build_markdown(
            text=text, company=company_name, segment=segment,
            source_type="trade press mention", source_year=date.today().year,
            source_url="", ticker=ticker, industry=meta.get("industry", ""),
            sic=str(meta.get("sic", "")), exchange=meta.get("exchange", ""),
            hq_country=meta.get("hq_country", ""), quality_flag=quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path} [{quality}] ({len(results)} mentions)")

    append_to_index({
        "company": company_name, "segment": segment, "ticker": ticker,
        "industry": meta.get("industry", ""), "source_type": "trade press mention",
        "source_year": date.today().year, "source_url": "",
        "file_path": str(out_path), "quality_flag": quality,
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
                "ticker": name.upper().replace(" ", "_")[:20], "company": name,
                "meta": {"segment": "startup", "industry": "", "sic": "",
                         "exchange": "", "hq_country": ""},
            })
        logger.info(f"Loaded {len(targets):,} startups")
    elif tickers:
        name_map = {}
        if csv.exists():
            df = pd.read_csv(csv)
            name_map = {str(r["ticker"]).upper(): r.to_dict()
                       for _, r in df.iterrows() if pd.notna(r.get("ticker"))}
        targets = [{"ticker": t.upper(),
                    "company": name_map.get(t.upper(), {}).get("company", t),
                    "meta": name_map.get(t.upper(), {"segment": "public"})}
                   for t in tickers]
    elif companies:
        targets = [{"ticker": c.upper().replace(" ", "_")[:20], "company": c,
                    "meta": {"segment": "startup", "industry": "", "sic": "",
                             "exchange": "", "hq_country": ""}}
                   for c in companies]
    else:
        if not csv.exists():
            logger.error(f"Run build_company_list.py first")
            return
        df = pd.read_csv(csv)
        if limit:
            df = df.head(limit)
        targets = [{"ticker": str(r["ticker"]).upper(), "company": r.get("company", ""),
                    "meta": r.to_dict()}
                   for _, r in df.iterrows() if pd.notna(r.get("ticker"))]
        logger.info(f"Loaded {len(targets):,} public companies")

    found, not_found = 0, 0
    for t in targets:
        logger.info(f"\n── {t['ticker']} ({t['company']}) ──")
        try:
            success = fetch_one(t["ticker"], t["company"], t["meta"], cfg)
            found += success
            not_found += (not success)
        except Exception as e:
            logger.error(f"  {t['ticker']}: {e}")
            not_found += 1
        time.sleep(2)

    logger.info(f"\nDone. Found: {found} | Not found: {not_found}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch trade press mentions via DuckDuckGo HTML search"
    )
    p.add_argument("--tickers", nargs="+")
    p.add_argument("--companies", nargs="+")
    p.add_argument("--from-startups", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, companies=args.companies,
        from_startups=args.from_startups, limit=args.limit, config_path=args.config)
