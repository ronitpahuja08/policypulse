"""
PolicyPulse — Crawler #1: RBI Notifications
===========================================

Fetches the RBI "Notifications" listing, and for each notification downloads its
PDF, extracts the text, and stores a record in Supabase (skipping ones we've already
seen). The shared download/extract/store logic lives in `crawler/common.py`; this
file only knows how to read RBI's specific listing page.

HOW TO RUN
----------
    python crawler/rbi_notifications.py --limit 5     # test on the 5 newest
    python crawler/rbi_notifications.py               # all items on the listing

Secrets (SUPABASE_URL, SUPABASE_SERVICE_KEY) come from a local .env file.
"""

import argparse
import logging
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import common  # shared helpers (run as: python crawler/rbi_notifications.py)


# --------------------------------------------------------------------------------------
# RBI-specific configuration
# --------------------------------------------------------------------------------------

# The page that lists RBI notifications (an ASP.NET page).
LISTING_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx"

# Used to turn the relative links on the page into absolute ones.
BASE_URL = "https://www.rbi.org.in/Scripts/"

# The `source` label stored on every row this crawler creates (matches the filename).
SOURCE = "rbi_notifications"

log = logging.getLogger("rbi")


# --------------------------------------------------------------------------------------
# Fetch and parse the RBI listing page
# --------------------------------------------------------------------------------------

def fetch_listing_items():
    """Download the RBI notifications page and return a list of items.

    Each item is a dict: {"title", "published_date", "url", "pdf_url"}.

    The RBI page is one big table. Some rows are *date headers* (their text is just a
    date like "Jul 09, 2026"); the notification rows that follow belong to that date,
    until the next date header appears.
    """
    log.info("Fetching listing: %s", LISTING_URL)
    resp = common.get_with_retries(LISTING_URL)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    first_link = soup.select_one("a.link2[href*='NotificationUser.aspx?Id=']")
    if first_link is None:
        log.error("Could not find any notification links — RBI may have changed their page layout.")
        return []
    table = first_link.find_parent("table")

    items = []
    current_date = None  # the most recent date-header we've seen while scanning rows

    for row in table.find_all("tr"):
        title_link = row.select_one("a.link2[href*='NotificationUser.aspx?Id=']")

        if title_link is None:
            # Might be a date-header row. Try to read a date from it.
            parsed = common.parse_listing_date(row.get_text(" ", strip=True))
            if parsed:
                current_date = parsed
            continue

        # This is a notification row.
        title = title_link.get_text(" ", strip=True)
        detail_url = urljoin(BASE_URL, title_link.get("href"))

        # RBI puts the PDF link right in the same row.
        pdf_link = row.select_one("a[href$='.PDF'], a[href$='.pdf']")
        pdf_url = pdf_link.get("href") if pdf_link else None

        items.append(
            {
                "title": title,
                "published_date": current_date,
                "url": detail_url,
                "pdf_url": pdf_url,
            }
        )

    log.info("Found %d notifications on the listing page.", len(items))
    return items


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crawl RBI notifications into Supabase.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the N most recent notifications (handy for testing).",
    )
    args = parser.parse_args()

    common.configure_logging()
    client = common.get_supabase_client()

    time.sleep(common.POLITE_DELAY_SECONDS)  # be polite before the first request too
    items = fetch_listing_items()

    if args.limit is not None:
        items = items[: args.limit]
        log.info("Limiting this run to the %d most recent notifications.", len(items))

    counts = {"ok": 0, "needs_review": 0, "skipped": 0, "failed": 0}
    for item in items:
        outcome = common.store_pdf_document(
            client,
            SOURCE,
            item["title"],
            item["published_date"],
            item["url"],
            item["pdf_url"],
            log,
        )
        counts[outcome] += 1

    common.print_summary(log, counts, len(items))


if __name__ == "__main__":
    main()
