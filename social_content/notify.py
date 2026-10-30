"""Emails reviewers. Reuses the daily brief's mailer (Resend or SMTP, REPORT_FROM)."""
import html
import logging
import os

from daily_report.mailer import send_email

from .auth import make_token
from .config import PLATFORMS, TZ

log = logging.getLogger(__name__)


def base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def login_link(email: str, next_path: str = "/") -> str:
    return f"{base_url()}/auth?token={make_token(email)}&next={next_path}"


def send_login_link(email: str) -> None:
    link = login_link(email)
    send_email(
        "Your Barrister Social Studio sign-in link",
        f'<p><a href="{html.escape(link)}">Sign in to Barrister Social Studio</a></p>'
        f"<p>The link works for 7 days. If you didn't ask for it, ignore this email.</p>",
        f"Sign in to Barrister Social Studio: {link}\n\nThe link works for 7 days.",
        [email],
    )


def send_drafts_ready(loc, rows: list[dict]) -> None:
    """One email per reviewer, with a sign-in link that lands on the location's queue."""
    if not loc.reviewers:
        log.warning("%s has no reviewers; drafts are waiting in the app", loc.slug)
        return
    first = min(r["scheduled_at"] for r in rows).astimezone(TZ)
    items = "".join(
        f"<li>{PLATFORMS[r['platform']].label}, {r['scheduled_at'].astimezone(TZ):%a %b %d}: "
        f"{html.escape(r['draft_text'][:90])}…</li>" for r in rows)
    for email in loc.reviewers:
        link = login_link(email, f"/l/{loc.slug}")
        send_email(
            f"{len(rows)} social drafts for {loc.name} need your review",
            f"<p>Claude drafted next week's posts for <b>{html.escape(loc.name)}</b>. Please edit them in your "
            f"own voice and approve by <b>{first:%A %b %d}</b>, when the first one is due.</p>"
            f"<ul>{items}</ul><p><a href=\"{html.escape(link)}\">Review the drafts</a></p>",
            f"{len(rows)} drafts for {loc.name} need review before {first:%A %b %d}.\n\nReview: {link}",
            [email],
        )
