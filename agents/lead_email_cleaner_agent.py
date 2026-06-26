"""
LeadBot Email Cleaner

Keeps useful business emails.
Drops junk/system/tracking emails, malformed addresses, character-soup strings,
and vendor/internal addresses like sentry.wixpress.com.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List


EMAIL_RE = re.compile(
    r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)

HEX_RE = re.compile(r"^[a-f0-9]{20,}$", re.IGNORECASE)

BAD_DOMAINS = {
    "sentry.io",
    "sentry.wixpress.com",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "email.com",
    "test.com",
    "localhost.com",
}

BAD_DOMAIN_PARTS = (
    "sentry.",
    "wixpress",
    "cloudflare",
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook",
    "fbcdn",
    "schema.org",
)

BAD_LOCAL_EXACT = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "no_reply",
    "mailer-daemon",
    "postmaster",
    "root",
    "admin@example",
    "test",
    "example",
    "null",
    "undefined",
}

BAD_LOCAL_PARTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "bounce",
    "bounces",
    "notification",
    "notifications",
    "sentry",
    "wix",
    "tracking",
    "analytics",
    "abuse",
    "privacy",
    "terms",
    "legal",
)

GOOD_GENERIC_LOCALS = {
    "info",
    "hello",
    "contact",
    "sales",
    "support",
    "service",
    "office",
    "admin",
    "team",
    "help",
    "booking",
    "reservations",
    "orders",
    "careers",
    "jobs",
}


def _flatten(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)

    return str(value)


def _rebuild_character_soup(value: str) -> str:
    """
    Rebuild emails that were split into character soup:
    6, 5, 0, a, @, s, e, n, t, r, y, ., i, o
    """
    text = _flatten(value)

    # Keep original plus a compact version that removes CSV/list noise.
    compact = re.sub(r"[^A-Za-z0-9@._%+\-']", "", text)

    # Also handle spaced/comma character soup more aggressively.
    pieces = re.findall(r"[A-Za-z0-9@._%+\-']", text)
    joined = "".join(pieces)

    extras = []
    if "@" in compact and "." in compact:
        extras.append(compact)
    if "@" in joined and "." in joined and joined != compact:
        extras.append(joined)

    if extras:
        return text + " " + " ".join(extras)

    return text


def _split_candidates(value: Any) -> List[str]:
    text = _rebuild_character_soup(_flatten(value))

    candidates = EMAIL_RE.findall(text)

    # Also handle simple separators for already-clean values.
    for piece in re.split(r"[\s,;|<>\"()\[\]{}]+", text):
        piece = piece.strip().strip(".,;:")
        if "@" in piece and "." in piece:
            candidates.extend(EMAIL_RE.findall(piece))

    return candidates


def _domain_root(domain: str) -> str:
    parts = [p for p in domain.lower().split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain.lower()


def _looks_like_hash_email(local: str) -> bool:
    clean = re.sub(r"[^a-zA-Z0-9]", "", local or "")
    if len(clean) >= 20 and HEX_RE.match(clean):
        return True

    # Long random-looking local parts are almost never useful business emails.
    if len(clean) >= 28:
        letters = sum(c.isalpha() for c in clean)
        digits = sum(c.isdigit() for c in clean)
        if letters >= 8 and digits >= 8:
            return True

    return False


def _is_good_email(email: str) -> bool:
    email = (email or "").strip().lower().strip(".,;:'\"()[]{}<>")

    if not email or "@" not in email:
        return False

    if len(email) > 120:
        return False

    if email.count("@") != 1:
        return False

    local, domain = email.split("@", 1)
    local = local.strip()
    domain = domain.strip().strip(".")

    if not local or not domain or "." not in domain:
        return False

    if len(local) < 2 or len(domain) < 4:
        return False

    if domain in BAD_DOMAINS:
        return False

    if _domain_root(domain) in BAD_DOMAINS:
        return False

    if any(part in domain for part in BAD_DOMAIN_PARTS):
        return False

    if local in BAD_LOCAL_EXACT:
        return False

    # Keep normal generic business inboxes, unless the domain itself is bad.
    if local in GOOD_GENERIC_LOCALS:
        return True

    if any(part in local for part in BAD_LOCAL_PARTS):
        return False

    if _looks_like_hash_email(local):
        return False

    # Drop obvious placeholder/dev/build artifacts.
    if re.search(r"(localhost|webpack|sentry|wixpress|example|undefined|null)", email):
        return False

    # Require a normal-ish domain ending.
    tld = domain.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[a-z]{2,24}", tld):
        return False

    return True


def clean_email_field(value: Any) -> str:
    """
    Return a comma-separated list of cleaned emails.
    Compatibility wrapper used by older LeadBot code.
    """
    found: List[str] = []

    for raw in _split_candidates(value):
        email = raw.strip().lower().strip(".,;:'\"()[]{}<>")
        if _is_good_email(email) and email not in found:
            found.append(email)

    return ", ".join(found)


def clean_lead_emails(lead_or_value: Any, lead_domain: str = "", website: str = "", **kwargs) -> str:
    """
    Clean emails from either:
    - a raw email string/list
    - a lead row dict containing emails/email/contact_email fields
    """
    if isinstance(lead_or_value, dict):
        values: List[Any] = []

        for key in (
            "emails",
            "email",
            "best_email",
            "contact_email",
            "contact_emails",
            "raw_emails",
        ):
            if lead_or_value.get(key):
                values.append(lead_or_value.get(key))

        return clean_email_field(values)

    return clean_email_field(lead_or_value)


def clean_email_list(values: Iterable[Any]) -> List[str]:
    cleaned = clean_email_field(values)
    if not cleaned:
        return []
    return [x.strip() for x in cleaned.split(",") if x.strip()]
