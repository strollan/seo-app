import json
import os
import re
import urllib.parse
import urllib.request
from html import unescape


# === GOOGLE PLACES QUOTA GUARD START ===
_GOOGLE_PLACES_QUOTA_STOPPED = False

def google_places_quota_stopped():
    return bool(_GOOGLE_PLACES_QUOTA_STOPPED)

def _google_places_quota_text(value):
    haystack = str(value or "").lower()
    return (
        "429" in haystack
        or "too many requests" in haystack
        or "quota" in haystack
        or "resource_exhausted" in haystack
        or "rate limit" in haystack
        or "403" in haystack
        or "forbidden" in haystack
        or "permission_denied" in haystack
        or "does not have permission" in haystack
        or "caller does not have permission" in haystack
    )

def _google_places_stop_for_quota(reason=""):
    global _GOOGLE_PLACES_QUOTA_STOPPED
    if not _GOOGLE_PLACES_QUOTA_STOPPED:
        print(
            "GOOGLE PLACES QUOTA GUARD: stopping address lookups for this process. "
            f"Reason: {str(reason)[:260]}",
            flush=True,
        )
    _GOOGLE_PLACES_QUOTA_STOPPED = True
# === GOOGLE PLACES QUOTA GUARD END ===


def google_places_disabled():
    return str(os.environ.get("LEADBOT_DISABLE_PLACES_MAIN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }




ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{2,80}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|"
    r"Place|Pl|Highway|Hwy|Route|Rte|Parkway|Pkwy|Turnpike|Tpke|Way|Circle|Cir)\b"
    r"(?:[, ]+[A-Za-z .'-]{2,60})?"
    r"(?:[, ]+[A-Z]{2})?"
    r"(?:\s+\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)


def clean_text(value):
    value = unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_domain(value):
    value = str(value or "").strip().lower()
    value = value.replace("https://", "").replace("http://", "")
    value = value.replace("www.", "").split("/")[0]
    return value


def clean_address(value):
    value = clean_text(value)
    value = value.strip(" -|,")
    bad = {"", "not found", "none", "null", "nan", "n/a", "unknown"}
    if value.lower() in bad:
        return ""
    return value




def is_full_street_address(value):
    value = clean_address(value)
    if not value:
        return False

    # Real address should usually have a street number and street-type word.
    if ADDRESS_RE.search(value):
        return True

    # Accept common formatted Google style: number + words + state/zip.
    if re.search(r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{3,}", value) and re.search(r"\b[A-Z]{2}\b|\b\d{5}(?:-\d{4})?\b", value):
        return True

    return False


def weak_address_value(value):
    value = clean_address(value)
    if not value:
        return True

    low = value.lower()
    weak_values = {
        "long island",
        "long island ny",
        "suffolk county",
        "suffolk county ny",
        "nassau county",
        "nassau county ny",
        "new york",
        "ny",
        "usa",
    }

    if low in weak_values:
        return True

    return not is_full_street_address(value)

def existing_address_from_row(row):
    keys = [
        "address",
        "full_address",
        "business_address",
        "formatted_address",
        "street_address",
        "place_address",
    ]

    for key in keys:
        found = clean_address(row.get(key))
        if found and is_full_street_address(found):
            return found

    street = clean_address(row.get("street") or row.get("address1") or row.get("address_1"))
    city = clean_address(row.get("city"))
    state = clean_address(row.get("state") or row.get("region"))
    zip_code = clean_address(row.get("zip") or row.get("zipcode") or row.get("postal_code"))

    parts = [x for x in [street, city, state, zip_code] if x]
    combined = ", ".join(parts)

    if combined and is_full_street_address(combined):
        return combined

    return ""


def fetch_url(url, timeout=8):
    if not url:
        return ""

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 LeadBot address lookup",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(700000)
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def address_from_json_ld(html):
    for blob in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue

            addr = item.get("address")
            if isinstance(addr, dict):
                street = clean_address(addr.get("streetAddress"))
                city = clean_address(addr.get("addressLocality"))
                region = clean_address(addr.get("addressRegion"))
                postal = clean_address(addr.get("postalCode"))
                parts = [x for x in [street, city, region, postal] if x]
                if parts:
                    return ", ".join(parts)

            if isinstance(addr, str):
                found = clean_address(addr)
                if found:
                    return found

            for value in item.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)

    return ""


def address_from_html(html):
    html = html or ""

    found = address_from_json_ld(html)
    if found:
        return found

    text = clean_text(html)

    for match in ADDRESS_RE.finditer(text):
        candidate = clean_address(match.group(0))
        if candidate and len(candidate) >= 10:
            return candidate

    return ""




def google_places_address(title, domain, market=""):
    if google_places_disabled():
        return ""

    if google_places_quota_stopped():
        return ""

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return ""

    query = " ".join(x for x in [title, domain, market] if x).strip()
    if not query:
        return ""

    url = "https://places.googleapis.com/v1/places:searchText"
    payload = json.dumps({
        "textQuery": query,
        "maxResultCount": 3,
        "languageCode": "en"
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri"
            },
        )

        with urllib.request.urlopen(req, timeout=10) as res:
            status = getattr(res, "status", "")
            if _google_places_quota_text(status):
                _google_places_stop_for_quota(f"HTTP status {status}")
                return ""
            print(f"GOOGLE PLACES STATUS: {status}", flush=True)
            data = json.loads(res.read().decode("utf-8", errors="ignore"))

        places = data.get("places") or []
        if not places:
            return ""

        domain_clean = clean_domain(domain)

        # Prefer a place whose website matches the lead domain.
        for place in places:
            website = clean_domain(place.get("websiteUri"))
            address = clean_address(place.get("formattedAddress"))
            if address and domain_clean and website and website == domain_clean:
                return address

        # Otherwise use first formatted address.
        for place in places:
            address = clean_address(place.get("formattedAddress"))
            if address:
                return address

    except Exception as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        if _google_places_quota_text(f"{e} {body}"):
            _google_places_stop_for_quota(f"{e} {body}")
            return ""
        print(f"GOOGLE PLACES ADDRESS ERROR: {e} {body}", flush=True)

    return ""

def google_cse_address(title, domain, market=""):
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY".lower())
    cse_id = os.environ.get("GOOGLE_CSE_ID") or os.environ.get("CSE_ID") or os.environ.get("GOOGLE_CSE_ID".lower())

    if not api_key or not cse_id:
        return ""

    query = " ".join(x for x in [title, domain, market, "address"] if x).strip()

    params = urllib.parse.urlencode({
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": 3,
    })

    url = "https://www.googleapis.com/customsearch/v1?" + params

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LeadBot address lookup"})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8", errors="ignore"))
    except Exception:
        return ""

    chunks = []
    for item in data.get("items", []) or []:
        chunks.append(item.get("title", ""))
        chunks.append(item.get("snippet", ""))
        chunks.append(item.get("htmlSnippet", ""))

    return address_from_html(" ".join(chunks))


def find_business_address(row, market=""):
    existing = existing_address_from_row(row)
    if existing and is_full_street_address(existing):
        return existing

    title = clean_text(row.get("title") or row.get("business_name") or row.get("name"))
    url = row.get("url") or row.get("website") or ""
    domain = clean_domain(row.get("domain") or url)
    phone = clean_text(row.get("phone") or row.get("telephone"))

    # Stronger Places query: name + domain + phone + market.
    query_market = market or row.get("market") or row.get("location") or ""
    places_title = " ".join(x for x in [title, phone] if x).strip()

    found = google_places_address(places_title or title, domain, query_market)
    if found and is_full_street_address(found):
        return found

    # Fallback with title + market only.
    found = google_places_address(title, "", query_market)
    if found and is_full_street_address(found):
        return found

    return ""

# === LEADBOT LONG ISLAND ADDRESS GUARDRAIL START ===
# Prevents bad enriched addresses like Manhattan / 72nd St from being saved
# into Suffolk, Nassau, or Long Island lead cards.

def _leadbot_address_market_scope(market: str = "") -> str:
    value = " ".join(str(market or "").lower().replace(",", " ").split())

    if "suffolk" in value:
        return "suffolk"
    if "nassau" in value:
        return "nassau"
    if "long island" in value:
        return "long_island"

    return ""


_LEADBOT_ADDRESS_NYC_REJECT_BITS = {
    "manhattan",
    "new york, ny",
    "new york ny",
    "nyc",
    "upper east side",
    "upper west side",
    "midtown",
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

_LEADBOT_ADDRESS_NASSAU_BITS = {
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

_LEADBOT_ADDRESS_SUFFOLK_BITS = {
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


def _leadbot_address_allowed_for_market(address: str = "", market: str = "") -> bool:
    scope = _leadbot_address_market_scope(market)

    if not scope:
        return True

    blob = " " + str(address or "").lower() + " "

    if not blob.strip():
        return True

    for bad in _LEADBOT_ADDRESS_NYC_REJECT_BITS:
        if bad in blob:
            return False

    if scope == "suffolk":
        if any((" " + bit + " ") in blob for bit in _LEADBOT_ADDRESS_NASSAU_BITS):
            if not any((" " + bit + " ") in blob for bit in _LEADBOT_ADDRESS_SUFFOLK_BITS):
                return False

    if scope == "nassau":
        if any((" " + bit + " ") in blob for bit in _LEADBOT_ADDRESS_SUFFOLK_BITS):
            if not any((" " + bit + " ") in blob for bit in _LEADBOT_ADDRESS_NASSAU_BITS):
                return False

    return True


_leadbot_original_find_business_address = find_business_address

def find_business_address(row, market=""):
    found = _leadbot_original_find_business_address(row, market)

    try:
        query_market = market or ""
        if isinstance(row, dict):
            query_market = query_market or row.get("market") or row.get("location") or row.get("market_seed") or ""

        if found and not _leadbot_address_allowed_for_market(found, query_market):
            print(
                f"LEADBOT ADDRESS GUARDRAIL: rejected out-of-market address for market={query_market}: {found}",
                flush=True,
            )
            return ""

    except Exception as exc:
        print(f"LEADBOT ADDRESS GUARDRAIL ERROR: {exc}", flush=True)

    return found
# === LEADBOT LONG ISLAND ADDRESS GUARDRAIL END ===

