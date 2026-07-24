from pathlib import Path
import json
import os
from urllib.parse import urlparse
from business_competitor_finder import find_business_competitors
from agents.contact_extraction_agent import extract_contact_from_url
from agents.lead_blacklist_agent import is_blocked_lead_domain



# === LEADBOT EMAIL CLEANER START ===

# === LEADBOT EXPORT ROOT DOMAIN BLOCK SAFETY START ===
def _leadbot_export_block_normalize_domain(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""

    if "://" not in raw:
        raw = "http://" + raw

    try:
        parsed = urlparse(raw)
        host = parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        host = raw.split("/")[0]

    host = host.split("@")[-1].split(":")[0].strip().strip(".").lower()

    if host.startswith("www."):
        host = host[4:]

    return host


def _leadbot_export_block_candidates(row):
    vals = []
    if isinstance(row, dict):
        for key in ("domain", "website", "url", "link", "business_url", "landing_page", "contact_page"):
            if row.get(key):
                vals.append(row.get(key))
    else:
        vals.append(row)

    out = set()
    for val in vals:
        host = _leadbot_export_block_normalize_domain(val)
        if not host:
            continue

        out.add(host)
        out.add("www." + host)

        parts = host.split(".")
        if len(parts) >= 2:
            root = ".".join(parts[-2:])
            out.add(root)
            out.add("www." + root)

    return out


def _leadbot_export_is_blocked(row):
    path = Path("data/leadbot_fast_blocklist.json")
    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception:
        return False

    domains = set()
    patterns = set()

    for item in data.get("domains", []) or []:
        host = _leadbot_export_block_normalize_domain(item)
        if host:
            domains.add(host)
            domains.add("www." + host)

    for item in data.get("patterns", []) or []:
        item = str(item or "").strip().lower()
        if item:
            patterns.add(item)

    for host in _leadbot_export_block_candidates(row):
        if host in domains:
            return True

        for pattern in patterns:
            if pattern.startswith("*."):
                suffix = pattern[1:]
                if host.endswith(suffix) or host == suffix.lstrip("."):
                    return True
            elif pattern and pattern in host:
                return True

    return False


def _leadbot_export_filter_blocked_rows(rows):
    clean = []
    removed = 0

    for row in rows or []:
        try:
            if _leadbot_export_is_blocked(row):
                removed += 1
                continue
        except Exception:
            pass
        clean.append(row)

    if removed:
        print(f"LEADBOT EXPORT BLOCK FILTER removed {removed} blocked rows", flush=True)

    return clean
# === LEADBOT EXPORT ROOT DOMAIN BLOCK SAFETY END ===


def leadbot_clean_email_list(value):
    import re

    if value is None:
        return []

    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [str(value)]

    joined = " ".join(str(x or "") for x in raw)

    # Repair char-split garbage like: a, ,, b, ,, c, ,, @, ,, d...
    compact = joined.replace(",,", "").replace(",", "").replace(" ", "")

    candidates = []
    candidates.extend(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", joined))
    candidates.extend(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", compact))

    junk_domains = (
        "sentry.wixpress.com",
        "sentry-next.wixpress.com",
        "wixpress.com",
        "sentry.io",
    )

    cleaned = []
    seen = set()

    for email in candidates:
        email = email.strip().strip(".,;:()[]{}<>").lower()
        if not email or "@" not in email:
            continue

        domain = email.split("@", 1)[1]

        # Drop tracking/error-ingestion emails, not real business contacts.
        if domain in junk_domains:
            continue

        # Drop obvious fake placeholders.
        if email in {"abc@abc.abc", "test@test.com", "example@example.com"}:
            continue

        if email not in seen:
            seen.add(email)
            cleaned.append(email)

    return cleaned
# === LEADBOT EMAIL CLEANER END ===


def _leadbot_safe_lead_dict(item):
    if isinstance(item, dict):
        return item
    if isinstance(item, (tuple, list)):
        for part in item:
            if isinstance(part, dict):
                return part
            if isinstance(part, (tuple, list)):
                nested = _leadbot_safe_lead_dict(part)
                if nested:
                    return nested
    return {}




# === LEADBOT TUPLE LEAD NORMALIZER START ===
def leadbot_normalize_lead_object(lead):
    if isinstance(lead, dict):
        return lead

    if isinstance(lead, (tuple, list)):
        for item in lead:
            if isinstance(item, dict):
                return item

    return {}
# === LEADBOT TUPLE LEAD NORMALIZER END ===


DIRECTORY_HINTS = {
    "indeed", "indeed.com", "www.indeed.com",
    "yelp", "angi", "homeadvisor", "bbb", "thumbtack", "facebook",
    "instagram", "linkedin", "youtube", "yellowpages", "mapquest",
    "gaf", "dailyvoice", "dailyvoice.com", "directory", "reviews", "top 10", "best roofers",
}


INDUSTRY_TERMS = {
    "roofing": {
        "good": ["roof", "roofing", "roofer", "roofers", "roof repair", "roof replacement", "shingle", "flat roof"],
        "bad": ["plumbing", "plumber", "water heater", "drain cleaning", "painting", "painter", "attorney", "dentist"],
    },
    "plumbing": {
        "good": ["plumbing", "plumber", "drain", "water heater", "sewer", "pipe", "leak"],
        "bad": ["roofing", "roofer", "painting", "painter", "attorney", "dentist"],
    },
    "cesspool": {
        "good": ["cesspool", "septic", "sewer", "drain", "pumping", "cleaning"],
        "bad": ["roofing", "roofer", "water heater", "painting", "painter", "attorney", "dentist"],
    },
    "painting": {
        "good": ["painting", "painter", "interior painting", "exterior painting", "paint contractor"],
        "bad": ["roofing", "roofer", "plumbing", "plumber", "attorney", "dentist"],
    },
    "seo": {
        "good": ["seo", "search engine optimization", "digital marketing", "local seo", "marketing agency"],
        "bad": ["roofing", "plumbing", "painting", "dentist", "attorney"],
    },
}



def _lead_bot_fast_mode():
    return os.environ.get("LEAD_BOT_FAST", "").strip().lower() in {"1", "true", "yes", "fast"}

def clean_domain(url):
    if not url:
        return ""

    parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
    domain = parsed.netloc.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def make_query(industry, market, service_keyword=None):
    industry = (industry or "").strip()
    market = (market or "").strip()
    service_keyword = (service_keyword or "").strip()

    if service_keyword:
        return f"{service_keyword} {market}".strip()

    defaults = {
        "roofing": "roofing contractor",
        "plumbing": "plumbing company",
        "cesspool": "cesspool service",
        "painting": "painting contractor",
        "seo": "SEO company",
    }

    base = defaults.get(industry.lower(), industry or "local service company")
    return f"{base} {market}".strip()











def score_lead(item, industry, market):
    industry = (industry or "").lower()
    market = (market or "").lower()

    title = item.get("title", "") or ""
    url = item.get("url", "") or item.get("link", "") or ""
    domain = item.get("domain", "") or clean_domain(url)
    snippet = item.get("snippet", "") or item.get("description", "") or ""

    blob = f"{title} {url} {domain} {snippet}".lower()

    score = 50
    flags = []

    if any(hint in blob for hint in DIRECTORY_HINTS):
        score -= 50
        flags.append("directory_or_social")

    rules = INDUSTRY_TERMS.get(industry, {})
    good_hits = [term for term in rules.get("good", []) if term in blob]
    bad_hits = [term for term in rules.get("bad", []) if term in blob]

    if good_hits:
        score += min(30, len(good_hits) * 8)

    if bad_hits:
        score -= min(35, len(bad_hits) * 12)
        flags.append("mixed_industry")

    if market and market in blob:
        score += 15

    if domain and not any(x in domain for x in ["facebook", "yelp", "angi", "bbb", "homeadvisor", "thumbtack"]):
        score += 10

    if any(x in blob for x in ["llc", "inc", "company", "contractor", "services", "service"]):
        score += 5

    score = max(0, min(100, score))

    if score >= 80:
        status = "strong"
    elif score >= 60:
        status = "usable"
    else:
        status = "weak"

    serp_page = item.get("serp_page")
    serp_position = item.get("serp_position")

    seo_opportunity_score = score

    if serp_position:
        if 11 <= int(serp_position) <= 40:
            seo_opportunity_score += 20
        elif int(serp_position) <= 10:
            seo_opportunity_score += 10
        elif int(serp_position) > 40:
            seo_opportunity_score += 5

    seo_opportunity_score = max(0, min(100, seo_opportunity_score))

    return {
        "title": title,
        "url": url,
        "domain": domain,
        "snippet": snippet,
        "score": score,
        "seo_opportunity_score": seo_opportunity_score,
        "serp_page": serp_page,
        "serp_position": serp_position,
        "status": status,
        "flags": flags,
    }



def _leadbot_preserve_source_metadata(scored, item):
    """
    Preserve upstream SERP/source metadata after lead scoring/contact enrichment.
    score_lead() may not know about newer fields like DataForSEO rank_absolute.
    """
    if not isinstance(scored, dict) or not isinstance(item, dict):
        return scored

    for key in (
        "source",
        "lead_source_label",
        "rank_group",
        "rank_absolute",
        "dataforseo_cost",
        "places_position",
        "place_id",
    ):
        if item.get(key) not in (None, ""):
            scored[key] = item.get(key)

    # Recalculate organic page from organic position for DataForSEO.
    if scored.get("source") == "dataforseo":
        try:
            pos = int(scored.get("serp_position") or item.get("serp_position") or 0)
        except Exception:
            pos = 0

        if pos > 0:
            scored["serp_position"] = pos
            scored["serp_page"] = ((pos - 1) // 10) + 1

        scored["lead_source_label"] = scored.get("lead_source_label") or "Google Organic"

    return scored


def _leadbot_is_page_one_organic_result(item):
    """Return True only for a real organic SERP position from 1 through 10."""
    if not isinstance(item, dict):
        return False

    source = str(item.get("source") or "").strip().lower()
    serp_page = str(item.get("serp_page") or "").strip().lower()
    if source == "google_places" or serp_page == "google places":
        return False

    try:
        position = int(str(item.get("serp_position") or "").strip())
    except (TypeError, ValueError):
        return False

    return 1 <= position <= 10


def find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
    """
    on_candidate: optional callback invoked synchronously, once, for each
    candidate the instant it clears the same scoring/dedupe/threshold gate
    used below -- before this function returns. Selection is first-N-that-
    qualify in discovery order (page 1 before page 2, organic before the
    Google Places supplement), not a global best-of-all-pages sort, so that
    an early candidate's acceptance is final and never retracted by a later,
    higher-scoring one. Must be side-effect-light and must not raise; any
    exception from it is caught and ignored so a UI/publishing failure can
    never break the underlying scan.
    """
    query = make_query(industry, market, service_keyword)

    disable_places_main = False
    try:
        import os as _leadbot_os
        disable_places_main = (_leadbot_os.getenv("LEADBOT_DISABLE_PLACES_MAIN") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
    except Exception as exc:
        print(f"LEADBOT DROP GOOGLE PLACES MAIN ROWS ERROR: {exc}", flush=True)

    leads = []
    seen_domains = set()

    def _process_item(item):
        """Score one raw SERP/Places item; return the finalized lead dict if
        it clears every existing gate (dedupe, score >= 50), else None.
        Identical logic to the original single-pass loop, just callable
        per-item so it can run once a page/supplement item is available
        instead of only after every page has been fetched."""
        # Page-one organic businesses are already ranking successfully and
        # are not Lead Finder prospects. Reject them before scoring or
        # contact crawling so they cannot consume downstream work, stream,
        # count toward the quota, or reach a new export.
        if _leadbot_is_page_one_organic_result(item):
            return None

        scored = score_lead(item, industry, market)

        scored = _leadbot_preserve_source_metadata(scored, item)
        # Google Places returns real local businesses, not normal organic SERP rows.
        # Preserve those fields and keep them from being discarded by the older SERP score gate.
        if isinstance(item, dict) and item.get("source") == "google_places":
            for key in [
                "source",
                "lead_source_label",
                "rank_group",
                "rank_absolute",
                "dataforseo_cost",
                "places_position",
                "place_id",
                "address",
                "formatted_address",
                "business_address",
                "google_maps_url",
                "rating",
                "review_count",
            ]:
                if item.get(key) and not scored.get(key):
                    scored[key] = item.get(key)

            places_phone = item.get("best_phone") or item.get("phone") or ""
            if places_phone and not scored.get("best_phone"):
                scored["best_phone"] = places_phone

            scored["score"] = max(int(scored.get("score") or 0), 82)
            scored["lead_source_label"] = scored.get("lead_source_label") or "Google Places"

            # Places is not organic Page 1, but local/maps position still matters.
            # Force the page label so Google Places does not appear as fake organic Page 1.
            scored["serp_page"] = "Google Places"
            scored["serp_position"] = item.get("places_position") or item.get("serp_position") or scored.get("serp_position") or ""
            scored["places_position"] = item.get("places_position") or item.get("serp_position") or scored.get("places_position") or scored.get("serp_position") or ""

        domain = scored.get("domain") or ""

        # Google Places may return a phone/address/local profile without a website.
        # Keep those restaurant leads, but dedupe them safely by place_id/name/phone.
        if isinstance(item, dict) and item.get("source") == "google_places":
            places_key = (
                item.get("place_id")
                or item.get("title")
                or item.get("name")
                or item.get("phone")
                or item.get("best_phone")
                or domain
            )
            dedupe_key = f"google_places:{places_key}".lower().strip()
            if not domain:
                scored["domain"] = "Google Places"
        else:
            dedupe_key = str(domain or "").lower().strip()

        if not dedupe_key or dedupe_key in seen_domains:
            return None

        seen_domains.add(dedupe_key)

        if scored["score"] < 50:
            return None

        places_phone = ""
        places_address = ""

        if isinstance(item, dict) and item.get("source") == "google_places":
            places_phone = item.get("best_phone") or item.get("phone") or scored.get("best_phone") or ""
            places_address = (
                item.get("address")
                or item.get("formatted_address")
                or item.get("business_address")
                or scored.get("address")
                or ""
            )

        if _lead_bot_fast_mode():
            contact = {
                "best_phone": "",
                "phones": [],
                "emails": [],
                "contact_page_url": "",
                "confidence": 0,
                "flags": ["fast_mode_no_contact_crawl"],
            }
        else:
            contact = extract_contact_from_url(scored["url"], market=market)

        scored["best_phone"] = contact.get("best_phone", "") or places_phone
        scored["phones"] = contact.get("phones", []) or ([places_phone] if places_phone else [])
        scored["emails"] = contact.get("emails", [])
        scored["contact_page_url"] = contact.get("contact_page_url", "")
        scored["contact_confidence"] = contact.get("confidence", 0)

        if isinstance(item, dict) and item.get("source") == "google_places":
            if scored["best_phone"] and not scored["contact_confidence"]:
                scored["contact_confidence"] = 75

            if places_address:
                scored["address"] = places_address
                scored["formatted_address"] = places_address
                scored["business_address"] = places_address

        scored["contact_flags"] = contact.get("flags", [])

        if scored["emails"] and scored["best_phone"]:
            scored["outreach_status"] = "email_and_call_ready"
        elif scored["best_phone"]:
            scored["outreach_status"] = "call_ready"
        elif scored["emails"]:
            scored["outreach_status"] = "email_ready"
        else:
            scored["outreach_status"] = "needs_manual_research"

        # Final score should prioritize page 1/page 2/page 3/page 4 SEO opportunity,
        # but avoid making every good lead a 100.
        is_google_places = isinstance(item, dict) and item.get("source") == "google_places"

        serp_position = scored.get("serp_position")
        contact_confidence = int(scored.get("contact_confidence") or 0)

        final_score = 60

        if serp_position and not is_google_places:
            pos = int(serp_position)

            if 11 <= pos <= 20:
                final_score += 18
            elif 21 <= pos <= 30:
                final_score += 16
            elif pos <= 10:
                # Organic page 1 is more competitive, so it gets the smallest organic boost.
                final_score += 6
            else:
                final_score += 8

        if scored["outreach_status"] == "email_and_call_ready":
            final_score += 12
        elif scored["outreach_status"] == "call_ready":
            final_score += 7
        elif scored["outreach_status"] == "email_ready":
            final_score += 6
        else:
            final_score -= 20

        if contact_confidence >= 90:
            final_score += 5
        elif contact_confidence >= 60:
            final_score += 2
        elif contact_confidence < 40:
            final_score -= 12

        base_score = int(scored.get("score") or 0)

        if base_score >= 95:
            final_score += 3
        elif base_score >= 85:
            final_score += 1
        elif base_score < 70:
            final_score -= 10

        # Keep a little separation at the top.
        if scored["outreach_status"] != "email_and_call_ready":
            final_score = min(final_score, 94)

        if contact_confidence < 90:
            final_score = min(final_score, 94)

        scored["final_lead_score"] = max(0, min(100, final_score))

        finalized = leadbot_normalize_lead_object(scored)
        if not finalized:
            return None
        finalized = _leadbot_safe_lead_dict(finalized)
        if not finalized:
            return None

        return finalized

    def _accept(finalized):
        """Record an already-fully-scored, already-gated lead and, if a
        caller supplied on_candidate, publish it immediately. Returns True
        once `limit` has been reached (signal to stop fetching more)."""
        leads.append(finalized)

        if on_candidate is not None:
            try:
                on_candidate(finalized)
            except Exception as exc:
                print(f"LEADBOT ON_CANDIDATE CALLBACK ERROR: {exc}", flush=True)

        return len(leads) >= limit

    # Selection is first-N-that-qualify in discovery order: organic pages
    # 1-4 in order, then (only if still short of `limit`) the Google Places
    # supplement for food-type queries. This intentionally replaces the old
    # "collect every page, score everything, sort by final_lead_score,
    # truncate to `limit`" behavior -- that global sort/truncate required
    # every page to be fetched before any single lead was safe to publish,
    # which is exactly the latency this per-page early-emission exists to
    # remove. Every candidate still passes the identical scoring/dedupe/
    # score>=50 gate in _process_item(); only the cross-page ranking step
    # was removed.
    quota_reached = False

    for serp_page in (1, 2, 3, 4):
        if quota_reached:
            break

        page_results = find_business_competitors(
            query,
            own_domain=own_domain,
            location=market or "United States",
            limit=max(limit * 2, 20),
            pages=[serp_page],
        )

        for item in page_results or []:
            if disable_places_main and isinstance(item, dict) and item.get("source") == "google_places":
                # Mirrors the original HARD SAFETY guard: never let Places
                # rows count as organic-page output when the flag is set.
                continue

            finalized = _process_item(item)
            if finalized is None:
                continue

            if _accept(finalized):
                quota_reached = True
                break

    # Restaurant/food searches need Google Places as a supplement because
    # organic SERPs are packed with listicles, directories, travel sites, and
    # forums. Only run this (and only score/emit as many as still needed) if
    # the organic pages above did not already fill the quota.
    if not quota_reached and not disable_places_main:
        try:
            places_text = f"{industry} {service_keyword} {query}".lower()
            places_terms = (
                "restaurant",
                "restaurants",
                "dining",
                "food",
                "cafe",
                "coffee",
                "bakery",
                "bar",
                "grill",
                "bistro",
                "eatery",
            )

            if any(term in places_text for term in places_terms):
                from business_competitor_finder import _leadbot_google_places_search

                places_results = []
                for places_page in (1, 2):
                    page_results = _leadbot_google_places_search(
                        keyword=service_keyword or industry or query,
                        location=market or "United States",
                        page=places_page,
                        num=20,
                    )
                    if page_results:
                        places_results.extend(list(page_results or []))

                if places_results:
                    print(f"LEADBOT GOOGLE PLACES SUPPLEMENT ADDED: {len(places_results)}", flush=True)

                for item in places_results:
                    if quota_reached:
                        break

                    finalized = _process_item(item)
                    if finalized is None:
                        continue

                    if _accept(finalized):
                        quota_reached = True

        except Exception as exc:
            print(f"LEADBOT GOOGLE PLACES SUPPLEMENT ERROR: {exc}", flush=True)

    return {
        "query": query,
        "industry": industry,
        "market": market,
        "count": len(leads[:limit]),
        "leads": leads[:limit],
    }
