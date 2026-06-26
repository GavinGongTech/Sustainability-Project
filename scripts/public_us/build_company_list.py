"""
scripts/public_us/build_company_list.py

Week 3: Build the public company list filtered by SIC code and exchange.

Approach:
  EDGAR publishes two complete JSON files updated daily:
    - company_tickers_exchange.json: every SEC filer with ticker + exchange
    - submissions/CIK{cik}.json: per-company metadata including SIC code

  Strategy:
    1. Download company_tickers_exchange.json — one request, all tickers
    2. Filter to NYSE/NASDAQ/AMEX listed companies only
    3. For each company, fetch their submissions JSON to get SIC code
    4. Filter to companies whose SIC code is in our target list
    5. Save to public_company_list.csv

  This approach makes two categories of EDGAR requests:
    - One request for the master ticker file
    - One request per company to get their SIC code
  Much more reliable than the atom feed which has inconsistent XML tags.

Outputs: data/index/public_company_list.csv
  Columns: company, ticker, cik, sic, industry, exchange, hq_country,
           segment, naics

Run from project root:
    python scripts/public_us/build_company_list.py
    python scripts/public_us/build_company_list.py --sic 3711 3714
    python scripts/public_us/build_company_list.py --limit 50
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

# EDGAR's complete ticker-to-exchange mapping — updated daily, one request
EDGAR_TICKERS_EXCHANGE = "https://www.sec.gov/files/company_tickers_exchange.json"
# Per-company metadata including SIC code
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def get_headers(config: dict) -> dict:
    return {"User-Agent": f"{config['user']['name']} {config['user']['email']}"}


def fetch_exchange_listed_companies(headers: dict,
                                    target_exchanges: list[str]) -> list[dict]:
    """
    Download EDGAR's complete company_tickers_exchange.json and filter
    to companies listed on target exchanges (NYSE, NASDAQ, AMEX).

    Returns list of dicts with: cik, name, ticker, exchange
    This is one HTTP request for potentially thousands of companies.
    """
    logger.info("Downloading EDGAR master ticker/exchange file...")
    resp = requests.get(EDGAR_TICKERS_EXCHANGE, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Format: {"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}
    fields = data.get("fields", [])
    rows   = data.get("data", [])

    logger.info(f"Total SEC filers in master file: {len(rows):,}")

    # Build list of dicts
    target_upper = [e.upper() for e in target_exchanges]
    companies = []
    for row in rows:
        entry = dict(zip(fields, row))
        exchange = (entry.get("exchange") or "").upper()
        if exchange in target_upper:
            companies.append({
                "cik":      str(entry.get("cik", "")).zfill(10),
                "company":  entry.get("name", ""),
                "ticker":   entry.get("ticker", ""),
                "exchange": entry.get("exchange", ""),
            })

    logger.info(f"Companies on {target_exchanges}: {len(companies):,}")
    return companies


def fetch_sic_for_company(cik: str, headers: dict,
                           limiter: RateLimiter) -> dict | None:
    """
    Fetch the submissions JSON for one company to get their SIC code,
    SIC description, and state of incorporation.
    Returns None on failure.
    """
    url = EDGAR_SUBMISSIONS.format(cik=int(cik))
    limiter.wait()
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {
            "sic":      data.get("sic", ""),
            "industry": data.get("sicDescription", ""),
            "hq_country": "USA" if data.get("stateOfIncorporation") else "",
        }
    except Exception as e:
        logger.warning(f"  CIK {cik}: submissions fetch failed: {e}")
        return None


def run(sic_override: list[int] | None = None,
        limit: int | None = None,
        config_path: str = "config/config.yaml"):

    cfg = load_config(config_path)
    headers = get_headers(cfg)
    limiter = RateLimiter(cfg["rate_limits"]["sec_requests_per_second"])
    target_exchanges = cfg["exchanges"]

    # Build set of target SIC codes
    if sic_override:
        target_sics = set(str(s) for s in sic_override)
    else:
        target_sics = set()
        for codes in cfg["sic_codes"].values():
            for code in codes:
                target_sics.add(str(code))

    logger.info(f"Target SIC codes: {len(target_sics)} codes across "
                f"{len(cfg['sic_codes'])} industries")
    logger.info(f"Target exchanges: {target_exchanges}")

    # Step 1: get all exchange-listed companies in one request
    all_listed = fetch_exchange_listed_companies(headers, target_exchanges)
    time.sleep(1)

    # Step 2: apply limit if testing
    if limit:
        all_listed = all_listed[:limit]
        logger.info(f"Limiting to first {limit} companies for testing")

    # Step 3: fetch SIC for each company and filter to target SICs
    logger.info(f"\nFetching SIC codes for {len(all_listed):,} companies...")
    logger.info("This may take a while — one EDGAR request per company.\n")

    matched = []
    failed  = 0

    for company in tqdm(all_listed, desc="Fetching SIC codes"):
        meta = fetch_sic_for_company(company["cik"], headers, limiter)
        if not meta:
            failed += 1
            continue

        sic = str(meta.get("sic", ""))
        if sic in target_sics:
            matched.append({
                **company,
                "sic":        sic,
                "industry":   meta["industry"],
                "hq_country": meta["hq_country"],
                "segment":    "public",
                "naics":      "",
            })

    logger.info(f"\nResults:")
    logger.info(f"  Matched target SIC codes: {len(matched):,}")
    logger.info(f"  Failed to fetch:          {failed:,}")
    logger.info(f"  No SIC match:             "
                f"{len(all_listed) - len(matched) - failed:,}")

    if not matched:
        logger.warning("No companies matched — check SIC codes and exchange filters.")
        return

    df = pd.DataFrame(matched)
    df = df.drop_duplicates(subset=["ticker"])
    df = df.sort_values(["industry", "company"])

    out_path = Path(cfg["paths"]["index"]) / "public_company_list.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    logger.info(f"\nSaved {len(df):,} companies to {out_path}")
    logger.info(f"\nTop industries by company count:")
    logger.info(df["industry"].value_counts().head(20).to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build public company list from EDGAR"
    )
    parser.add_argument(
        "--sic", nargs="*", type=int, default=None,
        help="Override SIC codes, e.g. --sic 3711 3714 2800"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit to first N exchange-listed companies (for testing)"
    )
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    run(sic_override=args.sic, limit=args.limit, config_path=args.config)
