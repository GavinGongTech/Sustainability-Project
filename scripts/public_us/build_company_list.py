"""Create the public-company master list from SEC EDGAR.

This script is the entry point for the public-company branch of the pipeline. It
looks up the SEC's master ticker list, fetches SIC metadata for each company,
and filters to the set of industry codes defined in config.yaml.

This file answers the question: "Which public companies are in scope for the
research project?" The result is a CSV used downstream by the EDGAR fetchers and
other source collectors.

Typical usage:
    python scripts/public_us/build_company_list.py
    python scripts/public_us/build_company_list.py --limit 100
    python scripts/public_us/build_company_list.py --sic 3711 3714
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger, RateLimiter

import requests
import pandas as pd
from tqdm import tqdm

logger = get_logger("build_company_list")

EDGAR_TICKERS     = "https://www.sec.gov/files/company_tickers_exchange.json"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

cd /Users/gavinyu/Desktop/Carty_Project
git fetch origin
git pull --rebase origin main
git push -u origin main
def get_headers(cfg):
    return {"User-Agent": f"{cfg['user']['name']} {cfg['user']['email']}"}


def fetch_all_companies(headers):
    """Download the SEC master filer list and keep only ticker-bearing firms.

    The SEC master file is a compact, one-shot listing of all securities filers.
    We prune out funds, non-public entities, and shell-like entries that do not
    have a ticker or are otherwise not useful for the project.
    """
    resp = requests.get(EDGAR_TICKERS, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    fields = data["fields"]

    companies = [
        {
            "cik":      str(row[fields.index("cik")]).zfill(10),
            "company":  row[fields.index("name")],
            "ticker":   row[fields.index("ticker")],
            "exchange": row[fields.index("exchange")] or "",
        }
        for row in data["data"]
        if row[fields.index("ticker")]  # keep only companies with a ticker
    ]
    logger.info(f"SEC filers: {len(data['data']):,} total → "
                f"{len(companies):,} with ticker symbols")
    return companies


def fetch_sic(cik, headers, limiter):
    """Return SIC metadata for one SEC CIK.

    This calls the EDGAR submissions endpoint for a single company and reads the
    recorded SIC and industry description. The result is later used to decide
    whether the company belongs in the project's target industries.
    """
    limiter.wait()
    try:
        resp = requests.get(
            EDGAR_SUBMISSIONS.format(cik=int(cik)),
            headers=headers, timeout=15
        )
        resp.raise_for_status()
        d = resp.json()
        return {
            "sic":        str(d.get("sic", "")),
            "industry":   d.get("sicDescription", ""),
            "hq_country": "USA" if d.get("stateOfIncorporation") else "",
        }
    except Exception as e:
        logger.warning(f"CIK {cik}: {e}")
        return None


def run(sic_override=None, limit=None, config_path="config/config.yaml"):
    """Build the target public-company CSV and write it to the index directory.

    This is the main execution function for the script. It combines all SEC
    metadata retrieval, filtering logic, and CSV output into one reproducible
    process.
    """
    cfg     = load_config(config_path)
    headers = get_headers(cfg)
    limiter = RateLimiter(cfg["rate_limits"]["sec_requests_per_second"])

    target_sics = (
        {str(s) for s in sic_override} if sic_override
        else {str(c) for codes in cfg["sic_codes"].values() for c in codes}
    )
    logger.info(f"{len(target_sics)} target SIC codes across "
                f"{len(cfg['sic_codes'])} industries")

    companies = fetch_all_companies(headers)
    time.sleep(1)

    if limit:
        companies = companies[:limit]
        logger.info(f"Test mode: capped at {limit} companies")

    matched, failed = [], 0
    for co in tqdm(companies, desc="Fetching SIC codes"):
        meta = fetch_sic(co["cik"], headers, limiter)
        if not meta:
            failed += 1
            continue
        if meta["sic"] in target_sics:
            matched.append({**co, **meta, "segment": "public", "naics": ""})

    logger.info(f"Matched: {len(matched):,} | Failed: {failed:,} | "
                f"No match: {len(companies) - len(matched) - failed:,}")

    if not matched:
        logger.warning("No companies matched — check SIC codes and exchanges.")
        return

    out = Path(cfg["paths"]["index"]) / "public_company_list.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    (pd.DataFrame(matched)
       .drop_duplicates(subset=["ticker"])
       .sort_values(["industry", "company"])
       .to_csv(out, index=False))

    df = pd.read_csv(out)
    logger.info(f"Saved {len(df):,} companies → {out}")
    logger.info(f"Top industries:\n{df['industry'].value_counts().head(15).to_string()}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Build public company list from EDGAR")
    p.add_argument("--sic",   nargs="*", type=int, help="Override SIC codes")
    p.add_argument("--limit", type=int,            help="Cap at N companies (testing)")
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(sic_override=args.sic, limit=args.limit, config_path=args.config)


# Csv structure: cik,company,ticker,exchange,sic,industry,hq_country,segment,naics
