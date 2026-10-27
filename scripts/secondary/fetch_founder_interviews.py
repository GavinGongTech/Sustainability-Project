"""
scripts/secondary/fetch_founder_interviews.py

Searches YouTube's own public search page directly for founder/executive
interviews, talks, and podcast appearances. YouTube's search results page
has much lighter bot detection than Google's main search, and is a strong
direct source for this category since most founder interviews (podcasts,
conference talks, news segments) end up uploaded there.

Does NOT require SerpAPI — this is YouTube's own site search, same
direct-scraping technique used throughout this project.

Run from project root:
    python scripts/secondary/fetch_founder_interviews.py --tickers ECL TILE
    python scripts/secondary/fetch_founder_interviews.py --companies "Solugen"
    python scripts/secondary/fetch_founder_interviews.py --from-startups --limit 20
    python scripts/secondary/fetch_founder_interviews.py --from-startups
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import date
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import (
    load_config, get_logger, build_markdown,
    source_filepath, append_to_index
)

import pandas as pd
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

logger = get_logger("fetch_founder_interviews")

SEARCH_QUERIES = [
    "{company} founder interview",
    "{company} CEO interview",
]

MIN_TITLE_LEN = 10
MAX_RESULTS   = 5


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }


def search_youtube(company_name: str) -> list[dict]:
    """
    Search YouTube's own results page for founder/CEO interviews.
    Returns [] if nothing plausible found.
    """
    from playwright.sync_api import sync_playwright

    company_first_word = company_name.lower().split()[0] if company_name.split() else ""
    if len(company_first_word) < 5:
        return []  # too generic a word to safely match against video titles

    results = []
    seen_urls = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = browser.new_context(
                user_agent=get_headers()["User-Agent"],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()

            for template in SEARCH_QUERIES:
                query = template.format(company=company_name)
                url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(3)  # let YouTube's JS render results
                    html = page.content()
                except Exception:
                    continue

                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("/watch?v="):
                        continue
                    title = a.get("title") or a.get_text(strip=True)
                    if not title or len(title) < MIN_TITLE_LEN:
                        continue
                    if company_first_word not in title.lower():
                        continue
                    full_url = "https://www.youtube.com" + href
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    results.append({"title": title[:200], "url": full_url,
                                    "query": query})
                    if len(results) >= MAX_RESULTS:
                        break
                if len(results) >= MAX_RESULTS:
                    break

            browser.close()

    except Exception as e:
        logger.warning(f"    YouTube search failed: {str(e)[:60]}")

    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "(No founder interviews found)"
    lines = ["# Founder / Executive Interviews (YouTube)", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r['title']}")
        lines.append(f"**Link:** {r['url']}")
        lines.append(f"**Found via query:** {r['query']}")
        lines.append("")
    return "\n".join(lines)


def fetch_one(ticker: str, company_name: str, meta: dict, cfg: dict):
    logger.info(f"  Searching YouTube for '{company_name}'")
    results = search_youtube(company_name)

    quality = "clean" if results else "flagged_empty"
    text    = format_results(results)
    segment = meta.get("segment", "public")
    folder_key = ticker if segment == "public" else company_name

    out_path = source_filepath(
        company=folder_key, source_type="founder interview",
        year=date.today().year, base_dir=cfg["paths"]["normalized"],
    )
    out_path.write_text(
        build_markdown(
            text=text, company=company_name, segment=segment,
            source_type="founder interview", source_year=date.today().year,
            source_url="https://www.youtube.com/results?search_query=" +
                       quote_plus(f"{company_name} founder interview"),
            ticker=ticker, industry=meta.get("industry", ""),
            sic=str(meta.get("sic", "")), exchange=meta.get("exchange", ""),
            hq_country=meta.get("hq_country", ""), quality_flag=quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path} [{quality}] ({len(results)} interviews)")

    append_to_index({
        "company": company_name, "segment": segment, "ticker": ticker,
        "industry": meta.get("industry", ""), "source_type": "founder interview",
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
    p = argparse.ArgumentParser(description="Fetch founder interviews via YouTube search")
    p.add_argument("--tickers", nargs="+")
    p.add_argument("--companies", nargs="+")
    p.add_argument("--from-startups", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, companies=args.companies,
        from_startups=args.from_startups, limit=args.limit, config_path=args.config)
