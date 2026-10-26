"""Sends the brief by email.

Uses Resend's HTTPS API when RESEND_API_KEY is set (works on hosts that block
outbound SMTP, such as Railway below the Pro plan), otherwise plain SMTP, which
works with Gmail, Microsoft 365, Brevo, SendGrid and Amazon SES."""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import requests

from .sources import require_env


def send_email(subject: str, html: str, text: str, recipients: list[str]) -> None:
    if os.getenv("RESEND_API_KEY"):
        _send_via_resend(subject, html, text, recipients)
    else:
        _send_via_smtp(subject, html, text, recipients)


def _send_via_resend(subject: str, html: str, text: str, recipients: list[str]) -> None:
    log = logging.getLogger(__name__)
    log.info("Sending via Resend API...")
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": require_env("REPORT_FROM"), "to": recipients,
              "subject": subject, "html": html, "text": text},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend rejected the email ({resp.status_code}): {resp.text[:300]}")
    log.info("Email sent to %s", ", ".join(recipients))


def _send_via_smtp(subject: str, html: str, text: str, recipients: list[str]) -> None:
    host = require_env("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    sender = require_env("REPORT_FROM")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip(">"))
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    log = logging.getLogger(__name__)
    log.info("Connecting to %s:%s...", host, port)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        if username:
            server.login(username, password)
        server.send_message(msg)
    log.info("Email sent to %s", ", ".join(recipients))
