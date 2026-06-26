import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except Exception:
    requests = None


ADDRESS_KEYS = [
    "address",
    "business_address",
    "street_address",
    "full_address",
    "mailing_address",
    "location_address",
    "place_address",
    "formatted_address",
    "google_address",
    "maps_address",
    "location",
]


def clean(value):
    return str(value or "").strip()



def clean_address_candidate(value):
    value = clean(value)
    if not value:
        return ""

    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&amp;", "&", value, flags=re.I)
    value = re.sub(r"&copy;.*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.-|")

    # If a scrape starts too early, trim to the first street-looking number.
    m = re.search(
        r"\b\d{1,6}\s+(?:[A-Za-z0-9.'#\-]+\s+){0,7}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl|Way|Highway|Hwy|Parkway|Pkwy|Turnpike|Circle|Cir|Terrace|Ter|Trail|Trl)\b",
        value,
        flags=re.I,
    )
    if m and m.start() > 0:
        value = value[m.start():]

    # Cut off common website/footer junk after the ZIP.
    zip_match = re.search(r"\b\d{5}(?:-\d{4})?\b", value)
    if zip_match:
        value = value[:zip_match.end()]

    value = re.sub(r"\s+", " ", value).strip(" ,.-|")
    return value


def is_plausible_street_address(value):
    value = clean_address_candidate(value)
    if not value:
        return False

    lower = value.lower()

    if len(value) < 12 or len(value) > 150:
        return False

    junk_bits = [
        "years ago",
        "stars",
        "review",
        "reviews",
        "insurance company",
        "couldn't",
        "hot water",
        "home emergency",
        "talk to an expert",
        "business hours",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "copyright",
        "all rights",
        "we offer financing",
        "connect business",
    ]

    if any(bit in lower for bit in junk_bits):
        # Allow only if it still cleanly trims to a street + state + ZIP.
        trimmed = clean_address_candidate(value)
        if trimmed == value:
            return False

    has_street = re.search(
        r"\b\d{1,6}\s+(?:[A-Za-z0-9.'#\-]+\s+){0,7}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl|Way|Highway|Hwy|Parkway|Pkwy|Turnpike|Circle|Cir|Terrace|Ter|Trail|Trl)\b",
        value,
        flags=re.I,
    )

    if not has_street:
        return False

    # Prefer city/state/ZIP or at least state/ZIP.
    has_state_zip = re.search(r"\b[A-Z]{2}\b\s*,?\s*\d{5}(?:-\d{4})?\b", value)
    has_city_state_zip = re.search(r"\b[A-Za-z .'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", value)

    if not (has_state_zip or has_city_state_zip):
        return False

    return True


def row_market_text(row):
    if not isinstance(row, dict):
        return ""

    keys = [
        "market",
        "location",
        "city",
        "state",
        "query",
        "query_used",
        "base_keyword",
        "search_market",
    ]

    return " ".join(clean(row.get(k)) for k in keys if clean(row.get(k))).lower()


def address_allowed_for_row(row, address):
    """
    Basic sanity guard. Avoid saving a totally different state/city address
    when the row/search clearly says another market.
    """
    address_l = clean(address).lower()
    market_l = row_market_text(row)

    if not address_l:
        return False

    # Colorado Springs scan should not accept Mesa/AZ/etc.
    if "colorado springs" in market_l:
        if "mesa, az" in address_l or " arizona" in address_l or re.search(r"\bAZ\b", address, flags=re.I):
            return False
        if "colorado springs" not in address_l and " co " not in f" {address_l} " and ", co" not in address_l:
            return False

    # Long Island guardrail lite.
    if "long island" in market_l or "suffolk" in market_l or "nassau" in market_l:
        bad = [" manhattan", " brooklyn", " bronx", " queens, ny", " new york, ny", " jersey city", " hoboken"]
        if any(bit in address_l for bit in bad):
            return False

    return True



def normalize_url(value):
    value = clean(value)
    if not value or value.lower() in {"not found", "none", "nan", "null"}:
        return ""

    if value.startswith("//"):
        value = "https:" + value

    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    return value


def domain_from_url(value):
    value = clean(value)
    if not value:
        return ""

    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    try:
        host = urlparse(value).netloc.lower()
        return host.replace("www.", "")
    except Exception:
        return ""


def existing_address(row):
    for key in ADDRESS_KEYS:
        val = clean(row.get(key))
        if val and val.lower() not in {"not found", "none", "nan", "null"}:
            return val
    return ""


def extract_jsonld_addresses(html_text):
    found = []

    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text or "",
        flags=re.I | re.S,
    ):
        raw = match.group(1).strip()

        try:
            data = json.loads(raw)
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]

        def walk(obj):
            if isinstance(obj, dict):
                address = obj.get("address")
                if isinstance(address, dict):
                    parts = [
                        address.get("streetAddress"),
                        address.get("addressLocality"),
                        address.get("addressRegion"),
                        address.get("postalCode"),
                    ]
                    value = clean_address_candidate(", ".join([clean(p) for p in parts if clean(p)]))
                    if value and is_plausible_street_address(value):
                        found.append(value)
                elif isinstance(address, str) and address.strip():
                    value = clean_address_candidate(address)
                    if value and is_plausible_street_address(value):
                        found.append(value)

                for v in obj.values():
                    walk(v)

            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        for item in items:
            walk(item)

    return found


def extract_address_from_text(text):
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    street_type = r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl|Way|Highway|Hwy|Parkway|Pkwy|Turnpike|Circle|Cir|Terrace|Ter|Trail|Trl)"

    patterns = [
        # Street, optional suite/unit, city, state ZIP
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,7}}{street_type}"
        rf"(?:\s+(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?"
        rf"\s*,?\s*[A-Za-z .'-]{{2,40}}\s*,?\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY)\s+\d{{5}}(?:-\d{{4}})?\b",

        # Street, state ZIP
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,7}}{street_type}"
        rf"(?:\s+(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?"
        rf"\s*,?\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY)\s+\d{{5}}(?:-\d{{4}})?\b",
    ]

    candidates = []

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            value = clean_address_candidate(m.group(0))
            if is_plausible_street_address(value):
                candidates.append(value)

    # Prefer candidates with ZIP and city/state, then shorter cleaner candidates.
    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda v: (0 if re.search(r",\s*[A-Z]{2}\s+\d{5}", v) else 1, len(v)))

    return candidates[0] if candidates else ""

def fetch(url):
    if not requests:
        return "", "", "requests_missing", ""

    url = normalize_url(url)
    if not url:
        return "", "", "missing_url", ""

    headers = {
        "User-Agent": "Mozilla/5.0 LeadBot Address + Domain Checker"
    }

    try:
        response = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
        status = str(response.status_code)
        final_url = response.url or url

        if response.status_code >= 400:
            return "", final_url, status, ""

        return response.text or "", final_url, status, ""
    except Exception as e:
        return "", url, "error", str(e)[:120]


def get_first(row, keys):
    for key in keys:
        value = clean(row.get(key))
        if value and value.lower() not in {"not found", "none", "nan", "null"}:
            return value
    return ""


def enrich_row(row):
    website = get_first(row, ["website", "url", "site", "domain_url", "link"])
    contact_page = get_first(row, ["contact_page", "contact_url", "contact"])

    domain = get_first(row, ["domain", "root_domain"])
    if not website and domain:
        website = domain

    address = existing_address(row)

    urls_to_try = []
    for value in [contact_page, website]:
        url = normalize_url(value)
        if url and url not in urls_to_try:
            urls_to_try.append(url)

    # Also try the root homepage. Many obvious addresses live in footer widgets
    # and may not be visible/extracted from a deep landing page.
    try:
        root_source = website or domain
        root_url = normalize_url(root_source)
        if root_url:
            parsed = urlparse(root_url)
            home_url = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""
            if home_url and home_url not in urls_to_try:
                urls_to_try.append(home_url)
    except Exception:
        pass

    domain_status = ""
    final_url = ""
    domain_ok = "No"
    domain_error = ""

    for i, url in enumerate(urls_to_try[:2]):
        html_text, fetched_final_url, status, error = fetch(url)

        if i == 0 or not domain_status:
            domain_status = status
            final_url = fetched_final_url
            domain_error = error

        if status.isdigit() and int(status) < 400:
            domain_ok = "Yes"

        if not address and html_text:
            jsonld_addresses = extract_jsonld_addresses(html_text)
            if jsonld_addresses:
                candidate = clean_address_candidate(jsonld_addresses[0])
                if is_plausible_street_address(candidate) and address_allowed_for_row(row, candidate):
                    address = candidate
            else:
                candidate = clean_address_candidate(extract_address_from_text(html_text))
                if is_plausible_street_address(candidate) and address_allowed_for_row(row, candidate):
                    address = candidate

        if address and domain_ok == "Yes":
            break

    address = clean_address_candidate(address)
    if address and not (is_plausible_street_address(address) and address_allowed_for_row(row, address)):
        address = ""

    row["address"] = address or ""
    row["domain_checked_url"] = normalize_url(website)
    row["domain_final_url"] = final_url
    row["domain_http_status"] = domain_status
    row["domain_ok"] = domain_ok
    row["domain_error"] = domain_error

    return row



# === LEADBOT LONG ISLAND MISSING-STATE ADDRESS POLISH START ===
# Handles visible homepage addresses like:
# "Located at 836 Montauk Highway Bayport 11705"
# The original extractor was too strict because it expected "NY" before ZIP.

_LEADBOT_US_STATES_RE = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|"
    r"NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY"
)

_LEADBOT_STREET_TYPE_RE = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Place|Pl|Way|Highway|Hwy|Parkway|Pkwy|Turnpike|Circle|Cir|Terrace|Ter|Trail|Trl"
)

_LEADBOT_LONG_ISLAND_CITY_HINTS = {
    "bayport", "riverhead", "freeport", "wading river", "rockville centre",
    "bohemia", "oceanside", "lindenhurst", "syosset", "patchogue",
    "huntington", "smithtown", "islip", "babylon", "northport", "montauk",
    "hampton bays", "port jefferson", "east main", "long beach",
}


def _leadbot_text_suggests_ny(text):
    blob = " " + str(text or "").lower() + " "
    if " long island " in blob or " new york " in blob or " nassau " in blob or " suffolk " in blob:
        return True
    if re.search(r"\bNY\b", str(text or ""), flags=re.I):
        return True
    return any((" " + city + " ") in blob for city in _LEADBOT_LONG_ISLAND_CITY_HINTS)


def _leadbot_insert_ny_before_zip_if_needed(candidate, full_text=""):
    candidate = clean_address_candidate(candidate)
    if not candidate:
        return ""

    # Already has state before ZIP.
    if re.search(rf"\b(?:{_LEADBOT_US_STATES_RE})\b\s*,?\s*\d{{5}}(?:-\d{{4}})?\b", candidate, flags=re.I):
        return candidate

    if not _leadbot_text_suggests_ny((full_text or "") + " " + candidate):
        return candidate

    # Add NY before final ZIP: "836 Montauk Highway Bayport 11705"
    # -> "836 Montauk Highway Bayport, NY 11705"
    candidate = re.sub(
        r"\s+(\d{5}(?:-\d{4})?)\b",
        r", NY \1",
        candidate,
        count=1,
    )

    return clean_address_candidate(candidate)


def is_plausible_street_address(value):
    value = clean_address_candidate(value)
    if not value:
        return False

    lower = value.lower()

    if len(value) < 12 or len(value) > 150:
        return False

    junk_bits = [
        "years ago", "stars", "review", "reviews", "insurance company",
        "couldn't", "hot water", "home emergency", "talk to an expert",
        "business hours", "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday", "copyright", "all rights",
        "we offer financing", "connect business",
    ]

    if any(bit in lower for bit in junk_bits):
        return False

    has_street = re.search(
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,7}}(?:{_LEADBOT_STREET_TYPE_RE})\b",
        value,
        flags=re.I,
    )

    if not has_street:
        return False

    has_state_zip = re.search(
        rf"\b(?:{_LEADBOT_US_STATES_RE})\b\s*,?\s*\d{{5}}(?:-\d{{4}})?\b",
        value,
        flags=re.I,
    )

    return bool(has_state_zip)


def extract_address_from_text(text):
    if not text:
        return ""

    raw_text = text
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)

    street = rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,7}}(?:{_LEADBOT_STREET_TYPE_RE})\b"
    suite = r"(?:\s+(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?"

    patterns = [
        # Full address with city/state/ZIP.
        rf"{street}{suite}\s*,?\s*[A-Za-z .'-]{{2,45}}\s*,?\s*(?:{_LEADBOT_US_STATES_RE})\s+\d{{5}}(?:-\d{{4}})?\b",

        # Street + city + ZIP, missing state.
        # Example: 836 Montauk Highway Bayport 11705
        rf"{street}{suite}\s*,?\s*[A-Za-z .'-]{{2,45}}\s+\d{{5}}(?:-\d{{4}})?\b",
    ]

    candidates = []

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            candidate = clean_address_candidate(m.group(0))
            candidate = _leadbot_insert_ny_before_zip_if_needed(candidate, raw_text)
            if is_plausible_street_address(candidate):
                candidates.append(candidate)

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda v: (0 if ", NY " in v or " NY " in v else 1, len(v)))

    return candidates[0] if candidates else ""


# === LEADBOT LONG ISLAND MISSING-STATE ADDRESS POLISH END ===



# === LEADBOT FOOTER/WIDGET ADDRESS POLISH START ===
# Later definitions intentionally override earlier stricter versions.
# Goal: catch obvious footer/widget addresses without accepting review paragraphs.

_LEADBOT_WIDGET_STATE_RE = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|"
    r"NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY"
)

_LEADBOT_WIDGET_STREET_RE = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Place|Pl|Way|Highway|Hwy|Parkway|Pkwy|Turnpike|Circle|Cir|Terrace|Ter|"
    r"Trail|Trl|Main|Montauk"
)

_LEADBOT_WIDGET_STOP_RE = re.compile(
    r"\b(?:phone|tel|call|email|hours|open|closed|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|copyright|all rights|menu|home|contact|facebook|instagram|directions|"
    r"view map|reservation|order online)\b",
    flags=re.I,
)


def _leadbot_widget_plain_text(value):
    value = str(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:div|p|li|span|section|footer|address|h1|h2|h3|h4)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&nbsp;", " ", value, flags=re.I)
    value = re.sub(r"&amp;", "&", value, flags=re.I)
    value = re.sub(r"\r", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def _leadbot_widget_context_says_ny(text):
    blob = " " + str(text or "").lower() + " "
    ny_hints = [
        " long island ", " nassau ", " suffolk ", " bayport ", " riverhead ", " freeport ",
        " wading river ", " rockville centre ", " bohemia ", " oceanside ", " lindenhurst ",
        " syosset ", " patchogue ", " huntington ", " smithtown ", " islip ", " babylon ",
        " northport ", " montauk ", " port jefferson ", " port jeff ", " hampton bays ",
    ]
    return bool(re.search(r"\bNY\b", str(text or ""), flags=re.I)) or " new york " in blob or any(h in blob for h in ny_hints)


def _leadbot_widget_clean_candidate(value, full_text=""):
    value = clean(value)
    if not value:
        return ""

    value = _leadbot_widget_plain_text(value)
    value = re.sub(r"\s+", " ", value).strip(" ,.-|")

    # Trim anything before the first street-looking number.
    street_start = re.search(
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,8}}(?:{_LEADBOT_WIDGET_STREET_RE})\b",
        value,
        flags=re.I,
    )
    if street_start and street_start.start() > 0:
        value = value[street_start.start():]

    # Cut after ZIP when present.
    z = re.search(r"\b\d{5}(?:-\d{4})?\b", value)
    if z:
        value = value[:z.end()]

    # Remove common lead-in words.
    value = re.sub(r"^(?:located at|address|location|visit us at)\s*:?\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.-|")

    # If local page gives street + city + ZIP without NY, add NY.
    has_state_zip = re.search(rf"\b(?:{_LEADBOT_WIDGET_STATE_RE})\b\s*,?\s*\d{{5}}(?:-\d{{4}})?\b", value, flags=re.I)
    has_zip = re.search(r"\b\d{5}(?:-\d{4})?\b", value)

    if has_zip and not has_state_zip and _leadbot_widget_context_says_ny((full_text or "") + " " + value):
        value = re.sub(r"\s+(\d{5}(?:-\d{4})?)\b", r", NY \1", value, count=1)

    return re.sub(r"\s+", " ", value).strip(" ,.-|")


def is_plausible_street_address(value):
    value = _leadbot_widget_clean_candidate(value, value)
    if not value:
        return False

    lower = value.lower()

    if len(value) < 12 or len(value) > 150:
        return False

    bad_bits = [
        "years ago", "review", "reviews", "stars", "insurance company", "hot water",
        "home emergency", "talk to an expert", "business hours", "copyright",
        "all rights", "we offer financing", "connect business",
    ]
    if any(bit in lower for bit in bad_bits):
        return False

    has_street = re.search(
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,8}}(?:{_LEADBOT_WIDGET_STREET_RE})\b",
        value,
        flags=re.I,
    )
    if not has_street:
        return False

    has_state_zip = re.search(
        rf"\b(?:{_LEADBOT_WIDGET_STATE_RE})\b\s*,?\s*\d{{5}}(?:-\d{{4}})?\b",
        value,
        flags=re.I,
    )

    return bool(has_state_zip)


def extract_address_from_text(text):
    if not text:
        return ""

    raw = str(text or "")
    plain = _leadbot_widget_plain_text(raw)

    street = rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,8}}(?:{_LEADBOT_WIDGET_STREET_RE})\b"

    patterns = [
        # Normal: street city/state/zip
        rf"{street}(?:\s+(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?[\s,]+[A-Za-z .'-]{{2,50}}[\s,]+(?:{_LEADBOT_WIDGET_STATE_RE})\s+\d{{5}}(?:-\d{{4}})?\b",

        # Missing state: street city zip
        rf"{street}(?:\s+(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?[\s,]+[A-Za-z .'-]{{2,50}}\s+\d{{5}}(?:-\d{{4}})?\b",
    ]

    candidates = []

    for pattern in patterns:
        for m in re.finditer(pattern, plain, flags=re.I):
            candidate = _leadbot_widget_clean_candidate(m.group(0), raw)
            if is_plausible_street_address(candidate):
                candidates.append(candidate)

    # Footer/widget fallback: take a short window after the street core, then clean it.
    for m in re.finditer(street, plain, flags=re.I):
        window = plain[m.start():m.start() + 180]
        stop = _LEADBOT_WIDGET_STOP_RE.search(window)
        if stop:
            window = window[:stop.start()]

        candidate = _leadbot_widget_clean_candidate(window, raw)
        if is_plausible_street_address(candidate):
            candidates.append(candidate)

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda v: (0 if re.search(r",\s*(?:NY|New York)\s+\d{5}", v, flags=re.I) else 1, len(v)))

    return candidates[0] if candidates else ""


# === LEADBOT FOOTER/WIDGET ADDRESS POLISH END ===



# === LEADBOT STATE NAME ADDRESS FIX START ===
# Final override: do not turn "Vermont 05156" into "Vermont, NY 05156".
# Normalize full state names before ZIP into abbreviations.

_LEADBOT_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

_LEADBOT_STATE_NAME_RE = "|".join(
    sorted((re.escape(k) for k in _LEADBOT_STATE_NAME_TO_ABBR.keys()), key=len, reverse=True)
)


def _leadbot_normalize_state_name_before_zip(value):
    value = clean(value)
    if not value:
        return ""

    def repl(match):
        state_name = match.group(1).lower()
        zip_code = match.group(2)
        abbr = _LEADBOT_STATE_NAME_TO_ABBR.get(state_name, state_name.upper())
        return f"{abbr} {zip_code}"

    value = re.sub(
        rf"\b({_LEADBOT_STATE_NAME_RE})\b\s*,?\s*(\d{{5}}(?:-\d{{4}})?)\b",
        repl,
        value,
        flags=re.I,
    )

    return value


def _leadbot_widget_clean_candidate(value, full_text=""):
    value = clean(value)
    if not value:
        return ""

    value = _leadbot_widget_plain_text(value)
    value = re.sub(r"\s+", " ", value).strip(" ,.-|")
    value = _leadbot_normalize_state_name_before_zip(value)

    # Trim anything before the first street-looking number.
    street_start = re.search(
        rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'#\-]+\s+){{0,8}}(?:{_LEADBOT_WIDGET_STREET_RE})\b",
        value,
        flags=re.I,
    )
    if street_start and street_start.start() > 0:
        value = value[street_start.start():]

    # Cut after ZIP when present.
    z = re.search(r"\b\d{5}(?:-\d{4})?\b", value)
    if z:
        value = value[:z.end()]

    value = re.sub(r"^(?:located at|address|location|visit us at|farm location)\s*:?\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.-|")
    value = _leadbot_normalize_state_name_before_zip(value)

    has_state_zip = re.search(rf"\b(?:{_LEADBOT_WIDGET_STATE_RE})\b\s*,?\s*\d{{5}}(?:-\d{{4}})?\b", value, flags=re.I)
    has_zip = re.search(r"\b\d{5}(?:-\d{4})?\b", value)

    # Only add NY when there is no explicit state abbreviation/name.
    if has_zip and not has_state_zip and _leadbot_widget_context_says_ny((full_text or "") + " " + value):
        value = re.sub(r"\s+(\d{5}(?:-\d{4})?)\b", r", NY \1", value, count=1)

    # Clean accidental double comma from pages like "Deer Park, 11729"
    value = re.sub(r",\s*,+", ",", value)
    value = re.sub(r"\s+,", ",", value)
    value = re.sub(r",\s*", ", ", value)

    return re.sub(r"\s+", " ", value).strip(" ,.-|")


# === LEADBOT STATE NAME ADDRESS FIX END ===



# === LEADBOT ADDRESS TURD CLEANUP START ===
# Final cleanup wrapper: removes ugly comma/spacing leftovers like:
# "832 Grand Blvd, Deer Park,, NY 11729"

def _leadbot_clean_address_punctuation(value):
    value = clean(value)
    if not value:
        return ""

    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+,", ",", value)
    value = re.sub(r",\s*,+", ",", value)
    value = re.sub(r",\s*", ", ", value)
    value = re.sub(r"\s{2,}", " ", value)

    # Fix city/state spacing: "Deer Park NY 11729" -> "Deer Park, NY 11729"
    value = re.sub(
        r"\b([A-Za-z .'-]{2,40})\s+(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY)\s+(\d{5}(?:-\d{4})?)\b",
        lambda m: f"{m.group(1).strip()}, {m.group(2).upper()} {m.group(3)}",
        value,
        flags=re.I,
    )

    # Fix accidental ",, NY"
    value = re.sub(r",\s*,\s*([A-Z]{2}\s+\d{5})", r", \1", value)

    # Remove junk punctuation at ends.
    value = value.strip(" ,.-|")

    return value


_leadbot_previous_widget_clean_candidate = _leadbot_widget_clean_candidate

def _leadbot_widget_clean_candidate(value, full_text=""):
    value = _leadbot_previous_widget_clean_candidate(value, full_text)
    return _leadbot_clean_address_punctuation(value)


_leadbot_previous_clean_address_candidate = clean_address_candidate

def clean_address_candidate(value):
    value = _leadbot_previous_clean_address_candidate(value)
    return _leadbot_clean_address_punctuation(value)


# === LEADBOT ADDRESS TURD CLEANUP END ===


def main():
    if len(sys.argv) < 2:
        print("Usage: python agents/lead_address_agent.py path/to/leads.csv")
        raise SystemExit(1)

    input_path = Path(sys.argv[1])

    if not input_path.exists():
        matches = list(Path(".").rglob(input_path.name))
        if not matches:
            raise SystemExit(f"CSV not found: {input_path}")
        input_path = matches[0]

    output_path = input_path.with_name(input_path.stem + "_address_checked.csv")

    with input_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    added = [
        "address",
        "domain_checked_url",
        "domain_final_url",
        "domain_http_status",
        "domain_ok",
        "domain_error",
    ]

    for col in added:
        if col not in fieldnames:
            fieldnames.append(col)

    enriched = []
    total = len(rows)

    for idx, row in enumerate(rows, 1):
        title = clean(row.get("title") or row.get("business_name") or row.get("domain"))
        print(f"[{idx}/{total}] Checking {title[:70]}")
        enriched.append(enrich_row(row))

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched)

    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
