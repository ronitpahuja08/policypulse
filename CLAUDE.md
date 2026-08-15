# PolicyPulse

## What this is (plain English)

PolicyPulse is a **regulatory-intelligence service** for Indian financial compliance.

Every day it visits the websites of Indian financial regulators (starting with the
**RBI — Reserve Bank of India**), reads every new notification/circular they publish,
and saves a structured record of each one into a database. Later phases will
**classify** each notification (how severe is it? who does it affect?) and generate a
**daily compliance brief** — a short email/summary that fintech and NBFC compliance
officers can read in a few minutes instead of manually checking regulator sites.

Think of it as: *crawl → store → (later) classify → (later) brief*.

## Who is building it

A **solo, beginner founder**. That means the code should be:

- **Simple over clever.** Readable beats fancy. No frameworks we don't need.
- **Heavily commented.** Assume the reader is still learning Python.
- **Free tiers only.** No paid infrastructure. (Supabase free tier, GitHub Actions
  free tier, etc.)
- **Python** everywhere.

## Current status (Phase 1)

**Two crawlers are built and working end-to-end**, both storing into the same
`documents` table:

- `crawler/rbi_notifications.py` — fetches the RBI notifications listing (an ASP.NET
  page). The PDF link is on the listing row itself, so it's one request per item.
- `crawler/sebi_circulars.py` — fetches SEBI's circulars listing (server-rendered
  HTML). The listing only gives a *detail-page* link, so it makes one extra request per
  circular to pull the PDF URL out of the detail page's `<iframe src='...?file=...pdf'>`.

The shared "back half" (download PDF → extract text → sha256 fingerprint → de-dup →
insert, with `needs_review` handling) lives in **`crawler/common.py`** so it's written
once and reused by every crawler. Each crawler only contains the logic for reading its
own regulator's listing page.

Both use a `content_hash` (fingerprint) to skip documents we've already seen, so running
repeatedly only adds genuinely new items.

**Classifier (built):**

- `classify/classify.py` — reads each unclassified document's text and asks **Google
  Gemini (free tier)** to fill in `severity` (high/medium/low), `entities_affected`, and a
  1–2 sentence `summary`, writing them back to the row. **Inert by default**: with no
  `GEMINI_API_KEY` it prints how to get one and exits. Processes `severity is null` rows
  (covers backlog + new). Skips `needs_review` rows (no text). Rate-limit friendly (delay +
  retry/backoff) for the free tier.

**Brief generator + email distribution (built; email delivery INERT):**

- `distribution/generate_brief.py` — `build_brief(client, since_hours)` builds the real
  brief from **classified** docs: grouped by severity (high→low), each with its stored
  `summary` and `entities_affected`, plus a "needs review" list. No AI call at build time
  (classification already did it), so it's cheap and idempotent.
- `distribution/send_brief.py` — emails the brief via SMTP. Still **inert by default**:
  previews only; won't send unless SMTP creds are set AND `--send` is passed. NOT wired into
  the daily workflow. Subscribers live in a `subscribers` table (`schema.sql`); preview
  without it via `--test-recipient`. Chosen approach: **fully automated email**, not Substack.

**De-dup:** crawlers skip on `content_hash` (new/changed text) AND on `(source, url)` — the
secondary URL guard prevents PDF-extraction drift from creating duplicate rows.

**Not built yet (future phases — do NOT build unless asked):**

- More regulators (IRDAI, MCA, etc.)
- Activation: running the classifier daily (add `GEMINI_API_KEY` secret + a workflow step)
  and turning on email sending (subscribers table + `SMTP_*` secrets + `--send` step).

## How the pieces fit together

```
GitHub Actions (daily 00:30 UTC / 6:00am IST)
        |
        v
crawler/rbi_notifications.py  --->  RBI website   \
crawler/sebi_circulars.py     --->  SEBI website    > (fetch + parse + download PDFs)
        |  (both use crawler/common.py)           /
        v
Supabase Postgres  (table: documents)
        |
        v
classify/classify.py  --->  Google Gemini (fills severity/entities/summary)
        |
        v
distribution/generate_brief.py  (builds brief)  -->  distribution/send_brief.py (INERT)
```

The two crawlers run **sequentially** in the same daily job (RBI, then SEBI). The classifier
is built but not yet in the daily workflow (needs the `GEMINI_API_KEY` secret + a web-UI step).

## The database

One table, `documents`, in Supabase (Postgres). Key columns:

- `source` — which crawler produced it (e.g. `rbi_notifications`).
- `title`, `published_date`, `url` (detail page), `pdf_url`.
- `full_text` — extracted PDF text.
- `content_hash` — **unique**; our change-detection / de-duplication key. For normal
  rows it's the sha256 of the extracted text; for `needs_review` rows (no readable
  text) it's the sha256 of the detail URL so they still de-dup across runs.
- `status` — `'ok'` when we extracted text, `'needs_review'` when a notification had
  no PDF or an unreadable (e.g. scanned/image) PDF. We store these instead of dropping
  them, so nothing is silently lost. `full_text` is null for `needs_review` rows.
- `severity`, `entities_affected`, `summary` — filled in by `classify/classify.py` (null
  until a document is classified). `severity` is `'high'`/`'medium'`/`'low'`.
- `created_at` — when we inserted the row.

## Secrets & configuration

- Local runs read secrets from a `.env` file (never committed — see `.gitignore`):
  `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (crawlers + everything), `GEMINI_API_KEY`
  (classifier), and optional `SMTP_*` (only to actually send email).
- The automated GitHub Actions run reads the Supabase values from **GitHub repository
  secrets**. `GEMINI_API_KEY` would be added there too when the classifier is put on the
  daily schedule.

## Conventions

- Be **polite to regulator websites**: send a realistic browser User-Agent and wait
  2–3 seconds between requests. These are government sites — do not hammer them.
- **Never crash the whole run** because one document failed. Log the failure, skip it,
  keep going.
- **Naming convention for crawlers:** each crawler is `crawler/<source>.py`, and the
  `source` string it writes to the database matches its filename (e.g.
  `crawler/sebi_circulars.py` → `source = 'sebi_circulars'`). Follow this for any new
  regulator.
- Put shared logic in `crawler/common.py`; keep each crawler file limited to reading its
  own regulator's listing.
- Each crawler is runnable on its own with a `--limit N` flag for quick testing. SEBI
  also has `--days N` (only circulars from the last N days), used for backfills.
