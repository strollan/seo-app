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
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


class InvalidMarketLocationError(Exception):
    """
    Raised when a market string can't be confidently resolved into a valid
    DataForSEO location_name and isn't a ZIP code either -- e.g. a bare
    city name with no state ("Southampton"). This is a user-input problem,
    not a provider failure, and is deliberately a distinct exception from
    business_competitor_finder.SearchProviderUnavailableError so the two
    are never conflated: run_job() surfaces this one as a validation
    message ("Enter a City, State or ZIP Code...") rather than "the lead
    search service is temporarily unavailable".
    """

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


# DataForSEO only accepts specific canonical location_name values. Keep
# "Long Island" (and similar informal NY-area names) usable in the search
# keyword/query, but send a valid nearby canonical location to DataForSEO
# so the API does not reject it.
#
# DataForSEO has no single canonical location for the whole Long Island
# region (it spans Nassau + Suffolk counties), so bare "Long Island"
# aliases deliberately keep using the broad "New York,New York,United
# States" value -- the LEADBOT LONG ISLAND LOCAL SERP GUARDRAIL below
# filters out NYC/borough leakage from those broad results. Nassau County
# and Suffolk County, unlike Long Island as a whole, each DO have their
# own precise DataForSEO canonical location (confirmed via a live
# locations lookup: location_code 9058760 and 1023413 respectively), so
# those two aliases use their exact county location instead of the
# broad NYC value.
_LEADBOT_LOCATION_ALIASES = {
    "long island": "New York,New York,United States",
    "long island ny": "New York,New York,United States",
    "long island new york": "New York,New York,United States",
    "nassau county": "Nassau County,New York,United States",
    "nassau county ny": "Nassau County,New York,United States",
    "suffolk county": "Suffolk County,New York,United States",
    "suffolk county ny": "Suffolk County,New York,United States",
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

_LEADBOT_STATE_ABBREVIATIONS = {
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

_LEADBOT_STATE_FULL_NAMES_LOWER = {
    full_name.lower(): full_name for full_name in _LEADBOT_STATE_ABBREVIATIONS.values()
}

_LEADBOT_ZIP_CODE_RE = re.compile(r"^\d{5}(-\d{4})?$")


def _leadbot_resolve_state_name(state_token: str) -> Optional[str]:
    """Return the canonical full state name for a 2-letter abbreviation
    (with optional periods, e.g. "N.Y.") or a full state name spelled out
    (case-insensitive, e.g. "New York") -- or None if unrecognized. Full
    state names are matched as one unit (not word-by-word) so multi-word
    names like "New York" or "North Carolina" resolve correctly."""
    token = (state_token or "").strip()
    if not token:
        return None

    abbreviation = token.upper().replace(".", "")
    if abbreviation in _LEADBOT_STATE_ABBREVIATIONS:
        return _LEADBOT_STATE_ABBREVIATIONS[abbreviation]

    return _LEADBOT_STATE_FULL_NAMES_LOWER.get(token.lower())


def _leadbot_is_zip_code(value: str) -> bool:
    """5-digit ZIP or ZIP+4 -- matches the existing (unchanged) pass-through
    behavior: a bare ZIP code is sent to DataForSEO as-is, not rewritten
    into a City,State,Country value."""
    return bool(_LEADBOT_ZIP_CODE_RE.fullmatch((value or "").strip()))


_LEADBOT_CITY_WORD_SEPARATOR_RE = re.compile(r"([ \-]+)")


def _leadbot_capitalize_city_word(word: str) -> str:
    """Capitalize a single city-name word, with special handling for the
    "Mc" and "O'" prefixes so a full renormalization pass doesn't mangle
    names like "McAllen" or "O'Fallon" into "Mcallen"/"O'fallon" the way a
    plain str.capitalize() would."""
    if not word:
        return word

    lowered = word.lower()

    if lowered.startswith("mc") and len(lowered) > 2:
        return "Mc" + lowered[2].upper() + lowered[3:]

    if "'" in word:
        return "'".join(part[:1].upper() + part[1:] for part in lowered.split("'"))

    return lowered[:1].upper() + lowered[1:]


def _leadbot_city_word_looks_plausible(word: str) -> bool:
    """A word already "looks" correctly cased if it starts with an
    uppercase letter and isn't entirely upper-case -- ALL CAPS input
    (e.g. "ALBANY") still needs normalizing even though it starts with a
    capital letter. Punctuation-only tokens (e.g. a trailing period) never
    block plausibility on their own."""
    if not word or not word[0].isalpha():
        return True
    return word[0].isupper() and not word.isupper()


def _normalize_city_case(city: str) -> str:
    """
    Normalize a parsed city name's casing without blindly title-casing
    every input (a plain str.title() call would turn already-correct
    names like "McAllen" or "O'Fallon" into "Mcallen"/"O'fallon").

    If every word in the city already looks plausibly cased (starts with
    an uppercase letter, isn't ALL CAPS), the input is returned completely
    unchanged -- so correctly typed names are never touched. Otherwise the
    whole string is renormalized word-by-word (split on spaces and
    hyphens) via _leadbot_capitalize_city_word(), which still produces the
    right result for "Mc"/"O'" names even when renormalized from a fully
    lowercase or fully uppercase input.
    """
    city = (city or "").strip()
    if not city:
        return city

    tokens = _LEADBOT_CITY_WORD_SEPARATOR_RE.split(city)
    words = [
        token for token in tokens
        if token and not _LEADBOT_CITY_WORD_SEPARATOR_RE.fullmatch(token)
    ]

    if all(_leadbot_city_word_looks_plausible(word) for word in words):
        return city

    return "".join(
        token if _LEADBOT_CITY_WORD_SEPARATOR_RE.fullmatch(token) else _leadbot_capitalize_city_word(token)
        for token in tokens
    )


def _location_name(market: str) -> str:
    """
    Resolve a free-text market string (as typed by a user: "Albany, NY",
    "Albany,NY", "Albany , NY", "Albany NY", "New York, NY", a ZIP code,
    or one of the informal NY-area aliases) into DataForSEO's required
    canonical "City,State,Country" location_name -- or raise
    InvalidMarketLocationError if it can't be confidently resolved, rather
    than sending DataForSEO a malformed value (which it rejects with task
    error 40501 "Invalid Field: 'location_name'").

    Deliberately does not guess a state for a bare, ambiguous city name
    with no state or ZIP at all (e.g. "Southampton") -- that's exactly the
    kind of guess that produced malformed values before this fix.
    """
    market = (market or "").strip()

    normalized_market = " ".join(market.lower().replace(",", " ").split())
    if normalized_market in _LEADBOT_LOCATION_ALIASES:
        return _LEADBOT_LOCATION_ALIASES[normalized_market]

    if _leadbot_is_zip_code(market):
        return market

    # Split on the FINAL comma so a multi-word city ("Winston-Salem") or a
    # full multi-word state name after the comma ("Albany, New York") both
    # parse correctly -- unlike splitting on whitespace, which can't tell
    # a two-word city from a two-word state.
    if "," in market:
        city_part, _, state_part = market.rpartition(",")
        city = city_part.strip().rstrip(",").strip()
        state_token = state_part.strip()
    else:
        # No comma at all: preserve the existing "City ST" (space-only)
        # convention, e.g. "Albany NY", by treating the last whitespace
        # token as the state.
        tokens = market.split()
        if len(tokens) >= 2:
            city = " ".join(tokens[:-1]).strip()
            state_token = tokens[-1].strip()
        else:
            city = ""
            state_token = ""

    resolved_state = _leadbot_resolve_state_name(state_token) if state_token else None

    if city and resolved_state:
        return f"{_normalize_city_case(city)},{resolved_state},United States"

    raise InvalidMarketLocationError(
        "Enter a City, State or ZIP Code, such as Albany, NY or 12207."
    )


# === LEADBOT PROVIDER FAILURE DIAGNOSTICS START ===
# journalctl is unavailable in production and this app has no other log
# file, so a genuine DataForSEO provider failure (as opposed to a user
# input problem like InvalidMarketLocationError, which is never logged
# here) previously left no trace beyond a generic "temporarily
# unavailable" message -- there was no way to tell a rate limit from a
# timeout from a 5xx after the fact. This appends one sanitized JSON line
# per failure: provider name, a best-effort failure category, the numeric
# DataForSEO status code (never the raw response body), and the location
# that was being searched. Never touches control flow -- the caught
# exception is always re-raised unchanged, so behavior is identical to
# before this was added.
_LEADBOT_PROVIDER_DIAGNOSTICS_LOG = Path(__file__).resolve().parents[1] / "data" / "leadbot_provider_diagnostics.log"

# Confirmed from DataForSEO's official task-status-code table:
#   40101 = Internal SE server error; requested search engine could not
#           process the request -- transient, safe to retry.
#   40103 = Task execution failed; DataForSEO's own guidance is to retry.
#   40102 = No Search Results -- NOT an error, a legitimate empty result.
_LEADBOT_DATAFORSEO_RETRYABLE_TASK_STATUS_CODES = {40101, 40103}
_LEADBOT_DATAFORSEO_NO_RESULTS_TASK_STATUS_CODE = 40102
_LEADBOT_DATAFORSEO_MAX_ATTEMPTS = 3  # 1 initial attempt + 2 retries
_LEADBOT_DATAFORSEO_RETRY_BACKOFF_SECONDS = [1, 2]  # before retry 1, before retry 2


# === LEADBOT DATAFORSEO CIRCUIT BREAKER START ===
# Production runs LeadMeLeads as a single uvicorn process (no --workers
# flag; confirmed against the deployed systemd unit and the running
# process list), so a thread-safe in-process circuit breaker -- plain
# module-level state guarded by one lock -- is the smallest correct
# mechanism. No file/SQLite-backed shared state is needed since there is
# only one process to share it across; concurrent scans within that one
# process run their per-query work on background threads (see
# call_find_leads_with_timeout() in agents/lead_live_job_agent.py), so
# thread-safety (not multi-process-safety) is what actually matters here.
#
# Scope: only the two retryable task codes (40101, 40103) ever feed this
# breaker. A permanent error (auth, malformed field, rate limit) or a
# network-level timeout/connection error never touches it, exactly as
# before this change -- those still raise on attempt 1 with no retry and
# no effect on the circuit's state.
_circuit_breaker_lock = threading.Lock()
_circuit_breaker_state = {
    "exhaustion_timestamps": [],  # monotonic times of exhausted retryable ops
    "opened_until": None,         # monotonic time the circuit reopens for probing, or None if closed
    "probe_in_progress": False,   # True while the one allowed post-cooldown probe is in flight
}

_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
_CIRCUIT_BREAKER_FAILURE_WINDOW_SECONDS = 60
_CIRCUIT_BREAKER_OPEN_SECONDS = 120


def _circuit_breaker_now() -> float:
    """Wrapped so tests can patch this one function to control time
    deterministically instead of sleeping for real."""
    return time.monotonic()


def _circuit_breaker_gate(now: float) -> str:
    """Returns "closed" (proceed normally), "probe" (this call is the one
    allowed post-cooldown probe -- proceed), or "open" (fail fast, make no
    request). Thread-safe."""
    with _circuit_breaker_lock:
        state = _circuit_breaker_state

        if state["opened_until"] is None:
            return "closed"

        if now < state["opened_until"]:
            return "open"

        # Cooldown has elapsed. Exactly one probe is allowed through;
        # every other concurrent caller keeps failing fast until this
        # probe's outcome (success or exhaustion) is known.
        if state["probe_in_progress"]:
            return "open"

        state["probe_in_progress"] = True
        return "probe"


def _circuit_breaker_record_success() -> None:
    """Any normal successful provider response -- including a genuine
    40102 zero-result completion -- clears stale failure history and
    fully closes the circuit. Safe to call unconditionally regardless of
    whether this call was the probe or an ordinary closed-circuit call."""
    with _circuit_breaker_lock:
        _circuit_breaker_state["exhaustion_timestamps"] = []
        _circuit_breaker_state["opened_until"] = None
        _circuit_breaker_state["probe_in_progress"] = False


def _circuit_breaker_record_exhaustion(now: float, is_probe: bool) -> None:
    """Call exactly once when a retryable-task-code operation has
    exhausted every attempt. `is_probe` must be the caller's own locally
    captured probe flag from _circuit_breaker_gate() -- never re-derived
    from the shared state here -- so one thread's unrelated failure can
    never be mistaken for a different thread's active probe failing."""
    with _circuit_breaker_lock:
        state = _circuit_breaker_state

        if is_probe:
            # The one allowed probe just failed -- reopen immediately,
            # independent of the ordinary threshold/window accounting.
            state["probe_in_progress"] = False
            state["opened_until"] = now + _CIRCUIT_BREAKER_OPEN_SECONDS
            state["exhaustion_timestamps"] = []
            return

        cutoff = now - _CIRCUIT_BREAKER_FAILURE_WINDOW_SECONDS
        recent = [t for t in state["exhaustion_timestamps"] if t >= cutoff]
        recent.append(now)
        state["exhaustion_timestamps"] = recent

        if len(recent) >= _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            state["opened_until"] = now + _CIRCUIT_BREAKER_OPEN_SECONDS
            state["exhaustion_timestamps"] = []


def _circuit_breaker_release_probe_if_still_claimed() -> None:
    """Safety net for a probe call that ends in something other than a
    recorded success or a recorded exhaustion (e.g. a permanent/non-
    retryable error, or a timeout, happens to land on the probe attempt).
    Permanent errors and timeouts must never contribute to the circuit's
    threshold or reopen it, but the probe slot still has to be released
    so a later request can try again -- otherwise the circuit would stay
    wedged open forever with no further probe ever allowed. A no-op if
    the probe was already resolved by record_success/record_exhaustion."""
    with _circuit_breaker_lock:
        _circuit_breaker_state["probe_in_progress"] = False
# === LEADBOT DATAFORSEO CIRCUIT BREAKER END ===


def _leadbot_classify_provider_failure(exc: Exception) -> str:
    """Best-effort sanitized failure category for diagnostics only. Never
    used for control flow -- a wrong guess here only mislabels a log
    line, it can't change what error the user sees."""
    message = str(exc)

    code_match = re.search(r"\b(40101|40102|40103)\b", message)
    if code_match:
        code = code_match.group(1)
        if code == "40101":
            return "provider_internal_error"
        if code == "40102":
            return "no_results"
        if code == "40103":
            return "task_execution_failed"

    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "timeout_or_connectivity"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return "rate_limit"
        if status in (401, 403):
            return "authentication"
        if isinstance(status, int) and 500 <= status < 600:
            return "provider_5xx"
        return "http_error"

    message_lower = message.lower()
    if "invalid field" in message_lower:
        return "malformed_request_field"
    if "not enough credit" in message_lower or "balance" in message_lower:
        return "account_balance"
    if "unauthorized" in message_lower or "auth" in message_lower:
        return "authentication"
    if "rate limit" in message_lower or "too many requests" in message_lower:
        return "rate_limit"
    return "unknown"


_LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES = 2 * 1024 * 1024  # ~2 MB


def _leadbot_rotate_provider_diagnostics_log_if_needed() -> None:
    """Keep the active diagnostics log bounded: once it would exceed
    ~2MB, move it to a single ".1" backup (replacing any older one) and
    let a fresh active file start on the next append. Never raises --
    rotation is best-effort only, exactly like the logging it supports,
    so a rotation failure can never break a scan or hide the real
    provider exception."""
    try:
        if not _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.exists():
            return
        if _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.stat().st_size < _LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES:
            return
        backup_path = _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.parent / (_LEADBOT_PROVIDER_DIAGNOSTICS_LOG.name + ".1")
        _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.replace(backup_path)
    except Exception:
        pass


def _leadbot_log_provider_diagnostic(
    failure_category: str,
    status_code: Optional[int] = None,
    location_name: str = "",
    location_code: Optional[int] = None,
    attempt: int = 1,
    outcome: str = "failed",
) -> None:
    """Append one sanitized diagnostic line. Deliberately never includes
    the raw response body, request headers, credentials, or any lead
    data -- only a provider name, failure category, numeric status code,
    the location being searched, the attempt number, and the outcome
    ("retrying", "exhausted", "recovered", or "circuit_open"). Diagnostics
    must never break the actual search path, so any failure here
    (including a rotation failure) is swallowed."""
    try:
        _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        _leadbot_rotate_provider_diagnostics_log_if_needed()
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": "dataforseo",
            "failure_category": failure_category,
            "status_code": status_code,
            "location_code": location_code,
            "location_name": location_name,
            "attempt": attempt,
            "outcome": outcome,
        }
        with _LEADBOT_PROVIDER_DIAGNOSTICS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass
# === LEADBOT PROVIDER FAILURE DIAGNOSTICS END ===


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

    resolved_location_name = _location_name(market)

    payload = [
        {
            "keyword": keyword,
            "location_name": resolved_location_name,
            "language_code": language_code,
            "device": device,
            "os": os_name,
            "depth": depth,
        }
    ]

    # Fail fast, before spending a single paid request, if the circuit is
    # open. Only a genuinely open circuit blocks here -- "closed" and
    # "probe" both proceed to the retry loop below exactly as before.
    circuit_gate = _circuit_breaker_gate(_circuit_breaker_now())
    if circuit_gate == "open":
        _leadbot_log_provider_diagnostic(
            failure_category="circuit_breaker_open",
            status_code=None,
            location_name=resolved_location_name,
            attempt=0,
            outcome="circuit_open",
        )
        raise RuntimeError("DataForSEO circuit breaker is open")

    is_probe = circuit_gate == "probe"
    task = None

    try:
        # Bounded retry for the two DataForSEO task-status codes confirmed
        # to be transient/provider-side (40101 "Internal SE server error",
        # 40103 "Task execution failed; try resubmitting" -- per
        # DataForSEO's own error table). Every other failure (auth,
        # validation, malformed field, rate limit, etc.) raises
        # immediately on the first attempt, unchanged from before. 40102
        # "No Search Results" is not an error at all and returns []
        # without ever entering the retry path.
        for attempt in range(1, _LEADBOT_DATAFORSEO_MAX_ATTEMPTS + 1):
            envelope_status_code = None
            task_status_code = None

            try:
                response = requests.post(
                    url,
                    headers=_auth_header(),
                    json=payload,
                    timeout=90,
                )

                response.raise_for_status()
                data = response.json()

                envelope_status_code = data.get("status_code")
                if envelope_status_code != 20000:
                    raise RuntimeError(
                        f"DataForSEO API error: {envelope_status_code} {data.get('status_message')}"
                    )

                tasks = data.get("tasks") or []
                if not tasks:
                    _circuit_breaker_record_success()
                    return []

                task = tasks[0]
                task_status_code = task.get("status_code")

                if task_status_code == _LEADBOT_DATAFORSEO_NO_RESULTS_TASK_STATUS_CODE:
                    # A genuine, legitimate zero-result completion -- not
                    # a failure, so it clears stale failure history same
                    # as any other successful response.
                    _circuit_breaker_record_success()
                    return []

                if task_status_code != 20000:
                    raise RuntimeError(
                        f"DataForSEO task error: {task_status_code} {task.get('status_message')}"
                    )

                _circuit_breaker_record_success()

                if attempt > 1:
                    _leadbot_log_provider_diagnostic(
                        failure_category="recovered",
                        status_code=task_status_code,
                        location_name=resolved_location_name,
                        attempt=attempt,
                        outcome="recovered",
                    )
                break
            except Exception as exc:
                status_for_log = task_status_code if task_status_code is not None else envelope_status_code
                is_retryable = task_status_code in _LEADBOT_DATAFORSEO_RETRYABLE_TASK_STATUS_CODES
                is_last_attempt = attempt == _LEADBOT_DATAFORSEO_MAX_ATTEMPTS
                will_retry = is_retryable and not is_last_attempt

                _leadbot_log_provider_diagnostic(
                    failure_category=_leadbot_classify_provider_failure(exc),
                    status_code=status_for_log,
                    location_name=resolved_location_name,
                    attempt=attempt,
                    outcome="retrying" if will_retry else "exhausted",
                )

                if will_retry:
                    time.sleep(_LEADBOT_DATAFORSEO_RETRY_BACKOFF_SECONDS[attempt - 1])
                    continue

                if is_retryable:
                    # Exhausted every attempt on a retryable code -- this
                    # counts toward the circuit breaker (or, if this call
                    # was itself the one allowed probe, reopens it
                    # immediately). Permanent/non-retryable errors never
                    # reach this branch, so they never affect the breaker.
                    _circuit_breaker_record_exhaustion(_circuit_breaker_now(), is_probe)

                raise
    finally:
        if is_probe:
            _circuit_breaker_release_probe_if_still_claimed()

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

