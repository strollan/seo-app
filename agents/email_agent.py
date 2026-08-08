"""Provider-neutral transactional email transport.

Sends mail over standard SMTP so swapping providers (Mailgun, Resend, or any
other SMTP-speaking service) is done entirely through SMTP_* environment
variables -- no application code needs to change. Callers own the message
content (subject/body); this module only owns "how to get it to a mail
server."
"""
import os
import smtplib
from email.mime.text import MIMEText

# Placeholder hosts left in .env.example / docs -- treated as "not configured".
_SMTP_PLACEHOLDERS = frozenset({
    "smtp.yourmailprovider.com",
    "smtp.example.com",
    "your-smtp-host",
    "mail.example.com",
})


def is_configured():
    """True once a real SMTP host has been set (not blank/placeholder)."""
    host = os.getenv("SMTP_HOST", "").strip().lower()
    return bool(host) and host not in _SMTP_PLACEHOLDERS


def send_email(to_email, subject, body):
    """Send a plain-text transactional email over SMTP.

    Raises on any SMTP/connection failure -- callers decide whether that is
    fatal to the request or safe to log-and-continue.
    """
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM", smtp_user).strip()

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(from_email, to_email, msg.as_string())
