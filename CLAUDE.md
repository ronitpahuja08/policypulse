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

## Current status (Phase 1 — today)

Only the **first crawler is built and working end-to-end**:

- `crawler/rbi_notifications.py` — fetches the RBI notifications listing, parses each
  item, downloads its PDF, extracts the text, and stores a record in Supabase. It uses
  a `content_hash` (a fingerprint of the text) to skip documents we've already seen, so
  running it repeatedly only adds genuinely new notifications.

**Not built yet (future phases — do NOT build unless asked):**

- SEBI crawler (and other regulators: IRDAI, MCA, etc.)
- Classifier (fills in `severity` and `entities_affected`)
- Daily compliance brief generator

## How the pieces fit together

```
GitHub Actions (daily 00:30 UTC / 6:00am IST)
        |
        v
crawler/rbi_notifications.py  --->  RBI website (fetch + parse + download PDFs)
        |
        v
Supabase Postgres  (table: documents)
```

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
- `severity`, `entities_affected` — **null for now**; the future classifier fills these.
- `created_at` — when we inserted the row.

## Secrets & configuration

- Local runs read `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from a `.env` file
  (never committed — see `.gitignore`).
- The automated GitHub Actions run reads the same two values from
  **GitHub repository secrets**.

## Conventions

- Be **polite to regulator websites**: send a realistic browser User-Agent and wait
  2–3 seconds between requests. These are government sites — do not hammer them.
- **Never crash the whole run** because one document failed. Log the failure, skip it,
  keep going.
- Each crawler lives in `crawler/` and is runnable on its own with a `--limit N` flag
  for quick testing.
