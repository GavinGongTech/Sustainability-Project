"""
scripts/secondary/fetch_industry_journals.py

Searches Semantic Scholar's free, keyless public API for academic/industry
journal articles that mention the company. This is a legitimate sanctioned
API (built by the Allen Institute for AI) meant exactly for this kind of
keyword search — not a scraping gray area at all.

Does NOT require SerpAPI. Coverage will likely be low for early-stage
startups specifically (most academic papers discuss research, not a
company's product), but should catch university-spinout companies and
any startup whose technology has been benchmarked/cited in papers.

API docs: https://api.semanticscholar.org/api-docs/graph
No API key required for this basic search volume; rate limited politely
with delays between requests.

Run from project root:
    python scripts/secondary/fetch_industry_journals.py --tickers ECL TILE
    python scripts/secondary/fetch_industry_journals.py --companies "Solugen"
    python scripts/secondary/fetch_industry_journals.py --from-startups --limit 20
    python scripts/secondary/fetch_industry_journals.py --from-startups
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import (
    load_config, get_logger, build_markdown,
    source_filepath, append_to_index
)

import requests
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

logger = get_logger("fetch_industry_journals")

API_URL      = "https://api.semanticscholar.org/graph/v1/paper/search"
MAX_RESULTS  = 5
REQUEST_DELAY = 3.0  # be polite to the free public tier


def get_headers():
    return {"User-Agent": "NYU-CSB-Research/1.0 (academic research project)"}


def search_semantic_scholar(company_name: str) -> list[dict]:
    """
    Search Semantic Scholar for papers mentioning the company name.
    Validates the company name actually appears in title or abstract
    to reduce false positives from generic word matches.
    """
    params = {
        "query": company_name,
        "limit": MAX_RESULTS,
        "fields": "title,abstract,venue,year,url,authors.name",
    }
    try:
        resp = requests.get(API_URL, params=params, headers=get_headers(), timeout=20)
        if resp.status_code == 429:
            logger.warning("    Rate limited by Semantic Scholar — backing off")
            time.sleep(10)
            return []
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        logger.warning(f"    Semantic Scholar request failed: {str(e)[:60]}")
        return []

    papers = data.get("data", [])
    company_lower = company_name.lower()
    results = []

    for paper in papers:
        title    = paper.get("title", "") or ""
        abstract = paper.get("abstract", "") or ""
        # Require the company name to genuinely appear — Semantic Scholar's
        # search is fuzzy/semantic, so a plain keyword-presence check here
        # filters out tangentially-related results.
        if company_lower not in title.lower() and company_lower not in abstract.lower():
            continue
        authors = ", ".join(a.get("name", "") for a in paper.get("authors", [])[:3])
        results.append({
            "title":    title,
            "venue":    paper.get("venue", "") or "Unknown venue",
            "year":     paper.get("year", ""),
            "authors":  authors,
            "url":      paper.get("url", ""),
            "abstract": abstract[:500],
        })

    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "(No industry journal mentions found)"
    lines = ["# Industry / Academic Journal Mentions", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r['title']}")
        lines.append(f"**Venue:** {r['venue']} ({r['year']})")
        if r["authors"]:
            lines.append(f"**Authors:** {r['authors']}")
        if r["url"]:
            lines.append(f"**Link:** {r['url']}")
        if r["abstract"]:
            lines.append(f"\n{r['abstract']}")
        lines.append("")
    return "\n".join(lines)


def fetch_one(ticker: str, company_name: str, meta: dict, cfg: dict):
    logger.info(f"  Searching Semantic Scholar for '{company_name}'")
    results = search_semantic_scholar(company_name)

    quality = "clean" if results else "flagged_empty"
    text    = format_results(results)
    segment = meta.get("segment", "public")
    folder_key = ticker if segment == "public" else company_name

    out_path = source_filepath(
        company=folder_key, source_type="industry journal",
        year=date.today().year, base_dir=cfg["paths"]["normalized"],
    )
    out_path.write_text(
        build_markdown(
            text=text, company=company_name, segment=segment,
            source_type="industry journal", source_year=date.today().year,
            source_url=f"https://www.semanticscholar.org/search?q={company_name}",
            ticker=ticker, industry=meta.get("industry", ""),
            sic=str(meta.get("sic", "")), exchange=meta.get("exchange", ""),
            hq_country=meta.get("hq_country", ""), quality_flag=quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path} [{quality}] ({len(results)} journal mentions)")

    append_to_index({
        "company": company_name, "segment": segment, "ticker": ticker,
        "industry": meta.get("industry", ""), "source_type": "industry journal",
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
        time.sleep(REQUEST_DELAY)

    logger.info(f"\nDone. Found: {found} | Not found: {not_found}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch industry journal mentions via Semantic Scholar API"
    )
    p.add_argument("--tickers", nargs="+")
    p.add_argument("--companies", nargs="+")
    p.add_argument("--from-startups", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, companies=args.companies,
        from_startups=args.from_startups, limit=args.limit, config_path=args.config)
