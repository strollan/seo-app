"""
DataForSEO Business Listings Agent

Google-free address/contact enrichment.

Purpose:
- Query DataForSEO Business Listings live endpoint
- Match returned business listings to existing LeadBot organic leads
- Fill address / phone / website when confidence is strong
- Never touches Google Cloud
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ENDPOINT = "https://api.dataforseo.com/v3/business_data/business_listings/search/live"


@dataclass
class BusinessListingMatch:
    matched: bool
    score: float
    reason: str
    title: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    domain: str = ""
    rating_value: str = ""
    rating_votes: str = ""
    category: str = ""
    place_id: str = ""
    cid: str = ""
    source: str = "dataforseo_business_listings"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return ""


def dataforseo_auth_header() -> str:
    login = env_first("DATAFORSEO_LOGIN", "DATAFORSEO_USERNAME", "DATAFORSEO_USER")
    password = env_first("DATAFORSEO_PASSWORD", "DATAFORSEO_API_PASSWORD", "DATAFORSEO_PASS")

    if not login or not password:
        raise RuntimeError(
            "Missing DataForSEO credentials. Expected DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD."
        )

    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def normalize_domain(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path

    value = value.replace("www.", "")
    value = value.split("/")[0]
    value = value.split("?")[0]
    value = value.strip(". ")
    return value


def normalize_name(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(
        r"\b(llc|inc|co|company|corp|corporation|restaurant|dispensary|store|shop|the)\b",
        " ",
        value,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def name_similarity(a: str, b: str) -> float:
    a2 = normalize_name(a)
    b2 = normalize_name(b)

    if not a2 or not b2:
        return 0.0

    ratio = SequenceMatcher(None, a2, b2).ratio()

    a_words = set(a2.split())
    b_words = set(b2.split())
    overlap = 0.0
    if a_words and b_words:
        overlap = len(a_words & b_words) / max(1, len(a_words | b_words))

    return max(ratio, overlap)


def market_tokens(market: str) -> List[str]:
    tokens = []
    for part in re.split(r"[\s,]+", str(market or "").lower()):
        part = part.strip()
        if len(part) >= 4:
            tokens.append(part)
    return tokens


def pick_address(item: Dict[str, Any]) -> str:
    for key in ["address", "full_address", "formatted_address"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    address_info = item.get("address_info")
    if isinstance(address_info, dict):
        parts = []
        for key in ["address", "street", "city", "region", "zip", "postal_code", "country_code"]:
            value = address_info.get(key)
            if value:
                parts.append(str(value).strip())
        if parts:
            return ", ".join(parts)

    return ""


def pick_phone(item: Dict[str, Any]) -> str:
    for key in ["phone", "phone_number", "main_phone"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    contact_info = item.get("contact_info")
    if isinstance(contact_info, dict):
        for key in ["phone", "phone_number", "main_phone"]:
            value = contact_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    contacts = item.get("contacts")
    if isinstance(contacts, dict):
        for key in ["phone", "phone_numbers", "main_phone"]:
            value = contacts.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                return str(value[0]).strip()

    return ""


def pick_website(item: Dict[str, Any]) -> str:
    for key in ["url", "website", "domain", "site"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def pick_rating_value(item: Dict[str, Any]) -> str:
    rating = item.get("rating")
    if isinstance(rating, dict):
        value = rating.get("value")
        if value is not None:
            return str(value)
    if rating is not None:
        return str(rating)
    return ""


def pick_rating_votes(item: Dict[str, Any]) -> str:
    rating = item.get("rating")
    if isinstance(rating, dict):
        value = rating.get("votes_count")
        if value is not None:
            return str(value)
    return ""


def extract_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    tasks = data.get("tasks") or []
    items: List[Dict[str, Any]] = []

    for task in tasks:
        results = task.get("result") or []
        for result in results:
            result_items = result.get("items") or []
            for item in result_items:
                if isinstance(item, dict):
                    items.append(item)

    return items


def request_business_listings(
    category: str,
    location_coordinate: str,
    limit: int = 50,
    title: str = "",
    description: str = "",
    is_claimed: Optional[bool] = None,
) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "categories": [category],
        "location_coordinate": location_coordinate,
        "limit": int(limit),
    }

    if title:
        task["title"] = title

    if description:
        task["description"] = description

    if is_claimed is not None:
        task["is_claimed"] = bool(is_claimed)

    payload = json.dumps([task]).encode("utf-8")

    req = Request(
        ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": dataforseo_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DataForSEO HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"DataForSEO URL error: {exc}") from exc


def lead_title(row: Dict[str, Any]) -> str:
    return (
        row.get("business_name")
        or row.get("company")
        or row.get("title")
        or row.get("name")
        or ""
    )


def lead_domain(row: Dict[str, Any]) -> str:
    for key in ["domain", "website", "url", "link", "domain_final_url", "domain_checked_url"]:
        d = normalize_domain(row.get(key, ""))
        if d:
            return d
    return ""


def listing_to_match(item: Dict[str, Any]) -> BusinessListingMatch:
    title = str(item.get("title") or item.get("name") or "").strip()
    website = pick_website(item)
    domain = normalize_domain(item.get("domain") or website)
    rating_value = pick_rating_value(item)
    rating_votes = pick_rating_votes(item)

    return BusinessListingMatch(
        matched=False,
        score=0,
        reason="candidate",
        title=title,
        address=pick_address(item),
        phone=pick_phone(item),
        website=website,
        domain=domain,
        rating_value=rating_value,
        rating_votes=rating_votes,
        category=str(item.get("category") or ""),
        place_id=str(item.get("place_id") or ""),
        cid=str(item.get("cid") or ""),
    )


def score_listing_for_lead(
    row: Dict[str, Any],
    item: Dict[str, Any],
    market: str = "",
) -> BusinessListingMatch:
    lead_name = lead_title(row)
    lead_dom = lead_domain(row)

    candidate = listing_to_match(item)
    candidate_name = candidate.title
    candidate_dom = normalize_domain(candidate.domain or candidate.website)
    candidate_address = candidate.address or ""

    score = 0.0
    reasons: List[str] = []

    sim = name_similarity(lead_name, candidate_name)
    if sim:
        score += sim * 45
        reasons.append(f"name={sim:.2f}")

    if lead_dom and candidate_dom:
        if lead_dom == candidate_dom:
            score += 55
            reasons.append("domain_exact")
        elif lead_dom in candidate_dom or candidate_dom in lead_dom:
            score += 35
            reasons.append("domain_partial")

    address_l = candidate_address.lower()
    for token in market_tokens(market):
        if token in address_l:
            score += 5
            reasons.append(f"market:{token}")

    if candidate.address:
        score += 8
        reasons.append("has_address")

    if candidate.phone:
        score += 4
        reasons.append("has_phone")

    # Strong enough if domain matches, or name is very close with market/address.
    matched = score >= 58

    # If no domain match, require stronger name similarity.
    if lead_dom and candidate_dom and lead_dom != candidate_dom and not (lead_dom in candidate_dom or candidate_dom in lead_dom):
        if sim < 0.72:
            matched = False
            reasons.append("weak_name_no_domain")

    if not candidate.address:
        matched = False
        reasons.append("no_address")

    candidate.matched = bool(matched)
    candidate.score = round(score, 2)
    candidate.reason = ";".join(reasons) or "no_score"
    return candidate


def best_match_for_lead(
    row: Dict[str, Any],
    listings: List[Dict[str, Any]],
    market: str = "",
) -> BusinessListingMatch:
    scored = [score_listing_for_lead(row, item, market=market) for item in listings]
    scored = sorted(scored, key=lambda x: x.score, reverse=True)

    if not scored:
        return BusinessListingMatch(
            matched=False,
            score=0,
            reason="no_candidates",
        )

    best = scored[0]
    if not best.matched:
        best.reason = "below_threshold;" + best.reason
    return best


def row_has_address(row: Dict[str, Any]) -> bool:
    for key in ["address", "full_address", "business_address", "formatted_address"]:
        if str(row.get(key) or "").strip():
            return True
    return False


def safe_filename_piece(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "business_listings"


def apply_match_to_row(row: Dict[str, Any], match: BusinessListingMatch) -> None:
    if not match.matched:
        return

    for key in ["address", "full_address", "business_address", "formatted_address"]:
        row[key] = match.address

    row["address_source"] = match.source
    row["address_status"] = "found"

    if match.phone:
        if not str(row.get("phone") or "").strip():
            row["phone"] = match.phone
        if not str(row.get("best_phone") or "").strip():
            row["best_phone"] = match.phone

    if match.website:
        if not str(row.get("website") or "").strip():
            row["website"] = match.website

    row["business_listing_title"] = match.title
    row["business_listing_rating"] = match.rating_value
    row["business_listing_votes"] = match.rating_votes
    row["business_listing_place_id"] = match.place_id
    row["business_listing_cid"] = match.cid
    row["business_listing_match_score"] = str(match.score)
    row["business_listing_match_reason"] = match.reason


def enrich_export_copy(
    export_path: str | Path,
    category: str,
    location_coordinate: str,
    limit: int = 50,
    max_rows: int = 20,
    output_path: str | Path | None = None,
) -> Dict[str, Any]:
    export_path = Path(export_path)

    with export_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    data = request_business_listings(
        category=category,
        location_coordinate=location_coordinate,
        limit=limit,
    )
    listings = extract_items(data)

    extra_fields = [
        "address",
        "full_address",
        "business_address",
        "formatted_address",
        "address_source",
        "address_status",
        "phone",
        "best_phone",
        "website",
        "business_listing_title",
        "business_listing_rating",
        "business_listing_votes",
        "business_listing_place_id",
        "business_listing_cid",
        "business_listing_match_score",
        "business_listing_match_reason",
    ]

    for field in extra_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    checked = 0
    updated = 0
    matches: List[Dict[str, Any]] = []

    for row in rows:
        if checked >= max_rows:
            break

        if row_has_address(row):
            continue

        checked += 1
        match = best_match_for_lead(row, listings, market=row.get("market") or "")

        matches.append({
            "lead_title": lead_title(row),
            "lead_domain": lead_domain(row),
            **match.to_dict(),
        })

        if match.matched:
            apply_match_to_row(row, match)
            updated += 1

    if output_path is None:
        category_piece = safe_filename_piece(category)
        output_path = export_path.with_name(
            export_path.stem + f"_business_listings_{category_piece}.csv"
        )
    output_path = Path(output_path)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "input": str(export_path),
        "output": str(output_path),
        "api_status_code": data.get("status_code"),
        "api_status_message": data.get("status_message"),
        "api_cost": data.get("cost"),
        "listings_returned": len(listings),
        "rows_checked": checked,
        "rows_updated": updated,
        "matches": matches,
    }
