"""
utils/common.py
Shared helpers used across all pipeline scripts.
"""

import os
import time
import logging
import yaml
import hashlib
import re
from pathlib import Path
from datetime import date
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


# ── Logging ───────────────────────────────────────────────────────────────────

def get_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler
    fh = logging.FileHandler(f"{log_dir}/{name}.log")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, requests_per_second: float):
        self.min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.time()


# ── File helpers ──────────────────────────────────────────────────────────────

def safe_filename(s: str, max_len: int = 80) -> str:
    """Convert a string to a safe filename fragment."""
    s = re.sub(r"[^\w\s-]", "", s.lower())
    s = re.sub(r"[\s]+", "_", s.strip())
    return s[:max_len]


def source_filepath(company: str, source_type: str, year: int,
                    base_dir: str = "data/normalized") -> Path:
    """Build a deterministic output path for one source file."""
    company_slug = safe_filename(company)
    source_slug = safe_filename(source_type)
    folder = Path(base_dir) / company_slug

    # If something exists at this path that isn't a directory, remove it
    if folder.exists() and not folder.is_dir():
        folder.unlink()

    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{source_slug}_{year}.md"


def url_hash(url: str) -> str:
    """Short hash of a URL — used to detect duplicate sources."""
    return hashlib.md5(url.encode()).hexdigest()[:10]


# ── Markdown output ───────────────────────────────────────────────────────────

def build_markdown(
    text: str,
    company: str,
    segment: str,
    source_type: str,
    source_year: int,
    source_url: str,
    ticker: str = "",
    industry: str = "",
    sic: str = "",
    naics: str = "",
    exchange: str = "",
    hq_country: str = "",
    founded: str = "",
    revenue_usd: str = "",
    quality_flag: str = "clean",
    ai_mention: Optional[bool] = None,
    sustainability_mention: Optional[bool] = None,
) -> str:
    """
    Wrap cleaned text in the project's standard markdown format.
    Produces the header block defined in section 7 of the data plan.
    """
    def yn(val):
        if val is None:
            return ""
        return "yes" if val else "no"

    # Quick keyword scan for flags (coarse — classification is the Senior RA's job)
    text_lower = text.lower()
    if ai_mention is None:
        ai_mention = any(kw in text_lower for kw in
                         ["artificial intelligence", " ai ", "machine learning",
                          "deep learning", "neural network"])
    if sustainability_mention is None:
        sustainability_mention = any(kw in text_lower for kw in
                                     ["sustainab", "circula", "carbon", "emission",
                                      "recycl", "renewable", "net zero", "esg"])

    header = f"""---
company: {company}
segment: {segment}
ticker: {ticker}
industry: {industry}
sic: {sic}
naics: {naics}
exchange: {exchange}
hq_country: {hq_country}
founded: {founded}
revenue_usd: {revenue_usd}
source_type: {source_type}
source_year: {source_year}
source_url: {source_url}
ai_mention: {yn(ai_mention)}
sustainability_mention: {yn(sustainability_mention)}
quality_flag: {quality_flag}
date_fetched: {date.today().isoformat()}
---

"""
    return header + text.strip()


# ── Master index helpers ──────────────────────────────────────────────────────

INDEX_COLUMNS = [
    "company", "segment", "ticker", "industry",
    "source_type", "source_year", "source_url",
    "file_path", "quality_flag",
]


def append_to_index(row: dict, index_path: str = "data/index/master_index.csv"):
    """Append one row to the master CSV index. Creates file+header if absent."""
    import pandas as pd
    Path(index_path).parent.mkdir(parents=True, exist_ok=True)
    df_new = pd.DataFrame([{col: row.get(col, "") for col in INDEX_COLUMNS}])
    if Path(index_path).exists():
        df_new.to_csv(index_path, mode="a", header=False, index=False)
    else:
        df_new.to_csv(index_path, index=False)
