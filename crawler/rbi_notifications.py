"""
PolicyPulse — Crawler #1: RBI Notifications
===========================================

WHAT THIS DOES (plain English)
------------------------------
1. Visits the RBI "Notifications" listing page.
2. Reads every notification on it: title, date, link to its detail page, and the
   link to its PDF.
3. For each notification, downloads the PDF and extracts the text inside it.
4. Makes a "fingerprint" (a sha256 hash) of that text.
5. Saves a record in our Supabase database — but SKIPS anything whose fingerprint
   we've already stored. That fingerprint is how we detect "is this new or not".

It is deliberately POLITE to the RBI website: it pretends to be a normal browser and
waits a couple of seconds between requests, because RBI is a government site and we
must not hammer it.

HOW TO RUN
----------
    # test on just the 5 newest notifications:
    python crawler/rbi_notifications.py --limit 5

    # full run (all notifications on the first listing page):
    python crawler/rbi_notifications.py

Secrets (SUPABASE_URL, SUPABASE_SERVICE_KEY) are read from a local .env file.
"""

import argparse
import hashlib
import io
import logging
import os
import sys
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client


# --------------------------------------------------------------------------------------
# Configuration / constants
# --------------------------------------------------------------------------------------

# The page that lists RBI notifications (an ASP.NET page).
LISTING_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx"

# A value used to build absolute links from the relative ones on the page.
BASE_URL = "https://www.rbi.org.in/Scripts/"

# We identify ourselves as a real browser. Some government sites reject the default
# Python "requests" user-agent, so we send a realistic one.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Seconds to wait between network requests, to be polite to RBI's servers.
POLITE_DELAY_SECONDS = 2.5

# Which "source" label we store on every row this crawler creates.
SOURCE_LABEL = "rbi_notifications"


# Set up simple, readable logging (timestamps + level + message).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rbi")


# --------------------------------------------------------------------------------------
# Step 1 — Connect to Supabase
# --------------------------------------------------------------------------------------

def get_supabase_client():
    """Read secrets from .env and return a ready-to-use Supabase client.

    Exits with a friendly message if the secrets are missing, so a beginner isn't
    confronted with a confusing stack trace.
    """
    load_dotenv()  # loads variables from the local .env file into the environment
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not url or not key:
        log.error(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_KEY. "
            "Create a .env file (see .env.example) with both values."
        )
        sys.exit(1)

    return create_client(url, key)


# --------------------------------------------------------------------------------------
# Step 2 — Fetch and parse the listing page
# --------------------------------------------------------------------------------------

def fetch_listing_items():
    """Download the RBI notifications page and return a list of items.

    Each item is a dict:
        {
          "title":          str,
          "published_date": "YYYY-MM-DD" or None,
          "url":            str  (link to the detail page),
          "pdf_url":        str or None (link to the PDF),
        }

    How the RBI page is laid out: it's one big table. Some rows are *date headers*
    (their text is just a date like "Jul 09, 2026") and the notification rows that
    follow belong to that date, until the next date header appears.
    """
    log.info("Fetching listing: %s", LISTING_URL)
    resp = get_with_retries(LISTING_URL)  # retries a few times on transient network errors
    resp.encoding = "utf-8"   # RBI serves UTF-8; be explicit about it

    soup = BeautifulSoup(resp.text, "lxml")

    # The notifications live in the table that contains the detail links. We locate
    # that table by finding the first detail link and climbing up to its table.
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
            # No title link -> this might be a date-header row. Try to read a date.
            text = row.get_text(" ", strip=True)
            parsed = _try_parse_date(text)
            if parsed:
                current_date = parsed
            continue  # header rows have no notification to collect

        # This is a notification row.
        title = title_link.get_text(" ", strip=True)
        detail_url = urljoin(BASE_URL, title_link.get("href"))

        # The PDF link (if any) sits in the same row and points at rbidocs.rbi.org.in.
        pdf_url = None
        pdf_link = row.select_one("a[href$='.PDF'], a[href$='.pdf']")
        if pdf_link:
            pdf_url = pdf_link.get("href")

        items.append(
            {
                "title": title,
                "published_date": current_date,  # "YYYY-MM-DD" string, or None
                "url": detail_url,
                "pdf_url": pdf_url,
            }
        )

    log.info("Found %d notifications on the listing page.", len(items))
    return items


def _try_parse_date(text):
    """Return 'YYYY-MM-DD' if `text` looks like an RBI date header (e.g. 'Jul 09, 2026'),
    otherwise None."""
    try:
        return datetime.strptime(text, "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Step 3 — Download a PDF and extract its text
# --------------------------------------------------------------------------------------

# How many times to retry a network request before giving up, and how long to wait
# between tries. Government sites (and DNS) occasionally blip; a couple of retries
# turns a transient failure into a success without any human intervention.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3


def get_with_retries(url):
    """GET a URL, retrying a few times on transient network errors.

    Returns the successful response. Re-raises the last error if all attempts fail.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_error = exc
            log.warning("Attempt %d/%d failed for %s (%s)", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    # All attempts failed — let the caller handle it.
    raise last_error


def download_pdf(pdf_url):
    """Download a PDF and return its raw bytes (with automatic retries)."""
    resp = get_with_retries(pdf_url)
    return resp.content


def extract_text(pdf_bytes):
    """Extract text from PDF bytes.

    Primary method: pdfplumber. If that fails or returns nothing, fall back to
    PyMuPDF (imported as 'fitz'). Returns the extracted text (may be empty).
    """
    # --- Primary: pdfplumber ---
    try:
        import pdfplumber

        pages_text = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                pages_text.append(page.extract_text() or "")
        text = "\n".join(pages_text).strip()
        if text:
            return text
        log.warning("pdfplumber extracted no text; trying PyMuPDF fallback.")
    except Exception as exc:  # noqa: BLE001 — we intentionally catch everything and fall back
        log.warning("pdfplumber failed (%s); trying PyMuPDF fallback.", exc)

    # --- Fallback: PyMuPDF (fitz) ---
    try:
        import fitz  # provided by the 'pymupdf' package

        pages_text = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pages_text.append(page.get_text())
        return "\n".join(pages_text).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("PyMuPDF also failed (%s).", exc)
        return ""


def sha256_of(text):
    """Return the sha256 hex fingerprint of some text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Step 4 — Talk to the database (check for duplicates, insert new rows)
# --------------------------------------------------------------------------------------

def hash_already_stored(client, content_hash):
    """Return True if a row with this content_hash already exists in `documents`."""
    result = (
        client.table("documents")
        .select("id")
        .eq("content_hash", content_hash)
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def insert_document(client, record):
    """Insert one notification record into the `documents` table."""
    client.table("documents").insert(record).execute()


# --------------------------------------------------------------------------------------
# Step 5 — Tie it all together
# --------------------------------------------------------------------------------------

def process_item(client, item):
    """Process a single notification. Returns one of:
        'ok'           -> inserted with extracted text
        'needs_review' -> inserted, but no PDF / text couldn't be extracted
        'skipped'      -> we already had this one (nothing to do)
        'failed'       -> a real error (e.g. network) — will be retried next run

    We DON'T throw away notifications we can't read: instead we still store the
    title/date/URL and mark them 'needs_review' so they're visible, not lost.

    This function NEVER raises — any unexpected error is logged and turned into a
    'failed' result, so one bad document can't crash the whole run.
    """
    title = item["title"]

    try:
        text = None

        # Try to get the PDF text, if there's a PDF at all.
        if item["pdf_url"]:
            # Be polite: pause before hitting RBI's file server.
            time.sleep(POLITE_DELAY_SECONDS)
            log.info("Downloading PDF for: %s", title[:70])
            pdf_bytes = download_pdf(item["pdf_url"])  # a network error here -> 'failed', retried next run
            extracted = extract_text(pdf_bytes)
            text = extracted or None  # treat empty string as "no text"

        # Decide status + the fingerprint we use for de-duplication.
        if text:
            status = "ok"
            # Fingerprint from the text: catches genuinely new/changed documents.
            content_hash = sha256_of(text)
        else:
            status = "needs_review"
            # No usable text, so fingerprint from the detail URL instead. This is
            # stable per notification, so we won't re-insert the same one every day.
            content_hash = sha256_of(item["url"])
            reason = "no PDF attached" if not item["pdf_url"] else "no extractable text (scanned/image PDF?)"
            log.warning("Marking needs_review (%s): %s", reason, title[:70])

        # Change detection: if we've stored this fingerprint before, skip it.
        if hash_already_stored(client, content_hash):
            log.info("Already in database (skipping): %s", title[:70])
            return "skipped"

        record = {
            "source": SOURCE_LABEL,
            "title": title,
            "published_date": item["published_date"],  # 'YYYY-MM-DD' or None
            "url": item["url"],
            "pdf_url": item["pdf_url"],
            "full_text": text,          # None for needs_review rows
            "content_hash": content_hash,
            "status": status,           # 'ok' or 'needs_review'
            "severity": None,           # filled in later by the future classifier
            "entities_affected": None,  # filled in later by the future classifier
        }
        insert_document(client, record)
        log.info("INSERTED [%s]: %s", status, title[:70])
        return status

    except Exception as exc:  # noqa: BLE001 — never let one document crash the whole run
        log.error("FAILED on '%s': %s", title[:70], exc)
        return "failed"


def main():
    parser = argparse.ArgumentParser(description="Crawl RBI notifications into Supabase.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the N most recent notifications (handy for testing).",
    )
    args = parser.parse_args()

    client = get_supabase_client()

    # Be polite before the very first request too.
    time.sleep(POLITE_DELAY_SECONDS)
    items = fetch_listing_items()

    if args.limit is not None:
        items = items[: args.limit]
        log.info("Limiting this run to the %d most recent notifications.", len(items))

    # Tally the outcomes so we can print a clear summary at the end.
    counts = {"ok": 0, "needs_review": 0, "skipped": 0, "failed": 0}
    for item in items:
        outcome = process_item(client, item)
        counts[outcome] += 1

    log.info(
        "DONE. inserted_ok=%d  inserted_needs_review=%d  skipped(already had)=%d  failed=%d  (of %d processed)",
        counts["ok"],
        counts["needs_review"],
        counts["skipped"],
        counts["failed"],
        len(items),
    )


if __name__ == "__main__":
    main()
