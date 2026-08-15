"""
PolicyPulse — shared crawler helpers
====================================

Both crawlers (RBI, SEBI, and any future ones) share the same "back half":
download a PDF, extract its text, fingerprint it, and store it in Supabase —
skipping anything we've already seen, and marking unreadable items 'needs_review'
instead of dropping them.

That shared logic lives here so we only write (and fix) it once. Each individual
crawler is then small: it just knows how to read ITS regulator's listing page and
hand us (title, date, detail-url, pdf-url) tuples.
"""

import hashlib
import io
import logging
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from supabase import create_client


# --------------------------------------------------------------------------------------
# Shared configuration
# --------------------------------------------------------------------------------------

# We identify ourselves as a real browser. Government sites often reject the default
# Python "requests" user-agent, so we send a realistic one.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Seconds to wait between network requests, to be polite to regulator servers.
POLITE_DELAY_SECONDS = 2.5

# Retry a few times on transient network errors (DNS blips, timeouts) before giving up.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3


def configure_logging():
    """Set up simple, readable logging (timestamps + level + message)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------------------
# Supabase connection
# --------------------------------------------------------------------------------------

def get_supabase_client():
    """Read secrets from .env (or the environment) and return a Supabase client.

    Exits with a friendly message if the secrets are missing, so a beginner isn't
    confronted with a confusing stack trace.
    """
    load_dotenv()  # loads variables from a local .env file, if present
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not url or not key:
        logging.error(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_KEY. "
            "Create a .env file (see .env.example) with both values."
        )
        sys.exit(1)

    return create_client(url, key)


# --------------------------------------------------------------------------------------
# Networking (with retries)
# --------------------------------------------------------------------------------------

def get_with_retries(url):
    """GET a URL, retrying a few times on transient network errors.

    Returns the successful response, or re-raises the last error if all attempts fail.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_error = exc
            logging.warning("Attempt %d/%d failed for %s (%s)", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise last_error


def download_pdf(pdf_url):
    """Download a PDF and return its raw bytes (with automatic retries)."""
    return get_with_retries(pdf_url).content


# --------------------------------------------------------------------------------------
# PDF text extraction
# --------------------------------------------------------------------------------------

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
        logging.warning("pdfplumber extracted no text; trying PyMuPDF fallback.")
    except Exception as exc:  # noqa: BLE001 — intentionally catch everything and fall back
        logging.warning("pdfplumber failed (%s); trying PyMuPDF fallback.", exc)

    # --- Fallback: PyMuPDF (fitz) ---
    try:
        import fitz  # provided by the 'pymupdf' package

        pages_text = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pages_text.append(page.get_text())
        return "\n".join(pages_text).strip()
    except Exception as exc:  # noqa: BLE001
        logging.warning("PyMuPDF also failed (%s).", exc)
        return ""


def sha256_of(text):
    """Return the sha256 hex fingerprint of some text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------------------

def parse_listing_date(text):
    """Parse a listing date like 'Jul 09, 2026' into 'YYYY-MM-DD', or None if it
    doesn't match. Both RBI and SEBI happen to use this exact format."""
    try:
        return datetime.strptime(text.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------------------
# Database: de-dup check + insert
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


def url_already_stored(client, source, url):
    """Return True if this source already has a row for this exact URL.

    This is a *secondary* de-dup guard. The main guard is content_hash (which detects
    genuinely new or changed text). But if the PDF-extraction library is ever upgraded and
    reads the same PDF into slightly different text, its content_hash would change and it
    would look 'new'. Matching on (source, url) as well means we never insert a second row
    for a document whose page/URL we've already stored.
    """
    result = (
        client.table("documents")
        .select("id")
        .eq("source", source)
        .eq("url", url)
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def insert_document(client, record):
    """Insert one record into the `documents` table."""
    client.table("documents").insert(record).execute()


# --------------------------------------------------------------------------------------
# The shared "back half": store one document (with de-dup + needs_review handling)
# --------------------------------------------------------------------------------------

def store_pdf_document(client, source, title, published_date, url, pdf_url, log):
    """Download + extract + store one document. Shared by every crawler.

    Returns one of:
        'ok'           -> inserted with extracted text
        'needs_review' -> inserted, but no PDF / text couldn't be extracted
        'skipped'      -> we already had this one (nothing to do)
        'failed'       -> a real error (e.g. network) — will be retried next run

    Never raises: any unexpected error is logged and turned into 'failed', so one
    bad document can't crash the whole run.
    """
    try:
        text = None

        # Try to get the PDF text, if there's a PDF at all.
        if pdf_url:
            time.sleep(POLITE_DELAY_SECONDS)  # be polite before hitting the file server
            log.info("Downloading PDF for: %s", title[:70])
            pdf_bytes = download_pdf(pdf_url)  # network error here -> 'failed', retried next run
            text = extract_text(pdf_bytes) or None  # treat empty string as "no text"

        # Decide status + the fingerprint we use for de-duplication.
        if text:
            status = "ok"
            content_hash = sha256_of(text)  # fingerprint from the text
        else:
            status = "needs_review"
            content_hash = sha256_of(url)  # stable per-document fingerprint from its URL
            reason = "no PDF attached" if not pdf_url else "no extractable text (scanned/image PDF?)"
            log.warning("Marking needs_review (%s): %s", reason, title[:70])

        # Change detection: if we've stored this fingerprint before, skip it.
        if hash_already_stored(client, content_hash):
            log.info("Already in database (skipping): %s", title[:70])
            return "skipped"

        # Secondary guard: skip if we already have this exact URL for this source, even if
        # the text (and thus the hash) came out slightly different this time.
        if url_already_stored(client, source, url):
            log.info("URL already in database (skipping): %s", title[:70])
            return "skipped"

        record = {
            "source": source,
            "title": title,
            "published_date": published_date,  # 'YYYY-MM-DD' or None
            "url": url,
            "pdf_url": pdf_url,
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


def print_summary(log, counts, total):
    """Print the standard end-of-run tally."""
    log.info(
        "DONE. inserted_ok=%d  inserted_needs_review=%d  skipped(already had)=%d  failed=%d  (of %d processed)",
        counts["ok"],
        counts["needs_review"],
        counts["skipped"],
        counts["failed"],
        total,
    )
