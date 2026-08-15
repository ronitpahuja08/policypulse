"""
PolicyPulse — Brief generator
=============================

Builds the *content* of the daily compliance brief from documents the classifier has
already processed. It reads the stored `severity`, `entities_affected`, and `summary`
fields — the AI work was done once, at classification time — so building (and re-building)
a brief is cheap and deterministic.

`build_brief(client, since_hours)` returns a `(subject, html_body, text_body)` tuple, which
is exactly what `send_brief.py` expects. That means the sender doesn't care how the brief
is produced; this file owns all the "what goes in the email" logic.

Items are grouped by severity (high → medium → low). Documents we couldn't auto-read
(`needs_review`) are listed at the bottom so nothing is silently hidden.
"""

from datetime import datetime, timedelta, timezone


# Order and display styling for each severity level.
SEVERITY_ORDER = ["high", "medium", "low"]
SEVERITY_LABEL = {"high": "High priority", "medium": "Medium priority", "low": "Low priority"}
SEVERITY_COLOR = {"high": "#c0392b", "medium": "#b9770e", "low": "#5d6d7e"}

SOURCE_LABEL = {"rbi_notifications": "RBI", "sebi_circulars": "SEBI"}


def build_brief(client, since_hours):
    """Return (subject, html_body, text_body) for documents from the last `since_hours`."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = (
        client.table("documents")
        .select("source,title,url,published_date,severity,entities_affected,summary,status")
        .gte("created_at", cutoff)
        .order("published_date", desc=True)
        .execute()
        .data
    )

    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    # Split into classified items (have a severity) and ones needing review (no text read).
    classified = [r for r in rows if r.get("severity")]
    needs_review = [r for r in rows if not r.get("severity") and r.get("status") == "needs_review"]

    # Bucket the classified items by severity.
    by_severity = {level: [] for level in SEVERITY_ORDER}
    for r in classified:
        by_severity.get(r["severity"], by_severity["low"]).append(r)

    high_count = len(by_severity["high"])

    if not classified and not needs_review:
        subject = f"PolicyPulse brief — {today} (no new updates)"
        text_body = "No new regulatory updates in the selected window."
        return subject, _wrap_html(today, f"<p>{text_body}</p>"), text_body

    subject = f"PolicyPulse brief — {today} ({len(classified)} updates, {high_count} high)"

    text_lines = [f"PolicyPulse daily compliance brief — {today}", ""]
    html_sections = []

    # One section per severity level, highest first.
    for level in SEVERITY_ORDER:
        items = by_severity[level]
        if not items:
            continue
        text_lines.append(f"{SEVERITY_LABEL[level].upper()} ({len(items)})")
        html_items = []
        for r in items:
            src = SOURCE_LABEL.get(r["source"], r["source"])
            entities = r.get("entities_affected") or "General"
            summary = r.get("summary") or ""
            date = r.get("published_date") or ""

            text_lines.append(f"  - [{src} {date}] {r['title']}")
            text_lines.append(f"      Affects: {entities}")
            text_lines.append(f"      {summary}")

            html_items.append(
                "<li style='margin:0 0 14px'>"
                f"<a href='{r['url']}' style='font-weight:600;color:#1a1a1a'>{_escape(r['title'])}</a>"
                f"<div style='font-size:12px;color:#888;margin:2px 0'>{src} · {date} · "
                f"Affects: {_escape(entities)}</div>"
                f"<div style='font-size:14px;color:#333'>{_escape(summary)}</div>"
                "</li>"
            )
        text_lines.append("")
        html_sections.append(
            f"<h3 style='margin:22px 0 8px;color:{SEVERITY_COLOR[level]}'>"
            f"{SEVERITY_LABEL[level]} "
            f"<span style='color:#aaa;font-weight:normal'>({len(items)})</span></h3>"
            f"<ul style='margin:0;padding-left:18px;list-style:disc'>{''.join(html_items)}</ul>"
        )

    # Anything we couldn't auto-read, listed plainly so it isn't lost.
    if needs_review:
        text_lines.append(f"NEEDS REVIEW — couldn't auto-read ({len(needs_review)})")
        nr_items = []
        for r in needs_review:
            text_lines.append(f"  - {r['title']}")
            nr_items.append(f"<li><a href='{r['url']}'>{_escape(r['title'])}</a></li>")
        html_sections.append(
            f"<h3 style='margin:22px 0 8px;color:#888'>Needs review "
            f"<span style='font-weight:normal'>({len(needs_review)})</span></h3>"
            f"<p style='font-size:12px;color:#888;margin:0 0 6px'>No readable PDF — check manually.</p>"
            f"<ul style='margin:0;padding-left:18px'>{''.join(nr_items)}</ul>"
        )

    text_body = "\n".join(text_lines)
    html_body = "".join(html_sections)
    return subject, _wrap_html(today, html_body), text_body


def _wrap_html(today, inner_html):
    """Wrap the brief body in a minimal, email-safe template."""
    return (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "max-width:640px;margin:0 auto;color:#1a1a1a'>"
        "<h2 style='margin:0 0 4px'>PolicyPulse</h2>"
        f"<p style='color:#666;margin:0 0 8px'>Daily compliance brief · {today}</p>"
        f"{inner_html}"
        "</div>"
    )


def _escape(text):
    """Very small HTML-escape so titles/summaries can't break the markup."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
