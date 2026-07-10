"""
PolicyPulse — Crawler #2: SEBI Circulars
========================================

Fetches SEBI's "Circulars" listing, and for each circular downloads its PDF,
extracts the text, and stores a record in Supabase (skipping ones we've already
seen). The shared download/extract/store logic lives in `crawler/common.py`; this
file only knows how to read SEBI's specific pages.

HOW SEBI DIFFERS FROM RBI
-------------------------
- SEBI's listing gives us the title, date, and a link to an HTML *detail page* — but
  NOT the PDF directly. So for each circular we make one extra request to the detail
  page, where the PDF sits inside an <iframe src='...web/?file=<PDF_URL>'>. We pull the
  real PDF URL out of that iframe's `file=` parameter.

HOW TO RUN
----------
    python crawler/sebi_circulars.py --limit 5     # test on the 5 newest
    python crawler/sebi_circulars.py --days 21     # everything from the last 3 weeks
    python crawler/sebi_circulars.py               # all circulars on the listing

Secrets (SUPABASE_URL, SUPABASE_SERVICE_KEY) come from a local .env file.
"""

import argparse
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

import common  # shared helpers


# --------------------------------------------------------------------------------------
# SEBI-specific configuration
# --------------------------------------------------------------------------------------

# SEBI's circulars listing (server-rendered; sid/ssid select the Circulars section).
LISTING_URL = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0"

# Used to turn any relative links into absolute ones.
BASE_URL = "https://www.sebi.gov.in"

# The `source` label stored on every row this crawler creates (matches the filename).
SOURCE = "sebi_circulars"

log = logging.getLogger("sebi")


# --------------------------------------------------------------------------------------
# Step 1 — fetch and parse the SEBI listing page (title, date, detail-page URL)
# --------------------------------------------------------------------------------------

def fetch_listing_items():
    """Download the SEBI circulars page and return a list of items.

    Each item is a dict: {"title", "published_date", "url"}.  (The PDF URL is not on
    the listing — we resolve it later from each detail page.)

    The listing is a single table with two columns: Date | Title. The title cell holds
    a link to the circular's detail page.
    """
    log.info("Fetching listing: %s", LISTING_URL)
    resp = common.get_with_retries(LISTING_URL)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    first_link = soup.select_one("a[href*='/legal/circulars/']")
    if first_link is None:
        log.error("Could not find any circular links — SEBI may have changed their page layout.")
        return []
    table = first_link.find_parent("table")

    items = []
    for row in table.find_all("tr"):
        link = row.select_one("a[href*='/legal/circulars/']")
        if link is None:
            continue  # header row (or any row without a circular link)

        title = link.get_text(" ", strip=True)
        detail_url = urljoin(BASE_URL, link.get("href"))

        # The date is in the row's first cell.
        cells = row.find_all("td")
        date_text = cells[0].get_text(" ", strip=True) if cells else ""
        published_date = common.parse_listing_date(date_text)

        items.append(
            {
                "title": title,
                "published_date": published_date,
                "url": detail_url,
            }
        )

    log.info("Found %d circulars on the listing page.", len(items))
    return items


# --------------------------------------------------------------------------------------
# Step 2 — resolve the PDF URL from a circular's detail page
# --------------------------------------------------------------------------------------

def resolve_pdf_url(detail_url):
    """Fetch a SEBI circular detail page and return its PDF URL, or None if there
    isn't one. The PDF lives inside an <iframe src='...web/?file=<PDF_URL>'>."""
    resp = common.get_with_retries(detail_url)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    # Primary: the embedded PDF viewer iframe, whose `file=` param is the real PDF.
    iframe = soup.select_one("iframe[src*='.pdf'], iframe[src*='.PDF']")
    if iframe:
        src = iframe.get("src", "")
        file_param = parse_qs(urlparse(src).query).get("file", [None])[0]
        if file_param:
            return file_param
        # Occasionally the iframe src IS the PDF (no viewer wrapper).
        if src.lower().endswith(".pdf"):
            return urljoin(detail_url, src)

    # Fallback: a plain link to a PDF attachment.
    pdf_link = soup.select_one("a[href$='.pdf'], a[href$='.PDF']")
    if pdf_link:
        return urljoin(detail_url, pdf_link.get("href"))

    return None


# --------------------------------------------------------------------------------------
# Helpers for --days filtering
# --------------------------------------------------------------------------------------

def within_last_days(published_date, days):
    """Return True if `published_date` ('YYYY-MM-DD') is within the last `days` days.
    Items with no date are kept (we'd rather review them than silently drop them)."""
    if published_date is None:
        return True
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    return published_date >= cutoff


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crawl SEBI circulars into Supabase.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the N most recent circulars (handy for testing).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only process circulars published within the last N days (e.g. --days 21).",
    )
    args = parser.parse_args()

    common.configure_logging()
    client = common.get_supabase_client()

    time.sleep(common.POLITE_DELAY_SECONDS)  # be polite before the first request too
    items = fetch_listing_items()

    # Optional date window (used for the backfill).
    if args.days is not None:
        items = [it for it in items if within_last_days(it["published_date"], args.days)]
        log.info("Keeping %d circulars from the last %d days.", len(items), args.days)

    # Optional count limit (used for quick testing).
    if args.limit is not None:
        items = items[: args.limit]
        log.info("Limiting this run to the %d most recent circulars.", len(items))

    counts = {"ok": 0, "needs_review": 0, "skipped": 0, "failed": 0}
    for item in items:
        title = item["title"]

        # Step 2: resolve the PDF URL from the detail page (one extra polite request).
        time.sleep(common.POLITE_DELAY_SECONDS)
        try:
            log.info("Opening detail page for: %s", title[:70])
            pdf_url = resolve_pdf_url(item["url"])
        except Exception as exc:  # noqa: BLE001 — a detail-page network error -> retry next run
            log.error("FAILED (detail page) on '%s': %s", title[:70], exc)
            counts["failed"] += 1
            continue

        # Shared back half: download + extract + store (handles needs_review + de-dup).
        outcome = common.store_pdf_document(
            client, SOURCE, title, item["published_date"], item["url"], pdf_url, log
        )
        counts[outcome] += 1

    common.print_summary(log, counts, len(items))


if __name__ == "__main__":
    main()
