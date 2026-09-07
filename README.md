# Research Pipeline — Data Compilation
**Project:** Using AI in Design to Improve Product Sustainability  
**Role:** Junior RA — data layer owner  
**Owner:** Gavin Gong

---

## Directory structure

```
research_pipeline/
├── config/
│   └── config.yaml            # All settings — edit this first
├── data/
│   ├── raw/                   # Raw fetched files (untouched)
│   ├── normalized/            # Cleaned markdown files (one per source)
│   │   └── <company_slug>/
│   │       └── <source_type>_<year>.md
│   └── index/
│       ├── master_index.csv       # One row per source (all segments)
│       ├── public_company_list.csv  # Public companies by SIC
│       └── startup_candidates.csv   # Startups from VC portfolio crawls
├── logs/                      # One log file per script
├── scripts/
│   ├── test_pull.py           # Week 2: end-to-end test (run this first)
│   ├── public_us/
│   │   ├── build_company_list.py   # Week 3: public company list from EDGAR
│   │   └── fetch_edgar.py          # Week 4: fetch 10-K Item 1 and 7
│   ├── startups/
│   │   └── crawl_vc_portfolios.py  # Week 3: discover startups from VC pages
│   └── utils/
│       ├── common.py          # Shared helpers (config, logging, markdown)
│       └── web_scraper.py     # General scraper (requests + Playwright fallback)
└── requirements.txt
```

---

## Week 1 checklist — Access setup

Before running any code, get these accounts set up:

| Access | How | Notes |
|--------|-----|-------|
| SEC EDGAR | Free, no account needed | Just need your email for User-Agent header |
| Crunchbase Pro | Account from project team | Use for manual list exports, not scraping |
| Harmonic.ai | Account from project team | Same — manual exports only |
| Shared drive | From project team | Where you'll deliver outputs |

Update `config/config.yaml` → `user.name` and `user.email` with your real NYU email before any EDGAR calls. SEC enforces this.

---

## Setup (Week 2)

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser (needed for JS-heavy sites)
playwright install chromium

# 4. Verify everything works
python -c "import requests, bs4, pandas, rapidfuzz, playwright, yaml; print('All imports OK')"
```

---

## Run order

### Week 2 — Test pull (do this first)
```bash
# Run from the research_pipeline/ directory
python scripts/test_pull.py
```
This pulls 10-K filings for 3 US companies and scrapes 2 startup sites.
Check `data/normalized/` for markdown files and `data/index/master_index.csv` for the index.

Expect ~5 files and a few log warnings for JS-heavy startup pages — that's normal.

### Week 3 — Build company lists
```bash
# Build public company list from EDGAR SIC codes
python scripts/public_us/build_company_list.py

# Crawl VC portfolio pages for startup discovery
python scripts/startups/crawl_vc_portfolios.py

# Test one VC before running all
python scripts/startups/crawl_vc_portfolios.py --vc "Third Derivative"
```

### Week 4 — Full EDGAR fetch
```bash
# Fetch 10-K filings for all tickers in public_company_list.csv
# (build a small loop around fetch_edgar.py — Week 4 task)
python scripts/public_us/fetch_edgar.py --tickers TILE ECL ENOV --years 2023 2024
```

---

## Output format

Every source file follows this structure:

```markdown
---
company: Acme Corp
segment: public
ticker: ACME
industry: Automotive
sic: 3711
...
quality_flag: clean
date_fetched: 2026-06-24
---

# Business (Item 1)

Cleaned source text...
```

`quality_flag` values:
- `clean` — looks good, ready for classification
- `flagged_short` — content extracted but very thin, flag for Senior RA
- `flagged_empty` — extraction failed entirely, needs manual review

---

## Rules (from data plan section 8)

- Always run from the project root directory (not from inside `scripts/`)
- Respect `robots.txt` — check before adding a new site to the scraper
- SEC: keep requests under 10/sec; the config default is 8/sec — do not increase it
- Add your real NYU email to `config.yaml` before any EDGAR calls
- Never invent or fill in missing data — leave blank and flag it
- Log what was fetched and what failed — logs go in `logs/`
- Re-running the pipeline should update, not duplicate

---

## Troubleshooting

**EDGAR returns 403**  
→ Check that `user.email` in config is set to your real email (not a placeholder).

**Playwright not found**  
→ Run `playwright install chromium` after pip install.

**Startup page scrape returns `flagged_empty`**  
→ The site may block bots. Try opening it in a browser and checking `robots.txt`.  
→ If the page requires login, note it in the master index and flag for manual review.

**Import errors**  
→ Make sure you're running from `research_pipeline/` directory, not from inside `scripts/`.

**Rate limit warning from SEC**  
→ Lower `sec_requests_per_second` in config.yaml to 5 and re-run.
