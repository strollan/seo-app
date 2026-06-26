"""
LeadBot Quality Gate v1

Filters and sorts LeadBot candidates so client-facing cards are more likely
to be real local businesses instead of directories, media, social, tourism,
government, education, or junk aggregator results.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


DIRECTORY_ROOTS = {
    "yelp",
    "tripadvisor",
    "yellowpages",
    "angi",
    "angieslist",
    "homeadvisor",
    "bbb",
    "mapquest",
    "manta",
    "chamberofcommerce",
    "opencorporates",
    "bizapedia",
    "buzzfile",
    "dnb",
    "dandb",
    "zoominfo",
    "crunchbase",
    "alignable",
    "nextdoor",
    "houzz",
    "thumbtack",
    "porch",
    "expertise",
    "threebestrated",
    "findglocal",
    "cybo",
    "whereorg",
    "merchantcircle",
    "superpages",
    "localdatabase",
    "citysearch",
    "foursquare",
    "hotfrog",
    "ezlocal",
    "showmelocal",
    "usbusiness",
    "cylex",
}

SOCIAL_ROOTS = {
    "facebook",
    "instagram",
    "linkedin",
    "youtube",
    "youtu",
    "tiktok",
    "twitter",
    "x",
    "reddit",
    "pinterest",
    "threads",
}

INFO_JUNK_ROOTS = {
    "wikipedia",
    "wikidata",
    "wikimedia",
    "indeed",
    "glassdoor",
    "ziprecruiter",
    "salary",
    "amazon",
    "ebay",
    "walmart",
}

NEWS_WORDS = {
    "news",
    "newspaper",
    "magazine",
    "journal",
    "tribune",
    "times",
    "post",
    "daily",
    "gazette",
    "observer",
    "herald",
    "press",
    "media",
    "radio",
    "weather",
    "broadcast",
}

BAD_TLDS = {
    "gov",
    "edu",
}

BUSINESS_POSITIVE_WORDS = {
    "services",
    "service",
    "company",
    "co",
    "llc",
    "inc",
    "group",
    "solutions",
    "pros",
    "repair",
    "plumbing",
    "roofing",
    "dental",
    "law",
    "auto",
    "hvac",
    "electric",
    "construction",
    "cleaning",
    "landscaping",
    "restaurant",
    "store",
    "shop",
    "clinic",
    "contractor",
}


def normalize_domain(value: Any) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.strip(" .,:;()[]{}<>\"'")
    return value


def domain_root(domain: str) -> str:
    domain = normalize_domain(domain)
    if not domain:
        return ""

    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2]

    return parts[0]


def domain_tld(domain: str) -> str:
    domain = normalize_domain(domain)
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-1]
    return ""


def row_domain(row: Dict[str, Any]) -> str:
    for key in (
        "domain",
        "Domain",
        "website",
        "Website",
        "url",
        "URL",
        "site",
        "Site",
        "homepage",
        "Homepage",
    ):
        value = row.get(key)
        if value:
            return normalize_domain(value)
    return ""


def row_text(row: Dict[str, Any]) -> str:
    parts = []
    for key in (
        "name",
        "Name",
        "business_name",
        "Business Name",
        "title",
        "Title",
        "description",
        "Description",
        "snippet",
        "Snippet",
        "category",
        "Category",
        "source",
        "Source",
        "reason",
        "Reason",
        "address",
        "Address",
    ):
        value = row.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def hard_block_reason(lead: Dict[str, Any]) -> str:
    domain = row_domain(lead)
    root = domain_root(domain)
    tld = domain_tld(domain)
    text = row_text(lead)

    if not domain:
        return "missing_domain"

    if tld in BAD_TLDS:
        return f"bad_tld_{tld}"

    if root in DIRECTORY_ROOTS:
        return f"directory_{root}"

    if root in SOCIAL_ROOTS:
        return f"social_{root}"

    if root in INFO_JUNK_ROOTS:
        return f"info_junk_{root}"

    # Ordering / menu platforms are not the business website.
    order_platform_roots = {
        "incentivio",
        "toasttab",
        "square",
        "clover",
        "doordash",
        "ubereats",
        "grubhub",
        "seamless",
    }

    if root in order_platform_roots:
        return f"order_platform_{root}"

    if root.startswith("visit"):
        return "tourism_visit_domain"

    # Conservative civic/tourism/community .org filter.
    # Blocks city/portal orgs like annarbor.org without blocking every .org.
    civic_org_roots = {
        "annarbor",
    }

    civic_org_words = (
        "visit",
        "tourism",
        "downtown",
        "chamber",
        "mainstreet",
        "cityof",
        "county",
        "community",
        "destination",
        "discover",
        "explore",
    )

    if tld == "org" and (root in civic_org_roots or any(word in root for word in civic_org_words)):
        return "civic_org_portal"

    # Known content/media/review domains that are not direct business leads.
    content_roots = {
        "mlive",
        "freep",
        "rubinjen",
        "hourdetroit",
    }

    if root in content_roots:
        return f"content_media_{root}"

    if any(word in root for word in NEWS_WORDS):
        return "news_media_domain"

    # Article/listicle/review titles/pages are usually not business leads.
    content_page_words = (
        "great places",
        "best places",
        "top places",
        "things to do",
        "where to find",
        "where to get",
        "guide to",
        "review of",
        "my review",
        "coming to",
    )

    if any(word in text for word in content_page_words):
        return "content_page_not_business"

    # Common article/list pages are usually not business leads.
    junk_page_words = (
        "top 10",
        "best of",
        "best ",
        "near me",
        "directory",
        "reviews",
        "ranking",
        "ranked",
        "guide",
        "things to do",
    )
    if any(word in text for word in junk_page_words) and root in DIRECTORY_ROOTS:
        return "directory_listicle"

    return ""


def lead_quality_score(lead: Dict[str, Any]) -> int:
    domain = row_domain(lead)
    root = domain_root(domain)
    text = row_text(lead)

    score = 0

    if domain:
        score += 20

    # Real business websites often have branded/simple roots.
    if root and root not in DIRECTORY_ROOTS and root not in SOCIAL_ROOTS:
        score += 20

    # Penalize suspicious generic/info roots even if not hard-blocked.
    if any(word in root for word in NEWS_WORDS):
        score -= 40

    if root.startswith("visit"):
        score -= 50

    # Contactable lead signals.
    for key in ("phone", "Phone", "email", "Email", "contact_page", "Contact Page", "website", "Website"):
        if lead.get(key):
            score += 5

    # Business-ish text/domain clues.
    combined = f"{root} {text}"
    for word in BUSINESS_POSITIVE_WORDS:
        if word in combined:
            score += 4

    # Local/business clues.
    if "address" in lead and lead.get("address"):
        score += 6
    if "Address" in lead and lead.get("Address"):
        score += 6

    # Bad-content penalties.
    bad_text_words = (
        "official tourism",
        "visitor guide",
        "travel guide",
        "local news",
        "breaking news",
        "weather",
        "obituary",
        "jobs",
        "careers",
        "reviews and ratings",
    )
    for word in bad_text_words:
        if word in text:
            score -= 20

    return score


def quality_gate_leads(leads: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Tuple[int, Dict[str, Any]]] = []
    removed = 0
    reasons: Dict[str, int] = {}

    for lead in leads or []:
        if not isinstance(lead, dict):
            continue

        reason = hard_block_reason(lead)
        if reason:
            removed += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue

        score = lead_quality_score(lead)
        kept.append((score, lead))

    kept.sort(key=lambda item: item[0], reverse=True)

    final = [lead for score, lead in kept]

    if removed:
        print(
            f"LEADBOT QUALITY GATE: removed={removed} kept={len(final)} reasons={reasons}",
            flush=True,
        )
    else:
        print(
            f"LEADBOT QUALITY GATE: removed=0 kept={len(final)}",
            flush=True,
        )

    return final


def clean_export_csv_file(path: Path) -> int:
    path = Path(path)

    if not path.exists() or path.suffix.lower() != ".csv":
        return 0

    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not fieldnames:
        return 0

    cleaned = quality_gate_leads(rows)
    removed = len(rows) - len(cleaned)

    if removed:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(cleaned)

    return removed


def clean_all_exports(exports_dir: str = "exports") -> int:
    total_removed = 0
    base = Path(exports_dir)

    if not base.exists():
        return 0

    for csv_path in base.glob("leads_*.csv"):
        removed = clean_export_csv_file(csv_path)
        if removed:
            print(f"LEADBOT QUALITY GATE: removed {removed} from {csv_path}", flush=True)
        total_removed += removed

    return total_removed


# === LEADBOT CIVIC ORG / LISTICLE FILTER START ===
def leadbot_is_civic_org_or_content_junk(lead):
    """
    Conservative junk filter.

    Blocks obvious city/tourism/chamber/community .org portals and
    article/listicle/review pages that are not actual business websites.
    Does NOT block every .org.
    """
    import re
    from urllib.parse import urlparse

    def val(*keys):
        for key in keys:
            v = lead.get(key) if isinstance(lead, dict) else ""
            if v:
                return str(v)
        return ""

    url = val("url", "website", "link")
    title = val("title", "name", "business_name")
    snippet = val("snippet", "description")

    joined = f"{url} {title} {snippet}".lower()

    parsed = urlparse(url if "://" in url else "http://" + url)
    domain = (parsed.netloc or parsed.path).lower()
    domain = domain.replace("www.", "").split("/")[0]

    content_title_patterns = [
        r"\b\d+\s+(best|great|top)\s+(places|spots|restaurants|shops|bagel|bagels)\b",
        r"\bbest\s+(places|spots|restaurants|shops)\b",
        r"\bgreat\s+(places|spots)\b",
        r"\bthings\s+to\s+do\b",
        r"\bwhere\s+to\s+(find|get|eat|buy)\b",
        r"\bguide\s+to\b",
        r"\breview\s+of\b",
        r"\bmy\s+review\b",
        r"\barticle\b",
        r"\bnews\b",
    ]

    if any(re.search(pattern, joined) for pattern in content_title_patterns):
        return True

    civic_org_signals = [
        "visit",
        "tourism",
        "downtown",
        "chamber",
        "mainstreet",
        "main-street",
        "cityof",
        "city-of",
        "county",
        "community",
        "destination",
        "discover",
        "explore",
        "annarbor",
    ]

    if domain.endswith(".org") and any(signal in domain for signal in civic_org_signals):
        return True

    return False
# === LEADBOT CIVIC ORG / LISTICLE FILTER END ===
