"""
scripts/patents/fetch_patents_direct.py

Fetch patent filings directly from Google Patents via Playwright.
No API key required. Works for both public companies and startups.

Validates all results to reject HTML/CSS garbage that appears when
Google Patents returns an error page instead of patent data.

Run from project root:
    python scripts/patents/fetch_patents_direct.py                     # all public companies
    python scripts/patents/fetch_patents_direct.py --from-startups     # all startups
    python scripts/patents/fetch_patents_direct.py --tickers ECL TILE  # specific tickers
    python scripts/patents/fetch_patents_direct.py --companies "Solugen" "Twelve"
"""

import re
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
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

logger = get_logger("fetch_patents_direct")

# ── Validation patterns ────────────────────────────────────────────────────────
# These patterns indicate HTML/CSS garbage returned when Google Patents
# returns an error page instead of real patent data.
BAD_PATTERNS = re.compile(
    r'(^(<|!doctype|html|head|body|meta|script|style|div))'
    r'|minimum-scale|initial-scale|margin:0|padding:0'
    r'|font-size|viewport|@charset|\{font:|body\{',
    re.IGNORECASE
)

# Real patent IDs look like US1234567B2, EP1234567, WO2024123456
PATENT_ID_RE = re.compile(
    r'^(US|EP|WO|CN|JP|KR|AU|CA|DE|FR|GB)\d{6,12}[A-Z]?\d*$',
    re.IGNORECASE
)


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }


def clean_assignee(company_name: str) -> str:
    """
    Strip legal suffixes and taglines for better patent search matching.
    Also strips anything after common tagline separators.
    """
    # Strip apostrophes and special chars that break patent search
    company_name = company_name.replace("'", "").replace('’', "")

    # Strip EDGAR state incorporation markers e.g. /DE/ /NY/ /CA/
    import re as _re
    company_name = _re.sub(r"\s*/[A-Z]{2}/?\s*$", "", company_name).strip()

    # Strip taglines first
    for sep in [" / ", " — ", " - ", " | ", ": "]:
        if sep in company_name:
            company_name = company_name.split(sep)[0].strip()

    suffixes = [
        # Legal entity suffixes only
        " inc", " inc.", " corp", " corp.", " ltd", " ltd.",
        " llc", " llc.", " co.", " co,", " co",
        " ag", " sa", " s.a.", " plc", " nv", " n.v.",
        " bv", " b.v.", " gmbh", " se", " oy", " oyj",
        " ab", " srl", " spa", " s.p.a.", " kk", " k.k.",
        " holdings", " holding",
    ]
    name = company_name.lower().strip()
    changed = True
    while changed:
        changed = False
        for s in suffixes:
            if name.endswith(s):
                name = name[:-len(s)].strip().rstrip(",.")
                changed = True
    return name.title()


def is_valid_patent_id(patent_id: str) -> bool:
    """
    Check if a string looks like a real patent number.
    Rejects HTML tags, CSS, viewport metadata, and other garbage.
    """
    if not patent_id or len(patent_id) > 30:
        return False
    if BAD_PATTERNS.search(patent_id):
        return False
    if not any(c.isdigit() for c in patent_id):
        return False
    # Must match known patent number format
    return bool(PATENT_ID_RE.match(patent_id.strip()))


def is_valid_title(title: str) -> bool:
    """
    Check if a string looks like a real patent title.
    Rejects HTML/CSS garbage.
    """
    if not title or len(title) < 10:
        return False
    if BAD_PATTERNS.search(title):
        return False
    # Real titles don't contain CSS/HTML syntax characters
    if any(c in title for c in ['{', '}', '<', '>', ';', '=']):
        return False
    # Real titles have actual words
    words = title.split()
    if len(words) < 2:
        return False
    return True


def search_google_patents(company_name: str, max_results: int = 10) -> list[dict]:
    """Search Google Patents for a company and validate the raw results.

    This function is intentionally strict about patent format validation. It
    rejects HTML/CSS junk pages and only keeps IDs that look like real patent
    numbers so that empty or broken searches do not contaminate the data.
    """
    clean_name = clean_assignee(company_name)

    # Skip obviously bad company names (taglines, descriptions)
    if len(clean_name) > 60 or len(clean_name.split()) > 8:
        logger.warning(f"  Skipping — name too long/tagline: '{clean_name[:50]}'")
        return []

    logger.info(f"  Searching Google Patents for '{clean_name}'")
    try:
        from playwright.sync_api import sync_playwright

        search_url = f"https://patents.google.com/?assignee={clean_name.replace(' ', '+')}&q=sustainable"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(5)

            # Use the XHR download API
            api_path = f"/xhr/query?url=q%3D(sustainable)%26assignee%3D{clean_name.replace(' ', '%2B')}&exp=&download=true"
            # Escape single quotes to prevent JS syntax errors
            safe_api_path = api_path.replace("'", "%27")
            result = page.evaluate(f"""
                async () => {{
                    const resp = await fetch('https://patents.google.com{safe_api_path}');
                    const text = await resp.text();
                    return text;
                }}
            """)
            html = page.content()

            # If no results, retry with first word only (still inside browser context)
            if not result or len(result) < 100 or result.strip().startswith('<'):
                first_word = clean_name.split()[0] if clean_name.split() else clean_name
                if first_word != clean_name:
                    logger.info(f"  Retrying inside browser with first word: '{first_word}'")
                    api_path2 = f"/xhr/query?url=q%3D(sustainable)%26assignee%3D{first_word.replace(' ', '%2B')}&exp=&download=true"
                    result = page.evaluate(f"""
                        async () => {{
                            const resp = await fetch('https://patents.google.com{api_path2}');
                            const text = await resp.text();
                            return text;
                        }}
                    """)

            browser.close()

        patents = []
        seen    = set()

        # Parse CSV result — validate every field strictly
        if result and len(result) > 100 and not result.strip().startswith('<'):
            lines = result.strip().split("\n")
            logger.info(f"  API returned {len(lines)} lines")
            for line in lines[1:max_results+1]:
                parts     = line.split(",")
                patent_id = parts[0].strip().strip('"') if parts else ""
                title     = parts[1].strip().strip('"') if len(parts) > 1 else ""

                # Strict validation — reject anything that looks like HTML/CSS
                if not is_valid_patent_id(patent_id):
                    logger.debug(f"  Rejected invalid patent ID: {patent_id[:40]}")
                    continue
                if not is_valid_title(title):
                    logger.debug(f"  Rejected invalid title: {title[:40]}")
                    continue
                if patent_id in seen:
                    continue

                seen.add(patent_id)
                patents.append({
                    "patent_number": patent_id,
                    "title":         title,
                    "abstract":      "",
                    "filing_date":   parts[5].strip().strip('"') if len(parts) > 5 else "",
                    "grant_date":    parts[6].strip().strip('"') if len(parts) > 6 else "",
                    "inventors":     parts[8].strip().strip('"') if len(parts) > 8 else "",
                    "link":          f"https://patents.google.com/patent/{patent_id}/en",
                    "source":        "Google Patents",
                })

        # Fallback: extract patent numbers from PDF image URLs
        if not patents:
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "patentimages.storage.googleapis.com" in href:
                    match = re.search(
                        r'/(US|EP|WO|CN|JP)(\d+[A-Z]?\d*)\.(pdf|png)', href
                    )
                    if match:
                        patent_num = f"{match.group(1)}{match.group(2)}"
                        if patent_num not in seen and is_valid_patent_id(patent_num):
                            seen.add(patent_num)
                            patents.append({
                                "patent_number": patent_num,
                                "title":         "",
                                "abstract":      "",
                                "filing_date":   "",
                                "grant_date":    "",
                                "inventors":     "",
                                "link":          f"https://patents.google.com/patent/{patent_num}/en",
                                "source":        "Google Patents (image URL)",
                            })
                            if len(patents) >= max_results:
                                break

        logger.info(f"  Found {len(patents)} valid patents")
        return patents

    except Exception as e:
        logger.error(f"  Google Patents search failed: {e}")
        return []


def search_uspto_patft(company_name: str, max_results: int = 10) -> list[dict]:
    """Search USPTO PatFT as a structured patent source before fallback search.

    The USPTO endpoint often returns cleaner and more straightforward patent
    records than Google Patents, so the script tries it first and only falls back
    when it finds nothing or the endpoint is unavailable.
    """
    assignee = clean_assignee(company_name)
    logger.info(f"  Trying USPTO PatFT for: '{assignee}'")

    params = {
        "Sect1": "PTO2", "Sect2": "HITOFF", "p": "1",
        "u": "/netahtml/PTO/search-adv.htm", "r": "0",
        "f": "S", "l": str(max_results),
        "TERM1": assignee, "FIELD1": "ASNM",
        "co1": "AND", "TERM2": "", "FIELD2": "",
    }
    try:
        resp = requests.get(
            "https://patft.uspto.gov/netacgi/nph-Parser",
            params=params, headers=get_headers(), timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        if "No patents were found" in soup.get_text():
            return []

        patents = []
        for row in soup.find_all("tr"):
            link = row.find("a", href=True)
            if not link:
                continue
            num = link.get_text(strip=True)
            if not num or not any(c.isdigit() for c in num):
                continue
            patent_url = f"https://patft.uspto.gov{link['href']}"
            try:
                d_resp = requests.get(patent_url, headers=get_headers(), timeout=10)
                d_soup = BeautifulSoup(d_resp.text, "lxml")
                lines  = [l.strip() for l in d_soup.get_text("\n").splitlines()
                          if l.strip()]
                title  = next(
                    (l for l in lines[:30]
                     if len(l) > 10 and not l.startswith("United States")
                     and is_valid_title(l)),
                    ""
                )
                if title:
                    patents.append({
                        "patent_number": num,
                        "title":         title,
                        "abstract":      "",
                        "filing_date":   "",
                        "grant_date":    "",
                        "inventors":     "",
                        "link":          patent_url,
                        "source":        "USPTO PatFT",
                    })
                if len(patents) >= max_results:
                    break
            except Exception:
                continue
            time.sleep(0.5)

        logger.info(f"  Found {len(patents)} USPTO patents")
        return patents

    except Exception as e:
        logger.warning(f"  USPTO unavailable: {e}")
        return []


def format_patents(patents: list[dict]) -> str:
    """Format validated patent list as markdown body text."""
    if not patents:
        return "(No patents found)"
    lines = []
    for i, p in enumerate(patents, 1):
        lines.append(f"## Patent {i}: {p.get('title', 'Untitled')}")
        if p.get("patent_number"): lines.append(f"**Patent number:** {p['patent_number']}")
        if p.get("filing_date"):   lines.append(f"**Filing date:** {p['filing_date']}")
        if p.get("grant_date"):    lines.append(f"**Grant date:** {p['grant_date']}")
        if p.get("inventors"):     lines.append(f"**Inventors:** {p['inventors']}")
        if p.get("link"):          lines.append(f"**Link:** {p['link']}")
        if p.get("source"):        lines.append(f"**Source:** {p['source']}")
        if p.get("abstract"):      lines.append(f"\n{p['abstract']}")
        lines.append("")
    return "\n".join(lines)


def fetch_company_patents(ticker: str, company_name: str, meta: dict,
                          cfg: dict, max_results: int = 10):
    """Fetch and save patent evidence for one company or startup.

    This is the central single-company execution helper used by `run()`. It tries
    the structured USPTO path first and then falls back to Google Patents, then
    writes the result to the normalized markdown directory and appends the record
    to the master index.
    """
    logger.info(f"── {ticker} ({company_name}) ──────────────────────────")

    # Try USPTO first (more structured), fall back to Google Patents
    patents = search_uspto_patft(company_name, max_results)
    if not patents:
        patents = search_google_patents(company_name, max_results)

    quality    = "clean" if patents else "flagged_empty"
    text       = format_patents(patents)
    segment    = meta.get("segment", "public")
    folder_key = ticker if segment == "public" else company_name

    out_path = source_filepath(
        company     = folder_key,
        source_type = "patent",
        year        = date.today().year,
        base_dir    = cfg["paths"]["normalized"],
    )
    out_path.write_text(
        build_markdown(
            text         = text,
            company      = company_name,
            segment      = segment,
            source_type  = "patent",
            source_year  = date.today().year,
            source_url   = f"https://patents.google.com/?assignee={company_name}",
            ticker       = ticker,
            industry     = meta.get("industry", ""),
            sic          = str(meta.get("sic", "")),
            exchange     = meta.get("exchange", ""),
            hq_country   = meta.get("hq_country", ""),
            quality_flag = quality,
        ),
        encoding="utf-8"
    )
    logger.info(f"  Saved: {out_path} [{quality}] ({len(patents)} patents)")

    append_to_index({
        "company":      company_name,
        "segment":      segment,
        "ticker":       ticker,
        "industry":     meta.get("industry", ""),
        "source_type":  "patent",
        "source_year":  date.today().year,
        "source_url":   f"https://patents.google.com/?assignee={company_name}",
        "file_path":    str(out_path),
        "quality_flag": quality,
    }, index_path=f"{cfg['paths']['index']}/master_index.csv")

    return len(patents)


def run(tickers=None, companies=None, from_startups=False,
        max_results=10, limit=None, config_path="config/config.yaml"):
    """Execute the patent collection pass for a target list of companies.

    The function selects the target set from either the public-company CSV,
    startup candidates, or explicit CLI arguments, and then fetches patent data
    for each one. It is the main driver for the patent module.
    """
    cfg         = load_config(config_path)
    csv         = Path(cfg["paths"]["index"]) / "public_company_list.csv"
    startup_csv = Path(cfg["paths"]["index"]) / "startup_candidates.csv"

    if from_startups:
        if not startup_csv.exists():
            logger.error(f"Run crawl_vc_portfolios.py first — {startup_csv} not found")
            return
        df = pd.read_csv(startup_csv)
        targets = {}
        for _, r in df.iterrows():
            name = str(r.get("name", "")).strip()
            if not name or name == "nan" or len(name) > 60:
                continue
            key = name.upper().replace(" ", "_")[:20]
            targets[key] = {
                "company":    name,
                "ticker":     key,
                "segment":    "startup",
                "industry":   "",
                "sic":        "",
                "exchange":   "",
                "hq_country": "",
            }
        logger.info(f"Loaded {len(targets):,} startups from {startup_csv}")

    elif tickers:
        name_map = {}
        if csv.exists():
            df = pd.read_csv(csv)
            name_map = {
                str(r["ticker"]).upper(): r.to_dict()
                for _, r in df.iterrows()
                if pd.notna(r.get("ticker"))
            }
        targets = {
            t.upper(): name_map.get(t.upper(), {
                "company": t, "ticker": t.upper(), "segment": "public"
            })
            for t in tickers
        }

    elif companies:
        targets = {
            c.upper().replace(" ", "_"): {
                "company": c, "ticker": c, "segment": "startup",
            }
            for c in companies
        }

    else:
        if not csv.exists():
            logger.error(f"Run build_company_list.py first — {csv} not found")
            return
        df = pd.read_csv(csv)
        targets = {
            str(r["ticker"]).upper(): r.to_dict()
            for _, r in df.iterrows()
            if pd.notna(r.get("ticker"))
        }
        logger.info(f"Loaded {len(targets):,} public companies from {csv}")
    if limit:
        targets = dict(list(targets.items())[:limit])
        logger.info(f"Limited to {len(targets)} companies for testing")

    total = 0
    for ticker, meta in targets.items():
        try:
            total += fetch_company_patents(
                ticker, meta.get("company", ticker), meta, cfg, max_results
            )
        except Exception as e:
            logger.error(f"{ticker}: {e}")
        time.sleep(2)

    logger.info(f"\nDone. Total valid patents found: {total}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch patents from Google Patents/USPTO with validation"
    )
    p.add_argument("--tickers",       nargs="+",
                   help="Public company tickers e.g. ECL TILE")
    p.add_argument("--companies",     nargs="+",
                   help="Company names e.g. 'Solugen' 'Twelve'")
    p.add_argument("--from-startups", action="store_true",
                   help="Run across all startups in startup_candidates.csv")
    p.add_argument("--max",    type=int, default=10)
    p.add_argument("--limit",  type=int, default=None, help="Limit to N companies for testing")
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, companies=args.companies,
        from_startups=args.from_startups,
        max_results=args.max, limit=args.limit, config_path=args.config)