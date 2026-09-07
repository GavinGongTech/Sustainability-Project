# Sustainability Research Pipeline — Code Documentation

This repository is a research and data collection pipeline for building a company-level sustainability dataset. It gathers information about public companies, startups, patents, ESG reports, and public mentions across news and technical sources. The project is organized around a shared output schema: each fetcher saves markdown files in a normalized data folder and appends rows to a master index CSV.

The active code lives mostly under the [sustainability_research](sustainability_research) directory. The repository root also contains the project-level notes and dependency list.

---

## 1. High-level purpose

The project aims to collect source material for a sustainability classification pipeline:

- Public companies from SEC EDGAR and public-company lists
- Startup companies discovered from VC and accelerator portfolio pages
- ESG or sustainability reports from company websites and filings
- Patents, press releases, engineering blogs, white papers, founder interviews, and trade press mentions
- Normalized outputs in markdown files for manual review and later AI classification

The pipeline uses a consistent pattern:

1. Load configuration from [sustainability_research/config/config.yaml](sustainability_research/config/config.yaml)
2. Pull a company list or candidate list
3. Fetch source material from web or SEC APIs
4. Save normalized markdown text under [sustainability_research/data/normalized](sustainability_research/data/normalized)
5. Append metadata to [sustainability_research/data/index/master_index.csv](sustainability_research/data/index/master_index.csv)
6. Log each run under [sustainability_research/logs](sustainability_research/logs)

---

## 2. Repository structure

### Root files

- [README.md](README.md): project overview and operational instructions
- [requirements.txt](requirements.txt): Python dependencies
- [PROJECT_CODE_DOCUMENTATION.md](PROJECT_CODE_DOCUMENTATION.md): this file, summarizing code structure

### Main project directory

- [sustainability_research/config/config.yaml](sustainability_research/config/config.yaml): all primary configuration values, SIC code collections, target companies, and rate limits
- [sustainability_research/data](sustainability_research/data): raw downloads, normalized files, and indexes
- [sustainability_research/logs](sustainability_research/logs): execution logs
- [sustainability_research/scripts](sustainability_research/scripts): active pipeline scripts
- [sustainability_research/tester](sustainability_research/tester): utility scripts for cleanup, review, and CSV adjustments
- [sustainability_research/anmol](sustainability_research/anmol): alternative/older implementation of the fetchers
- [sustainability_research/anmol_run](sustainability_research/anmol_run): second variant of the fetcher pipeline

---

## 3. Data flow and execution order

The intended pipeline is roughly:

1. [sustainability_research/scripts/public_us/build_company_list.py](sustainability_research/scripts/public_us/build_company_list.py) builds a list of public companies by SIC from SEC EDGAR.
2. [sustainability_research/scripts/startups/crawl_vc_portfolios.py](sustainability_research/scripts/startups/crawl_vc_portfolios.py) discovers startup candidates from VC portfolio pages.
3. [sustainability_research/scripts/startups/scrape_startup_websites.py](sustainability_research/scripts/startups/scrape_startup_websites.py) crawls startup websites for product-page text.
4. [sustainability_research/scripts/public_us/fetch_edgar.py](sustainability_research/scripts/public_us/fetch_edgar.py) downloads EDGAR 10-K Item 1 and Item 7 text.
5. [sustainability_research/scripts/esg/fetch_esg_direct.py](sustainability_research/scripts/esg/fetch_esg_direct.py) fetches ESG or sustainability reports for public firms and startups.
6. [sustainability_research/scripts/patents/fetch_patents_direct.py](sustainability_research/scripts/patents/fetch_patents_direct.py) fetches patent metadata and saves it as markdown.
7. One or more secondary-source scripts gather trade press, blogs, interviews, white papers, and press releases.
8. Outputs are reviewed and normalized before downstream classification.

The script [sustainability_research/scripts/test_pull.py](sustainability_research/scripts/test_pull.py) is a smoke-test entry point that exercises the pipeline on a small sample of companies.

---

## 4. Shared utility layer

### [sustainability_research/scripts/utils/common.py](sustainability_research/scripts/utils/common.py)

This is the core support module for the active pipeline.

Responsibilities:

- Loads YAML config files
- Creates structured loggers
- Builds safe output paths for normalized markdown files
- Writes the YAML frontmatter block used in every output file
- Maintains the master index CSV with append-only row writes
- Manages environment/secrets lookup for API keys
- Includes helper logic for SKIP/queued run filtering in some later scripts

Key functions:

- `load_config()`
- `get_logger()`
- `RateLimiter`
- `safe_filename()`
- `source_filepath()`
- `build_markdown()`
- `append_to_index()`
- `get_secret()`

This file provides the project’s “shared contract” for how outputs are saved and indexed. Most fetchers depend on it.

### [sustainability_research/scripts/utils/web_scraper.py](sustainability_research/scripts/utils/web_scraper.py)

This is the general-purpose website scraper used across the project.

Responsibilities:

- Strips irrelevant HTML boilerplate such as nav, footer, scripts, and cookie banners
- Tries a normal `requests` fetch first
- Falls back to Playwright when the page is JavaScript-heavy or thin
- Saves cleaned text to markdown using the standard frontmatter format
- Appends the result to the master CSV index

This script is a reusable “product page” or general web-text collector for startup or company landing pages.

---

## 5. Public company collection and SEC pipeline

### [sustainability_research/scripts/public_us/build_company_list.py](sustainability_research/scripts/public_us/build_company_list.py)

Builds the public-company list from SEC EDGAR.

What it does:

- Requests EDGAR’s master ticker/exchange file
- Keeps only companies with a ticker symbol
- Calls EDGAR submissions data for each CIK to retrieve SIC and industry metadata
- Filters to the SIC groups defined in the config
- Saves the result to [sustainability_research/data/index/public_company_list.csv](sustainability_research/data/index/public_company_list.csv)

This is the foundational list generator for the public-company branch of the research project.

### [sustainability_research/scripts/public_us/fetch_edgar.py](sustainability_research/scripts/public_us/fetch_edgar.py)

Downloads 10-K annual report text from SEC EDGAR and extracts sections that matter for sustainability analysis.

What it does:

- Resolves a ticker to its CIK
- Finds relevant 10-K filings for selected fiscal years
- Finds the correct document URL from the SEC index page
- Extracts Item 1 (Business) and Item 7 (MD&A)
- Cleans extracted text and writes one markdown file per item per year
- Appends metadata to the master index

This is the central data source for public-company narrative text.

### [sustainability_research/scripts/public_us/fetch_patents.py](sustainability_research/scripts/public_us/fetch_patents.py)

This is an older patent-fetching script based on SerpAPI and the Google Patents API.

What it does:

- Uses a `SERPAPI_KEY` environment variable
- Searches Google Patents by assignee/company name
- Filters to patents that appear to belong to the target company
- Formats results as markdown text
- Saves output under the normalized directory

This file is a legacy approach, and the project later moved to a more direct and self-contained method in [sustainability_research/scripts/patents/fetch_patents_direct.py](sustainability_research/scripts/patents/fetch_patents_direct.py).

---

## 6. Startup discovery and startup website scraping

### [sustainability_research/scripts/startups/crawl_vc_portfolios.py](sustainability_research/scripts/startups/crawl_vc_portfolios.py)

This is the central VC/accelerator crawling script.

Responsibilities:

- Visits VC portfolio pages and accelerators
- Extracts candidate startup names and URLs
- Filters out obvious nav and junk links
- Rejects social media, blank, and non-company links
- Deduplicates results per VC and saves a clean startup candidate CSV
- Writes run summaries for each VC crawl

Outputs:

- [sustainability_research/data/index/startup_candidates.csv](sustainability_research/data/index/startup_candidates.csv)
- [sustainability_research/data/index/crawl_run_summary.csv](sustainability_research/data/index/crawl_run_summary.csv)

This is the startup discovery layer that turns portfolio pages into a candidate company list.

### [sustainability_research/scripts/startups/crawl_vc_portfolios_fix_one.py](sustainability_research/scripts/startups/crawl_vc_portfolios_fix_one.py)

This appears to be a one-off or earlier patch version of the startup crawl logic.

The file documents a series of fixes and refinements to the main crawler, such as:

- keeping internal portfolio detail links instead of dropping them
- improving exact matching for nav names
- fixing domain stripping bugs
- using better browser headers and Playwright behavior
- fixing name clean-up logic

This file is useful as a “historical fix log” or alternate implementation, but the version in [sustainability_research/scripts/startups/crawl_vc_portfolios.py](sustainability_research/scripts/startups/crawl_vc_portfolios.py) is the version to use for current work.

### [sustainability_research/scripts/startups/scrape_startup_websites.py](sustainability_research/scripts/startups/scrape_startup_websites.py)

This is the startup website collection script.

What it does:

- Reads startup candidates from the CSV
- Filters to valid URLs
- Scrapes each startup website for product page or company text
- Saves markdown files to the normalized area
- Logs counts of clean, flagged, and failed scrapes

This is the startup equivalent of public-company EDGAR extraction.

---

## 7. ESG and sustainability report gathering

### [sustainability_research/scripts/esg/fetch_esg_direct.py](sustainability_research/scripts/esg/fetch_esg_direct.py)

This is the project’s ESG-focused fetcher.

Responsibilities:

- Looks for sustainability, ESG, climate, and responsibility pages on company websites
- Checks common URL patterns and on-page links
- Uses robots.txt and request throttling to stay respectful
- Caches HTML locally to reduce repeated requests
- Finds PDF reports and extracts text when possible
- Classifies report type using pattern detection (ESG report vs sustainability report)
- May also fall back to EDGAR filing text for public companies
- Saves results to markdown with metadata and master index rows

This is the most important document-focused script for sustainability evidence collection.

Key helpers inside the file:

- `classify_report_type()`
- `RobotsCache`
- `RateLimiter`
- `DiskCache`
- `BrowserManager`
- `ESG_PATH_PATTERNS`, `NAV_LINK_TERMS`, `REPORT_HINTS`

---

## 8. Patent collection

### [sustainability_research/scripts/patents/fetch_patents_direct.py](sustainability_research/scripts/patents/fetch_patents_direct.py)

This is the active direct patent search script.

What it does:

- Uses Google Patents / USPTO with validation to avoid broken page garbage
- Normalizes assignee names and strips legal suffixes
- Rejects malformed patent IDs and bad HTML/CSS data
- Searches for patents by company name and saves metadata
- Writes markdown outputs with patent title, number, dates, inventors, and source link
- Appends to the master index

This is the project’s main patent ingestion script.

### [sustainability_research/scripts/public_us/fetch_patents.py](sustainability_research/scripts/public_us/fetch_patents.py)

Older/alternate patent collection method using SerpAPI and Google Patents endpoint search. It is now effectively superseded by the direct patent fetcher.

---

## 9. Secondary-source fetchers

These scripts collect additional evidence types beyond EDGAR, ESG, and patents. Each follows the same general pattern: search for a company, format results, save markdown, and append to the master index.

### [sustainability_research/scripts/secondary/fetch_trade_press_mentions.py](sustainability_research/scripts/secondary/fetch_trade_press_mentions.py)

Searches DuckDuckGo’s plain HTML search for company mentions in trade publications such as GreenBiz, Canary Media, and Chemical Week.

### [sustainability_research/scripts/secondary/fetch_founder_interviews.py](sustainability_research/scripts/secondary/fetch_founder_interviews.py)

Searches YouTube for founder or CEO interview material.

### [sustainability_research/scripts/secondary/fetch_industry_journals.py](sustainability_research/scripts/secondary/fetch_industry_journals.py)

Searches Semantic Scholar for academic and industry journal mentions and citation material.

### [sustainability_research/scripts/secondary/fetch_engineering_blogs.py](sustainability_research/scripts/secondary/fetch_engineering_blogs.py)

Looks for engineering or technical blog sections on company domains using common patterns and keyword detection.

### [sustainability_research/scripts/secondary/fetch_press_releases.py](sustainability_research/scripts/secondary/fetch_press_releases.py)

Searches company websites for press-release/news pages and pulls usable text.

### [sustainability_research/scripts/secondary/fetch_white_papers.py](sustainability_research/scripts/secondary/fetch_white_papers.py)

Searches for white papers/resources, downloads PDFs when present, and extracts text from them.

### [sustainability_research/scripts/secondary/_coverage_page_helpers.py](sustainability_research/scripts/secondary/_coverage_page_helpers.py)

This is a shared helper module for “coverage pages” like media/news pages. It defines:

- known coverage URL patterns
- domain classification lists for trade press, interview, and journal links
- logic to extract and classify links from a company’s coverage page

It is not a direct fetcher by itself; it provides shared logic reused by the media collection scripts.

---

## 10. Smoke test and validation scripts

### [sustainability_research/scripts/test_pull.py](sustainability_research/scripts/test_pull.py)

This is the end-to-end sanity-check script for the project.

What it does:

- Runs a small set of sample public companies documented in config
- Calls EDGAR retrieval
- Simulates startup scraping
- Checks if the output files and index are generated correctly

It acts as a “week 2 validation” script and is a useful starting point when debugging the pipeline.

---

## 11. Anmol and anmol_run variants

### [sustainability_research/anmol](sustainability_research/anmol)

This directory contains a variant implementation of the fetcher logic. It includes files such as:

- [sustainability_research/anmol/common_anmol.py](sustainability_research/anmol/common_anmol.py)
- [sustainability_research/anmol/fetch_engineering_blogs.py](sustainability_research/anmol/fetch_engineering_blogs.py)
- [sustainability_research/anmol/fetch_founder_interviews.py](sustainability_research/anmol/fetch_founder_interviews.py)
- [sustainability_research/anmol/fetch_industry_journals.py](sustainability_research/anmol/fetch_industry_journals.py)
- [sustainability_research/anmol/fetch_patents_direct.py](sustainability_research/anmol/fetch_patents_direct.py)
- [sustainability_research/anmol/fetch_press_releases.py](sustainability_research/anmol/fetch_press_releases.py)
- [sustainability_research/anmol/fetch_trade_press_mentions.py](sustainability_research/anmol/fetch_trade_press_mentions.py)
- [sustainability_research/anmol/fetch_white_papers.py](sustainability_research/anmol/fetch_white_papers.py)

These files are functionally similar to the active pipeline scripts but appear to be an alternate implementation or earlier draft. They are useful if someone wants to compare approaches or recover an older method.

### [sustainability_research/anmol_run/scripts/anmol](sustainability_research/anmol_run/scripts/anmol)

This is another variant set of the same fetcher family, with a slightly different path structure and helper naming conventions. It appears to be a second run or packaging of the same concept, likely used for a separate operational pass.

If someone is reading the repo in the future, these files should be treated as parallel implementations rather than the canonical production pipeline.

---

## 12. Tester / review / repair scripts

The [sustainability_research/tester](sustainability_research/tester) directory contains support scripts for fixing or auditing the output and metadata.

### [sustainability_research/tester/detect_tagline_names.py](sustainability_research/tester/detect_tagline_names.py)

Detects whether a company name is actually a tagline or marketing phrase rather than a real company name.

### [sustainability_research/tester/fix_tagline_names.py](sustainability_research/tester/fix_tagline_names.py)

Cleans up startup names that were misread from marketing taglines or copy.

### [sustainability_research/tester/fix_internal_vc_urls.py](sustainability_research/tester/fix_internal_vc_urls.py)

Repairs URLs that point to internal VC pages rather than external company websites.

### [sustainability_research/tester/fix_lowercarbon_urls.py](sustainability_research/tester/fix_lowercarbon_urls.py)

Fixes URL resolution issues for Lowercarbon/portfolio-related startup pages.

### [sustainability_research/tester/interrogate_sic.py](sustainability_research/tester/interrogate_sic.py)

Looks up and validates SIC codes against broader industrial categories. This is a data-quality script used to justify the SIC buckets in the config.

### [sustainability_research/tester/pull_questioned_sics.py](sustainability_research/tester/pull_questioned_sics.py)

Pulls SIC codes that require manual review or were flagged as ambiguous.

### [sustainability_research/tester/merge_nonus_into_public_list.py](sustainability_research/tester/merge_nonus_into_public_list.py)

Combines non-US public-company rows into a public-company list or comparison table.

### [sustainability_research/tester/merge_renamed_dirs.py](sustainability_research/tester/merge_renamed_dirs.py)

Merges directories that were renamed during a cleanup or rerun.

### [sustainability_research/tester/merge_truncated_dirs.py](sustainability_research/tester/merge_truncated_dirs.py)

Handles directory names truncated during downloads or environment issues.

### [sustainability_research/tester/merge_truncated_renamed_dirs.py](sustainability_research/tester/merge_truncated_renamed_dirs.py)

Combined utility for renames and truncation cleanup.

### [sustainability_research/tester/scrape_arpae.py](sustainability_research/tester/scrape_arpae.py)

Scrapes ARPA-E or related startup/company data sources for supplemental research.

### [sustainability_research/tester/scrape_arpae_new.py](sustainability_research/tester/scrape_arpae_new.py)

Updated or alternate version of the ARPA-E scraper.

These tester scripts are not the primary execution path; they are cleanup, normalization, and auditing tools for the research pipeline.

---

## 13. Configuration file: critical entry point

### [sustainability_research/config/config.yaml](sustainability_research/config/config.yaml)

This is one of the most important files in the project.

It contains:

- user identity for SEC requests
- request rate limits and retry settings
- file path configuration
- exchange list
- SIC grouping definitions for industry targeting
- test-company samples for smoke tests

If someone wants to understand the project quickly, this file is the best starting point because it defines the project’s domain boundaries and data collection targets.

---

## 14. Output conventions

The project writes outputs in a consistent format.

### Normalized markdown output

Each source is stored as a markdown file containing:

- YAML frontmatter with company, ticker, source type, year, URL, and quality flags
- plain text body extracted from the source

This makes output easy to review manually or feed into later LLM or classification pipelines.

### Master index

The master index is a CSV that tracks each row as a source record. Typical columns include:

- company
- segment
- ticker
- industry
- source_type
- report_type
- source_year
- source_url
- file_path
- quality_flag

This is the project’s central metadata index and is critical for downstream analysis.

---

## 15. Recommended reading order for new contributors

For a maintainer or PI reading the code for the first time, the recommended order is:

1. [README.md](README.md)
2. [sustainability_research/config/config.yaml](sustainability_research/config/config.yaml)
3. [sustainability_research/scripts/utils/common.py](sustainability_research/scripts/utils/common.py)
4. [sustainability_research/scripts/utils/web_scraper.py](sustainability_research/scripts/utils/web_scraper.py)
5. [sustainability_research/scripts/public_us/build_company_list.py](sustainability_research/scripts/public_us/build_company_list.py)
6. [sustainability_research/scripts/public_us/fetch_edgar.py](sustainability_research/scripts/public_us/fetch_edgar.py)
7. [sustainability_research/scripts/startups/crawl_vc_portfolios.py](sustainability_research/scripts/startups/crawl_vc_portfolios.py)
8. [sustainability_research/scripts/startups/scrape_startup_websites.py](sustainability_research/scripts/startups/scrape_startup_websites.py)
9. [sustainability_research/scripts/esg/fetch_esg_direct.py](sustainability_research/scripts/esg/fetch_esg_direct.py)
10. [sustainability_research/scripts/patents/fetch_patents_direct.py](sustainability_research/scripts/patents/fetch_patents_direct.py)
11. Secondary fetchers in [sustainability_research/scripts/secondary](sustainability_research/scripts/secondary)
12. Tester utilities in [sustainability_research/tester](sustainability_research/tester)

---

## 16. Notes for maintainers

- The active pipeline is centered on the [sustainability_research/scripts](sustainability_research/scripts) tree.
- The [sustainability_research/anmol](sustainability_research/anmol) and [sustainability_research/anmol_run](sustainability_research/anmol_run) folders are parallel implementations and should be treated as historical or alternative variants.
- The project uses a metadata-first design: normalized markdown files are saved, then a CSV index tracks them.
- Many scripts are web-data acquisition scripts, not analytical modeling code. Their job is to collect evidence and store it in a reviewable format.
- Several source fetchers are designed to work for both public companies and startups.
- The project is intentionally tolerant of missing data; it flags weak or empty results rather than fabricating them.

---

## 17. Summary

This repository is best understood as a research ingestion and normalization pipeline rather than a traditional application service. The code is built to collect sustainability-related evidence from many sources, store it consistently, and leave it ready for downstream classification and review.

If a new person needs to understand the project quickly, the best entry points are the configuration file, the common utility module, and the public-company/startup acquisition scripts in order. Everything else is a specialized source collector or data-cleanup utility built on top of that shared foundation.
