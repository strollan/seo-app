from urllib.parse import urlparse

from serper_client import google_search, get_organic_results


try:
    from agents.dataforseo_serp_agent import search_google_organic
except Exception:
    search_google_organic = None

# === LEADBOT NON-SERPER FALLBACK START ===

# === LEADBOT FAST BLOCKLIST FILTER START ===

# === LEADBOT ROOT DOMAIN BLOCK MATCH START ===
def _leadbot_block_normalize_domain(value):
    """
    Normalize URLs/domains so blocking joe.coffee blocks:
    - joe.coffee
    - www.joe.coffee
    - https://joe.coffee/menu
    - order.joe.coffee
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""

    raw = raw.replace("\\", "/")

    if "://" not in raw:
        test = "http://" + raw
    else:
        test = raw

    try:
        parsed = urlparse(test)
        host = parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        host = raw.split("/")[0]

    host = host.split("@")[-1].split(":")[0].strip().strip(".").lower()

    if host.startswith("www."):
        host = host[4:]

    return host


def _leadbot_block_domain_candidates(row_or_value):
    values = []

    if isinstance(row_or_value, dict):
        for key in (
            "domain", "website", "url", "link", "business_url",
            "landing_page", "displayed_url", "final_url",
            "contact_page", "source_url"
        ):
            v = row_or_value.get(key)
            if v:
                values.append(v)
    else:
        values.append(row_or_value)

    out = set()

    for value in values:
        host = _leadbot_block_normalize_domain(value)
        if not host:
            continue

        out.add(host)

        if host.startswith("www."):
            out.add(host[4:])
        else:
            out.add("www." + host)

        parts = host.split(".")
        if len(parts) >= 2:
            root = ".".join(parts[-2:])
            out.add(root)
            out.add("www." + root)

    return out


def _leadbot_load_fast_block_rules():
    path = Path("data/leadbot_fast_blocklist.json")
    if not path.exists():
        return set(), set()

    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception:
        return set(), set()

    domains = set()
    patterns = set()

    for item in data.get("domains", []) or []:
        host = _leadbot_block_normalize_domain(item)
        if host:
            domains.add(host)
            if host.startswith("www."):
                domains.add(host[4:])

    for item in data.get("patterns", []) or []:
        item = str(item or "").strip().lower()
        if item:
            patterns.add(item)

    return domains, patterns


def _leadbot_is_fast_blocked_row(row_or_value):
    domains, patterns = _leadbot_load_fast_block_rules()
    candidates = _leadbot_block_domain_candidates(row_or_value)

    for host in candidates:
        if host in domains:
            return True

        for pattern in patterns:
            if pattern.startswith("*."):
                suffix = pattern[1:]  # .joe.coffee
                if host.endswith(suffix) or host == suffix.lstrip("."):
                    return True
            elif pattern and pattern in host:
                return True

    return False
# === LEADBOT ROOT DOMAIN BLOCK MATCH END ===


def _leadbot_fast_blocked_domains():
    try:
        import json
        from pathlib import Path

        path = Path("data") / "leadbot_fast_blocklist.json"

        if not path.exists():
            return set()

        data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
        domains = data.get("domains") or {}
        patterns = data.get("patterns") or {}

        blocked = set()

        if isinstance(domains, dict):
            blocked.update(str(k).strip().lower() for k in domains.keys() if str(k).strip())

        if isinstance(domains, list):
            blocked.update(str(k).strip().lower() for k in domains if str(k).strip())

        if isinstance(patterns, dict):
            blocked.update(str(k).strip().lower() for k in patterns.keys() if str(k).strip())

        if isinstance(patterns, list):
            blocked.update(str(k).strip().lower() for k in patterns if str(k).strip())

        return blocked
    except Exception:
        return set()
# === LEADBOT FAST BLOCKLIST FILTER END ===


def _leadbot_env_flag(name: str, default: str = "") -> str:
    import os

    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=True)
    except Exception:
        pass

    return str(os.getenv(name, default) or "").strip()


def _leadbot_use_live_serp() -> bool:
    return _leadbot_env_flag("USE_LIVE_SERP", "false").lower() in {"1", "true", "yes", "y", "on"}


def _leadbot_clean_text(value: str, limit: int = 500) -> str:
    import re

    value = re.sub(r"\s+", " ", str(value or "").strip())
    return value[:limit].strip()


def _leadbot_build_fallback_query(keyword: str, location: str = "United States") -> str:
    keyword = _leadbot_clean_text(keyword, 200)
    location = _leadbot_clean_text(location, 120)

    if not keyword:
        return location

    if not location or location.lower() in {"united states", "usa", "us"}:
        return keyword

    # Avoid doubling: "plumber Chula Vista CA Chula Vista CA"
    if location.lower() in keyword.lower():
        return keyword

    return f"{keyword} {location}".strip()


def _leadbot_google_places_search(keyword: str, location: str = "United States", page: int = 1, num: int = 10):
    """
    Google Places fallback for LeadBot.

    This is not organic SERP ranking. It is local business discovery.
    That is fine for LeadBot because we need business names, websites,
    phones, addresses, and local prospects.
    """

    # HARD SAFETY:
    # Do not allow Google Places to feed the main LeadBot lead list.
    # Address enrichment uses separate address_finding_agent/app helpers, not this function.
    import os as _leadbot_places_os
    if (_leadbot_places_os.getenv("LEADBOT_DISABLE_PLACES_MAIN") or "").strip().lower() in {"1", "true", "yes", "on"}:
        print(
            f"LEADBOT GOOGLE PLACES MAIN DISABLED: query={keyword!r} location={location!r}",
            flush=True,
        )
        return []

    import os
    import requests
    from pathlib import Path

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(".env"), override=True)
    except Exception:
        pass

    api_key = (os.getenv("GOOGLE_PLACES_API_KEY") or "").strip()

    if not api_key:
        return []

    query = _leadbot_build_fallback_query(keyword, location)

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.websiteUri,"
            "places.googleMapsUri,"
            "places.businessStatus,"
            "places.rating,"
            "places.userRatingCount"
        ),
    }

    payload = {
        "textQuery": query,
        "pageSize": max(1, min(int(num or 10), 20)),
    }

    response = requests.post(url, headers=headers, json=payload, timeout=20)

    if not response.ok:
        print(
            f"LEADBOT GOOGLE PLACES ERROR: status={response.status_code} body={response.text[:400]}",
            flush=True,
        )
        response.raise_for_status()

    data = response.json() or {}
    places = data.get("places") or []

    results = []

    for idx, place in enumerate(places, 1):
        name = ((place.get("displayName") or {}).get("text") or "").strip()
        website = (place.get("websiteUri") or "").strip()
        maps_url = (place.get("googleMapsUri") or "").strip()
        address = (place.get("formattedAddress") or "").strip()
        phone = (place.get("nationalPhoneNumber") or "").strip()
        rating = place.get("rating", "")
        reviews = place.get("userRatingCount", "")

        # LeadBot is built around domains, so prefer places with websites.
        # If no website exists, skip for now to avoid maps.google.com becoming the lead domain.
        if not website:
            continue

        snippet_parts = []
        if address:
            snippet_parts.append(address)
        if phone:
            snippet_parts.append(phone)
        if rating:
            snippet_parts.append(f"Rating {rating}")
        if reviews:
            snippet_parts.append(f"{reviews} reviews")

        results.append({
            "title": name or website,
            "link": website,
            "snippet": " · ".join(str(x) for x in snippet_parts if x),
            "place_id": place.get("id", ""),
            "address": address,
            "formatted_address": address,
            "business_address": address,
            "best_phone": phone,
            "phone": phone,
            "google_maps_url": maps_url,
            "rating": rating,
            "review_count": reviews,
            "source": "google_places",
            "lead_source_label": "Google Places",
            # Google Places is not organic Page 1, but its result order still matters.
            # Store it as a local/maps position so LeadBot keeps the ranking signal.
            "serp_page": "Google Places",
            "serp_position": idx,
            "places_position": idx,
        })

    print(
        f"LEADBOT GOOGLE PLACES: query={query!r} page={page} results={len(results)}",
        flush=True,
    )

    return _leadbot_filter_fast_blocked_rows(results)


def _leadbot_bing_search(query: str, page: int = 1, num: int = 10):
    import requests
    from bs4 import BeautifulSoup

    first = max(1, ((int(page or 1) - 1) * int(num or 10)) + 1)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    response = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "first": str(first), "count": str(num or 10)},
        headers=headers,
        timeout=18,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text or "", "html.parser")
    results = []

    for block in soup.select("li.b_algo"):
        a = block.select_one("h2 a")
        if not a:
            continue

        title = _leadbot_clean_text(a.get_text(" ", strip=True), 300)
        link = str(a.get("href") or "").strip()

        snippet_el = block.select_one(".b_caption p") or block.select_one("p")
        snippet = _leadbot_clean_text(snippet_el.get_text(" ", strip=True), 500) if snippet_el else ""

        if not title or not link:
            continue

        if "bing.com" in link:
            continue

        results.append({
            "title": title,
            "link": link,
            "snippet": snippet,
        })

        if len(results) >= int(num or 10):
            break

    return _leadbot_filter_fast_blocked_rows(results)



def _leadbot_serp_provider() -> str:
    """
    LeadBot SERP source selector.

    dataforseo = real organic Google SERP
    google_places = local/maps fallback
    """
    import os as _os
    return (_os.getenv("LEADBOT_SERP_PROVIDER") or "").strip().lower()




def _leadbot_non_serper_search(keyword: str, location: str = "United States", page: int = 1, num: int = 10):
    """
    LeadBot organic search.

    HARD RULE:
    - Main LeadBot list uses DataForSEO organic only.
    - Google Places must not appear as Page Google Places in the main lead list.
    - Places can still be used elsewhere for address enrichment, but not here.
    """
    import os
    from pathlib import Path

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    except Exception:
        pass

    keyword = str(keyword or "").strip()
    location = str(location or "United States").strip()

    # DataForSEO depth 40 already gives page 1-4 style organic results.
    if int(page or 1) > 1:
        return []

    query = _leadbot_build_fallback_query(keyword, location)

    if os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() != "1":
        print(
            f"LEADBOT DATAFORSEO DISABLED: query={query!r} location={location!r}",
            flush=True,
        )
        return []

    # Restaurants/food SERPs are directory and article-heavy, so pull deeper
    # automatically. DATAFORSEO_DEPTH in .env still overrides this.
    depth_text = f"{query} {location}".lower()
    restaurant_depth_terms = (
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
    default_depth = 60 if any(term in depth_text for term in restaurant_depth_terms) else 40

    try:
        depth = int(os.getenv("DATAFORSEO_DEPTH") or str(default_depth))
    except Exception:
        depth = default_depth

    depth = max(10, min(depth, 100))

    try:
        from agents.dataforseo_serp_agent import search_google_organic

        results = search_google_organic(
            query,
            location,
            depth=depth,
        )

        print(
            f"LEADBOT DATAFORSEO ORGANIC ONLY: query={query!r} location={location!r} depth={depth} results={len(results or [])}",
            flush=True,
        )

        return list(results or [])

    except Exception as exc:
        print(
            f"LEADBOT DATAFORSEO ORGANIC ONLY ERROR: query={query!r} location={location!r} error={exc}",
            flush=True,
        )
        return []

# === LEADBOT NON-SERPER FALLBACK END ===

from serp_filters import filter_business_results
from serp_filters import final_filter_business_results


def normalize_domain(url):
    parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
    domain = parsed.netloc.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def _raw_find_business_competitors(keyword, own_domain=None, location="United States", limit=5, pages=None):
    """
    Pull Google SERPs through Serper, remove directories/forums/listicles,
    and return actual business websites.

    LeadBot strategy:
    - Page 1 gives the top 6-10 visible competitors/prospects.
    - Page 4 gives positions 31-40, usually better SEO outreach opportunities.
    """

    pages = pages or [1, 2, 3, 4]

    organic_results = []

    for serp_page in pages:
        if _leadbot_use_live_serp():
            try:
                data = google_search(keyword, location=location, page=serp_page, num=10)
                page_results = get_organic_results(data)
            except Exception as exc:
                print(f"LEADBOT SERPER ERROR; USING NON-SERPER FALLBACK: {exc}", flush=True)
                page_results = _leadbot_non_serper_search(keyword, location=location, page=serp_page, num=10)
        else:
            page_results = _leadbot_non_serper_search(keyword, location=location, page=serp_page, num=10)

        for idx, result in enumerate(page_results, 1):
            if isinstance(result, dict) and result.get("source") in {"dataforseo", "google_places"}:
                # DataForSEO and Google Places already carry their own ranking signals.
                # Do not overwrite real organic position or local/maps position.
                result.setdefault("serp_page", serp_page)
                result.setdefault("serp_position", ((serp_page - 1) * 10) + idx)
            else:
                result["serp_page"] = serp_page
                result["serp_position"] = ((serp_page - 1) * 10) + idx

            result["query_used"] = result.get("query_used") or keyword

        organic_results.extend(page_results)

        # Google Places Text Search does not work like organic SERP pagination here.
        # Without a page token, page 1-4 can repeat the same businesses and waste API calls.
        if any(isinstance(item, dict) and item.get("source") == "google_places" for item in page_results):
            break

    # Main list stays organic/DataForSEO.
    business_results = filter_business_results(organic_results)

    # HARD SAFETY: main LeadBot list must stay organic/DataForSEO.
    # Drop Google Places rows here even if an older path accidentally produced them.
    business_results = [
        item for item in business_results
        if not (isinstance(item, dict) and item.get("source") == "google_places")
    ]

    fast_blocked_domains = _leadbot_fast_blocked_domains()

    if fast_blocked_domains:
        before_fast_block_filter = len(business_results or [])
        business_results = [
            item for item in (business_results or [])
            if (
                normalize_domain(item.get("link") or item.get("url") or item.get("website") or "") not in fast_blocked_domains
                and not any(
                    pattern.startswith("*.") and normalize_domain(item.get("link") or item.get("url") or item.get("website") or "").endswith(pattern[1:])
                    for pattern in fast_blocked_domains
                )
            )
        ]
        dropped_fast_blocked = before_fast_block_filter - len(business_results or [])
        if dropped_fast_blocked:
            print(f"LEADBOT FAST BLOCKLIST FILTER DROPPED: {dropped_fast_blocked}", flush=True)

    own_domain_clean = normalize_domain(own_domain) if own_domain else None

    competitors = []
    seen_result_keys = set()

    for result in business_results:
        link = result.get("link") or ""
        domain = normalize_domain(link)

        if own_domain_clean and domain == own_domain_clean:
            continue

        # Dedupe exact repeats from fallback sources.
        # For Google Places, place_id is safest because franchise locations can share a domain.
        if isinstance(result, dict) and result.get("source") == "google_places":
            dedupe_key = result.get("place_id") or link or domain
        else:
            dedupe_key = link or domain

        dedupe_key = str(dedupe_key or "").strip().lower()

        if dedupe_key and dedupe_key in seen_result_keys:
            continue

        if dedupe_key:
            seen_result_keys.add(dedupe_key)

        competitors.append({
            "title": result.get("title", ""),
            "url": link,
            "domain": domain,
            "snippet": result.get("snippet", ""),
            "serp_page": result.get("serp_page"),
            "serp_position": result.get("serp_position"),
            "source": result.get("source", ""),
            "lead_source_label": result.get("lead_source_label", ""),
            "rank_group": result.get("rank_group", ""),
            "rank_absolute": result.get("rank_absolute", ""),
            "dataforseo_cost": result.get("cost", ""),
            "lead_source_label": result.get("lead_source_label", ""),
            "places_position": result.get("places_position", ""),
            "place_id": result.get("place_id", ""),
            "address": result.get("address", ""),
            "formatted_address": result.get("formatted_address", ""),
            "business_address": result.get("business_address", ""),
            "best_phone": result.get("best_phone", "") or result.get("phone", ""),
            "phone": result.get("phone", ""),
            "google_maps_url": result.get("google_maps_url", ""),
            "rating": result.get("rating", ""),
            "review_count": result.get("review_count", ""),
        })

        if len(competitors) >= limit:
            break

    # Google Places already returns real local business entities.
    # Do not run old organic-SERP cleanup filters on these, or valid local leads can be removed.
    if any(isinstance(item, dict) and item.get("source") == "google_places" for item in competitors):
        return _leadbot_filter_fast_blocked_rows(competitors)

    competitors = final_filter_business_results(competitors)
    return _leadbot_filter_fast_blocked_rows(competitors)


# === BUSINESS COMPETITOR INDUSTRY QUALITY GUARD ===

INDUSTRY_SIGNAL_WORDS = {
    "roofing": {
        "good": ["roof", "roofing", "roofer", "roofers", "shingle", "flat roof", "roof repair", "roof replacement"],
        "bad": ["plumbing", "plumber", "drain", "water heater", "painting", "painter", "electrical", "electrician", "dentist", "lawyer", "attorney"],
    },
    "plumbing": {
        "good": ["plumbing", "plumber", "plumbers", "drain", "water heater", "sewer", "pipe", "leak repair"],
        "bad": ["roofing", "roofer", "roof repair", "painting", "painter", "electrical", "electrician", "dentist", "lawyer", "attorney"],
    },
    "painting": {
        "good": ["painting", "painter", "painters", "paint contractor", "interior painting", "exterior painting"],
        "bad": ["roofing", "roofer", "plumbing", "plumber", "electrical", "electrician", "dentist", "lawyer", "attorney"],
    },
    "electrical": {
        "good": ["electrical", "electrician", "electricians", "electric", "panel", "wiring", "lighting"],
        "bad": ["roofing", "roofer", "plumbing", "plumber", "painting", "painter", "dentist", "lawyer", "attorney"],
    },
}

def _detect_query_industry_for_competitor_guard(text):
    blob = str(text or "").lower()
    scores = {}

    for industry, data in INDUSTRY_SIGNAL_WORDS.items():
        score = 0
        for word in data["good"]:
            if word in blob:
                score += 2
        for word in data["bad"]:
            if word in blob:
                score -= 2
        scores[industry] = score

    best_industry, best_score = max(scores.items(), key=lambda item: item[1])
    return best_industry if best_score > 0 else ""

def _result_text_for_competitor_guard(item):
    if isinstance(item, dict):
        return " ".join(str(item.get(k, "") or "") for k in ("domain", "url", "link", "title", "snippet", "description"))
    return str(item or "")

def _is_same_industry_business_result(item, target_industry):
    if not target_industry:
        return True

    blob = _result_text_for_competitor_guard(item).lower()
    rules = INDUSTRY_SIGNAL_WORDS.get(target_industry, {})
    good = rules.get("good", [])
    bad = rules.get("bad", [])

    good_hits = sum(1 for word in good if word in blob)
    bad_hits = sum(1 for word in bad if word in blob)

    # Strong reject: wrong-industry words with no matching industry signal.
    if bad_hits and not good_hits:
        return False

    # Mixed result: allow only if target industry is clearly stronger.
    if bad_hits and good_hits < bad_hits:
        return False

    return True

def _sort_business_results_by_industry_quality(results, target_industry):
    if not isinstance(results, list):
        return _leadbot_filter_fast_blocked_rows(results)

    def score(item):
        blob = _result_text_for_competitor_guard(item).lower()
        rules = INDUSTRY_SIGNAL_WORDS.get(target_industry, {})
        good = rules.get("good", [])
        bad = rules.get("bad", [])

        value = 0
        for word in good:
            if word in blob:
                value += 3
        for word in bad:
            if word in blob:
                value -= 5

        # Prefer real company/service-looking domains over vague pages.
        if any(x in blob for x in ["services", "contractor", "company", "inc", "llc"]):
            value += 1

        return value

    return sorted(results, key=score, reverse=True)


def find_business_competitors(*args, **kwargs):
    """
    Wrapper around the original competitor finder.
    Filters mixed-industry SERP results before they reach /analyze.
    """
    results = _raw_find_business_competitors(*args, **kwargs)

    query_blob = " ".join(str(x or "") for x in args) + " " + " ".join(str(v or "") for v in kwargs.values())
    target_industry = _detect_query_industry_for_competitor_guard(query_blob)

    if not target_industry or not isinstance(results, list):
        return _leadbot_filter_fast_blocked_rows(results)

    filtered = [item for item in results if _is_same_industry_business_result(item, target_industry)]
    filtered = _sort_business_results_by_industry_quality(filtered, target_industry)

    # If the filter is too strict, fall back to original results instead of returning nothing.
    return filtered or results


# === LEADBOT ROOT DOMAIN FINAL FILTER START ===
def _leadbot_filter_fast_blocked_rows(rows):
    clean = []
    for row in rows or []:
        try:
            if _leadbot_is_fast_blocked_row(row):
                continue
        except Exception:
            pass
        clean.append(row)
    return clean
# === LEADBOT ROOT DOMAIN FINAL FILTER END ===


# === LEADBOT TV STATION FILTER WRAPPER START ===
# Keep local TV/news stations out of LeadBot competitor results.
try:
    from agents.lead_tv_station_filter_agent import filter_tv_station_leads

    _leadbot_original_find_business_competitors = find_business_competitors

    def find_business_competitors(*args, **kwargs):
        results = _leadbot_original_find_business_competitors(*args, **kwargs)

        try:
            before_count = len(results or [])
        except Exception:
            before_count = "?"

        filtered = filter_tv_station_leads(results)

        try:
            after_count = len(filtered or [])
        except Exception:
            after_count = "?"

        print(
            f"LEADBOT TV FILTER DEBUG: before={before_count} after={after_count} args={args} kwargs={kwargs}",
            flush=True,
        )

        return filtered

except Exception as exc:
    print(f"LEADBOT TV FILTER WRAPPER DISABLED: {exc}", flush=True)
# === LEADBOT TV STATION FILTER WRAPPER END ===


# === LEADBOT QUALITY GATE WRAPPER START ===
# Runs after existing competitor finder + TV filter wrapper.
try:
    from agents.lead_quality_gate_agent import quality_gate_leads

    _leadbot_quality_gate_original_find_business_competitors = find_business_competitors

    def find_business_competitors(*args, **kwargs):
        results = _leadbot_quality_gate_original_find_business_competitors(*args, **kwargs)
        return quality_gate_leads(results)

except Exception as exc:
    print(f"LEADBOT QUALITY GATE WRAPPER DISABLED: {exc}", flush=True)
# === LEADBOT QUALITY GATE WRAPPER END ===

