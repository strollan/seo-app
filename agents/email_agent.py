"""Transactional email transport via the Resend HTTPS API.

Sends mail over HTTPS (POST https://api.resend.com/emails) instead of SMTP.
Production cannot reach outbound SMTP ports (25/465/587 all time out from
the droplet -- likely a provider-level egress block), but outbound HTTPS
(443) is unaffected. Callers own the message content (subject/body); this
module only owns "how to get it to Resend."
"""
import os

import requests

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10


class EmailSendError(RuntimeError):
    """Raised when an email could not be confirmed as accepted by Resend."""


def is_configured():
    """True once a Resend API key has been set (not blank)."""
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def send_email(to_email, subject, body):
    """Send a plain-text transactional email via the Resend HTTPS API.

    Raises EmailSendError on any failure -- missing config, network/timeout,
    or a response that isn't a confirmed acceptance -- so callers can use
    their existing "mail failed" handling. A send is only considered
    successful when Resend responds with an email id.
    """
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        raise EmailSendError("RESEND_API_KEY is not configured")

    from_email = os.getenv("SMTP_FROM", "").strip()
    if not from_email:
        raise EmailSendError("SMTP_FROM (sender address) is not configured")

    try:
        response = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=RESEND_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        raise EmailSendError(f"Resend request failed: {exc}") from exc

    if response.status_code >= 400:
        raise EmailSendError(
            f"Resend rejected the email (status {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise EmailSendError(f"Resend returned a non-JSON response: {exc}") from exc

    if not payload.get("id"):
        raise EmailSendError(f"Resend did not confirm acceptance: {payload}")
