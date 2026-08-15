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

**Email distribution scaffolding (built, deliberately INERT):**

- `distribution/send_brief.py` — the code that will email the daily brief to subscribers
  via SMTP (Python's built-in `smtplib`, no extra deps). It is **inert by default**: it
  only *previews* (dry run) and will not send unless SMTP credentials are set AND `--send`
  is passed. It is intentionally NOT wired into the daily GitHub Actions workflow.
- The brief content is a **placeholder** (`build_placeholder_brief`) that lists recently
  crawled `documents`. The real Day-6 generator replaces just that function.
- Subscribers live in a `subscribers` table (see `schema.sql`) — created only when the
  user is ready. Preview without it via `--test-recipient`.
- Chosen distribution approach (decided with the founder): **fully automated email**, not
  Substack. To activate later: create the `subscribers` table, add `SMTP_*` secrets, and
  add a `send_brief.py --send` step to the workflow.

**Not built yet (future phases — do NOT build unless asked):**

- More regulators (IRDAI, MCA, etc.)
- Classifier (fills in `severity` and `entities_affected`) — when built, also add the
  **URL as a secondary de-dup guard** (decided on Day 2) so extraction-drift can't create
  duplicate rows.
- Daily compliance brief *generator* (the real summary content; the email *delivery*
  layer above already exists).

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
```

The two crawlers run **sequentially** in the same daily job (RBI, then SEBI).

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
- **Naming convention for crawlers:** each crawler is `crawler/<source>.py`, and the
  `source` string it writes to the database matches its filename (e.g.
  `crawler/sebi_circulars.py` → `source = 'sebi_circulars'`). Follow this for any new
  regulator.
- Put shared logic in `crawler/common.py`; keep each crawler file limited to reading its
  own regulator's listing.
- Each crawler is runnable on its own with a `--limit N` flag for quick testing. SEBI
  also has `--days N` (only circulars from the last N days), used for backfills.
