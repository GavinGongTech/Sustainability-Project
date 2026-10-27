"""
scripts/patents/fetch_patents.py

Fetch patent filings for companies using SerpAPI's Google Patents endpoint.
For each company, searches Google Patents by assignee name and saves
patent titles, abstracts, and metadata as markdown files.

Requires SERPAPI_KEY environment variable:
    export SERPAPI_KEY="your_key_here"

Run from project root:
    python scripts/patents/fetch_patents.py --tickers TILE ECL
    python scripts/patents/fetch_patents.py --tickers TILE ECL --query "sustainable AI"
    python scripts/patents/fetch_patents.py  # runs all companies in public_company_list.csv
"""

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger, build_markdown, \
    source_filepath, append_to_index

import requests
import pandas as pd

logger = get_logger("fetch_patents")

SERPAPI_URL = "https://serpapi.com/search"


def get_api_key():
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise EnvironmentError(
            "SERPAPI_KEY not set. Run: export SERPAPI_KEY='your_key_here'"
        )
    return key


def search_patents(company_name, api_key, query_suffix="sustainable", num=10):
    """
    Search Google Patents for a company's patents via SerpAPI.
    Filters results to only patents where assignee matches company name,
    since Google Patents returns related patents from other assignees too.
    """
    query = f"assignee:{company_name}"
    if query_suffix:
        query += f" {query_suffix}"

    params = {
        "engine":  "google_patents",
        "q":       query,
        "api_key": api_key,
        "num":     num,
    }

    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"SerpAPI request failed: {e}")
        return []

    if data.get("search_metadata", {}).get("status") != "Success":
        logger.warning(f"SerpAPI non-success for {company_name}")
        return []

    results = data.get("organic_results", [])
    total   = data.get("search_information", {}).get("total_results", 0)

    # Filter to patents where assignee loosely matches company name.
    # Strip common suffixes (Inc, Corp, Ltd, LLC) and match on core name.
    # Handles variations: "Ecolab Inc." "Ecolab USA Inc" "Ecolab Ag" etc.
    stop_words = {"inc", "corp", "ltd", "llc", "co", "the", "group",
                  "company", "ag", "sa", "plc", "nv", "bv", "gmbh"}
    core_words = [
        w.lower().rstrip(".,") for w in company_name.split()
        if w.lower().rstrip(".,") not in stop_words and len(w) > 2
    ]

    def assignee_matches(assignee_str):
        a = (assignee_str or "").lower()
        return any(word in a for word in core_words)

    filtered = [r for r in results if assignee_matches(r.get("assignee", ""))]
    logger.info(f"  {total} total results → {len(filtered)} matched assignee")
    return filtered


def format_patent_text(patents):
    """
    Convert list of patent dicts to markdown body text.
    Field names confirmed from SerpAPI Google Patents JSON response.
    """
    lines = []
    for i, p in enumerate(patents, 1):
        title      = p.get("title", "").strip()
        pub_num    = p.get("publication_number", "")
        filing     = p.get("filing_date", "")
        priority   = p.get("priority_date", "")
        grant      = p.get("grant_date", "")
        abstract   = p.get("snippet", "").strip()
        inventor   = p.get("inventor", "")
        assignee   = p.get("assignee", "")
        link       = p.get("patent_link", p.get("pdf", ""))

        lines.append(f"## Patent {i}: {title}")
        if pub_num:  lines.append(f"**Publication number:** {pub_num}")
        if filing:   lines.append(f"**Filing date:** {filing}")
        if priority: lines.append(f"**Priority date:** {priority}")
        if grant:    lines.append(f"**Grant date:** {grant}")
        if inventor: lines.append(f"**Inventors:** {inventor}")
        if assignee: lines.append(f"**Assignee:** {assignee}")
        if link:     lines.append(f"**Link:** {link}")
        if abstract: lines.append(f"\n{abstract}")
        lines.append("")

    return "\n".join(lines)


def fetch_company_patents(ticker, company_name, cfg, api_key,
                          query_suffix="sustainable", num=10):
    """Fetch patents for one company and save as a markdown file."""
    logger.info(f"── {ticker} ({company_name}) ──────────────────────────")

    patents = search_patents(company_name, api_key,
                             query_suffix=query_suffix, num=num)

    if not patents:
        logger.warning(f"  No patents found for {company_name}")
        quality = "flagged_empty"
        text    = f"(No patents found for assignee:{company_name} {query_suffix})"
    else:
        text    = format_patent_text(patents)
        quality = "clean"
        logger.info(f"  Formatted {len(patents)} patents")

    out_path = source_filepath(
        company=ticker,
        source_type="patent",
        year=date.today().year,
        base_dir=cfg["paths"]["normalized"],
    )

    out_path.write_text(
        build_markdown(
            text         = text,
            company      = company_name,
            segment      = "public",
            source_type  = "patent",
            source_year  = date.today().year,
            source_url   = f"https://patents.google.com/?assignee={company_name}",
            ticker       = ticker,
            quality_flag = quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path}")

    append_to_index({
        "company":      company_name,
        "segment":      "public",
        "ticker":       ticker,
        "industry":     "",
        "source_type":  "patent",
        "source_year":  date.today().year,
        "source_url":   f"https://patents.google.com/?assignee={company_name}",
        "file_path":    str(out_path),
        "quality_flag": quality,
    }, index_path=f"{cfg['paths']['index']}/master_index.csv")

    return len(patents)


def run(tickers=None, query_suffix="sustainable", num=10,
        config_path="config/config.yaml"):
    cfg     = load_config(config_path)
    api_key = get_api_key()
    csv     = Path(cfg["paths"]["index"]) / "public_company_list.csv"

    if tickers:
        # Try to look up full company name from CSV if available
        if csv.exists():
            df = pd.read_csv(csv)
            ticker_to_name = {
                str(row["ticker"]).upper(): row["company"]
                for _, row in df.iterrows()
                if pd.notna(row.get("ticker")) and pd.notna(row.get("company"))
            }
        else:
            ticker_to_name = {}

        companies = {}
        for t in tickers:
            t_upper = t.upper()
            name = ticker_to_name.get(t_upper, t)
            companies[t_upper] = {"company": name, "ticker": t_upper}
    else:
        if not csv.exists():
            logger.error(f"Run build_company_list.py first — {csv} not found")
            return
        df = pd.read_csv(csv)
        companies = {
            row["ticker"].upper(): row.to_dict()
            for _, row in df.iterrows()
        }
        logger.info(f"Loaded {len(companies):,} companies from {csv}")

    total_patents = 0
    for ticker, meta in companies.items():
        company_name = meta.get("company", ticker)
        try:
            n = fetch_company_patents(
                ticker, company_name, cfg, api_key,
                query_suffix=query_suffix, num=num
            )
            total_patents += n
        except Exception as e:
            logger.error(f"{ticker}: {e}")
        time.sleep(1)  # polite pause between requests

    logger.info(f"\nDone. Total patents saved: {total_patents}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch patents via SerpAPI Google Patents")
    p.add_argument("--tickers",  nargs="+", help="Specific tickers e.g. TILE ECL")
    p.add_argument("--query",    default="sustainable",
                   help="Additional search keywords (default: 'sustainable')")
    p.add_argument("--num",      type=int, default=10,
                   help="Patents per company (default: 10, max: 10 per credit)")
    p.add_argument("--config",   default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, query_suffix=args.query,
        num=args.num, config_path=args.config)