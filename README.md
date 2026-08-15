# PolicyPulse

Regulatory-intelligence for Indian financial compliance. PolicyPulse crawls Indian
financial regulators daily (starting with the **RBI**), and stores a structured record
of every notification. Later it will classify them and generate a daily compliance
brief for fintech/NBFC compliance officers.

> New here? See [CLAUDE.md](CLAUDE.md) for a fuller plain-English description of the project.

## What works today

- **Crawler #1: RBI notifications** (`crawler/rbi_notifications.py`) — fetches the RBI
  notifications listing, downloads each PDF, extracts the text, and stores it in Supabase.
- **Crawler #2: SEBI circulars** (`crawler/sebi_circulars.py`) — same idea for SEBI
  (the PDF lives one click deeper, inside each circular's detail page).
- Both share `crawler/common.py` and de-duplicate using a content fingerprint, so
  re-running only adds genuinely new documents.
- **Distribution** (`distribution/send_brief.py`) — the code that
  will email the daily brief to subscribers. It is inert until you switch it on — see
  "Email distribution" below.

## Setup (one time)

1. **Install Python 3.12+** (this project was built with 3.14).

2. **Create a virtual environment and install dependencies:**

   ```bash
   python -m venv venv
   # Windows (PowerShell):
   venv\Scripts\Activate.ps1
   # macOS/Linux:
   source venv/bin/activate

   pip install -r requirements.txt
   ```

3. **Create the database table.** In the Supabase dashboard, open **SQL Editor → New
   query**, paste the contents of [schema.sql](schema.sql), and click **Run**.

4. **Add your secrets.** Copy `.env.example` to `.env` and fill in your values:

   ```
   SUPABASE_URL=https://your-project-ref.supabase.co
   SUPABASE_SERVICE_KEY=your-service-key
   ```

   `.env` is git-ignored — it never gets committed.

## Run it locally

```bash
# Test on just the 5 newest items:
python crawler/rbi_notifications.py --limit 5
python crawler/sebi_circulars.py --limit 5

# Full run:
python crawler/rbi_notifications.py
python crawler/sebi_circulars.py

# SEBI also supports a date window (e.g. last 3 weeks), handy for backfills:
python crawler/sebi_circulars.py --days 21
```

You'll see a live log of each item being downloaded, extracted, and either inserted or
skipped (because we already had it), then a summary line at the end.

## Email distribution (built, not yet active)

`distribution/send_brief.py` is the code that will eventually email the daily brief to
subscribers. **It is deliberately inert:** it will not send anything unless you both
provide SMTP credentials *and* pass `--send`. Until then it only previews.

```bash
# Preview the brief for one address (sends nothing; bypasses the subscribers table):
python distribution/send_brief.py --test-recipient you@example.com

# Widen the window if nothing was crawled in the last day:
python distribution/send_brief.py --test-recipient you@example.com --since-hours 2000
```

Notes:
- The brief content is currently a **placeholder** that lists recently-crawled documents.
  The real AI-generated summary is a later phase (Day 6); it will replace one function
  (`build_placeholder_brief`).
- **To activate it later:** (1) run the `subscribers` table SQL in [schema.sql](schema.sql);
  (2) fill in the `SMTP_*` / `FROM_EMAIL` / `SITE_URL` values in `.env` (and add them as
  GitHub secrets); (3) add a step to the workflow that runs `send_brief.py --send`.

## Daily automation (GitHub Actions)

The workflow at [.github/workflows/crawl.yml](.github/workflows/crawl.yml) runs the
crawler automatically:

- **Daily at 00:30 UTC (6:00 AM IST).**
- Or on demand: **Actions** tab → **Daily regulator crawl** → **Run workflow**.
- Runs both crawlers sequentially (RBI, then SEBI). Distribution is **not** in the
  workflow yet — that's a deliberate Day-6 step.

For automation to work, add your two secrets to the GitHub repository:
**Settings → Secrets and variables → Actions → New repository secret**, adding
`SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.

## Project layout

```
crawler/
  common.py                # Shared logic (download, extract, de-dup, insert)
  rbi_notifications.py     # Crawler #1 (RBI)
  sebi_circulars.py        # Crawler #2 (SEBI)
distribution/
  send_brief.py            # Email the daily brief (built, not yet active)
.github/workflows/
  crawl.yml                # Daily automation (both crawlers)
schema.sql                 # Database tables (documents, subscribers)
requirements.txt           # Python dependencies
.env                       # Your secrets (not committed)
CLAUDE.md                  # Project context / notes
```
