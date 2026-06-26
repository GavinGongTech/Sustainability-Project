"""
scripts/test_pull.py

Week 2: end-to-end test pull across all three company segments.
Runs a small sample defined in config.yaml under test_companies.

What this tests:
  1. EDGAR 10-K fetch (US public)
  2. Website scrape with requests / Playwright fallback (startups)
  3. Markdown output format
  4. Master index append

Run from the project root:
    python scripts/test_pull.py

Review output in:
    data/normalized/   — one markdown file per source
    data/index/master_index.csv  — index of all fetched sources
    logs/              — fetch logs
"""

import sys
import time
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils.common import load_config, get_logger
from scripts.public_us.fetch_edgar import fetch_ticker
from scripts.utils.web_scraper import scrape_and_save

logger = get_logger("test_pull")


def run_test(config_path: str = "config/config.yaml"):
    cfg = load_config(config_path)
    test = cfg.get("test_companies", {})
    current_year = date.today().year

    # ── 1. US public companies: EDGAR 10-K ───────────────────────────────────
    logger.info("=" * 60)
    logger.info("SEGMENT 1: US Public — EDGAR 10-K pulls")
    logger.info("=" * 60)

    for company in test.get("us_public", []):
        ticker = company["ticker"]
        logger.info(f"\nFetching {ticker} ({company['name']})")
        logger.info(f"  Note: {company.get('notes', '')}")
        try:
            # Fetch the most recent year only for the test
            fetch_ticker(ticker, cfg, years=[current_year - 1])
        except Exception as e:
            logger.error(f"EDGAR fetch failed for {ticker}: {e}")
        time.sleep(2)

    # ── 2. Non-US public: note only (EDGAR 20-F fetcher is a Week 4 task) ────
    logger.info("\n" + "=" * 60)
    logger.info("SEGMENT 2: Non-US Public — placeholder (20-F fetcher Week 4)")
    logger.info("=" * 60)
    for company in test.get("non_us_public", []):
        logger.info(f"  Queued (not yet built): {company['name']} — {company['notes']}")

    # ── 3. Startups: website scrape ───────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("SEGMENT 3: Startups — website scrape")
    logger.info("=" * 60)

    startup_pages = [
        # (company_name, url, source_type)
        ("Solugen",  "https://solugen.com",             "product page"),
        ("Solugen",  "https://solugen.com/technology",  "product page"),
        ("Twelve",   "https://twelve.co",                "product page"),
        ("Twelve",   "https://twelve.co/technology",     "product page"),
    ]

    for name, url, src_type in startup_pages:
        logger.info(f"\nScraping {name}: {url}")
        try:
            path, flag = scrape_and_save(
                url=url,
                company=name,
                segment="startup",
                source_type=src_type,
                source_year=current_year,
                config=cfg,
            )
            logger.info(f"  → {path}  [{flag}]")
        except Exception as e:
            logger.error(f"  Scrape failed: {e}")
        time.sleep(cfg["rate_limits"]["general_delay_seconds"])

    # ── Summary ───────────────────────────────────────────────────────────────
    index_path = Path(cfg["paths"]["index"]) / "master_index.csv"
    if index_path.exists():
        import pandas as pd
        df = pd.read_csv(index_path)
        logger.info("\n" + "=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total sources indexed: {len(df)}")
        logger.info(f"Quality breakdown:\n{df['quality_flag'].value_counts().to_string()}")
        logger.info(f"Source types:\n{df['source_type'].value_counts().to_string()}")
        logger.info(f"\nIndex saved to: {index_path}")
    else:
        logger.warning("Master index not found — check for errors above.")

    logger.info("\nTest pull complete. Review data/normalized/ for markdown files.")


if __name__ == "__main__":
    run_test()
