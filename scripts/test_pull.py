"""Smoke-test the pipeline on a small sample of companies.

This script exists to validate the overall research workflow early in the
project before a full sweep is run. It exercises the public-company and startup
branches with a handful of known sample companies and confirms that the project
writes normalized markdown files and updates the master index as expected.
"""

import sys
import time
import pandas as pd
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils.common import load_config, get_logger
from scripts.public_us.fetch_edgar import fetch_ticker
from scripts.utils.web_scraper import scrape_and_save

logger = get_logger("test_pull")


def section(title: str):
    """Log a section header."""
    logger.info("\n" + "=" * 60)
    logger.info(title)
    logger.info("=" * 60)


def run_test(config_path: str = "config/config.yaml"):
    """Execute the end-to-end validation pass for the sample companies.

    This is the project's sanity-check entry point: it pulls a few public-company
    filings and sample startup pages, logs the outcome, and verifies that the
    output structure is valid before a full data collection run begins.
    """
    cfg  = load_config(config_path)
    test = cfg.get("test_companies", {})
    year = date.today().year

    # ── Segment 1: US public companies — EDGAR 10-K ──────────────────────────
    section("SEGMENT 1: US Public — EDGAR 10-K pulls")
    for company in test.get("us_public", []):
        ticker = company["ticker"]
        logger.info(f"\n{ticker} ({company['name']}): {company.get('notes', '')}")
        try:
            fetch_ticker(ticker, cfg, years=[year - 1])
        except Exception as e:
            logger.error(f"  Failed: {e}")
        time.sleep(2)

    # ── Segment 2: Non-US public — placeholder until Week 4 ──────────────────
    section("SEGMENT 2: Non-US Public — queued for Week 4")
    for company in test.get("non_us_public", []):
        logger.info(f"  {company['name']} — {company.get('notes', '')}")

    # ── Segment 3: Startups — website scrape ─────────────────────────────────
    section("SEGMENT 3: Startups — website scrape")
    delay = cfg["rate_limits"]["general_delay_seconds"]
    for company in test.get("startups", []):
        name = company["name"]
        url  = company["url"]
        logger.info(f"\n{name}: {url}")
        try:
            path, flag = scrape_and_save(
                url=url,
                company=name,
                segment="startup",
                source_type="product page",
                source_year=year,
                config=cfg,
            )
            logger.info(f"  → {path}  [{flag}]")
        except Exception as e:
            logger.error(f"  Failed: {e}")
        time.sleep(delay)

    # ── Summary ───────────────────────────────────────────────────────────────
    index_path = Path(cfg["paths"]["index"]) / "master_index.csv"
    if not index_path.exists():
        logger.warning("Master index not found — check for errors above.")
        return

    df = pd.read_csv(index_path)
    section("SUMMARY")
    logger.info(f"Total sources indexed : {len(df)}")
    logger.info(f"Quality breakdown     :\n{df['quality_flag'].value_counts().to_string()}")
    logger.info(f"Source types          :\n{df['source_type'].value_counts().to_string()}")
    logger.info(f"\nIndex: {index_path}")
    logger.info("Done. Review data/normalized/ for markdown files.")


if __name__ == "__main__":
    run_test()
