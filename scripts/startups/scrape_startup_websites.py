"""Scrape startup product and company pages for research evidence.

This script acts as the startup equivalent of the EDGAR fetcher for public
companies. It reads the startup candidate CSV produced by the VC crawler, visits
each URL, and saves the resulting page text as normalized markdown so it can be
reviewed and classified later.

The output is intentionally conservative: weak or empty content is flagged rather
than being silently accepted as a clean source.
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger
from scripts.utils.web_scraper import scrape_and_save

import pandas as pd

logger = get_logger("scrape_startup_websites")


def run(limit=None, vc_filter=None, config_path="config/config.yaml"):
    """Run the startup scraping pass across a filtered set of candidates.

    This function reads the candidate CSV, optionally narrows to one VC, scrapes
    each URL via the shared web scraper, and tracks whether the result was clean,
    short, or failed. It is the main execution loop for the startup segment.
    """
    cfg        = load_config(config_path)
    csv_path   = Path(cfg["paths"]["index"]) / "startup_candidates.csv"
    delay      = cfg["rate_limits"]["general_delay_seconds"]

    if not csv_path.exists():
        logger.error(f"Startup list not found: {csv_path}")
        logger.error("Run crawl_vc_portfolios.py first.")
        return

    df = pd.read_csv(csv_path)

    # Filter to rows with valid URLs
    df = df[df["url"].notna() & (df["url"] != "") & (df["url"] != "nan")]

    # Filter to specific VC if requested
    if vc_filter:
        df = df[df["vc_source"].str.contains(vc_filter, case=False, na=False)]
        logger.info(f"Filtered to VC: {vc_filter} — {len(df)} startups")

    # Apply limit for testing
    if limit:
        df = df.head(limit)
        logger.info(f"Test mode: capped at {limit} startups")

    logger.info(f"Scraping {len(df):,} startup websites...")

    clean, flagged, failed = 0, 0, 0

    for i, (_, row) in enumerate(df.iterrows()):
        name = str(row.get("name", "")).strip()
        url  = str(row.get("url", "")).strip()

        if not name or not url or url.startswith("nan"):
            continue

        logger.info(f"[{i+1}/{len(df)}] {name}: {url}")

        try:
            path, quality = scrape_and_save(
                url         = url,
                company     = name,
                segment     = "startup",
                source_type = "product page",
                source_year = date.today().year,
                config      = cfg,
            )
            logger.info(f"  → {path} [{quality}]")
            if quality == "clean":
                clean += 1
            else:
                flagged += 1

        except Exception as e:
            logger.error(f"  Failed: {e}")
            failed += 1

        time.sleep(delay)

    logger.info(f"\nDone.")
    logger.info(f"  Clean:   {clean:,}")
    logger.info(f"  Flagged: {flagged:,}")
    logger.info(f"  Failed:  {failed:,}")
    logger.info(f"  Total:   {clean + flagged + failed:,}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Scrape startup websites")
    p.add_argument("--limit",  type=int, default=None,
                   help="Cap at N startups (for testing)")
    p.add_argument("--vc",     default=None,
                   help="Filter to one VC source e.g. 'Lowercarbon Capital'")
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(limit=args.limit, vc_filter=args.vc, config_path=args.config)
