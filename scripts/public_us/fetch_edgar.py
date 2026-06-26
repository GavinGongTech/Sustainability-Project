"""
scripts/public_us/fetch_edgar.py

Pulls 10-K filings from SEC EDGAR for a list of tickers.
Extracts Item 1 (Business) and Item 7 (MD&A) text.
Saves one normalized markdown file per item per company.

Usage:
    python scripts/public_us/fetch_edgar.py --tickers TILE ECL ENOV
    python scripts/public_us/fetch_edgar.py --tickers TILE --years 2023 2024
"""

import argparse
import re
import sys
import time
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import (
    load_config, get_logger, build_markdown,
    source_filepath, append_to_index
)

import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = get_logger("fetch_edgar")


# ── EDGAR helpers ─────────────────────────────────────────────────────────────

EDGAR_BASE = "https://data.sec.gov"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def get_headers(config: dict) -> dict:
    """SEC requires a User-Agent with name and email."""
    name = config["user"]["name"]
    email = config["user"]["email"]
    return {"User-Agent": f"{name} {email}"}


def ticker_to_cik(ticker: str, headers: dict) -> str | None:
    """Look up CIK for a ticker using the EDGAR company search."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2020-01-01&forms=10-K"
    # Simpler: use the tickers.json map
    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(tickers_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    return None


def get_10k_filing_urls(cik: str, headers: dict,
                         years: list[int] | None = None) -> list[dict]:
    """
    Return a list of dicts with filing metadata for 10-K filings.
    Each dict has: accession_number, filing_date, documents_url
    """
    url = SUBMISSIONS_URL.format(cik=int(cik))
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    filings = data.get("filings", {}).get("recent", {})
    forms      = filings.get("form", [])
    dates      = filings.get("filingDate", [])
    accessions = filings.get("accessionNumber", [])

    results = []
    for form, date_str, accession in zip(forms, dates, accessions):
        if form not in ("10-K", "10-K/A"):
            continue
        year = int(date_str[:4])
        if years and year not in years:
            continue
        acc_clean = accession.replace("-", "")
        docs_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc_clean}/{accession}-index.htm"
        )
        results.append({
            "accession": accession,
            "filing_date": date_str,
            "fiscal_year": year,
            "documents_url": docs_url,
            "cik": cik,
        })
    return results


def get_10k_document_url(index_url: str, headers: dict) -> str | None:
    """
    Parse the EDGAR filing index page to find the main 10-K document URL.
    EDGAR index pages have columns: Seq | Description | Document | Type | Size
    We look for rows where Type == 10-K and grab the Document link.
    Falls back to scanning all links for .htm files if table parse fails.
    """
    resp = requests.get(index_url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Strategy 1: find the documents table by header row
    for table in soup.find_all("table"):
        headers_row = table.find("tr")
        if not headers_row:
            continue
        col_headers = [th.get_text(strip=True).lower()
                       for th in headers_row.find_all(["th", "td"])]

        # Identify which column index holds "type" and "document"
        try:
            type_idx = next(i for i, h in enumerate(col_headers) if "type" in h)
            doc_idx  = next(i for i, h in enumerate(col_headers)
                            if "document" in h or "filename" in h)
        except StopIteration:
            continue

        for row in table.find_all("tr")[1:]:  # skip header row
            cells = row.find_all("td")
            if len(cells) <= max(type_idx, doc_idx):
                continue
            doc_type = cells[type_idx].get_text(strip=True)
            if doc_type in ("10-K", "10-K/A"):
                link = cells[doc_idx].find("a")
                if link:
                    href = link.get("href", "")
                    url = f"https://www.sec.gov{href}" if href.startswith("/") else href
                    # Strip XBRL inline viewer wrapper — ix?doc= returns JS shell not text
                    return url.replace("https://www.sec.gov/ix?doc=", "https://www.sec.gov")

    # Strategy 2: fallback — scan all links, prefer .htm that isn't the index itself
    logger.warning(f"Table parse failed for {index_url}, falling back to link scan")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if href.endswith((".htm", ".html")) and "index" not in href.lower():
            if any(kw in text for kw in ["10-k", "annual", "form"]):
                url = f"https://www.sec.gov{href}" if href.startswith("/") else href
                return url.replace("https://www.sec.gov/ix?doc=", "https://www.sec.gov")

    return None


def _is_toc_match(full_text: str, match_start: int, window: int = 300) -> bool:
    """
    Detect whether a regex match landed on a table of contents entry
    rather than the real section body.

    TOC entries are characterized by:
    - Very little text before the next Item header (the line is just a reference)
    - A page number (1-3 digits) appearing very shortly after the match
    - The chunk between this Item and the next Item being very short (<300 chars)
    """
    snippet = full_text[match_start: match_start + window]
    lines = [l.strip() for l in snippet.splitlines() if l.strip()]

    # If the snippet has fewer than 4 non-empty lines it's almost certainly a TOC
    if len(lines) < 4:
        return True

    # If a bare page number appears within the first 3 lines, it's a TOC entry
    for line in lines[:3]:
        if re.fullmatch(r"\d{1,3}", line):
            return True

    return False


def extract_item_text(html: str, item_num: str, next_item_num: str) -> str:
    """
    Extract the text of a numbered Item from a 10-K HTML document.

    item_num:      e.g. "1" or "7"
    next_item_num: e.g. "1A" or "7A"

    Uses three strategies in order:
      1. Find <a> anchor tags with id/name matching the item (most reliable)
      2. Find heading tags (h1-h4) whose text matches the item header
      3. Regex on the full plain text, skipping TOC matches (fallback)

    Key fix: 10-Ks always have TWO occurrences of each Item header —
    once in the table of contents (just a line + page number) and once
    at the start of the real section. All three strategies now detect
    and skip TOC matches before extracting content.
    """
    soup = BeautifulSoup(html, "lxml")

    # ── Strategy 1: anchor-based (most modern 10-Ks use named anchors) ────────
    anchor_pats = [
        re.compile(rf"^item\s*{re.escape(item_num)}\b", re.IGNORECASE),
        re.compile(rf"^item{re.escape(item_num)}[_\-]", re.IGNORECASE),
    ]
    next_pats = [
        re.compile(rf"^item\s*{re.escape(next_item_num)}\b", re.IGNORECASE),
        re.compile(rf"^item{re.escape(next_item_num)}[_\-]", re.IGNORECASE),
    ]

    def find_anchors(patterns):
        """Return ALL matching anchor tags, not just the first."""
        found = []
        for pat in patterns:
            for tag in soup.find_all(["a", "div", "span", "p"],
                                     id=lambda x: x and pat.match(x)):
                if tag not in found:
                    found.append(tag)
            for tag in soup.find_all("a", attrs={"name": True}):
                if any(pat.match(tag["name"]) for pat in patterns):
                    if tag not in found:
                        found.append(tag)
        return found

    start_tags = find_anchors(anchor_pats)
    end_tags   = find_anchors(next_pats)

    # Use the LAST start anchor before the first end anchor —
    # this skips the TOC anchor and lands on the real section anchor
    end_tag = end_tags[0] if end_tags else None

    for start_tag in reversed(start_tags):
        collecting = False
        chunks = []
        for tag in soup.find_all(True):
            if tag == start_tag:
                collecting = True
            if collecting:
                if end_tag and tag == end_tag:
                    break
                if tag.string:
                    chunks.append(tag.string.strip())
        text = "\n".join(c for c in chunks if c)
        if len(text) > 500:
            return _clean_text(text)

    # ── Strategy 2: heading-based, skip TOC headings ──────────────────────────
    item_head_pat = re.compile(
        rf"item\s*{re.escape(item_num)}\b.{{0,60}}",
        re.IGNORECASE
    )
    next_head_pat = re.compile(
        rf"item\s*{re.escape(next_item_num)}\b",
        re.IGNORECASE
    )

    # Collect ALL matching headings, then use the last one (real section, not TOC)
    candidate_starts = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "div"]):
        text_content = tag.get_text(" ", strip=True)
        if item_head_pat.match(text_content) and len(text_content) < 120:
            candidate_starts.append(tag)

    # Try from last to first — last match is most likely the real section
    for start_el in reversed(candidate_starts):
        chunks = []
        for sib in start_el.next_elements:
            if hasattr(sib, "get_text"):
                t = sib.get_text(" ", strip=True)
                if next_head_pat.match(t) and len(t) < 120:
                    break
                if t:
                    chunks.append(t)
            elif isinstance(sib, str) and sib.strip():
                chunks.append(sib.strip())
        text = "\n".join(chunks)
        if len(text) > 500:
            return _clean_text(text)

    # ── Strategy 3: full plain-text regex, skip TOC matches ───────────────────
    full_text = soup.get_text(separator="\n")

    start_pat = re.compile(
        rf"(?m)^[\s]*item[\s]*{re.escape(item_num)}[\s\.\:—–]",
        re.IGNORECASE
    )
    end_pat = re.compile(
        rf"(?m)^[\s]*item[\s]*{re.escape(next_item_num)}[\s\.\:—–]",
        re.IGNORECASE
    )

    # Collect ALL start matches, filter out TOC entries, use the last real one
    all_start_matches = list(start_pat.finditer(full_text))

    if not all_start_matches:
        # Last resort: non-anchored search
        start_pat2 = re.compile(
            rf"item\s+{re.escape(item_num)}\b", re.IGNORECASE
        )
        all_start_matches = list(start_pat2.finditer(full_text))

    if not all_start_matches:
        return ""

    # Filter out TOC matches, fall back to last match if all look like TOC
    real_matches = [m for m in all_start_matches
                    if not _is_toc_match(full_text, m.start())]
    start_match = real_matches[0] if real_matches else all_start_matches[-1]

    remainder = full_text[start_match.start():]
    end_match = end_pat.search(remainder)
    chunk = remainder[: end_match.start()] if end_match else remainder[:60000]

    return _clean_text(chunk)


def _clean_text(text: str) -> str:
    """Collapse blank lines and strip leading/trailing whitespace per line."""
    lines = [l.strip() for l in text.splitlines()]
    # Remove runs of more than one blank line
    cleaned = []
    blank_count = 0
    for line in lines:
        if not line:
            blank_count += 1
            if blank_count <= 1:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_ticker(ticker: str, config: dict, years: list[int] | None = None):
    headers = get_headers(config)
    delay = config["rate_limits"]["sec_requests_per_second"]
    sleep_time = 1.0 / delay

    logger.info(f"── {ticker} ──────────────────────────")

    # 1. Resolve CIK
    cik = ticker_to_cik(ticker, headers)
    if not cik:
        logger.error(f"Could not find CIK for ticker: {ticker}")
        return
    logger.info(f"CIK: {cik}")
    time.sleep(sleep_time)

    # 2. Get 10-K filing list
    filings = get_10k_filing_urls(cik, headers, years=years)
    logger.info(f"Found {len(filings)} 10-K filing(s)")
    if not filings:
        return
    time.sleep(sleep_time)

    for filing in filings:
        year = filing["fiscal_year"]
        logger.info(f"  Processing fiscal year {year}")

        # 3. Get main document URL
        doc_url = get_10k_document_url(filing["documents_url"], headers)
        if not doc_url:
            logger.warning(f"  Could not find main 10-K document for {year}")
            continue
        time.sleep(sleep_time)

        # 4. Fetch the document
        try:
            resp = requests.get(doc_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"  Failed to fetch document: {e}")
            continue
        time.sleep(sleep_time)

        html = resp.text

        # 5. Extract Item 1 and Item 7
        for item_num, next_num, source_type in [
            ("1",  "1A", "10-K Item 1"),
            ("7",  "7A", "10-K Item 7"),
        ]:
            text = extract_item_text(html, item_num, next_num)
            if not text or len(text) < 200:
                logger.warning(f"  {source_type}: extraction too short or empty")
                quality = "flagged_short"
            else:
                quality = "clean"
                logger.info(f"  {source_type}: {len(text):,} chars extracted")

            # 6. Save normalized markdown
            out_path = source_filepath(
                company=ticker,
                source_type=source_type,
                year=year,
                base_dir=config["paths"]["normalized"],
            )
            md = build_markdown(
                text=text if text else "(extraction failed — flag for review)",
                company=ticker,
                segment="public",
                source_type=source_type,
                source_year=year,
                source_url=doc_url,
                ticker=ticker,
                quality_flag=quality if text else "flagged_empty",
            )
            out_path.write_text(md, encoding="utf-8")
            logger.info(f"  Saved: {out_path}")

            # 7. Append to master index
            append_to_index({
                "company": ticker,
                "segment": "public",
                "ticker": ticker,
                "industry": "",
                "source_type": source_type,
                "source_year": year,
                "source_url": doc_url,
                "file_path": str(out_path),
                "quality_flag": quality if text else "flagged_empty",
            }, index_path=f"{config['paths']['index']}/master_index.csv")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch 10-K filings from EDGAR")
    parser.add_argument("--tickers", nargs="+", required=True,
                        help="One or more ticker symbols, e.g. TILE ECL ENOV")
    parser.add_argument("--years", nargs="*", type=int, default=None,
                        help="Fiscal years to fetch, e.g. 2023 2024. Omit for all.")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    for ticker in args.tickers:
        fetch_ticker(ticker.upper(), cfg, years=args.years)
