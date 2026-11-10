"""Lead Finder discovery from Google Maps listings (behind LEADBOT_MAPS_ENABLED).

When LEADBOT_MAPS_ENABLED=1, a live scan is ONE guarded Google Maps request:
- Organic SERP never runs alongside it or as a fallback (run_job() returns
  after this path, success or failure).
- DataForSEO OFF (Safe Mode) => nothing is sent and the scan says so.
- Spending goes through agents.discovery_spend_guard (persistent, fail closed).

Listings become leads in Maps rank order after a funnel whose counts always
reconcile exactly (see build_candidate_funnel):

  provider items = ads + area labels + closed + unnamed + other types + malformed + businesses
  businesses     = duplicates merged + unique
  unique         = off-category + ineligible + blocked + eligible
  eligible       = shown + over the requested limit

Websites: a listing's website is "provider reported". It becomes "verified"
only after we fetch it ourselves and it answers on the same site. Businesses
without a website stay leads ("No Website"); they are never crawled and never
get a guessed website, email or contact page. Social profiles are kept apart
from websites. Compare/CRM actions are only offered for verified websites.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DISCOVERY_SOURCE = "google_maps"
MAPS_DEPTH = 100  # one Maps SERP of up to 100 listings is one billed request
WEBSITE_CHECK_DEADLINE_SECONDS = 180
WEBSITE_CHECK_WORKERS = 5

# Extra CSV columns for Maps exports (appended after the standard columns).
MAPS_EXPORT_FIELDS = [
    "address",
    "place_id",
    "cid",
    "category",
    "website_status",
    "website_verified",
    "website_flag",
    "profile_url",
    "maps_position",
    "rating_value",
    "rating_votes",
]

WEBSITE_VERIFIED = "verified"
WEBSITE_UNVERIFIED = "unverified"
WEBSITE_MISSING = "missing"
WEBSITE_SOCIAL = "social_profile"

SAFE_MODE_CODE = "discovery_safe_mode"
SAFE_MODE_MESSAGE = "Lead search is in Safe Mode: paid search is turned off, so no search was run."
BUDGET_CODE = "discovery_budget_reached"
BUDGET_MESSAGE = (
    "The lead search spending limit has been reached, so no search was run. "
    "Please try again after midnight UTC."
)
LEDGER_CODE = "discovery_ledger_unavailable"
LEDGER_MESSAGE = "Lead search is paused because its spending records could not be verified. No search was run."


def maps_discovery_enabled() -> bool:
    return os.getenv("LEADBOT_MAPS_ENABLED", "0").strip() == "1"


# -- category relevance ----------------------------------------------------------

_STOPWORDS = {
    "a", "an", "and", "the", "of", "in", "near", "me", "best", "top", "local", "for", "my",
    "company", "companies", "service", "services", "business", "businesses",
}
_RETAIL = re.compile(
    r"\b(store|stores|supplier|suppliers|supply|wholesaler|wholesale|distributor|manufacturer|outlet|warehouse)\b"
)
# Small, explicit list of categories that are the same trade under another name.
_RELATED_CATEGORIES = {
    "plumb": ("drainage service", "septic system service", "sewer", "water heater", "drain"),
    "dentist": ("dental",),
    "lawy": ("attorney", "law firm"),
    "attorney": ("lawyer", "law firm"),
    "roof": ("roofer",),
    "electrician": ("electrical",),
}


def _stem(token: str) -> str:
    for suffix in ("ings", "ing", "ers", "er", "ies", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def keyword_stems(keyword: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]+", str(keyword or "").lower())
    return [_stem(t) for t in tokens if t not in _STOPWORDS and len(t) >= 3]


def _phrase_in(stems: List[str], text: str) -> bool:
    return bool(stems) and all(stem in text for stem in stems)


def _related_in(stems: List[str], text: str) -> bool:
    return any(rel in text for stem in stems for rel in _RELATED_CATEGORIES.get(stem, ()))


def category_relevance(row: Dict[str, Any], keyword: str) -> Tuple[bool, str]:
    """(relevant, why) for a Maps business against the searched business type."""
    stems = keyword_stems(keyword)
    if not stems:
        return True, "no_keyword"
    keyword_is_retail = bool(_RETAIL.search(str(keyword).lower()))
    primary = str(row.get("category") or "").strip().lower()
    additional = " | ".join(str(c).lower() for c in (row.get("additional_categories") or []))
    name = str(row.get("title") or "").lower()
    primary_is_retail = bool(_RETAIL.search(primary)) and not keyword_is_retail

    if primary_is_retail:
        return False, "retail_or_supplier_category"
    if primary and (_phrase_in(stems, primary) or _related_in(stems, primary)):
        return True, "primary_category"
    if additional and (_phrase_in(stems, additional) or _related_in(stems, additional)):
        return True, "additional_category"
    if _phrase_in(stems, name):
        # e.g. "Frank Savins Plumbing & Heating" listed as "Heating contractor".
        return True, "business_name"
    return False, "off_category"


# -- funnel ----------------------------------------------------------------------

def _is_blocked(row: Dict[str, Any], blocked: set, matcher: Callable[..., bool]) -> bool:
    """Same checks write_job()'s final gate applies, so nothing is dropped uncounted."""
    probe = {"domain": row.get("domain") or "", "url": row.get("website") or ""}
    if blocked and matcher(probe, blocked):
        return True
    from agents.lead_live_job_agent import _leadbot_is_blocked_by_final_gate
    return bool(_leadbot_is_blocked_by_final_gate(probe))


def build_candidate_funnel(
    items: List[Any],
    keyword: str,
    market: str,
    limit: int,
    blocked_domains: Iterable[str] = (),
) -> Dict[str, Any]:
    """Raw provider items -> ordered eligible listings, with reconciled counts."""
    from agents import dataforseo_maps_agent as maps
    from agents.leadbot_block_gate import lead_matches_blocked_domains

    items = list(items or [])
    malformed = sum(1 for it in items if not isinstance(it, dict))
    rows, skipped = maps.parse_maps_items(items, keyword=keyword, market=market, mode="maps")
    unique, dedupe = maps.dedupe_listings(rows)

    rejected: Dict[str, List[Dict[str, Any]]] = {"off_category": [], "ineligible": [], "blocked": []}
    eligible: List[Dict[str, Any]] = []
    blocked = set(blocked_domains or ())
    for row in unique:
        relevant, why = category_relevance(row, keyword)
        row["relevance"] = why
        if not relevant:
            rejected["off_category"].append(row)
        elif not row.get("lead_eligible", True):
            rejected["ineligible"].append(row)
        elif row.get("domain") and _is_blocked(row, blocked, lead_matches_blocked_domains):
            rejected["blocked"].append(row)
        else:
            eligible.append(row)

    eligible.sort(key=lambda r: (r.get("rank_absolute") is None, r.get("rank_absolute") or 0))
    limit = max(0, int(limit))
    shown, over = eligible[:limit], eligible[limit:]

    counts = {
        "provider_items": len(items),
        "ads": skipped["paid"],
        "area_labels": skipped["not_a_business"],
        "closed_forever": skipped["closed_forever"],
        "no_name": skipped["no_name"],
        "other_item_types": skipped["wrong_type"],
        "malformed": malformed,
        "businesses": len(rows),
        "duplicates_merged": dedupe["duplicates_merged"],
        "unique_businesses": len(unique),
        "off_category": len(rejected["off_category"]),
        "ineligible_no_contact": len(rejected["ineligible"]),
        "blocked": len(rejected["blocked"]),
        "eligible": len(eligible),
        "selected": len(shown),
        "over_limit": len(over),
        "requested": limit,
        "eligible_with_website": sum(1 for r in eligible if r.get("website")),
        "eligible_without_website": sum(1 for r in eligible if not r.get("website")),
        "eligible_social_profile_only": sum(1 for r in eligible if r.get("website_status") == "social_profile"),
    }
    reconcile_funnel(counts)
    return {
        "counts": counts,
        "selected": shown,
        "over_limit": over,
        "rejected": {k: [r.get("title") for r in v] for k, v in rejected.items()},
        "off_category_categories": sorted({str(r.get("category") or "(none)") for r in rejected["off_category"]}),
    }


def reconcile_funnel(c: Dict[str, int]) -> None:
    """Raise if any stage's totals do not add up exactly."""
    checks = [
        ("provider_items", c["ads"] + c["area_labels"] + c["closed_forever"] + c["no_name"]
         + c["other_item_types"] + c["malformed"] + c["businesses"]),
        ("businesses", c["duplicates_merged"] + c["unique_businesses"]),
        ("unique_businesses", c["off_category"] + c["ineligible_no_contact"] + c["blocked"] + c["eligible"]),
        ("eligible", c["selected"] + c["over_limit"]),
        ("eligible", c["eligible_with_website"] + c["eligible_without_website"]),
    ]
    for key, total in checks:
        if c[key] != total:
            raise AssertionError(f"Maps funnel does not reconcile: {key}={c[key]} but parts sum to {total}")


# -- website verification ------------------------------------------------------

def verify_website(url: str, fetch: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Fetch the provider-reported website ourselves. Verified only if it answers on the same site."""
    from agents.business_website import is_google_host
    from agents.same_site import site_identity

    if not url:
        return {"verified": False, "reason": "no_website"}
    if fetch is None:
        from agents.crawl_agent import crawl_get as fetch
    try:
        page = fetch(url)
    except Exception as exc:  # network / SSRF refusal / anything
        return {"verified": False, "reason": f"fetch_failed:{type(exc).__name__}"}
    status = int(getattr(page, "status_code", 0) or 0)
    text = getattr(page, "text", "") or ""
    final = str(getattr(page, "url", "") or url)
    if not (200 <= status < 400) or not text.strip():
        return {"verified": False, "reason": f"unreachable:{status}"}
    if is_google_host(final) or not site_identity(final) or site_identity(final) != site_identity(url):
        return {"verified": False, "reason": "redirects_offsite", "final_url": final}
    return {"verified": True, "reason": "fetched_same_site", "final_url": final}


def _lead_bot_fast_mode() -> bool:
    try:
        from agents.lead_finding_agent import _lead_bot_fast_mode as fast
        return bool(fast())
    except Exception:
        return False


def build_lead(
    row: Dict[str, Any],
    keyword: str,
    market: str,
    scan_date: str = "",
    check_website: bool = True,
    fetch: Optional[Callable[[str], Any]] = None,
    extract_contact: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """One eligible Maps listing -> a Lead Finder lead dict (no paid calls)."""
    from agents.lead_contact_quality_agent import resolve_outreach_status

    website = row.get("website") or ""
    phone = row.get("phone") or ""
    lead: Dict[str, Any] = {
        "title": row.get("title") or "",
        "business_name": row.get("title") or "",
        "domain": row.get("domain") or "",
        "url": website,
        "website": website,
        "link": website,
        "best_phone": phone,
        "phones": [phone] if phone else [],
        "emails": [],
        "contact_page_url": "",
        "address": row.get("address") or "",
        "formatted_address": row.get("address") or "",
        "business_address": row.get("address") or "",
        "has_street_address": bool(row.get("has_street_address")),
        "place_id": row.get("place_id") or "",
        "cid": row.get("cid") or "",
        "category": row.get("category") or "",
        "additional_categories": list(row.get("additional_categories") or []),
        "profile_url": row.get("profile_url") or "",
        "rating_value": row.get("rating_value") or "",
        "rating_votes": row.get("rating_votes") or "",
        "maps_position": row.get("maps_position") or "",
        "rank_absolute": row.get("rank_absolute"),
        "source": "dataforseo_maps",
        "lead_source_label": "Google Maps",
        "discovery_source": DISCOVERY_SOURCE,
        "serp_page": "Google Maps",
        "serp_position": str(row.get("maps_position") or ""),
        "query_used": keyword,
        "base_keyword": keyword,
        "market": market,
        "search_location": market,
        "scan_date": scan_date,
        "relevance": row.get("relevance") or "",
        "shared_domain_count": int(row.get("shared_domain_count") or 0),
        "address_source": "google_maps" if row.get("address") else "",
        "contact_flags": [],
    }

    if not website:
        lead["website_status"] = WEBSITE_SOCIAL if row.get("profile_url") else WEBSITE_MISSING
        lead["website_verified"] = False
        lead["website_flag"] = "No Website"
        lead["website_check"] = "not_applicable"
        lead["contact_flags"].append("no_website_no_crawl")
    elif not check_website or _lead_bot_fast_mode():
        lead["website_status"] = WEBSITE_UNVERIFIED
        lead["website_verified"] = False
        lead["website_flag"] = ""
        lead["website_check"] = "not_checked"
    else:
        check = verify_website(website, fetch=fetch)
        lead["website_check"] = check["reason"]
        lead["website_verified"] = bool(check["verified"])
        lead["website_status"] = WEBSITE_VERIFIED if check["verified"] else WEBSITE_UNVERIFIED
        lead["website_flag"] = ""
        if check["verified"]:
            if extract_contact is None:
                from agents.contact_extraction_agent import extract_contact_from_url as extract_contact
            try:
                contact = extract_contact(website, market=market) or {}
            except Exception:
                contact = {"flags": ["contact_crawl_failed"]}
            from agents.business_website import is_google_host
            from agents.same_site import site_identity

            contact_page = contact.get("contact_page_url") or ""
            if contact_page and (
                is_google_host(contact_page) or site_identity(contact_page) != site_identity(website)
            ):
                contact_page = ""
                lead["contact_flags"].append("contact_page_offsite_dropped")
            lead["emails"] = list(contact.get("emails") or [])
            lead["contact_page_url"] = contact_page
            lead["contact_flags"].extend(contact.get("flags") or [])
            if not phone and contact.get("best_phone"):
                # The listing had no phone; the business's own site does.
                lead["best_phone"] = contact["best_phone"]
                lead["phones"] = list(contact.get("phones") or [contact["best_phone"]])

    status, flags, confidence = resolve_outreach_status(
        emails=lead["emails"],
        phone=lead["best_phone"],
        website=lead["website"] if lead["website_verified"] else "",
        domain=lead["domain"] if lead["website_verified"] else "",
        contact_page_url=lead["contact_page_url"],
    )
    lead["outreach_status"] = status
    lead["contact_confidence"] = confidence
    for flag in flags or []:
        if flag not in lead["contact_flags"]:
            lead["contact_flags"].append(flag)

    score = 60
    score += {"email_and_call_ready": 12, "call_ready": 7, "email_ready": 6}.get(status, -20)
    if lead["has_street_address"]:
        score += 3
    if status != "email_and_call_ready":
        score = min(score, 94)
    lead["final_lead_score"] = max(0, min(100, score))
    lead["score"] = lead["final_lead_score"]
    return lead


def public_card_fields(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Fields the live card needs beyond lead_to_public()."""
    return {
        "discovery_source": DISCOVERY_SOURCE,
        "place_id": lead.get("place_id") or "",
        "cid": lead.get("cid") or "",
        "listing_key": ("place_id:" + lead["place_id"]) if lead.get("place_id") else (
            ("cid:" + lead["cid"]) if lead.get("cid") else ""
        ),
        "category": lead.get("category") or "",
        "website_status": lead.get("website_status") or "",
        "website_verified": bool(lead.get("website_verified")),
        "website_flag": lead.get("website_flag") or "",
        "website_check": lead.get("website_check") or "",
        "profile_url": lead.get("profile_url") or "",
        "maps_position": str(lead.get("maps_position") or ""),
        "rating_value": str(lead.get("rating_value") or ""),
        "rating_votes": str(lead.get("rating_votes") or ""),
        "has_street_address": bool(lead.get("has_street_address")),
        # Only a verified website of ONE business may be compared, audited or
        # sent to the CRM (chain locations sharing a website are ambiguous).
        "actions_domain": (lead.get("domain") or "") if (
            lead.get("website_verified") and int(lead.get("shared_domain_count") or 0) <= 1
        ) else "",
        "shared_domain_count": int(lead.get("shared_domain_count") or 0),
        # Maps discovery does not read page titles/meta descriptions; never
        # show the business name as if it were the site's title tag.
        "meta_title": "", "title_tag": "", "page_title": "", "site_title": "",
        "meta_description": "", "meta_desc": "", "page_description": "",
    }


def shortfall_note(found: int, requested: int, counts: Dict[str, int], keyword: str) -> str:
    note = f"Found {found} of {requested} requested leads."
    if found >= requested:
        return note
    if counts.get("unique_businesses", 0) == 0 and counts.get("businesses", 0) == 0:
        return note + " Google Maps returned no businesses for this search. Try a broader business type or a nearby city."
    parts = []
    if counts.get("off_category"):
        parts.append(f"{counts['off_category']} were a different type of business than “{keyword}”")
    if counts.get("ineligible_no_contact"):
        parts.append(f"{counts['ineligible_no_contact']} had no phone, website or street address")
    if counts.get("blocked"):
        parts.append(f"{counts['blocked']} were on a block list")
    if counts.get("duplicates_merged"):
        parts.append(f"{counts['duplicates_merged']} were duplicate listings")
    note += f" Google Maps listed {counts.get('businesses', 0)} businesses for this search"
    note += (": " + "; ".join(parts) + ".") if parts else "."
    if found < counts.get("eligible", 0):
        note += " The scan stopped before every eligible business was checked."
    return note


# -- the scan --------------------------------------------------------------------

def run_maps_job(job_id: str, post: Optional[Callable[..., Any]] = None,
                 fetch: Optional[Callable[[str], Any]] = None,
                 extract_contact: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
    """Run one live Lead Finder scan from a single guarded Google Maps request."""
    from agents import lead_live_job_agent as live
    from agents import dataforseo_maps_agent as maps
    from agents import discovery_spend_guard as spend

    job = live.read_job(job_id)
    if not job:
        return

    def finish_error(code: str, message: str, discovery: Dict[str, Any]) -> None:
        current = live.read_job(job_id) or job
        current.update({
            "status": "error", "error_code": code, "message": message,
            "discovery_source": DISCOVERY_SOURCE, "discovery": discovery,
            "requested": discovery.get("requested", 0), "updated_at": live.now_iso(),
        })
        live.write_job(current)

    try:
        params = job.get("params", {}) or {}
        industry = str(params.get("industry") or "").strip()
        market = str(params.get("market") or "").strip()
        keyword = str(params.get("keyword") or industry).strip() or industry
        total_limit = live.clean_int(params.get("limit"), 50, 1, 60)

        from agents.leadbot_block_gate import blocklist_owner_key, load_effective_blocked_domains
        blocked_domains = load_effective_blocked_domains(blocklist_owner_key(params))

        discovery: Dict[str, Any] = {"source": DISCOVERY_SOURCE, "requested": total_limit, "requests": 0}
        job.update({
            "status": "running", "message": "Searching Google Maps listings...",
            "discovery_source": DISCOVERY_SOURCE, "discovery": discovery,
            "requested": total_limit, "updated_at": live.now_iso(),
        })
        live.write_job(job)

        if not keyword or not market:
            finish_error("invalid_market_location", live.INVALID_MARKET_LOCATION_MESSAGE, discovery)
            return
        if not spend.paid_discovery_enabled():
            discovery["blocked"] = "safe_mode"
            finish_error(SAFE_MODE_CODE, SAFE_MODE_MESSAGE, discovery)
            return
        if live.is_cancel_requested(job_id):
            live.mark_job_cancelled(job_id)
            return

        guard = spend.DiscoverySpendGuard(job_id=job_id)
        try:
            with spend.scan_guard(guard):
                result = maps.search_google_maps(keyword, market, guard, depth=MAPS_DEPTH, post=post)
        except spend.SafeModeError:
            discovery["blocked"] = "safe_mode"
            finish_error(SAFE_MODE_CODE, SAFE_MODE_MESSAGE, discovery)
            return
        except spend.LedgerUnavailable:
            discovery["blocked"] = "ledger_unavailable"
            finish_error(LEDGER_CODE, LEDGER_MESSAGE, discovery)
            return
        except spend.SpendLimitExceeded:
            discovery["blocked"] = "budget"
            finish_error(BUDGET_CODE, BUDGET_MESSAGE, discovery)
            return
        except Exception as exc:
            from agents.dataforseo_serp_agent import InvalidLocationValueError, InvalidMarketLocationError
            discovery["requests"] = guard.total_calls
            if isinstance(exc, InvalidMarketLocationError):
                finish_error("invalid_market_location", live.INVALID_MARKET_LOCATION_MESSAGE, discovery)
            elif isinstance(exc, InvalidLocationValueError):
                finish_error("invalid_location_value", live.INVALID_LOCATION_VALUE_MESSAGE, discovery)
            else:
                print(f"LEADBOT MAPS DISCOVERY FAILED: job={job_id} error={type(exc).__name__}", flush=True)
                finish_error("search_provider_unavailable", live.SEARCH_PROVIDER_UNAVAILABLE_MESSAGE, discovery)
            return

        funnel = build_candidate_funnel(result["items"], keyword, market, total_limit, blocked_domains)
        counts = funnel["counts"]
        discovery.update({"requests": guard.total_calls, "provider_status": result["status"], "funnel": counts})
        try:
            guard.annotate(
                rejected={k: counts[k] for k in ("ads", "area_labels", "closed_forever", "no_name",
                                                 "other_item_types", "malformed", "duplicates_merged",
                                                 "off_category", "ineligible_no_contact", "blocked")},
                eligible=counts["eligible"], selected=counts["selected"],
            )
        except Exception as exc:  # bookkeeping only; the spend itself is already settled
            print(f"LEADBOT MAPS LEDGER ANNOTATE ERROR: {type(exc).__name__}", flush=True)

        job = live.read_job(job_id) or job
        job["discovery"] = discovery
        job["message"] = (
            f"Google Maps listed {counts['businesses']} businesses; "
            f"{counts['eligible']} match “{keyword}”. Checking websites..."
        )
        job["updated_at"] = live.now_iso()
        live.write_job(job)

        scan_date = str(job.get("created_at") or "")
        selected = funnel["selected"]
        deadline = time.monotonic() + WEBSITE_CHECK_DEADLINE_SECONDS
        all_rows: List[Dict[str, Any]] = []
        seen_keys = set(job.get("seen_listing_keys") or [])
        seen_domains = set(job.get("seen_domains") or [])

        def _build(row: Dict[str, Any], check: bool) -> Dict[str, Any]:
            return build_lead(row, keyword, market, scan_date=scan_date, check_website=check,
                              fetch=fetch, extract_contact=extract_contact)

        cancelled = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=WEBSITE_CHECK_WORKERS) as pool:
            futures = [pool.submit(_build, row, True) if row.get("website") else None for row in selected]
            for row, future in zip(selected, futures):
                if live.is_cancel_requested(job_id):
                    cancelled = True
                    break
                lead = None
                if future is not None:
                    try:
                        lead = future.result(timeout=max(0.0, deadline - time.monotonic()))
                    except Exception:
                        lead = None
                if lead is None:
                    # No website, or the website check ran out of time: no crawl.
                    lead = _build(row, False)
                key = maps.listing_key(row) or f"rank:{row.get('rank_absolute')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if lead.get("domain"):
                    seen_domains.add(lead["domain"])
                public = live.lead_to_public(lead)
                public.update(public_card_fields(lead))
                job["leads"].append(public)
                job["counts"]["found"] = len(job["leads"])
                if public.get("best_phone") or public.get("emails"):
                    job["counts"]["enriched"] += 1
                else:
                    job["counts"]["needs_research"] += 1
                job["seen_listing_keys"] = sorted(seen_keys)
                job["seen_domains"] = sorted(seen_domains)
                job["message"] = f"Found {len(job['leads'])} of {total_limit}: {lead['title']}"
                job["updated_at"] = live.now_iso()
                live.write_job(job)
                all_rows.append(lead)
            if cancelled:
                for future in futures:
                    if future is not None:
                        future.cancel()

        if cancelled:
            live.mark_job_cancelled(job_id)
            return

        job = live.read_job(job_id) or job
        found = len(job.get("leads") or [])
        discovery["shown"] = found
        discovery["shown_with_verified_website"] = sum(1 for l in all_rows if l.get("website_verified"))
        discovery["shown_with_unverified_website"] = sum(
            1 for l in all_rows if l.get("website") and not l.get("website_verified"))
        discovery["shown_without_website"] = sum(1 for l in all_rows if not l.get("website"))
        job["discovery"] = discovery

        if all_rows:
            try:
                from agents.lead_export_agent import export_leads_to_csv
                export = export_leads_to_csv(
                    {
                        "query": keyword, "industry": industry, "market": market,
                        "count": len(all_rows), "base_keyword": keyword,
                        "scan_date": scan_date, "leads": all_rows,
                    },
                    industry=industry or "leadbot", market=market or "market",
                    only_outreach_ready=False, extra_fields=MAPS_EXPORT_FIELDS,
                )
                path = export.get("path") or ""
                if path:
                    from pathlib import Path
                    job["export_file"] = Path(path).name
                    try:
                        from agents.lead_dashboard_agent import record_export_owner
                        record_export_owner(
                            job["export_file"],
                            owner_email=params.get("owner_email", ""),
                            owner_username=params.get("owner_username", ""),
                            is_partial=False,
                        )
                    except Exception as exc:
                        print(f"LEADBOT LIVE EXPORT OWNER RECORD ERROR: {exc}", flush=True)
            except Exception as exc:
                job.setdefault("errors", []).append(f"Export failed: {exc}")
                job["export_file"] = ""

        if live.is_cancel_requested(job_id):
            live.mark_job_cancelled(job_id)
            return

        job["status"] = "done"
        job["partial"] = result["status"] == "partial"
        job["requested"] = total_limit
        job["result_note"] = shortfall_note(found, total_limit, counts, keyword)
        job["message"] = "Done. Open Desktop is ready." if found else "Scan complete."
        job["updated_at"] = live.now_iso()
        live.write_job(job)
    except Exception as exc:
        failed = live.read_job(job_id) or {"job_id": job_id, "errors": []}
        failed["status"] = "error"
        failed["message"] = "The scan could not be completed."
        failed.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
        failed["discovery_source"] = DISCOVERY_SOURCE
        failed["updated_at"] = live.now_iso()
        live.write_job(failed)
