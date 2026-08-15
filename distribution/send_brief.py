"""
PolicyPulse — Distribution: email the daily brief to subscribers
================================================================

WHAT THIS DOES (plain English)
------------------------------
Eventually (Day 6), this script will:
  1. Read your list of subscribers from Supabase.
  2. Take the day's compliance brief.
  3. Email it to every subscriber, each with their own unsubscribe link.

RIGHT NOW IT IS DELIBERATELY INERT — it does not send anything unless you (a) provide
SMTP email credentials AND (b) pass the explicit --send flag. With no credentials and no
--send, it only *previews* the email (a "dry run"): it builds the message and prints it,
but sends nothing. So simply having this file in the repo does nothing on its own.

Two things are still placeholders until later phases:
  - The brief content itself. The real brief generator is Day 6; for now this script
    builds a simple PLACEHOLDER brief that just lists recently-crawled documents, so we
    can test the email pipeline end to end. `build_placeholder_brief()` gets replaced later.
  - The subscriber list. The `subscribers` table SQL is in schema.sql but you don't have
    to create it until you're ready. Use --test-recipient to preview without it.

HOW TO RUN
----------
    # Preview only (safe, sends nothing). Bypasses the subscribers table:
    python distribution/send_brief.py --test-recipient you@example.com

    # Preview using a wider time window (handy if nothing was crawled in the last day):
    python distribution/send_brief.py --test-recipient you@example.com --since-hours 2000

    # Actually send (ONLY works if SMTP_* env vars are set AND you pass --send):
    python distribution/send_brief.py --send

Config comes from a local .env file (see .env.example). SUPABASE_* are required to read
data; SMTP_* are only needed to actually send.
"""

import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from dotenv import load_dotenv
from supabase import create_client


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

FROM_NAME = "PolicyPulse"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("distribution")


def get_supabase_client():
    """Read Supabase secrets from .env and return a client (exits if missing)."""
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY. See .env.example.")
        sys.exit(1)
    return create_client(url, key)


def get_smtp_config():
    """Return an SMTP config dict if ALL required email vars are set, else None.

    Returning None is how we stay 'inert': no credentials -> we cannot (and will not) send.
    """
    load_dotenv()
    cfg = {
        "host": os.environ.get("SMTP_HOST"),
        "port": os.environ.get("SMTP_PORT"),
        "user": os.environ.get("SMTP_USER"),
        "password": os.environ.get("SMTP_PASS"),
        "from_email": os.environ.get("FROM_EMAIL"),
    }
    if all(cfg.values()):
        return cfg
    return None


def get_site_url():
    """Base URL used to build unsubscribe links. Placeholder until you have a real site."""
    load_dotenv()
    return os.environ.get("SITE_URL", "https://policypulse.example.com")


# --------------------------------------------------------------------------------------
# Subscribers
# --------------------------------------------------------------------------------------

def get_active_subscribers(client):
    """Read active subscribers from the `subscribers` table.

    Returns a list of dicts. Raises if the table doesn't exist yet — the caller handles
    that with a friendly message.
    """
    result = (
        client.table("subscribers")
        .select("email,name,unsubscribe_token")
        .eq("active", True)
        .execute()
    )
    return result.data


# --------------------------------------------------------------------------------------
# The brief content (PLACEHOLDER — replaced by the real generator on Day 6)
# --------------------------------------------------------------------------------------

def build_placeholder_brief(client, since_hours):
    """INTERIM brief: list documents crawled in the last `since_hours` hours, grouped by
    source. Returns (subject, html_body, text_body).

    This is a stand-in so we can test the *email pipeline* before the real brief generator
    exists. The Day-6 generator will replace this one function with proper summaries.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = (
        client.table("documents")
        .select("source,title,published_date,url")
        .gte("created_at", cutoff)
        .order("published_date", desc=True)
        .execute()
        .data
    )

    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    # Group the documents by their source (rbi_notifications, sebi_circulars, ...).
    by_source = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    # Friendly display names for known sources.
    source_names = {
        "rbi_notifications": "RBI — Notifications",
        "sebi_circulars": "SEBI — Circulars",
    }

    if not rows:
        subject = f"PolicyPulse brief — {today} (no new updates)"
        text_body = "No new regulatory updates in the selected window."
        html_body = f"<p>{text_body}</p>"
        return subject, _wrap_html(today, html_body, placeholder=True), text_body

    subject = f"PolicyPulse brief — {today} ({len(rows)} updates)"

    # Build both a plain-text and an HTML version (good email clients pick HTML).
    text_lines = [f"PolicyPulse daily brief — {today}", ""]
    html_sections = []
    for source, docs in by_source.items():
        heading = source_names.get(source, source)
        text_lines.append(f"{heading} ({len(docs)}):")
        html_items = []
        for d in docs:
            date = d.get("published_date") or ""
            text_lines.append(f"  - [{date}] {d['title']}")
            html_items.append(
                f"<li><span style='color:#666'>{date}</span> "
                f"<a href='{d['url']}'>{_escape(d['title'])}</a></li>"
            )
        text_lines.append("")
        html_sections.append(
            f"<h3 style='margin:18px 0 6px'>{_escape(heading)} "
            f"<span style='color:#888;font-weight:normal'>({len(docs)})</span></h3>"
            f"<ul style='margin:0;padding-left:20px;line-height:1.6'>{''.join(html_items)}</ul>"
        )

    text_body = "\n".join(text_lines)
    html_body = "".join(html_sections)
    return subject, _wrap_html(today, html_body, placeholder=True), text_body


def _wrap_html(today, inner_html, placeholder=False):
    """Wrap brief HTML in a minimal, email-safe template."""
    note = (
        "<p style='background:#fff8e1;border:1px solid #ffe082;padding:8px 12px;"
        "border-radius:6px;font-size:13px;color:#8a6d00'>"
        "⚠️ Placeholder brief — this lists recently-crawled documents. The real "
        "AI-generated summary arrives in a later phase.</p>"
        if placeholder
        else ""
    )
    return (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "max-width:640px;margin:0 auto;color:#1a1a1a'>"
        f"<h2 style='margin:0 0 4px'>PolicyPulse</h2>"
        f"<p style='color:#666;margin:0 0 16px'>Daily regulatory brief · {today}</p>"
        f"{note}{inner_html}"
        "</div>"
    )


def _escape(text):
    """Very small HTML-escape for titles (avoids breaking the markup)."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# --------------------------------------------------------------------------------------
# Rendering one subscriber's email
# --------------------------------------------------------------------------------------

def render_email(subscriber, brief, site_url, from_email):
    """Compose the MIME email for one subscriber, including their unsubscribe link."""
    subject, html_body, text_body = brief
    token = subscriber.get("unsubscribe_token", "TEST-TOKEN")
    unsubscribe_url = f"{site_url}/unsubscribe?token={token}"

    # Append the unsubscribe link to both versions (legally required for bulk email).
    text_with_footer = f"{text_body}\n\n---\nUnsubscribe: {unsubscribe_url}"
    html_with_footer = (
        f"{html_body}"
        f"<hr style='margin:24px 0;border:none;border-top:1px solid #eee'>"
        f"<p style='font-size:12px;color:#999'>You're receiving this because you "
        f"subscribed to PolicyPulse. <a href='{unsubscribe_url}'>Unsubscribe</a>.</p>"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_NAME, from_email))
    msg["To"] = subscriber["email"]
    # Header that lets email clients show a one-click unsubscribe button.
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg.attach(MIMEText(text_with_footer, "plain", "utf-8"))
    msg.attach(MIMEText(html_with_footer, "html", "utf-8"))
    return msg


def send_via_smtp(smtp_cfg, msg):
    """Send one message over SMTP (STARTTLS). Only called for real sends."""
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=30) as server:
        server.starttls()
        server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.send_message(msg)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Email the PolicyPulse daily brief.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send emails. Requires SMTP_* env vars. WITHOUT this flag, the "
             "script only previews (dry run) and sends nothing.",
    )
    parser.add_argument(
        "--test-recipient",
        metavar="EMAIL",
        help="Render the brief for this single address, bypassing the subscribers table. "
             "Great for previewing.",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Include documents crawled in the last N hours in the placeholder brief "
             "(default 24).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N subscribers (handy for testing).",
    )
    parser.add_argument(
        "--save-preview",
        metavar="PATH",
        help="Write the first rendered email's HTML to this file (for previewing).",
    )
    args = parser.parse_args()

    client = get_supabase_client()
    smtp_cfg = get_smtp_config()
    site_url = get_site_url()

    # --- Decide whether we're actually sending or just previewing ---
    sending = args.send
    if sending and smtp_cfg is None:
        log.error(
            "--send was passed but SMTP_* credentials are not set. Refusing to send. "
            "Set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/FROM_EMAIL in .env first."
        )
        sys.exit(1)
    if not sending:
        log.info("DRY RUN — previewing only, nothing will be sent. (Pass --send to send.)")

    from_email = smtp_cfg["from_email"] if smtp_cfg else "brief@policypulse.example.com"

    # --- Build the brief once (same content for everyone) ---
    brief = build_placeholder_brief(client, args.since_hours)
    log.info("Brief subject: %s", brief[0])

    # --- Work out the recipient list ---
    if args.test_recipient:
        recipients = [{"email": args.test_recipient, "name": None, "unsubscribe_token": "TEST-TOKEN"}]
        log.info("Using single test recipient: %s", args.test_recipient)
    else:
        try:
            recipients = get_active_subscribers(client)
        except Exception as exc:  # noqa: BLE001 — most likely the table doesn't exist yet
            log.error(
                "Could not read the 'subscribers' table (%s). Create it with the SQL in "
                "schema.sql, or use --test-recipient EMAIL to preview without it.",
                exc,
            )
            sys.exit(1)
        if not recipients:
            log.warning("No active subscribers found — nothing to do.")
            return

    if args.limit is not None:
        recipients = recipients[: args.limit]

    # --- Render (and optionally send) per subscriber ---
    counts = {"sent": 0, "previewed": 0, "failed": 0}
    for i, sub in enumerate(recipients):
        try:
            msg = render_email(sub, brief, site_url, from_email)

            if args.save_preview and i == 0:
                # Write the HTML part of the first email so it can be opened in a browser.
                html_part = msg.get_payload()[1].get_payload(decode=True).decode("utf-8")
                with open(args.save_preview, "w", encoding="utf-8") as fh:
                    fh.write(html_part)
                log.info("Saved HTML preview to %s", args.save_preview)

            if sending:
                send_via_smtp(smtp_cfg, msg)
                log.info("SENT to %s", sub["email"])
                counts["sent"] += 1
            else:
                # Dry run: show what WOULD be sent.
                log.info("PREVIEW to %s | subject: %s", sub["email"], msg["Subject"])
                counts["previewed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad recipient must not stop the rest
            log.error("FAILED for %s: %s", sub.get("email"), exc)
            counts["failed"] += 1

    log.info(
        "DONE. sent=%d  previewed=%d  failed=%d  (of %d recipients)",
        counts["sent"],
        counts["previewed"],
        counts["failed"],
        len(recipients),
    )
    if not sending:
        # In dry run, also print the plain-text brief so you can read it in the terminal.
        print("\n" + "=" * 70 + "\nPLAIN-TEXT PREVIEW OF THE BRIEF\n" + "=" * 70)
        print(brief[2])


if __name__ == "__main__":
    main()
