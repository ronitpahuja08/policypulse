"""
PolicyPulse — Classifier
========================

WHAT THIS DOES (plain English)
------------------------------
For every document we've crawled but not yet classified, this reads the document's text
and asks Google Gemini three questions:
  1. How severe is it? (high / medium / low)
  2. Who does it affect? (banks, NBFCs, mutual funds, ...)
  3. What changed, in one or two plain-English sentences?

It writes those three answers back onto the document's row in Supabase (the `severity`,
`entities_affected`, and `summary` columns). The daily brief generator then reads those
columns — the AI work happens here, once per document, not every time we build a brief.

We use **Google Gemini's free tier**, so this costs nothing to run. It is INERT until you
provide a GEMINI_API_KEY: with no key, it prints how to get one and exits without doing
anything.

HOW TO RUN
----------
    python classify/classify.py --limit 5     # classify 5 unclassified docs (test)
    python classify/classify.py               # classify ALL unclassified docs

Config comes from a local .env file (SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMINI_API_KEY).
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from pydantic import BaseModel
from supabase import create_client

from google import genai
from google.genai import types


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

# A free-tier Gemini model. "gemini-flash-latest" is a stable alias that always points to
# the current Flash model, so it won't go stale. Override with GEMINI_MODEL in .env.
DEFAULT_MODEL = "gemini-flash-latest"

# We only send the first N characters of each document to the model. Regulatory circulars
# put the important part up top, and this keeps requests fast and well within free limits.
MAX_TEXT_CHARS = 12000

# Be gentle with the free-tier rate limit (~15 requests/minute): wait between documents.
DELAY_SECONDS = 4.0

# Retry a few times on transient API errors before giving up on a document.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 8

VALID_SEVERITIES = {"high", "medium", "low"}

# The instruction we give Gemini for every document.
SYSTEM_INSTRUCTION = """You are a compliance analyst for Indian fintech and NBFC firms.
You read a notification or circular from an Indian financial regulator (RBI or SEBI) and
classify it for busy compliance officers.

Return:
- severity: one of exactly "high", "medium", or "low".
  - "high": urgent; regulated entities must act soon (a new compliance obligation, a
    deadline, a ban, penalties, or a mandatory reporting change).
  - "medium": a relevant change most affected entities should read (amendments, revised
    norms, changed formats).
  - "low": informational or niche (minor clarifications, administrative updates).
- entities_affected: a short comma-separated list of who is affected, e.g.
  "NBFCs, Banks" or "Mutual Funds, AMCs". If unclear, say "General".
- summary: one or two plain-English sentences saying what changed and who must act.
  No preamble, no "This circular...", just the substance."""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("classify")


# The exact JSON shape we want back from Gemini. Passing this as the response schema means
# the model is forced to return valid JSON with these three fields.
class Classification(BaseModel):
    severity: str
    entities_affected: str
    summary: str


# --------------------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------------------

def get_supabase_client():
    """Read Supabase secrets from .env and return a client (exits if missing)."""
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY. See .env.example.")
        sys.exit(1)
    return create_client(url, key)


def get_gemini_client():
    """Return a Gemini client, or exit with guidance if GEMINI_API_KEY isn't set.

    This is what keeps the classifier 'inert' until you opt in: no key, no work done.
    """
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error(
            "GEMINI_API_KEY is not set, so the classifier can't run.\n"
            "  1. Get a FREE key at https://aistudio.google.com/apikey\n"
            "  2. Add a line to your .env file:  GEMINI_API_KEY=your-key-here\n"
            "  3. Run this again."
        )
        sys.exit(1)
    return genai.Client(api_key=api_key)


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

def fetch_unclassified(client, limit):
    """Return documents that still need classifying: severity is null AND they have text.

    (needs_review rows have no full_text, so we can't classify them — we skip them.)
    """
    query = (
        client.table("documents")
        .select("id,source,title,full_text")
        .is_("severity", "null")
        .not_.is_("full_text", "null")
        .order("published_date", desc=True)
    )
    if limit is not None:
        query = query.limit(limit)
    return query.execute().data


def save_classification(client, doc_id, result):
    """Write severity / entities_affected / summary back onto the document row."""
    client.table("documents").update(
        {
            "severity": result.severity,
            "entities_affected": result.entities_affected,
            "summary": result.summary,
        }
    ).eq("id", doc_id).execute()


# --------------------------------------------------------------------------------------
# The classification call (with retries)
# --------------------------------------------------------------------------------------

def classify_text(gemini, model, text):
    """Ask Gemini to classify one document's text. Returns a Classification, or raises
    after exhausting retries."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = gemini.models.generate_content(
                model=model,
                contents=text[:MAX_TEXT_CHARS],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=Classification,
                ),
            )
            # With a response_schema set, the SDK parses the JSON for us.
            result = response.parsed

            # Guard against an unexpected severity value.
            severity = (result.severity or "").strip().lower()
            if severity not in VALID_SEVERITIES:
                log.warning("Unexpected severity %r; defaulting to 'medium'.", result.severity)
                severity = "medium"
            result.severity = severity
            return result
        except Exception as exc:  # noqa: BLE001 — retry on any transient API error
            last_error = exc
            log.warning("Attempt %d/%d failed (%s)", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise last_error


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Classify PolicyPulse documents with Gemini.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only classify the N most recent unclassified documents (handy for testing).",
    )
    args = parser.parse_args()

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    supabase = get_supabase_client()
    gemini = get_gemini_client()  # exits here if no GEMINI_API_KEY

    docs = fetch_unclassified(supabase, args.limit)
    if not docs:
        log.info("Nothing to classify — every document with text already has a severity.")
        return
    log.info("Classifying %d document(s) with model '%s'.", len(docs), model)

    counts = {"classified": 0, "failed": 0}
    for i, doc in enumerate(docs, start=1):
        title = doc["title"] or "(untitled)"
        try:
            if i > 1:
                time.sleep(DELAY_SECONDS)  # stay under the free-tier rate limit
            result = classify_text(gemini, model, doc["full_text"])
            save_classification(supabase, doc["id"], result)
            log.info(
                "[%s] %-6s | %-25s | %s",
                doc["source"].split("_")[0].upper(),
                result.severity.upper(),
                result.entities_affected[:25],
                title[:50],
            )
            counts["classified"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad doc must not stop the run
            log.error("FAILED on '%s': %s", title[:50], exc)
            counts["failed"] += 1

    log.info("DONE. classified=%d  failed=%d  (of %d)", counts["classified"], counts["failed"], len(docs))


if __name__ == "__main__":
    main()
