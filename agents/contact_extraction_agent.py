import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agents.crawl_agent import crawl_get


EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.I,
)

PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}"
)

BAD_EMAIL_HINTS = {
    "example.com",
    "domain.com",
    "email.com",
    "sentry",
    "wixpress",
    "wordpress",
    "schema",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "gif",
}

LONG_ISLAND_AREA_CODES = {"516", "631", "934"}


def area_code(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 3:
        return digits[:3]
    return ""


def choose_best_phone(phones, market=""):
    phones = phones or []
    market_l = (market or "").lower()

    if not phones:
        return ""

    if "long island" in market_l or "nassau" in market_l or "suffolk" in market_l:
        local = [p for p in phones if area_code(p) in LONG_ISLAND_AREA_CODES]
        if local:
            return local[0]

    return phones[0]


CONTACT_LINK_HINTS = {
    "contact",
    "contact-us",
    "request",
    "quote",
    "estimate",
    "free-estimate",
    "get-a-quote",
}


def clean_phone(phone):
    phone = re.sub(r"\s+", " ", phone or "").strip()
    return phone


def clean_email(email):
    return (email or "").strip().lower()


def is_good_email(email):
    email = clean_email(email)

    if not email or "@" not in email:
        return False

    if any(bad in email for bad in BAD_EMAIL_HINTS):
        return False

    return True


def extract_emails(text):
    emails = EMAIL_RE.findall(text or "")
    cleaned = []

    for email in emails:
        email = clean_email(email)
        if is_good_email(email) and email not in cleaned:
            cleaned.append(email)

    return cleaned


def extract_phones(text):
    phones = PHONE_RE.findall(text or "")
    cleaned = []

    for phone in phones:
        phone = clean_phone(phone)
        digits = re.sub(r"\D", "", phone)

        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]

        if len(digits) != 10:
            continue

        normalized = f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"

        if normalized not in cleaned:
            cleaned.append(normalized)

    return cleaned




def extract_tel_link_phones(html):
    soup = BeautifulSoup(html or "", "html.parser")
    phones = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "") or ""

        if not href.lower().startswith("tel:"):
            continue

        raw = href.split(":", 1)[1]
        found = extract_phones(raw)

        for phone in found:
            if phone not in phones:
                phones.append(phone)

    return phones


def filter_realistic_phones(phones, market=""):
    phones = phones or []
    market_l = (market or "").lower()

    allowed_area_codes = set()

    if "long island" in market_l or "nassau" in market_l or "suffolk" in market_l:
        allowed_area_codes.update({"516", "631", "934"})

    # Keep common toll-free numbers too.
    allowed_toll_free = {"800", "833", "844", "855", "866", "877", "888"}

    cleaned = []

    for phone in phones:
        digits = re.sub(r"\D", "", phone or "")

        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]

        if len(digits) != 10:
            continue

        area = digits[:3]
        exchange = digits[3:6]
        line = digits[6:]

        # reject obvious placeholders / fake patterns
        if area in {"000", "111", "222", "333", "444", "555", "666", "777", "999"}:
            continue

        if exchange in {"000", "111", "222", "333", "444", "555", "666", "777", "999"}:
            continue

        if line == "0000":
            continue

        if allowed_area_codes:
            if area not in allowed_area_codes and area not in allowed_toll_free:
                continue

        normalized = f"({area}) {exchange}-{line}"

        if normalized not in cleaned:
            cleaned.append(normalized)

    return cleaned


def find_contact_page_url(base_url, html):
    soup = BeautifulSoup(html or "", "html.parser")

    candidates = []

    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True).lower()
        href_l = href.lower()

        combined = f"{href_l} {text}"

        if any(hint in combined for hint in CONTACT_LINK_HINTS):
            candidates.append(urljoin(base_url, href))

    return candidates[0] if candidates else ""


def extract_contact_from_url(url, market=""):
    result = {
        "url": url,
        "contact_page_url": "",
        "emails": [],
        "phones": [],
        "best_phone": "",
        "confidence": 0,
        "flags": [],
    }

    if not url:
        result["flags"].append("missing_url")
        return result

    page = crawl_get(url)
    html = page.text or ""

    if not html:
        result["flags"].append("empty_homepage")
        return result

    homepage_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    emails = extract_emails(html + " " + homepage_text)
    phones = extract_tel_link_phones(html) + extract_phones(homepage_text)

    contact_url = find_contact_page_url(url, html)
    result["contact_page_url"] = contact_url

    if contact_url:
        contact_page = crawl_get(contact_url)
        contact_html = contact_page.text or ""
        contact_text = BeautifulSoup(contact_html, "html.parser").get_text(" ", strip=True)

        emails += extract_emails(contact_html + " " + contact_text)
        phones += extract_tel_link_phones(contact_html) + extract_phones(contact_text)

    result["emails"] = list(dict.fromkeys([e for e in emails if is_good_email(e)]))
    result["phones"] = filter_realistic_phones(list(dict.fromkeys([p for p in phones if p])), market)
    result["best_phone"] = choose_best_phone(result["phones"], market)

    confidence = 0

    if result["phones"]:
        confidence += 45

    if result["emails"]:
        confidence += 35

    if result["contact_page_url"]:
        confidence += 15

    if not result["phones"]:
        result["flags"].append("no_phone_found")

    if not result["emails"]:
        result["flags"].append("no_email_found")

    result["confidence"] = min(100, confidence)

    return result
