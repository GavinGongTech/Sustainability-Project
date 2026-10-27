"""Fetch and normalize SEC 10-K text for public-company research.

This script is the core public-company evidence collector. It resolves company
symbols to CIKs, finds annual filings, and extracts the narrative sections most
relevant to sustainability analysis: Item 1 (Business) and Item 7 (MD&A).

The output is a markdown record for each company/year/section, paired with an
index row that tracks where the file was saved and whether the extraction was
marked as clean or flagged for review.
"""

import re, sys, time, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils.common import load_config, get_logger, build_markdown, source_filepath, append_to_index

import warnings, requests, pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = get_logger("fetch_edgar")
TICKERS_URL     = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{:010d}.json"


def headers(cfg):
    return {"User-Agent": f"{cfg['user']['name']} {cfg['user']['email']}"}


def ticker_to_cik(ticker, h):
    data = requests.get(TICKERS_URL, headers=h, timeout=15).json()
    for e in data.values():
        if e["ticker"].upper() == ticker.upper():
            return str(e["cik_str"]).zfill(10)
    return None


def get_filings(cik, h, years=None):
    """Collect the annual 10-K filing metadata for one company.

    Returns a list of filing entries with the fiscal year and the documents index
    page. The script later walks each filing index to locate the actual document
    URL for the annual report text.
    """
    r = requests.get(SUBMISSIONS_URL.format(int(cik)), headers=h, timeout=15).json()
    recent = r.get("filings", {}).get("recent", {})
    out = []
    for form, date, acc in zip(recent.get("form",[]), recent.get("filingDate",[]), recent.get("accessionNumber",[])):
        if form not in ("10-K","10-K/A"): continue
        yr = int(date[:4])
        if years and yr not in years: continue
        clean = acc.replace("-","")
        out.append({"fiscal_year": yr,
                    "documents_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{clean}/{acc}-index.htm"})
    return out


def get_doc_url(index_url, h):
    """Resolve the actual annual-report document URL from the SEC filing index.

    SEC filing index pages list each document file associated with a filing. This
    function scans the table and picks the main 10-K document, which is the actual
    source used for the extraction logic below.
    """
    soup = BeautifulSoup(requests.get(index_url, headers=h, timeout=15).text, "lxml")
    for table in soup.find_all("table"):
        row0 = table.find("tr")
        if not row0: continue
        cols = [c.get_text(strip=True).lower() for c in row0.find_all(["th","td"])]
        try:
            ti = next(i for i,c in enumerate(cols) if "type" in c)
            di = next(i for i,c in enumerate(cols) if "document" in c or "filename" in c)
        except StopIteration: continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(ti,di): continue
            if cells[ti].get_text(strip=True) in ("10-K","10-K/A"):
                a = cells[di].find("a")
                if a:
                    href = a["href"]
                    url = f"https://www.sec.gov{href}" if href.startswith("/") else href
                    return url.replace("https://www.sec.gov/ix?doc=", "https://www.sec.gov")
    return None


def extract_item(html, num, next_num):
    """
    Extract one Item section from 10-K HTML.
    Tries anchors → headings → plain-text regex, skipping TOC matches.
    """
    soup  = BeautifulSoup(html, "lxml")
    full  = soup.get_text(separator="\n")
    ipat  = re.compile(rf"^item\s*{re.escape(num)}\b",      re.I)
    npat  = re.compile(rf"^item\s*{re.escape(next_num)}\b", re.I)
    epat  = re.compile(rf"(?m)^[\s]*item[\s]*{re.escape(next_num)}[\s\.\:—–]", re.I)

    def is_toc(text, pos, win=300):
        lines = [l.strip() for l in text[pos:pos+win].splitlines() if l.strip()]
        return len(lines) < 4 or any(re.fullmatch(r"\d{1,3}", l) for l in lines[:3])

    def collect_after(start_tag, end_tag):
        chunks, on = [], False
        for tag in soup.find_all(True):
            if tag == start_tag: on = True
            if on:
                if end_tag and tag == end_tag: break
                if tag.string: chunks.append(tag.string.strip())
        return "\n".join(c for c in chunks if c)

    # Strategy 1: named anchors
    def anchors(pat):
        found = []
        for tag in soup.find_all(["a","div","span","p"], id=lambda x: x and pat.match(x)):
            if tag not in found: found.append(tag)
        for tag in soup.find_all("a", attrs={"name": True}):
            if pat.match(tag["name"]) and tag not in found: found.append(tag)
        return found

    starts, ends = anchors(ipat), anchors(npat)
    end_tag = ends[0] if ends else None
    for st in reversed(starts):
        text = collect_after(st, end_tag)
        if len(text) > 500: return _clean(text)

    # Strategy 2: heading tags
    hpat = re.compile(rf"item\s*{re.escape(num)}\b.{{0,60}}", re.I)
    candidates = [t for t in soup.find_all(["h1","h2","h3","h4","p","div"])
                  if hpat.match(t.get_text(" ", strip=True)) and len(t.get_text()) < 120]
    for st in reversed(candidates):
        chunks = []
        for sib in st.next_elements:
            t = sib.get_text(" ", strip=True) if hasattr(sib,"get_text") else str(sib).strip()
            if npat.match(t) and len(t) < 120: break
            if t: chunks.append(t)
        text = "\n".join(chunks)
        if len(text) > 500: return _clean(text)

    # Strategy 3: plain-text regex
    spat = re.compile(rf"(?m)^[\s]*item[\s]*{re.escape(num)}[\s\.\:—–]", re.I)
    matches = list(spat.finditer(full)) or list(
        re.compile(rf"item\s+{re.escape(num)}\b", re.I).finditer(full))
    if not matches: return ""
    real = [m for m in matches if not is_toc(full, m.start())]
    m    = real[0] if real else matches[-1]
    rest = full[m.start():]
    end  = epat.search(rest)
    return _clean(rest[:end.start()] if end else rest[:60000])


def _clean(text):
    lines, blanks, out = text.splitlines(), 0, []
    for line in lines:
        line = line.strip()
        if not line:
            blanks += 1
            if blanks <= 1: out.append(line)
        else:
            blanks = 0; out.append(line)
    return "\n".join(out).strip()


def fetch_ticker(ticker, meta, cfg, years=None):
    """Fetch all relevant 10-K sections for one ticker and write them to disk.

    This is the single-company execution helper used by the script’s main run
    loop. It handles CIK lookup, filing retrieval, Item extraction, markdown save,
    and index registration.
    """
    h    = headers(cfg)
    pace = 1.0 / cfg["rate_limits"]["sec_requests_per_second"]

    cik = ticker_to_cik(ticker, h)
    if not cik: logger.error(f"{ticker}: CIK not found"); return
    logger.info(f"{ticker} → CIK {cik}")
    time.sleep(pace)

    filings = get_filings(cik, h, years)
    logger.info(f"  {len(filings)} 10-K filing(s)")
    if not filings: return
    time.sleep(pace)

    for filing in filings:
        year    = filing["fiscal_year"]
        doc_url = get_doc_url(filing["documents_url"], h)
        if not doc_url: logger.warning(f"  {year}: document URL not found"); continue
        time.sleep(pace)

        try:
            html = requests.get(doc_url, headers=h, timeout=30).text
        except Exception as e:
            logger.error(f"  {year}: download failed: {e}"); continue
        time.sleep(pace)

        for item_num, next_num, source_type in [("1","1A","10-K Item 1"),("7","7A","10-K Item 7")]:
            text    = extract_item(html, item_num, next_num)
            quality = "clean" if text and len(text) >= 200 else "flagged_short"
            logger.info(f"  {source_type}: {len(text):,} chars [{quality}]")

            path = source_filepath(ticker, source_type, year, cfg["paths"]["normalized"])
            path.write_text(build_markdown(
                text         = text or "(extraction failed)",
                company      = meta.get("company", ticker),
                segment      = "public",
                source_type  = source_type,
                source_year  = year,
                source_url   = doc_url,
                ticker       = ticker,
                industry     = meta.get("industry",""),
                sic          = str(meta.get("sic","")),
                exchange     = meta.get("exchange",""),
                hq_country   = meta.get("hq_country",""),
                quality_flag = quality,
            ), encoding="utf-8")
            logger.info(f"  Saved: {path}")

            append_to_index({"company": meta.get("company",ticker), "segment":"public",
                "ticker":ticker, "industry":meta.get("industry",""), "source_type":source_type,
                "source_year":year, "source_url":doc_url, "file_path":str(path),
                "quality_flag":quality}, f"{cfg['paths']['index']}/master_index.csv")


def run(tickers=None, years=None, config_path="config/config.yaml"):
    cfg = load_config(config_path)
    csv = Path(cfg["paths"]["index"]) / "public_company_list.csv"

    if tickers:
        companies = {t.upper(): {} for t in tickers}
    else:
        if not csv.exists():
            logger.error(f"Run build_company_list.py first — {csv} not found"); return
        df = pd.read_csv(csv)
        companies = {
            str(row["ticker"]).upper(): row.to_dict()
            for _, row in df.iterrows()
            if pd.notna(row.get("ticker"))
        }
        logger.info(f"Loaded {len(companies):,} companies from {csv}")

    for ticker, meta in companies.items():
        try:
            fetch_ticker(ticker, meta, cfg, years)
        except Exception as e:
            logger.error(f"{ticker}: {e}")
        time.sleep(2)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch 10-K filings from EDGAR")
    p.add_argument("--tickers", nargs="+", help="Specific tickers e.g. TILE ECL")
    p.add_argument("--years",   nargs="+", type=int, help="Fiscal years e.g. 2024 2025")
    p.add_argument("--config",  default="config/config.yaml")
    args = p.parse_args()
    run(tickers=args.tickers, years=args.years, config_path=args.config)
