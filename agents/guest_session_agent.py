"""
Guest (unauthenticated) beta-testing session support for LeadMeLeads.

Deliberately kept separate from agents.auth_agent: a guest is never written
to the users/sessions tables and never gets a real account or a DB-backed
session row. A guest is identified only by an unpredictable, temporary
cookie value used to:

  - scope an in-process scan rate limit per guest (mirrors the existing
    signup rate limiter in app.main: same in-process, IP-keyed,
    sliding-window pattern, just with its own counters)
  - let a guest poll/view only the live scan job(s) they personally started
    (matched against agents.lead_live_job_agent job params, which now carry
    an opaque "guest_id" the same way they already carry owner_email /
    owner_username for logged-in users)

No database changes and no new secrets. CSRF protection for guests uses a
stateless double-submit cookie (a second random cookie whose value must
match a hidden form field) instead of the DB-backed session/CSRF pair used
for logged-in users in agents.auth_agent, since guests have no session row
to hang a server-side CSRF hash off of.

Guest-created scan data (the job JSON file and the CSV export it produces)
is NOT automatically deleted by this module -- see the module docstring in
app.main's guest routes for what currently persists and how it could later
be expired.
"""

import secrets
import threading
import time

GUEST_ID_COOKIE = "lml_guest_id"
GUEST_CSRF_COOKIE = "lml_guest_csrf"

# Guests are meant to be a short, temporary way to try the product -- not a
# persistent identity -- so this cookie is intentionally short-lived.
GUEST_COOKIE_MAX_AGE_SECONDS = 24 * 60 * 60  # 1 day

# Hard ceiling for a guest scan, enforced server-side regardless of what a
# guest's form submission requests. These are intentionally much smaller
# than the authenticated-user ceiling already enforced in
# agents.lead_live_job_agent.run_job() (limit 1-60, others 1-12), so an
# unauthenticated visitor can never trigger a full-size (and, when
# LEADBOT_DATAFORSEO_ENABLED=1, paid-API-backed) scan.
GUEST_SCAN_LIMIT_MAX = 5
GUEST_SCAN_PER_BATCH_MAX = 2
GUEST_SCAN_PER_QUERY_LIMIT_MAX = 2
GUEST_SCAN_MAX_QUERIES_MAX = 2

# In-process, IP+guest-id keyed rate limit for starting guest scans. This is
# intentionally the same shape as app.main's SIGNUP_RATE_LIMIT_* /
# _signup_rate_limit_* pair (in-process only, does not need to survive a
# restart or be shared across servers for the current single-server
# deployment).
GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS = 3
GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour

_guest_scan_rate_limit_lock = threading.Lock()
_guest_scan_rate_limit_attempts = {}


def new_guest_id():
    """Unpredictable identifier for a temporary guest testing session."""
    return secrets.token_urlsafe(24)


def new_guest_csrf_value():
    """Unpredictable value for the guest double-submit CSRF cookie."""
    return secrets.token_urlsafe(24)


def guest_csrf_valid(cookie_value, submitted_value):
    """
    Double-submit CSRF check for guests: the value the browser sent back in
    the form must match the value the server previously set in the
    (HttpOnly-readable-by-server-only-at-render-time) guest CSRF cookie.

    An attacker forging a cross-site request can make the browser attach
    the cookie automatically, but cannot read its value to also put it in
    the forged form body, so the two only match for requests that actually
    loaded the form from this site first.
    """
    cookie_value = str(cookie_value or "")
    submitted_value = str(submitted_value or "")
    if not cookie_value or not submitted_value:
        return False
    return secrets.compare_digest(cookie_value, submitted_value)


def _guest_rate_limit_key(client_host, guest_id):
    host = str(client_host or "").strip()[:80] or "unknown"
    gid = str(guest_id or "").strip()[:80] or "no-guest-id"
    return f"{host}:{gid}"


def guest_scan_is_rate_limited(client_host, guest_id):
    key = _guest_rate_limit_key(client_host, guest_id)
    cutoff = time.time() - GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS

    with _guest_scan_rate_limit_lock:
        attempts = [t for t in _guest_scan_rate_limit_attempts.get(key, []) if t >= cutoff]
        _guest_scan_rate_limit_attempts[key] = attempts
        return len(attempts) >= GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS


def guest_scan_retry_after_seconds(client_host, guest_id):
    """Seconds until the oldest attempt in the current window expires, for
    a Retry-After header -- same pattern as app.main's
    analyze_retry_after_seconds for the /analyze anonymous rate limit."""
    key = _guest_rate_limit_key(client_host, guest_id)
    cutoff = time.time() - GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS

    with _guest_scan_rate_limit_lock:
        attempts = [t for t in _guest_scan_rate_limit_attempts.get(key, []) if t >= cutoff]
        if not attempts:
            return GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS
        oldest = min(attempts)

    return max(1, int(oldest + GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS - time.time()))


def guest_scan_record_attempt(client_host, guest_id):
    key = _guest_rate_limit_key(client_host, guest_id)
    now = time.time()
    cutoff = now - GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS

    with _guest_scan_rate_limit_lock:
        attempts = [t for t in _guest_scan_rate_limit_attempts.get(key, []) if t >= cutoff]
        attempts.append(now)
        _guest_scan_rate_limit_attempts[key] = attempts


def clamp_guest_scan_params(limit, per_batch, per_query_limit, max_queries):
    """Clamp requested scan params down to the guest ceiling, regardless of
    what a guest's form submission (or a direct API call) requests."""

    def _clamp(value, high):
        try:
            value = int(value)
        except Exception:
            value = high
        return max(1, min(value, high))

    return (
        _clamp(limit, GUEST_SCAN_LIMIT_MAX),
        _clamp(per_batch, GUEST_SCAN_PER_BATCH_MAX),
        _clamp(per_query_limit, GUEST_SCAN_PER_QUERY_LIMIT_MAX),
        _clamp(max_queries, GUEST_SCAN_MAX_QUERIES_MAX),
    )


def job_belongs_to_guest(job, guest_id):
    """True only if this job was created by the guest session presenting
    this exact guest_id cookie value."""
    guest_id = str(guest_id or "").strip()
    if not guest_id or not isinstance(job, dict):
        return False

    params = job.get("params") or {}
    if not isinstance(params, dict):
        return False

    job_guest_id = str(params.get("guest_id") or "").strip()
    if not job_guest_id:
        return False

    return secrets.compare_digest(job_guest_id, guest_id)
