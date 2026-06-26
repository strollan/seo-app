import csv
import html as html_lib
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def normalize_url(value):
    value = str(value or "").strip()

    if not value:
        return "", ""

    if "://" not in value:
        value = "https://" + value

    parsed = urlparse(value)
    host = parsed.netloc or parsed.path
    host = host.strip().strip("/").lower()

    if not host or "." not in host:
        return "", ""

    clean_domain = host.replace("www.", "")
    return clean_domain, f"{parsed.scheme or 'https'}://{host}".rstrip("/")


def fetch(url):
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=10) as res:
            raw = res.read(500000)
            return raw.decode("utf-8", errors="ignore"), res.geturl()
    except Exception:
        return "", url


def clean_phone(value):
    value = html_lib.unescape(str(value or ""))
    digits = re.sub(r"\D", "", value)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"

    return ""


def strip_html(raw):
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw


def extract_title(html_text, fallback):
    if not html_text:
        return fallback

    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)

    if not match:
        return fallback

    title = html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return title[:140] or fallback


def extract_contact(html_text):
    if not html_text:
        return "", []

    decoded = html_lib.unescape(html_text)

    mailto_hits = re.findall(r'href=["\']mailto:([^"\'?#]+)', decoded, flags=re.I)
    normal_email_hits = re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        decoded,
    )

    emails = sorted(set(mailto_hits + normal_email_hits))
    emails = [
        e.strip()
        for e in emails
        if e.strip()
        and not e.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js"))
        and "example.com" not in e.lower()
        and "domain.com" not in e.lower()
    ]

    phone = ""

    tel_hits = re.findall(r'href=["\']tel:([^"\']+)["\']', decoded, flags=re.I)
    for hit in tel_hits:
        phone = clean_phone(hit)
        if phone:
            break

    if not phone:
        text_only = strip_html(decoded)
        phone_candidates = re.findall(
            r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}",
            text_only,
        )

        for hit in phone_candidates:
            phone = clean_phone(hit)
            if phone:
                break

    return phone, emails[:3]


def discover_contact_links(html_text, current_url, clean_domain):
    if not html_text:
        return []

    links = []
    seen = set()

    for href in re.findall(r'href=["\']([^"\']+)["\']', html_text, flags=re.I):
        href = html_lib.unescape(href).strip()

        if not href or href.startswith("#"):
            continue

        low = href.lower()

        if any(word in low for word in [
            "contact",
            "about",
            "location",
            "locations",
            "office",
            "team",
            "staff",
            "appointment",
            "request",
        ]):
            full = urljoin(current_url, href).split("#")[0].rstrip("/")
            host = urlparse(full).netloc.lower().replace("www.", "")

            if full.startswith("http") and clean_domain in host and full not in seen:
                seen.add(full)
                links.append(full)

    return links[:12]


def manual_add_domain(domain, industry="", market="", keyword="", serp_page="", serp_position=""):
    clean_domain, base_url = normalize_url(domain)

    if not clean_domain:
        raise ValueError("Invalid domain or URL.")

    def clean_rank_value(value):
        value = str(value or "").strip()
        if not value:
            return ""
        if value.lower() in {"manual", "not found", "none", "null", "?"}:
            return ""
        return value

    def find_serp_position_for_domain():
        """
        Best-effort rank lookup using the existing LeadBot SERP finder.
        Saves real page/position only when the target domain is actually found.
        """
        service_keyword = str(keyword or industry or "").strip()
        if not service_keyword:
            return "", ""

        try:
            from agents.lead_finding_agent import find_leads

            result = find_leads(
                industry=industry or service_keyword,
                market=market,
                service_keyword=service_keyword,
                own_domain="",
                limit=40,
            )

            for item in result.get("leads", []):
                item_domain, _ = normalize_url(
                    item.get("domain")
                    or item.get("url")
                    or item.get("website")
                    or ""
                )

                if item_domain == clean_domain:
                    found_page = (
                        item.get("serp_page")
                        or item.get("page")
                        or item.get("page_number")
                        or item.get("result_page")
                        or item.get("google_page")
                    )
                    found_pos = (
                        item.get("serp_position")
                        or item.get("position")
                        or item.get("pos")
                        or item.get("rank")
                        or item.get("rank_position")
                        or item.get("google_position")
                    )
                    return clean_rank_value(found_page), clean_rank_value(found_pos)

        except Exception as exc:
            print(f"LEADBOT MANUAL SERP LOOKUP ERROR: {exc}", flush=True)

        return "", ""

    serp_page = clean_rank_value(serp_page)
    serp_position = clean_rank_value(serp_position)

    if not serp_page or not serp_position:
        found_page, found_position = find_serp_position_for_domain()
        serp_page = serp_page or found_page
        serp_position = serp_position or found_position

    # Only save real SERP numbers. If lookup fails, leave blank.
    serp_page = serp_page or ""
    serp_position = serp_position or ""

    candidates = []
    seen = set()

    for path in ["", "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/locations", "/location", "/appointment"]:
        candidate = (base_url + path).rstrip("/")
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    title = clean_domain
    best_phone = ""
    emails = []
    contact_page_url = ""
    discovered = []

    for candidate in candidates[:4]:
        html_text, final_url = fetch(candidate)

        if html_text and title == clean_domain:
            title = extract_title(html_text, clean_domain)

        if html_text:
            discovered.extend(discover_contact_links(html_text, final_url, clean_domain))

        phone, found_emails = extract_contact(html_text)

        if phone or found_emails:
            best_phone = phone
            emails = found_emails
            contact_page_url = final_url or candidate
            break

    for link in discovered:
        if link not in seen:
            seen.add(link)
            candidates.append(link)

    if not best_phone and not emails:
        for candidate in candidates[4:20]:
            html_text, final_url = fetch(candidate)

            if html_text and title == clean_domain:
                title = extract_title(html_text, clean_domain)

            phone, found_emails = extract_contact(html_text)

            if phone or found_emails:
                best_phone = phone
                emails = found_emails
                contact_page_url = final_url or candidate
                break

    outreach_status = "needs_manual_research"
    if best_phone and emails:
        outreach_status = "email_and_call_ready"
    elif best_phone:
        outreach_status = "call_ready"
    elif emails:
        outreach_status = "email_ready"

    contact_confidence = 90 if best_phone and emails else 80 if best_phone or emails else 0

    lead = {
        "title": title,
        "domain": clean_domain,
        "url": base_url + "/",
        "serp_page": serp_page,
        "serp_position": serp_position,
        "seo_opportunity_score": "manual",
        "best_phone": best_phone,
        "emails": ", ".join(emails),
        "contact_page_url": contact_page_url,
        "outreach_status": outreach_status,
        "score": "manual",
        "final_lead_score": "manual",
        "contact_confidence": str(contact_confidence),
        "contact_flags": "manual_add_domain",
        "reason": "Manually added domain. LeadBot checked the homepage and common contact pages, then saved this business profile.",
    }

    try:
        from agents.lead_business_cache_agent import save_business_from_lead
        save_business_from_lead(lead, enriched=bool(best_phone or emails or contact_page_url))
    except Exception:
        pass

    safe_industry = re.sub(r"[^a-z0-9]+", "_", (industry or keyword or "manual_lead").lower()).strip("_") or "manual_lead"
    safe_market = re.sub(r"[^a-z0-9]+", "_", (market or "market").lower()).strip("_") or "market"

    out_name = f"leads_{safe_industry}_{safe_market}_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_path = Path("exports") / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "title",
        "domain",
        "url",
        "serp_page",
        "serp_position",
        "seo_opportunity_score",
        "best_phone",
        "emails",
        "contact_page_url",
        "outreach_status",
        "score",
        "final_lead_score",
        "contact_confidence",
        "contact_flags",
        "reason",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(lead)

    return out_name, lead
