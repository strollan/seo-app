"""DataForSEO Google Maps business discovery for Lead Finder (behind LEADBOT_MAPS_ENABLED).

Why: Google organic results now link through google.com/goto?url=<opaque token>
and DataForSEO reports domain=google.com for every one (supervised scan
2026-11-10: 172 results, 0 usable websites). Maps listings are business records:
name, place_id, cid, phone, address and the website the owner entered.

Endpoints (DataForSEO SERP API, live mode):
- maps:         /v3/serp/google/maps/live/advanced          (items type "maps_search")
- local_finder: /v3/serp/google/local_finder/live/advanced  (items type "local_pack")

Guarantees:
- Nothing is sent unless LEADBOT_MAPS_ENABLED=1, DataForSEO is ON (not Safe
  Mode) AND the shared discovery spend guard allows it
  (agents/discovery_spend_guard.py).
- Every request is reserved against hard limits BEFORE it is sent (calls per
  scan, USD per scan, USD per UTC day, persisted across processes). A request
  with no reported cost is charged at the worst-case ceiling. No retries and
  no fallback: one failure = one stop.
- A website is only ever the listing's own non-Google URL (via
  agents.business_website.resolve_business_website). A listing without one is
  kept with website="" and website_status="missing" -- never guessed.
- Listings are deduplicated by place_id, then cid, then name+phone, then
  name+address.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

ENDPOINTS = {
    "maps": "https://api.dataforseo.com/v3/serp/google/maps/live/advanced",
    "local_finder": "https://api.dataforseo.com/v3/serp/google/local_finder/live/advanced",
}
ITEM_TYPES = {"maps": {"maps_search"}, "local_finder": {"local_pack"}}

SOURCE = "dataforseo_maps"
SOURCE_LABEL = {"maps": "Google Maps", "local_finder": "Google Local Finder"}

STATUS_OK = 20000
STATUS_NO_RESULTS = 40102
STATUS_PARTIAL = 40106

MAX_DEPTH = 100  # hard ceiling, independent of env
DEFAULT_DEPTH = 20

# Spending: one shared, persistent, fail-closed guard for ALL paid discovery
# (Maps and organic). MapsSpendGuard is kept as the historical name.
from agents.discovery_spend_guard import (  # noqa: E402
    DiscoverySpendGuard as MapsSpendGuard,
    LedgerUnavailable,
    SafeModeError,
    SpendLimitExceeded,
)


class MapsDisabledError(RuntimeError):
    pass


class MapsProviderError(RuntimeError):
    pass


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


# -- parsing --------------------------------------------------------------------

def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_phone_key(value: Any) -> str:
    digits = _digits(value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 7 else ""


def _norm_text(value: Any) -> str:
    value = str(value or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _address(item: Dict[str, Any]) -> str:
    value = item.get("address")
    if isinstance(value, str) and value.strip():
        return value.strip()
    info = item.get("address_info")
    if isinstance(info, dict):
        parts = [str(info.get(k)).strip() for k in ("address", "city", "region", "zip") if info.get(k)]
        return ", ".join(p for p in parts if p)
    return ""


def _rating(item: Dict[str, Any]) -> Tuple[str, str]:
    rating = item.get("rating")
    if isinstance(rating, dict):
        value, votes = rating.get("value"), rating.get("votes_count")
        return ("" if value is None else str(value), "" if votes is None else str(votes))
    return ("", "")


def _is_closed_forever(item: Dict[str, Any]) -> bool:
    return _work_status(item).lower() == "closed_forever"


# Listing "websites" that are a profile on someone else's platform. Using one as
# the business website would key every such business to e.g. facebook.com --
# the same collapse google.com/goto caused -- and SEO-audit the platform.
PROFILE_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "yelp.com", "nextdoor.com", "angi.com",
    "angieslist.com", "homeadvisor.com", "thumbtack.com", "houzz.com", "bbb.org",
    "yellowpages.com", "linktr.ee",
)
# Maps features that are places, not businesses (live capture 2026-11-10:
# "Plumbing" / "Plumbing, Kitchen and Bath" with category "Area", no phone).
NON_BUSINESS_CATEGORIES = {"area"}
_TRACKING_PARAM = re.compile(r"^(utm_[a-z_]+|gclid|fbclid|msclkid)$", re.I)


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_profile_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in PROFILE_HOSTS)


def strip_tracking_params(url: str) -> str:
    """Drop utm_* / click-id parameters Google Business Profile links carry."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def has_street_address(address: str) -> bool:
    """True for "12 Main St, ..." -- not for city-level "Shirley, NY 11967"."""
    return bool(re.match(r"^\s*\d+[a-zA-Z-]*\s+\S", str(address or "")))


def _work_status(item: Dict[str, Any]) -> str:
    block = item.get("work_hours") or item.get("work_time")
    if isinstance(block, dict):
        inner = block.get("work_hours") if isinstance(block.get("work_hours"), dict) else block
        return str(inner.get("current_status") or "")
    return ""


def parse_maps_items(
    items: List[Dict[str, Any]],
    keyword: str = "",
    market: str = "",
    mode: str = "maps",
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Turn provider items into Lead-Finder-shaped rows. Returns (rows, skipped counts)."""
    from agents.business_website import resolve_business_website

    allowed_types = ITEM_TYPES[mode]
    rows: List[Dict[str, Any]] = []
    skipped = {"wrong_type": 0, "paid": 0, "closed_forever": 0, "no_name": 0, "not_a_business": 0}

    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in allowed_types:
            # e.g. "maps_paid_item" (ads are their own item type in Maps results)
            skipped["paid" if "paid" in str(item.get("type") or "") else "wrong_type"] += 1
            continue
        if item.get("is_paid"):
            skipped["paid"] += 1
            continue
        if _is_closed_forever(item):
            skipped["closed_forever"] += 1
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            skipped["no_name"] += 1
            continue
        if str(item.get("category") or "").strip().lower() in NON_BUSINESS_CATEGORIES:
            skipped["not_a_business"] += 1
            continue

        site = resolve_business_website(item.get("url") or "", serp_domain=item.get("domain") or "")
        website = strip_tracking_params(site["url"]) if site["url"] else ""
        website_status = site["status"]
        profile_url = ""
        domain = _host_of(website) if website else ""
        if domain and _is_profile_host(domain):
            # A Facebook/Yelp/... page is not the business's own website.
            profile_url, website, domain, website_status = website, "", "", "social_profile"

        rating_value, rating_votes = _rating(item)
        phone = str(item.get("phone") or "").strip()
        address = _address(item)
        street = has_street_address(address)
        place_id = str(item.get("place_id") or "")
        cid = str(item.get("cid") or "")
        # Eligible without a website only when the listing is a stable Maps
        # record AND can be reached or located: verified phone or street address.
        reasons = []
        if not (place_id or cid):
            reasons.append("no_place_id")
        if not (website or phone or street):
            reasons.append("no_website_phone_or_street_address")
        rows.append({
            "title": title,
            "business_name": title,
            "link": website,
            "website": website,
            "domain": domain,
            "website_status": website_status,
            "has_website": bool(website),
            "website_flag": "" if website else "No Website",
            "profile_url": profile_url,
            "phone": phone,
            "address": address,
            "has_street_address": street,
            "place_id": place_id,
            "cid": cid,
            "category": str(item.get("category") or ""),
            "additional_categories": list(item.get("additional_categories") or []),
            "work_status": _work_status(item),
            "lead_eligible": not reasons,
            "ineligible_reasons": reasons,
            "rating_value": rating_value,
            "rating_votes": rating_votes,
            "is_claimed": item.get("is_claimed"),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "snippet": str(item.get("snippet") or item.get("description") or ""),
            "source": SOURCE,
            "lead_source_label": SOURCE_LABEL[mode],
            "maps_position": item.get("rank_group"),
            "rank_absolute": item.get("rank_absolute"),
            "query_used": keyword,
            "market": market,
        })
    return rows, skipped


def listing_key(row: Dict[str, Any]) -> str:
    if row.get("place_id"):
        return "place_id:" + row["place_id"]
    if row.get("cid"):
        return "cid:" + row["cid"]
    name = _norm_text(row.get("title"))
    phone = normalize_phone_key(row.get("phone"))
    if name and phone:
        return f"name_phone:{name}|{phone}"
    address = _norm_text(row.get("address"))
    if name and address:
        return f"name_address:{name}|{address}"
    return ""  # not safely identifiable: never merged


def dedupe_listings(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Keep the first (best-ranked) row per business; fill its blanks from later duplicates.

    Also mark rows whose website domain is shared by another distinct listing
    (chains / multi-location brands) with shared_domain_count, because the
    current domain-keyed Lead Finder pipeline would collapse them into one.
    """
    kept: List[Dict[str, Any]] = []
    # Secondary indexes so a listing seen once with place_id and once without
    # (e.g. maps vs local_finder) still merges when cid / name+phone agree.
    alias: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    for row in rows:
        keys = [k for k in (
            "place_id:" + row["place_id"] if row.get("place_id") else "",
            "cid:" + row["cid"] if row.get("cid") else "",
        ) if k]
        fallback = listing_key({**row, "place_id": "", "cid": ""})
        if fallback:
            keys.append(fallback)
        existing = next((alias[k] for k in keys if k in alias), None)
        if existing is not None:
            ids_conflict = (
                (row.get("place_id") and existing.get("place_id") and row["place_id"] != existing["place_id"])
                or (row.get("cid") and existing.get("cid") and row["cid"] != existing["cid"])
            )
            if not ids_conflict:
                duplicates += 1
                for field, value in row.items():
                    if value not in (None, "", False) and existing.get(field) in (None, "", False):
                        existing[field] = value
                if existing.get("website") and not existing.get("has_website"):
                    existing["has_website"] = True
                for k in keys:
                    alias.setdefault(k, existing)
                continue
        new = dict(row)
        kept.append(new)
        for k in keys:
            alias.setdefault(k, new)

    domain_counts: Dict[str, int] = {}
    for row in kept:
        if row.get("domain"):
            domain_counts[row["domain"]] = domain_counts.get(row["domain"], 0) + 1
    for row in kept:
        row["shared_domain_count"] = domain_counts.get(row.get("domain") or "", 0) if row.get("domain") else 0

    stats = {
        "input": len(rows),
        "unique": len(kept),
        "duplicates_merged": duplicates,
        "with_website": sum(1 for r in kept if r.get("website")),
        "without_website": sum(1 for r in kept if not r.get("website")),
        "with_phone": sum(1 for r in kept if r.get("phone")),
        "with_address": sum(1 for r in kept if r.get("address")),
        "shared_domain_rows": sum(1 for r in kept if r.get("shared_domain_count", 0) > 1),
        "social_profile_only": sum(1 for r in kept if r.get("website_status") == "social_profile"),
        "with_street_address": sum(1 for r in kept if r.get("has_street_address")),
        "lead_eligible": sum(1 for r in kept if r.get("lead_eligible", True)),
        "eligible_without_website": sum(1 for r in kept if r.get("lead_eligible", True) and not r.get("website")),
    }
    return kept, stats


# -- request --------------------------------------------------------------------

def search_google_maps(
    keyword: str,
    market: str,
    guard: MapsSpendGuard,
    depth: Optional[int] = None,
    mode: str = "maps",
    post: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """One guarded request.

    Returns {"rows", "items", "stats", "skipped", "cost", "status", "spend"}.
    `post` defaults to requests.post; tests inject a fake. Raises
    MapsDisabledError, SafeModeError / LedgerUnavailable / SpendLimitExceeded
    (all before sending), InvalidMarketLocationError (before sending),
    InvalidLocationValueError or MapsProviderError. Never retries.
    """
    if os.getenv("LEADBOT_MAPS_ENABLED", "0").strip() != "1":
        raise MapsDisabledError("LEADBOT_MAPS_ENABLED is not 1")
    if os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() != "1":
        raise SafeModeError("DataForSEO is OFF (Safe Mode); no Maps request sent")
    if mode not in ENDPOINTS:
        raise ValueError(f"unknown mode {mode!r}")
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("keyword required")

    from agents.dataforseo_serp_agent import (
        InvalidLocationValueError,
        _LEADBOT_DATAFORSEO_INVALID_LOCATION_TASK_STATUS_CODE as STATUS_INVALID_LOCATION,
        _auth_header,
        _location_name,
    )

    depth = int(depth or _env_int("LEADBOT_MAPS_DEPTH", DEFAULT_DEPTH) or DEFAULT_DEPTH)
    depth = max(1, min(depth, MAX_DEPTH))
    payload = [{
        "keyword": keyword,
        "location_name": _location_name(market),  # may raise InvalidMarketLocationError: no spend
        "language_code": os.getenv("DATAFORSEO_LANGUAGE_CODE", "en").strip() or "en",
        "depth": depth,
    }]
    headers = _auth_header()  # missing credentials fail here, before any reservation

    res_id = guard.reserve("maps", keyword=keyword, depth=depth, mode=mode)  # raises before sending
    if post is None:
        import requests
        post = requests.post
    try:
        response = post(ENDPOINTS[mode], headers=headers, json=payload, timeout=90)
    except Exception as exc:
        # Unknown whether DataForSEO accepted (and billed) it: book the worst case.
        guard.record(None, res_id=res_id, provider_status="request_failed")
        raise MapsProviderError(f"request failed: {type(exc).__name__}") from exc

    try:
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("response is not an object")
    except Exception as exc:
        guard.record(None, res_id=res_id, provider_status=f"http_{getattr(response, 'status_code', '?')}")
        raise MapsProviderError(f"bad response: {type(exc).__name__}") from exc

    tasks = data.get("tasks") or []
    task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
    reported = task.get("cost", data.get("cost"))
    task_status = task.get("status_code")
    items: List[Dict[str, Any]] = []
    if data.get("status_code") == STATUS_OK and task_status in (STATUS_OK, STATUS_PARTIAL):
        for block in task.get("result") or []:
            if isinstance(block, dict):
                items.extend(block.get("items") or [])
    cost = guard.record(
        reported, res_id=res_id,
        provider_status=f"{data.get('status_code')}/{task_status}",
        results=len(items),
    )

    if data.get("status_code") != STATUS_OK:
        raise MapsProviderError(f"DataForSEO {data.get('status_code')} {data.get('status_message')}")
    if task_status == STATUS_INVALID_LOCATION:
        raise InvalidLocationValueError(f"DataForSEO task error: {task_status} {task.get('status_message')}")
    if task_status not in (STATUS_OK, STATUS_PARTIAL, STATUS_NO_RESULTS):
        raise MapsProviderError(f"DataForSEO task {task_status} {task.get('status_message')}")

    rows, skipped = parse_maps_items(items, keyword=keyword, market=market, mode=mode)
    for row in rows:
        row["dataforseo_cost"] = cost
    unique, stats = dedupe_listings(rows)
    return {
        "rows": unique,
        "items": items,
        "stats": stats,
        "skipped": skipped,
        "cost": cost,
        "status": "partial" if task_status == STATUS_PARTIAL else ("no_results" if task_status == STATUS_NO_RESULTS else "ok"),
        "spend": guard.summary(),
    }
