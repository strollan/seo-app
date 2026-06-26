from __future__ import annotations

import re
from urllib.parse import urlparse

BAD_VALUES = {"", "not found", "none", "null", "nan", "n/a", "unknown", "manual", "?"}


def clean_value(value) -> str:
    value = str(value or "").strip()
    if value.lower() in BAD_VALUES:
        return ""
    return value


def get_value(row, *keys: str) -> str:
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            value = ""
        value = clean_value(value)
        if value:
            return value
    return ""


def normalize_domain(value: str) -> str:
    value = clean_value(value).lower()
    if not value:
        return ""

    if "://" not in value and "/" not in value:
        host = value
    else:
        parsed = urlparse(value if "://" in value else "https://" + value)
        host = parsed.netloc or parsed.path.split("/")[0]

    return host.replace("www.", "").strip().strip("/")


def has_real_phone(value: str) -> bool:
    digits = re.sub(r"\D+", "", clean_value(value))
    return len(digits) >= 7


def clean_emails(value, lead_domain: str = "", website: str = "") -> str:
    try:
        from agents.lead_email_cleaner_agent import clean_lead_emails
        return clean_lead_emails(value, lead_domain=lead_domain, website=website)
    except Exception:
        pass

    raw = value or ""
    if isinstance(raw, list):
        raw = ", ".join(str(x) for x in raw)

    found = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", str(raw))
    return ", ".join(dict.fromkeys(e.lower().strip() for e in found))


def email_matches_domain(emails: str, lead_domain: str = "", website: str = "") -> bool:
    domain = normalize_domain(lead_domain) or normalize_domain(website)
    if not domain or not emails:
        return False

    root = ".".join(domain.split(".")[-2:])

    for email in emails.split(","):
        email = email.strip().lower()
        if "@" not in email:
            continue
        email_domain = email.rsplit("@", 1)[-1]
        if email_domain == domain or email_domain.endswith("." + domain) or email_domain.endswith(root):
            return True

    return False


def has_real_contact_page(value: str) -> bool:
    value = clean_value(value)
    if not value:
        return False
    if value.lower().startswith("mailto:"):
        return False
    return value.startswith("http") or "contact" in value.lower()


def has_real_address(value: str) -> bool:
    return len(clean_value(value)) >= 8


def calculate_contact_confidence(row) -> int:
    website = get_value(row, "website", "url", "link", "homepage")
    domain = get_value(row, "domain") or normalize_domain(website)

    phone = get_value(row, "best_phone", "phone", "phone_number", "telephone", "tel")
    emails_raw = get_value(row, "emails", "email", "email_1")
    contact_page = get_value(row, "contact_page_url", "contact_page")
    address = get_value(
        row,
        "address",
        "full_address",
        "business_address",
        "formatted_address",
        "street_address",
        "place_address",
    )

    phone_ok = has_real_phone(phone)
    emails = clean_emails(emails_raw, lead_domain=domain, website=website)
    email_ok = bool(emails)
    email_domain_ok = email_matches_domain(emails, lead_domain=domain, website=website)
    contact_ok = has_real_contact_page(contact_page)
    address_ok = has_real_address(address)
    website_ok = bool(normalize_domain(website) or normalize_domain(domain))

    if not any([phone_ok, email_ok, contact_ok, address_ok, website_ok]):
        return 0

    score = 0
    if website_ok:
        score += 5
    if phone_ok:
        score += 35
    if email_ok:
        score += 30
    if email_domain_ok:
        score += 10
    if contact_ok:
        score += 10
    if address_ok:
        score += 10

    if phone_ok and email_ok and contact_ok and address_ok:
        score = max(score, 93)
    elif phone_ok and email_ok:
        score = max(score, 90)
    elif phone_ok and contact_ok and address_ok:
        score = max(score, 85)
    elif phone_ok or email_ok:
        score = max(score, 80)
    elif contact_ok:
        score = max(score, 70)
    elif website_ok:
        score = max(score, 50)

    return max(0, min(int(score), 93))


def apply_contact_confidence(row: dict) -> dict:
    row["contact_confidence"] = calculate_contact_confidence(row)
    return row


def cap_contact_confidence(value, cap: int = 93) -> int:
    try:
        number = int(float(str(value or 0).strip()))
    except Exception:
        number = 0
    return max(0, min(number, cap))
