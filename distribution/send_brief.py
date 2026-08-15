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

The brief content comes from `distribution/generate_brief.py`, which builds the real
briefing from classified documents (severity + summary). The subscriber list lives in the
`subscribers` table (schema.sql) — but you don't need it to preview: use --test-recipient.

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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from dotenv import load_dotenv
from supabase import create_client

import generate_brief  # sibling module: builds the brief content from classified docs


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
    brief = generate_brief.build_brief(client, args.since_hours)
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
