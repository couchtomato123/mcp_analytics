"""Sends the brief over SMTP: works with Gmail, Microsoft 365, Resend, Brevo,
SendGrid and Amazon SES, so no vendor SDK is needed."""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def send_email(subject: str, html: str, text: str, recipients: list[str]) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.environ["REPORT_FROM"]

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
