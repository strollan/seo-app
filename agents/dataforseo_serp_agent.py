"""
DataForSEO SERP Agent

Pulls real Google Organic SERP results and returns LeadBot-friendly rows:
- title
- link
- snippet
- organic page
- organic position
- absolute SERP position
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# === LEADBOT DATAFORSEO QUERY CLEANUP START ===
def _leadbot_clean_query_piece(value):
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _leadbot_norm_query_piece(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def leadbot_build_dataforseo_query(keyword, market=""):
    """
    Build a clean DataForSEO organic query.

    Prevents duplicated market text like:
      pet store Los Angeles CA Los Angeles CA

    Keeps useful market text when keyword is broad:
      pet store + Los Angeles CA -> pet store Los Angeles CA
    """
    keyword = _leadbot_clean_query_piece(keyword)
    market = _leadbot_clean_query_piece(market)

    if not keyword:
        return market

    if not market:
        return keyword

    keyword_norm = _leadbot_norm_query_piece(keyword)
    market_norm = _leadbot_norm_query_piece(market)

    if keyword_norm == market_norm:
        return keyword

    if keyword_norm.endswith(market_norm):
        return keyword

    if market_norm and market_norm in keyword_norm:
        return keyword

    return f"{keyword} {market}".strip()
# === LEADBOT DATAFORSEO QUERY CLEANUP END ===




def _load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return

    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()

        if not raw or raw.startswith("#") or "=" not in raw:
            continue

        if raw.startswith("export "):
            raw = raw.replace("export ", "", 1).strip()

        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default

    try:
        return int(value)
    except Exception:
        return default


def _auth_header() -> Dict[str, str]:
    _load_env()

    login = os.getenv("DATAFORSEO_LOGIN", "").strip()
    password = os.getenv("DATAFORSEO_PASSWORD", "").strip()

    if not login or not password:
        raise RuntimeError("DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD missing in .env")

    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("utf-8")

    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _location_name(market: str) -> str:
    market = (market or "").strip()

    # DataForSEO only accepts specific canonical location_name values.
    # Keep "Long Island" in the search keyword/query, but send a valid
    # nearby canonical location to DataForSEO so the API does not reject it.
    alias_map = {
        "long island": "New York,New York,United States",
        "long island ny": "New York,New York,United States",
        "long island new york": "New York,New York,United States",
        "nassau county": "New York,New York,United States",
        "nassau county ny": "New York,New York,United States",
        "suffolk county": "New York,New York,United States",
        "suffolk county ny": "New York,New York,United States",
        "nyc": "New York,New York,United States",
        "new york city": "New York,New York,United States",
        "brooklyn": "Brooklyn,New York,United States",
        "brooklyn ny": "Brooklyn,New York,United States",
        "queens": "Queens,New York,United States",
        "queens ny": "Queens,New York,United States",
        "bronx": "Bronx,New York,United States",
        "bronx ny": "Bronx,New York,United States",
        "manhattan": "New York,New York,United States",
        "manhattan ny": "New York,New York,United States",
        "staten island": "Staten Island,New York,United States",
        "staten island ny": "Staten Island,New York,United States",
    }

    normalized_market = " ".join(market.lower().replace(",", " ").split())
    if normalized_market in alias_map:
        return alias_map[normalized_market]

    state_map = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    }

    parts = market.split()

    if len(parts) >= 2:
        maybe_state = parts[-1].upper().replace(".", "")
        city = " ".join(parts[:-1]).strip()

        if maybe_state in state_map and city:
            return f"{city},{state_map[maybe_state]},United States"

    return market or "United States"


def search_google_organic(
    keyword: str,
    market: str = "",
    depth: Optional[int] = None,
) -> List[Dict[str, Any]]:
    _load_env()

    if os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() != "1":
        print("LEADBOT DATAFORSEO SERP DISABLED by LEADBOT_DATAFORSEO_ENABLED.", flush=True)
        return []

    keyword = (keyword or "").strip()
    market = (market or "").strip()

    if not keyword:
        return []

    depth = int(depth or _env_int("DATAFORSEO_DEPTH", 40))
    depth = max(10, min(depth, 100))

    language_code = os.getenv("DATAFORSEO_LANGUAGE_CODE", "en").strip() or "en"
    device = os.getenv("DATAFORSEO_DEVICE", "desktop").strip() or "desktop"
    os_name = os.getenv("DATAFORSEO_OS", "windows").strip() or "windows"

    url = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"

    payload = [
        {
            "keyword": keyword,
            "location_name": _location_name(market),
            "language_code": language_code,
            "device": device,
            "os": os_name,
            "depth": depth,
        }
    ]

    response = requests.post(
        url,
        headers=_auth_header(),
        json=payload,
        timeout=90,
    )

    response.raise_for_status()
    data = response.json()

    if data.get("status_code") != 20000:
        raise RuntimeError(
            f"DataForSEO API error: {data.get('status_code')} {data.get('status_message')}"
        )

    tasks = data.get("tasks") or []
    if not tasks:
        return []

    task = tasks[0]

    if task.get("status_code") != 20000:
        raise RuntimeError(
            f"DataForSEO task error: {task.get('status_code')} {task.get('status_message')}"
        )

    result_blocks = task.get("result") or []
    if not result_blocks:
        return []

    items = result_blocks[0].get("items") or []

    rows: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "organic":
            continue

        url_value = item.get("url") or ""
        if not url_value:
            continue

        rank_group = item.get("rank_group")
        rank_absolute = item.get("rank_absolute")

        try:
            organic_position = int(rank_group or 0)
        except Exception:
            organic_position = 0

        if organic_position <= 0:
            continue

        page = ((organic_position - 1) // 10) + 1

        rows.append(
            {
                "title": item.get("title") or "",
                "link": url_value,
                "snippet": item.get("description") or "",
                "source": "dataforseo",
                "lead_source_label": "Google Organic",
                "serp_page": page,
                "serp_position": organic_position,
                "rank_group": rank_group,
                "rank_absolute": rank_absolute,
                "query_used": keyword,
                "market": market,
                "cost": task.get("cost"),
            }
        )

    return rows


if __name__ == "__main__":
    results = search_google_organic("plumber chula vista ca", "Chula Vista CA", depth=10)

    print("COUNT:", len(results))

    for row in results:
        print(
            row.get("serp_page"),
            "|",
            row.get("serp_position"),
            "| abs",
            row.get("rank_absolute"),
            "|",
            row.get("title"),
            "|",
            row.get("link"),
        )

# === LEADBOT LONG ISLAND LOCAL SERP GUARDRAIL START ===
# Filters broad New York SERP leakage for Long Island / Suffolk / Nassau scans.
# DataForSEO needs broad NY location_name, but LeadBot should not show NYC/borough leads.

def _leadbot_market_scope_for_guardrail(market: str = "") -> str:
    value = " ".join(str(market or "").lower().replace(",", " ").split())

    if "suffolk" in value:
        return "suffolk"
    if "nassau" in value:
        return "nassau"
    if "long island" in value:
        return "long_island"

    return ""


def _leadbot_text_blob_for_guardrail(row: dict) -> str:
    keys = [
        "title",
        "domain",
        "url",
        "website",
        "absolute_url",
        "description",
        "snippet",
        "meta_description",
        "address",
        "location",
        "market",
        "query_used",
        "keyword_seed",
        "market_seed",
    ]

    parts = []

    if isinstance(row, dict):
        for key in keys:
            value = row.get(key)
            if value:
                parts.append(str(value))

    return " ".join(parts).lower()


_LEADBOT_NYC_REJECT_BITS = {
    " manhattan ",
    "new york, ny",
    "new york ny",
    "nyc",
    "upper east side",
    "upper west side",
    "midtown",
    "downtown nyc",
    "brooklyn",
    "queens",
    "bronx",
    "staten island",
    "long island city",
    "jersey city",
    "hoboken",
    "newark",
    "yonkers",
    "white plains",
    "westchester",
    "72nd st",
    "72nd street",
    "east 72",
    "west 72",
}

_LEADBOT_NASSAU_BITS = {
    "nassau",
    "hempstead",
    "north hempstead",
    "oyster bay",
    "glen cove",
    "long beach",
    "mineola",
    "garden city",
    "hicksville",
    "levittown",
    "bellmore",
    "merrick",
    "freeport",
    "rockville centre",
    "westbury",
    "plainview",
    "massapequa",
    "syosset",
    "manhasset",
    "great neck",
    "roslyn",
    "port washington",
}

_LEADBOT_SUFFOLK_BITS = {
    "suffolk",
    "babylon",
    "brookhaven",
    "huntington",
    "islip",
    "smithtown",
    "riverhead",
    "southold",
    "southampton",
    "east hampton",
    "shelter island",
    "amityville",
    "bay shore",
    "bohemia",
    "brentwood",
    "centereach",
    "central islip",
    "commack",
    "coram",
    "deer park",
    "dix hills",
    "east islip",
    "farmingville",
    "hauppauge",
    "holbrook",
    "holtsville",
    "kings park",
    "lake grove",
    "lindenhurst",
    "medford",
    "melville",
    "miller place",
    "mount sinai",
    "northport",
    "patchogue",
    "port jefferson",
    "ronkonkoma",
    "sayville",
    "selden",
    "shirley",
    "stony brook",
    "west islip",
    "yaphank",
}


def _leadbot_row_allowed_for_local_market(row: dict, market: str = "") -> bool:
    scope = _leadbot_market_scope_for_guardrail(market)

    if not scope:
        return True

    blob = " " + _leadbot_text_blob_for_guardrail(row) + " "

    # Hard reject NYC / borough / nearby non-LI leakage.
    for bad in _LEADBOT_NYC_REJECT_BITS:
        if bad in blob:
            return False

    # Suffolk-only scan: reject obvious Nassau results.
    if scope == "suffolk":
        if any((" " + bit + " ") in blob for bit in _LEADBOT_NASSAU_BITS):
            if not any((" " + bit + " ") in blob for bit in _LEADBOT_SUFFOLK_BITS):
                return False

    # Nassau-only scan: reject obvious Suffolk results.
    if scope == "nassau":
        if any((" " + bit + " ") in blob for bit in _LEADBOT_SUFFOLK_BITS):
            if not any((" " + bit + " ") in blob for bit in _LEADBOT_NASSAU_BITS):
                return False

    return True


_leadbot_original_search_google_organic = search_google_organic

def search_google_organic(*args, **kwargs):
    rows = _leadbot_original_search_google_organic(*args, **kwargs)

    try:
        market = kwargs.get("market", "")

        # Original signature is search_google_organic(keyword, market, ...)
        if not market and len(args) >= 2:
            market = args[1]

        if not isinstance(rows, list):
            return rows

        filtered = []
        removed = 0

        for row in rows:
            if _leadbot_row_allowed_for_local_market(row, market):
                filtered.append(row)
            else:
                removed += 1

        if removed:
            print(
                f"LEADBOT LOCAL SERP GUARDRAIL: removed {removed} broad/NYC rows for market={market}",
                flush=True,
            )

        return filtered

    except Exception as exc:
        print(f"LEADBOT LOCAL SERP GUARDRAIL ERROR: {exc}", flush=True)
        return rows
# === LEADBOT LONG ISLAND LOCAL SERP GUARDRAIL END ===

# === LEADBOT LEAD QUALITY GUARDRAIL START ===
# Removes obvious non-sales leads after SERP results return:
# directories, government pages, news, schools, hospitals, social pages,
# and nonprofit/shelter results for commercial pet-store style scans.

def _leadbot_quality_blob(row: dict, keyword: str = "", market: str = "") -> str:
    parts = [keyword, market]

    if isinstance(row, dict):
        for key in [
            "title",
            "domain",
            "url",
            "website",
            "absolute_url",
            "description",
            "snippet",
            "meta_description",
            "lead_source_label",
            "query_used",
            "keyword_seed",
            "market_seed",
        ]:
            value = row.get(key)
            if value:
                parts.append(str(value))

    return " ".join(parts).lower()


_LEADBOT_ALWAYS_REJECT_DOMAINS = {
    "yelp.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "tiktok.com",
    "wikipedia.org",
    "mapquest.com",
    "yellowpages.com",
    "angi.com",
    "angieslist.com",
    "thumbtack.com",
    "homeadvisor.com",
    "bbb.org",
    "manta.com",
    "chamberofcommerce.com",
    "merchantcircle.com",
    "nextdoor.com",
    "patch.com",
    "newsday.com",
    "nytimes.com",
}

_LEADBOT_ALWAYS_REJECT_BITS = {
    ".gov",
    " county government",
    " department of ",
    " official website",
    " public schools",
    " school district",
    " university",
    " college",
    " hospital",
    " medical center",
    " urgent care",
    " wikipedia",
    " directory",
    " reviews and ratings",
    " top 10 best",
    " best pros",
    " near me - yelp",
    " facebook",
    " instagram",
    " linkedin",
    " youtube",
    " tiktok",
    " yellow pages",
    " better business bureau",
}

_LEADBOT_PET_STORE_REJECT_BITS = {
    "spca",
    "s.p.c.a",
    "humane society",
    "animal shelter",
    "animal rescue",
    "pet adoption",
    "adopt a pet",
    "animal control",
    "lost and found pets",
    "veterinary hospital",
    "animal hospital",
    "veterinarian",
    "vet clinic",
    "dog park",
    "county park",
    "wildlife",
    "zoo",
}

_LEADBOT_DENTIST_REJECT_BITS = {
    "healthgrades",
    "zocdoc",
    "webmd",
    "sharecare",
    "opencare",
    "dentalplans",
    "insurance accepted",
    "find a doctor",
    "find a dentist",
}

_LEADBOT_PAINTING_REJECT_BITS = {
    "sherwin-williams",
    "home depot",
    "lowe's",
    "lowes",
    "benjamin moore",
    "paint store",
    "paint supplies",
}


def _leadbot_domain_from_row(row: dict) -> str:
    if not isinstance(row, dict):
        return ""

    for key in ["domain", "website", "url", "absolute_url"]:
        value = str(row.get(key) or "").lower().strip()
        if not value:
            continue

        value = value.replace("https://", "").replace("http://", "")
        value = value.replace("www.", "")
        value = value.split("/")[0].split("?")[0].split("#")[0]
        if value:
            return value

    return ""


def _leadbot_keyword_family(keyword: str = "") -> str:
    value = str(keyword or "").lower()

    if any(x in value for x in ["pet store", "pet shop", "pet supply", "pet supplies"]):
        return "pet_store"

    if any(x in value for x in ["dentist", "dental", "orthodont"]):
        return "dentist"

    if any(x in value for x in ["paint", "painter", "painting"]):
        return "painting"

    return ""


def _leadbot_row_allowed_for_quality(row: dict, keyword: str = "", market: str = "") -> bool:
    blob = " " + _leadbot_quality_blob(row, keyword, market) + " "
    domain = _leadbot_domain_from_row(row)
    family = _leadbot_keyword_family(keyword)

    if domain:
        for bad_domain in _LEADBOT_ALWAYS_REJECT_DOMAINS:
            if domain == bad_domain or domain.endswith("." + bad_domain):
                return False

        if domain.endswith(".gov"):
            return False

    for bad in _LEADBOT_ALWAYS_REJECT_BITS:
        if bad in blob:
            return False

    if family == "pet_store":
        for bad in _LEADBOT_PET_STORE_REJECT_BITS:
            if bad in blob:
                return False

    if family == "dentist":
        for bad in _LEADBOT_DENTIST_REJECT_BITS:
            if bad in blob:
                return False

    if family == "painting":
        for bad in _LEADBOT_PAINTING_REJECT_BITS:
            if bad in blob:
                return False

    return True


_leadbot_quality_original_search_google_organic = search_google_organic

def search_google_organic(*args, **kwargs):
    rows = _leadbot_quality_original_search_google_organic(*args, **kwargs)

    try:
        keyword = kwargs.get("keyword", "")
        market = kwargs.get("market", "")

        # Original signature is search_google_organic(keyword, market, ...)
        if not keyword and len(args) >= 1:
            keyword = args[0]
        if not market and len(args) >= 2:
            market = args[1]

        if not isinstance(rows, list):
            return rows

        filtered = []
        removed = 0

        for row in rows:
            if _leadbot_row_allowed_for_quality(row, keyword, market):
                filtered.append(row)
            else:
                removed += 1

        if removed:
            print(
                f"LEADBOT QUALITY GUARDRAIL: removed {removed} junk/non-business rows for keyword={keyword} market={market}",
                flush=True,
            )

        return filtered

    except Exception as exc:
        print(f"LEADBOT QUALITY GUARDRAIL ERROR: {exc}", flush=True)
        return rows
# === LEADBOT LEAD QUALITY GUARDRAIL END ===

