"""Pick the top competing business for a Lead Finder prospect from SAVED results only.

No network access and no paid calls: the only evidence is the prospect's own
Lead Finder export (rows with their captured query, ranking type, page and
position). Rules:

* Same search: same base keyword and search location as the prospect.
* Same ranking type: organic is never compared with Maps/places/paid rows.
* Same query first: positions are only comparable within one query. A result
  found by another query of the same search is offered only as a labelled
  fallback.
* A real, saved position is required (from_row never derives one).
* Excluded: the prospect itself (including www/subdomain/redirect-equivalent
  URLs), Lead Finder's own blocked directories, ordering/coupon platforms,
  national-chain location pages, rows without a usable URL, and duplicate sites.

Lead Finder exports keep only the leads it found, so page-one results that
were filtered out are not available; the explanation says so.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from agents.actii_search_metadata_agent import domain, first, from_row
from agents.lead_domain_filter_agent import bad_lead_reason
from agents.same_site import same_website, site_identity

MAX_EXPORT_BYTES = 2 * 1024 * 1024
REUSE_MAX_AGE_DAYS = 30

# Not local competitors even though they rank: ordering/marketplace hosts,
# coupon/deal sites, and review/listing sites missing from Lead Finder's list.
PLATFORM_DOMAINS = {
    "slicelife.com": "online ordering platform", "toasttab.com": "online ordering platform",
    "chownow.com": "online ordering platform", "menufy.com": "online ordering platform",
    "beyondmenu.com": "online ordering platform", "order.online": "online ordering platform",
    "allmenus.com": "menu directory", "menupages.com": "menu directory", "zmenu.com": "menu directory",
    "valpak.com": "coupon site", "restaurant.com": "coupon site", "coupons.com": "coupon site",
    "facebook.com": "social profile", "instagram.com": "social profile",
    "mapquest.com": "directory", "nextdoor.com": "directory", "bbb.org": "directory",
}
CHAIN_LOCATION_PREFIXES = ("locations.", "location.", "stores.", "restaurants.", "local.")


def _usable_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text if "://" in text else "https://" + text
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname or "." not in parts.hostname:
        return ""
    return candidate


def exclusion_reason(row: dict, url: str) -> str:
    host = domain(url)
    if not url:
        return "no usable website URL"
    for platform, label in PLATFORM_DOMAINS.items():
        if host == platform or host.endswith("." + platform):
            return label
    if host.startswith(CHAIN_LOCATION_PREFIXES):
        return "national chain location page"
    reason = bad_lead_reason(host, first(row, "title"), url)
    return f"directory or non-business site ({reason})" if reason else ""


def _entry(row: dict, url: str) -> dict:
    meta = from_row(row)
    return {
        "business": first(row, "title", "business", "company_name") or domain(url),
        "domain": domain(url), "url": url,
        "position": meta["serp_position"], "page": meta["serp_page"],
        "ranking_type": meta["ranking_type"], "query": meta["query_used"],
        "keyword": meta["base_keyword"], "location": meta["search_location"], "scan_date": meta["scan_date"],
    }


def read_rows(path: Path) -> list[dict]:
    path = Path(path)
    if path.stat().st_size > MAX_EXPORT_BYTES:
        raise ValueError("export is too large")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [row for row in csv.DictReader(stream) if isinstance(row, dict)]


def select_top_competitor(export_path: Path, prospect_domain: str) -> dict:
    """Return {"status": "selected"|"none"|"not_found", ...} with the evidence used."""
    rows = read_rows(export_path)
    prospect_rows = [row for row in rows if domain(first(row, "domain", "website", "url")) == domain(prospect_domain)]
    if not domain(prospect_domain) or not prospect_rows:
        return {"status": "not_found", "limitation": "This prospect is not in the selected Lead Finder export."}
    prospect_row = min(prospect_rows, key=lambda row: from_row(row)["serp_position"] or 10**9)
    prospect_url = _usable_url(first(prospect_row, "url", "website", "domain"))
    prospect = _entry(prospect_row, prospect_url)
    result = {"prospect": prospect, "excluded": [], "export": Path(export_path).name}
    if not prospect_url:
        return result | {"status": "none", "limitation": "The prospect has no usable website URL in this export."}
    if not (prospect["keyword"] and prospect["location"]):
        return result | {"status": "none", "limitation": "This export did not save the search keyword and location for the prospect."}

    candidates_by_query: dict[str, list[dict]] = {}
    for row in rows:
        url = _usable_url(first(row, "url", "website", "domain"))
        candidate = _entry(row, url)
        if url and same_website([prospect_url, prospect["domain"]], [url]):
            continue  # the prospect itself
        if (candidate["keyword"].lower(), candidate["location"].lower()) != (prospect["keyword"].lower(), prospect["location"].lower()):
            continue  # a different search
        reason = exclusion_reason(row, url)
        if not reason and candidate["ranking_type"] != prospect["ranking_type"]:
            reason = f"{candidate['ranking_type']} result (prospect is {prospect['ranking_type']}); rankings are not comparable"
        if not reason and not candidate["position"]:
            reason = "no saved ranking position"
        if reason:
            result["excluded"].append({"business": candidate["business"], "domain": candidate["domain"] or "(none)", "reason": reason})
            continue
        candidates_by_query.setdefault(candidate["query"], []).append(candidate)

    same_query = candidates_by_query.get(prospect["query"], []) if prospect["query"] else []
    if candidates_by_query:
        # Prefer the exact query. Otherwise choose a stable query group by
        # name, without comparing ranking positions across different queries.
        group = same_query or candidates_by_query[min(candidates_by_query)]
        # Deduplicate only after choosing the comparable query group: a site's
        # #1 on another query must never replace its #11 here.
        best_by_site: dict[str, dict] = {}
        for candidate in group:
            key = site_identity(candidate["url"]) or candidate["domain"]
            if key not in best_by_site or candidate["position"] < best_by_site[key]["position"]:
                if key in best_by_site:
                    result["excluded"].append({"business": best_by_site[key]["business"], "domain": best_by_site[key]["domain"], "reason": "duplicate website"})
                best_by_site[key] = candidate
            else:
                result["excluded"].append({"business": candidate["business"], "domain": candidate["domain"], "reason": "duplicate website"})
        competitor = min(best_by_site.values(), key=lambda c: (c["position"], c["page"] or 0, c["domain"]))
        if same_query:
            return result | {"status": "selected", "basis": "same_query", "competitor": competitor,
                             "prospect_outranks": bool(prospect["position"] and prospect["position"] < competitor["position"])}
        return result | {"status": "selected", "basis": "related_query", "competitor": competitor,
                         "prospect_outranks": None,
                         "note": "No eligible competitor was saved for the prospect's own query; this one was found by another query in the same search, so the two positions are not directly comparable."}
    return result | {"status": "none", "limitation": "No eligible competing business was saved for this search. Enter a competitor manually."}


def reusable_report(history: list, owner_id, prospect_url: str, competitor_url: str, now: datetime | None = None) -> dict | None:
    """The owner's newest saved comparison of these two websites from the last 30 days, if any."""
    if owner_id is None:
        return None
    now = now or datetime.now(timezone.utc)
    best = None
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict) or entry.get("owner_id") != owner_id or not entry.get("saved_report"):
            continue
        if not (same_website([entry.get("site_url", ""), entry.get("site_final_url", "")], [prospect_url])
                and same_website([entry.get("competitor_url", ""), entry.get("competitor_final_url", "")], [competitor_url])):
            continue
        try:
            saved = datetime.strptime(str(entry.get("timestamp", ""))[:19], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - saved > timedelta(days=REUSE_MAX_AGE_DAYS):
            continue
        if best is None or entry["timestamp"] > best["timestamp"]:
            best = entry
    return best
