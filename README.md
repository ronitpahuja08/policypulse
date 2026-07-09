# PolicyPulse

Regulatory-intelligence for Indian financial compliance. PolicyPulse crawls Indian
financial regulators daily (starting with the **RBI**), and stores a structured record
of every notification. Later it will classify them and generate a daily compliance
brief for fintech/NBFC compliance officers.

> New here? See [CLAUDE.md](CLAUDE.md) for a fuller plain-English description of the project.

## What works today

- **Crawler #1: RBI notifications** — fetches the RBI notifications listing, downloads
  each notification's PDF, extracts the text, and stores it in Supabase. It de-duplicates
  using a content fingerprint, so re-running only adds genuinely new notifications.

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
# Test on just the 5 newest notifications:
python crawler/rbi_notifications.py --limit 5

# Full run (all notifications on the first listing page):
python crawler/rbi_notifications.py
```

You'll see a live log of each notification being downloaded, extracted, and either
inserted or skipped (because we already had it), then a summary line at the end.

## Daily automation (GitHub Actions)

The workflow at [.github/workflows/crawl.yml](.github/workflows/crawl.yml) runs the
crawler automatically:

- **Daily at 00:30 UTC (6:00 AM IST).**
- Or on demand: **Actions** tab → **Daily RBI crawl** → **Run workflow**.

For automation to work, add your two secrets to the GitHub repository:
**Settings → Secrets and variables → Actions → New repository secret**, adding
`SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.

## Project layout

```
crawler/
  rbi_notifications.py     # Crawler #1 (RBI)
.github/workflows/
  crawl.yml                # Daily automation
schema.sql                 # Database table definition
requirements.txt           # Python dependencies
.env                       # Your secrets (not committed)
CLAUDE.md                  # Project context / notes
```
