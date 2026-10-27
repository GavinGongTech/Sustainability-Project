"""
scripts/secondary/_coverage_page_helpers.py

Shared logic used by fetch_founder_interviews.py, fetch_trade_press_mentions.py,
and fetch_industry_journals.py. Not run directly.

All three scripts look for a company's own "In the News" / "Media Coverage" /
"As Seen In" page and extract outbound links, then each one filters for and
saves only ITS category of link. The page is fetched fresh by each script
(simpler than sharing a cache across processes) but the classification logic
and domain lists live here once so they stay consistent across all three.
"""

import time
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

# URL patterns for aggregated third-party coverage pages
COVERAGE_PATTERNS = [
    "{base}/in-the-news",
    "{base}/media",
    "{base}/media-coverage",
    "{base}/press-coverage",
    "{base}/press-mentions",
    "{base}/as-seen-in",
    "{base}/featured-in",
    "{base}/news/in-the-news",
    "{base}/about/in-the-news",
    "{base}/about/media",
    "{base}/company/in-the-news",
    "{base}/company/media",
    "{base}/coverage",
]

REJECT_PHRASES = [
    "page not found", "404", "cookie consent", "we use cookies",
    "this website uses cookies", "access denied", "403 forbidden",
]

# ── Domain classification lists ────────────────────────────────────────────────

TRADE_PRESS_DOMAINS = {
    "greenbiz.com", "canarymedia.com", "chemweek.com", "chemicalweek.com",
    "plasticsnews.com", "cen.acs.org", "utilitydive.com", "waste360.com",
    "recyclingtoday.com", "agriculture.com", "agfundernews.com",
    "foodnavigator.com", "just-auto.com", "automotiveworld.com",
    "constructiondive.com", "chemistryworld.com", "c-and-en.com",
    "esgtoday.com", "sustainablebrands.com", "triplepundit.com",
    "environmentalleader.com", "climatetechvc.substack.com",
    "agfunder.com", "foodnavigator-usa.com",
}

INTERVIEW_DOMAINS = {
    "youtube.com", "youtu.be", "spotify.com", "podcasts.apple.com",
    "soundcloud.com", "anchor.fm", "podbean.com", "buzzsprout.com",
    "overcast.fm", "stitcher.com", "vimeo.com", "wistia.com",
}

# Academic / peer-reviewed / industry journal publishers — previously
# missing entirely from classification.
JOURNAL_DOMAINS = {
    "nature.com", "sciencedirect.com", "cell.com", "science.org",
    "springer.com", "link.springer.com", "wiley.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "mdpi.com", "plos.org", "journals.plos.org",
    "frontiersin.org", "jstor.org", "pnas.org", "acs.org", "pubs.acs.org",
    "rsc.org", "pubs.rsc.org", "iopscience.iop.org", "ieee.org",
    "ieeexplore.ieee.org", "arxiv.org", "biorxiv.org", "researchgate.net",
    "sciencemag.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
}

SKIP_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "crunchbase.com", "pitchbook.com",
}


def classify_link(url: str) -> str:
    """Classify a link's target domain into one category."""
    domain = urlparse(url).netloc.lower().lstrip("www.")
    if any(domain == d or domain.endswith("." + d) for d in JOURNAL_DOMAINS):
        return "journal"
    if any(domain == d or domain.endswith("." + d) for d in INTERVIEW_DOMAINS):
        return "interview"
    if any(domain == d or domain.endswith("." + d) for d in TRADE_PRESS_DOMAINS):
        return "trade_press"
    return "other"


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }


def find_coverage_links(base_url: str) -> list[dict]:
    """
    Visit the company's coverage page (trying each pattern) and return
    ALL external links found, each tagged with a category. Callers filter
    to the category they care about.
    Returns [] if no coverage page found.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-http2",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=get_headers()["User-Agent"],
            viewport={"width": 1280, "height": 800},
        )

        for pattern in COVERAGE_PATTERNS:
            url = pattern.format(base=base_url.rstrip("/"))
            try:
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=12000)
                if not resp or resp.status >= 400:
                    page.close()
                    continue

                time.sleep(1)
                html = page.content()
                text = page.inner_text("body")
                page.close()

                text_lower = text.lower()
                if any(p in text_lower for p in REJECT_PHRASES):
                    continue
                if len(text) < 200:
                    continue

                soup = BeautifulSoup(html, "lxml")
                own_domain = urlparse(base_url).netloc.lower().lstrip("www.")
                links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = urljoin(url, href)
                    if not href.startswith("http"):
                        continue
                    domain = urlparse(href).netloc.lower().lstrip("www.")
                    if domain == own_domain:
                        continue
                    if any(skip in domain for skip in SKIP_DOMAINS):
                        continue
                    title = a.get_text(strip=True)
                    if not title or len(title) < 5:
                        continue
                    links.append({
                        "title": title[:200],
                        "url": href,
                        "category": classify_link(href),
                        "found_on": url,
                    })

                if links:
                    browser.close()
                    return links

            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                continue

        browser.close()
    return []
