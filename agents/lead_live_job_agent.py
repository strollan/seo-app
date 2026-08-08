from agents.leadbot_block_gate import load_main_blocked_domains

import inspect
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import queue
from datetime import datetime
from pathlib import Path


JOB_DIR = Path("data/leadbot_live_jobs")
JOB_DIR.mkdir(parents=True, exist_ok=True)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The web process never runs scan work itself -- create_job() spawns this
# script as an isolated child OS process (see scripts/run_live_lead_job.py).
# A module-level constant, rather than inlining the path at the call site,
# so same-process tests can point it at a small fake worker script and
# exercise the real spawn/cancel/reap plumbing without running a real
# network crawl in the child. LEADBOT_LIVE_RUN_JOB_SCRIPT gives an
# out-of-process test (one that boots this app as a real `uvicorn`
# subprocess, so an in-process mock.patch can't reach it) the same
# override, the same way CRAWL_MAX_RESPONSE_BYTES already does for
# agents/crawl_agent.py. Unset in production, where this is always the
# real script.
RUN_JOB_SCRIPT = Path(
    os.environ.get("LEADBOT_LIVE_RUN_JOB_SCRIPT")
    or (REPO_ROOT / "scripts" / "run_live_lead_job.py")
)

# Hard ceiling on how long a single scan child process may run, enforced by
# a watchdog *inside* the child. Backstop for a scan that never finishes and
# is never cancelled by a user.
SCAN_TIMEOUT_SECONDS = 600

# How long a "cancelling" job is given to exit on its own (cooperative check
# or SIGTERM) before status polling escalates to SIGKILL. Kept short: the
# user-facing goal is "near-immediate", and killing a scan process has none
# of the "let in-flight work finish" concerns a graceful HTTP shutdown has.
CANCEL_GRACE_SECONDS = 5

# Bounds how many Lead Finder scans may run at once across the whole app.
# Scan work now lives in its own OS process rather than the web process, so
# this isn't protecting the event loop directly -- it protects the box
# (CPU, memory, file descriptors, outbound connections) from unbounded
# concurrent scans. Production is a 1 vCPU / 1 GB droplet: even with
# process isolation keeping the web process responsive, several
# simultaneous CPU/network-heavy scan processes could still exhaust the
# box itself, so only one scan is allowed to run at a time.
MAX_CONCURRENT_LIVE_SCANS = 1

# Production (and the real /lead-bot/live-start route) always spawns each
# scan as its own OS process -- that isolation is the actual fix for the P1
# incident this module's cancel/reap machinery exists for. Several existing
# regression tests (test_lead_live_job_export_ownership.py,
# test_failed_scan_retry_search.py, test_invalid_location_value_message.py,
# test_lead_explanation_removed.py, test_lead_why_note_restored.py) call
# create_job() directly and rely on mock.patch("agents.lead_finding_agent.
# find_leads", ...) taking effect -- which only works for code running in
# THIS interpreter. A spawned subprocess re-imports everything fresh from
# disk and can never see an in-process mock.patch, so those tests set this
# False to get the pre-fix in-process-thread execution instead. Nothing in
# app/main.py ever touches this constant.
RUN_SCANS_IN_SUBPROCESS = True

LIVE_SCAN_BATCH_TIMEOUT_SECONDS = 90

# Prefix marker call_find_leads_with_timeout() uses to carry a
# SearchProviderUnavailableError through its (leads, search_error) string
# return contract without changing that contract's shape. run_job() checks
# for this prefix to distinguish "the provider failed" from an ordinary
# per-query error, instead of masking either as a normal zero-result scan.
PROVIDER_UNAVAILABLE_MARKER = "PROVIDER_UNAVAILABLE:"

SEARCH_PROVIDER_UNAVAILABLE_MESSAGE = (
    "The lead search service is temporarily unavailable. This scan did not "
    "complete. Please try again shortly."
)

# Same string-marker technique as PROVIDER_UNAVAILABLE_MARKER, for
# agents.dataforseo_serp_agent.InvalidMarketLocationError: a market that
# can't be resolved into a valid provider location (e.g. a bare city name
# with no state, like "Southampton") is a user-input problem, not a
# provider failure, and must never be surfaced as one.
INVALID_MARKET_LOCATION_MARKER = "INVALID_MARKET_LOCATION:"

INVALID_MARKET_LOCATION_MESSAGE = "Enter a City, State or ZIP Code, such as Albany, NY or 12207."

# Same string-marker technique, for
# agents.dataforseo_serp_agent.InvalidLocationValueError: DataForSEO itself
# rejected our location_name value as invalid (e.g. a misspelled city like
# "Beverley Hills" instead of "Beverly Hills") -- a user-input problem
# confirmed by the provider, not a provider outage, and must never be
# surfaced with the generic "temporarily unavailable" wording (retrying an
# unrecognized location can never succeed).
INVALID_LOCATION_VALUE_MARKER = "INVALID_LOCATION_VALUE:"

INVALID_LOCATION_VALUE_MESSAGE = (
    "We couldn't find that location. Check the city and state spelling and try again."
)

# Shown when at least one query in a scan hit a genuine provider failure
# (PROVIDER_UNAVAILABLE_MARKER) but at least one OTHER query in the same
# scan succeeded (with results or a legitimate zero-result outcome). A
# single failing query variant must never discard leads a different
# query variant already found -- see run_job()'s per-query loop.
PARTIAL_RESULTS_WARNING_MESSAGE = (
    "Some search requests could not be completed. The results shown may be incomplete."
)

# How often the pre-first-lead heartbeat refreshes its status message.
# A module-level constant (rather than a literal in the closure) so tests
# can shrink it to force a real, fast heartbeat/streaming write race
# instead of waiting out the real cadence.
LEADBOT_HEARTBEAT_INTERVAL_SECONDS = 3.0

# === LEADBOT QUERY OUTCOME DIAGNOSTICS START ===
# Sanitized, job-level summary of how a scan's queries resolved -- how
# many were attempted, how many succeeded (with results or a legitimate
# zero-result outcome), how many hit a genuine provider failure, whether
# the scan ended up partial, and how many leads were ultimately
# published. Never logs query text, provider names, numeric provider
# status codes, or any lead/business data -- only counts and the job id.
_LEADBOT_QUERY_OUTCOME_LOG = Path("data") / "leadbot_query_outcomes.log"


def _leadbot_log_query_outcome(
    job_id,
    queries_attempted,
    queries_succeeded,
    queries_failed,
    leads_published,
    is_partial,
):
    try:
        _LEADBOT_QUERY_OUTCOME_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": now_iso(),
            "job_id": job_id,
            "queries_attempted": queries_attempted,
            "queries_succeeded": queries_succeeded,
            "queries_failed": queries_failed,
            "leads_published": leads_published,
            "partial": is_partial,
        }
        with _LEADBOT_QUERY_OUTCOME_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass
# === LEADBOT QUERY OUTCOME DIAGNOSTICS END ===



# === LEADBOT HARD BLOCKED DOMAIN FINAL GATE START ===
def _leadbot_clean_domain_for_block_gate(value) -> str:
    from urllib.parse import urlparse

    raw = str(value or "").strip().lower().strip(" ,")
    if not raw:
        return ""

    if "://" in raw:
        host = urlparse(raw).netloc.lower()
    else:
        host = raw.split("/")[0].lower()

    host = host.split("@")[-1].split(":")[0].strip()
    if host.startswith("www."):
        host = host[4:]

    return host if "." in host else ""


def _leadbot_load_blocked_domains_for_gate() -> set[str]:
    """
    Final live-scan block gate.

    Important:
    The UI Block button saves to JSON files, not just old .txt files.
    This gate must read both, otherwise blocked domains can still appear
    in live jobs/dashboard output.
    """
    from pathlib import Path
    import json

    txt_paths = [
        Path("data/leadbot_blocked_domains.txt"),
        Path("data/leadbot_blocked_domains_extracted.txt"),
        Path("data/leadbot_blocklist.txt"),
        Path("exports/leadbot_blocked_domains.txt"),
    ]

    json_paths = [
        Path("data/leadbot_fast_blocklist.json"),
        Path("data/leadbot_blocklist_global.json"),
    ]

    blocked = set()

    for path in txt_paths:
        try:
            if not path.exists():
                continue

            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                clean = _leadbot_clean_domain_for_block_gate(line)
                if clean:
                    blocked.add(clean)
        except Exception:
            pass

    for path in json_paths:
        try:
            if not path.exists():
                continue

            data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")

            for item in (data.get("domains") or []):
                clean = _leadbot_clean_domain_for_block_gate(item)
                if clean:
                    blocked.add(clean)

            for item in (data.get("patterns") or []):
                clean = _leadbot_clean_domain_for_block_gate(str(item).replace("*.", ""))
                if clean:
                    blocked.add(clean)

        except Exception:
            pass

    return blocked



def _leadbot_is_blocked_by_final_gate(lead) -> bool:
    try:
        from agents.lead_domain_filter_agent import is_bad_lead_domain
    except Exception:
        is_bad_lead_domain = None

    domain = ""
    url = ""

    if isinstance(lead, dict):
        domain = _leadbot_clean_domain_for_block_gate(lead.get("domain") or "")
        url = _leadbot_clean_domain_for_block_gate(lead.get("url") or "")
    else:
        domain = _leadbot_clean_domain_for_block_gate(lead)

    candidates = {x for x in [domain, url] if x}

    if is_bad_lead_domain:
        for candidate in candidates:
            try:
                if is_bad_lead_domain(candidate):
                    return True
            except Exception:
                pass

    blocked = _leadbot_load_blocked_domains_for_gate()

    for candidate in candidates:
        for blocked_domain in blocked:
            if candidate == blocked_domain or candidate.endswith("." + blocked_domain):
                return True

    return False


def _leadbot_apply_final_blocked_domain_gate(job) -> None:
    if not isinstance(job, dict):
        return

    leads = job.get("leads")
    if not isinstance(leads, list):
        return

    from agents.leadbot_block_gate import (
        blocklist_owner_key,
        lead_matches_blocked_domains,
        load_effective_blocked_domains,
    )
    blocked_domains = load_effective_blocked_domains(
        blocklist_owner_key(job.get("params") or {})
    )

    kept = []
    removed = []

    for lead in leads:
        if (
            lead_matches_blocked_domains(lead, blocked_domains)
            or _leadbot_is_blocked_by_final_gate(lead)
        ):
            if isinstance(lead, dict):
                removed.append(lead.get("domain") or lead.get("url") or "blocked")
            else:
                removed.append(str(lead))
            continue
        kept.append(lead)

    if removed:
        job["leads"] = kept
        job["blocked_domains_removed"] = sorted(set(str(x) for x in removed if x))

        counts = job.get("counts")
        if isinstance(counts, dict):
            counts["found"] = len(kept)
            counts["enriched"] = sum(
                1 for lead in kept
                if isinstance(lead, dict)
                and (
                    str(lead.get("best_phone") or "").strip()
                    or str(lead.get("emails") or "").strip()
                    or str(lead.get("contact_page_url") or "").strip()
                )
            )
            counts["needs_research"] = max(0, len(kept) - counts.get("enriched", 0))

# === LEADBOT HARD BLOCKED DOMAIN FINAL GATE END ===


_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")


def _resolve_find_leads_call(
    find_leads, *, industry, market, query, own_domain, limit, on_candidate,
    blocked_domains=None,
):
    """
    Decide how to call `find_leads`, defaulting to inspecting its signature
    up front rather than the risky pattern
    `try: fn(..., on_candidate=x) except TypeError: fn(...)` -- a TypeError
    raised from *inside* find_leads for an unrelated reason would otherwise
    be indistinguishable from "on_candidate isn't a supported parameter",
    and could cause the whole (potentially expensive, external SERP/contact)
    call to silently run a second time.

    inspect.signature() alone isn't quite enough: a callable that's actually
    a mock.patch(..., side_effect=real_fn) MagicMock reports a generic
    (*args, **kwargs) signature regardless of what real_fn really accepts,
    so it can look like it supports on_candidate when it doesn't. To handle
    that safely without reintroducing a broad except TypeError, the
    returned callable performs at most one *executed* find_leads call:
    if the richer attempt raises a TypeError whose message names exactly
    'on_candidate' as an unexpected keyword argument, it retries without
    it. This is safe to retry rather than double-execute, because Python
    binds keyword arguments (and raises this exact TypeError) before the
    callee's body ever runs -- so nothing inside find_leads has executed
    yet when this specific error occurs. Any other TypeError -- including
    one that happens to occur after argument binding succeeds -- is
    re-raised untouched, exactly as the caller would see it without any of
    this fallback logic.
    """
    try:
        sig = inspect.signature(find_leads)
        params = sig.parameters
        has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        accepts_service_keyword = has_var_kwargs or "service_keyword" in params
        accepts_on_candidate = has_var_kwargs or "on_candidate" in params
        accepts_blocked_domains = has_var_kwargs or "blocked_domains" in params
    except (TypeError, ValueError):
        # Signature couldn't be inspected at all (e.g. a builtin/C callable).
        accepts_service_keyword = False
        accepts_on_candidate = False
        accepts_blocked_domains = False

    if not accepts_service_keyword:
        # Oldest supported shape: positional-only, no on_candidate support.
        return lambda: find_leads(industry, market, query, own_domain, limit)

    def _call(include_on_candidate):
        kwargs = dict(industry=industry, market=market, service_keyword=query, own_domain=own_domain, limit=limit)
        if include_on_candidate:
            kwargs["on_candidate"] = on_candidate
        if accepts_blocked_domains:
            kwargs["blocked_domains"] = blocked_domains
        return find_leads(**kwargs)

    def call():
        if not accepts_on_candidate:
            return _call(False)
        try:
            return _call(True)
        except TypeError as exc:
            match = _UNEXPECTED_KWARG_RE.search(str(exc))
            if match and match.group(1) == "on_candidate":
                return _call(False)
            raise

    return call


def call_find_leads_with_timeout(
    find_leads, *, industry, market, query, own_domain, limit, publish_callback=None,
    blocked_domains=None,
):
    """
    Keep Live Scan from hanging forever on one SERP/search batch.
    If find_leads stalls, return a timeout error and let the job continue.

    Thread-safety contract: find_leads() runs in a background worker thread.
    That worker thread is only ever given a callback that does a thread-safe
    `queue.Queue.put()` -- it never touches the job object or write_job()
    directly. This function's own polling loop (which runs on the CALLER's
    thread, i.e. run_job()'s thread, since this function itself is called
    synchronously, not spawned) drains that queue and invokes
    `publish_callback` -- so all job mutation and all write_job() calls
    happen on the same single thread that already owned them before this
    streaming path existed. `queue.Queue` is documented as safe for exactly
    this producer/consumer pattern (CPython's GIL plus internal locking make
    put()/get() atomic across threads).
    """
    result_queue = queue.Queue(maxsize=1)
    progress_queue = queue.Queue()

    def _on_candidate(lead):
        # Runs on the WORKER thread. Must stay a pure, non-blocking,
        # thread-safe enqueue -- no job mutation, no write_job() here.
        progress_queue.put(lead)

    call_find_leads = _resolve_find_leads_call(
        find_leads,
        industry=industry,
        market=market,
        query=query,
        own_domain=own_domain,
        limit=limit,
        on_candidate=_on_candidate,
        blocked_domains=blocked_domains,
    )

    def target():
        try:
            result = call_find_leads()
            result_queue.put(("ok", result))
        except Exception as exc:
            result_queue.put(("error", exc))

    def _drain_progress():
        # Runs on the CALLER's thread (run_job()'s thread). Safe to mutate
        # job / call write_job() here via publish_callback.
        if publish_callback is None:
            return
        while True:
            try:
                candidate = progress_queue.get_nowait()
            except queue.Empty:
                return
            publish_callback(candidate)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    deadline = time.monotonic() + LIVE_SCAN_BATCH_TIMEOUT_SECONDS
    poll_interval = 0.2

    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(min(poll_interval, remaining))
        _drain_progress()

    _drain_progress()

    if thread.is_alive():
        return None, f"Search batch timed out after {LIVE_SCAN_BATCH_TIMEOUT_SECONDS}s; skipping to next batch"

    try:
        status, payload = result_queue.get_nowait()
    except Exception:
        return None, "Search ended without returning results"

    if status == "error":
        from agents.dataforseo_serp_agent import InvalidMarketLocationError, InvalidLocationValueError
        from business_competitor_finder import SearchProviderUnavailableError

        if isinstance(payload, InvalidMarketLocationError):
            return None, f"{INVALID_MARKET_LOCATION_MARKER}{payload}"
        if isinstance(payload, InvalidLocationValueError):
            return None, f"{INVALID_LOCATION_VALUE_MARKER}{payload}"
        if isinstance(payload, SearchProviderUnavailableError):
            return None, f"{PROVIDER_UNAVAILABLE_MARKER}{payload}"
        return None, str(payload)

    return payload, ""



def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def job_path(job_id):
    return JOB_DIR / f"{job_id}.json"


def write_job(job):
    # === CENTRAL BLOCKLIST GATE START ===
    try:
        _leadbot_apply_final_blocked_domain_gate(job)
    except Exception:
        pass
    # === CENTRAL BLOCKLIST GATE END ===
    path = job_path(job["job_id"])
    # Unique per-call tmp name: cancel_job() can now legitimately be called
    # concurrently for the same job_id by several overlapping HTTP requests
    # (repeated rapid Cancel clicks), and the worker's own process can write
    # the same job file around the same moment the web process's status
    # poll reaps it. A fixed ".tmp" name means two concurrent writers can
    # collide -- one's tmp.replace(path) consumes the file the other is
    # about to rename, raising FileNotFoundError. Each writer using its own
    # tmp file makes every write independently atomic; whichever finishes
    # last simply becomes the on-disk state, which is fine here since every
    # writer is publishing the same kind of monotonic status snapshot, not
    # merging conflicting data.
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_job(job_id):
    path = job_path(job_id)
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def job_belongs_to_authenticated_user(job, user) -> bool:
    """Match a live job to its authenticated owner without admin bypass."""
    if not isinstance(job, dict) or not user:
        return False

    params = job.get("params")
    if not isinstance(params, dict):
        return False

    def values(source, keys):
        if isinstance(source, dict):
            raw = (source.get(key) for key in keys)
        else:
            raw = (getattr(source, key, "") for key in keys)
        return {
            str(value or "").strip().lower()
            for value in raw
            if str(value or "").strip()
        }

    job_owners = values(params, ("owner_email", "owner_username"))
    user_owners = values(user, ("email", "username"))
    return bool(job_owners and user_owners and job_owners & user_owners)


# === LEADBOT LIVE CANCEL SUPPORT START ===
def is_cancel_requested(job_id):
    job = read_job(job_id)
    if not job:
        return False
    if bool(job.get("cancel_requested")):
        return True
    return str(job.get("status") or "").lower() in {"cancelling", "cancelled"}


def _pid_alive(pid):
    """True if `pid` names a live process this box can see.

    os.kill(pid, 0) sends no signal -- it only asks the kernel whether the
    process exists and is visible to us. A PermissionError means it exists
    (just owned by someone else), so that also counts as alive.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    return True


def _signal_worker(pid, sig):
    """Best-effort, non-blocking signal delivery to a scan's child process.

    Signals its whole process group (it was started with start_new_session
    so its pgid == its pid) in case it ever spawns helper processes of its
    own -- e.g. a future browser/subprocess-based crawl step. Falls back to
    signalling just the pid if the group signal isn't permitted. Every
    failure mode here (already exited, never existed, no permission) is
    swallowed: cancel must never raise back into the HTTP handler.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return

    if pid <= 0:
        return

    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError:
        return
    except Exception:
        pass

    try:
        os.kill(pid, sig)
    except Exception:
        pass


def _seconds_since(timestamp):
    if not timestamp:
        return None
    try:
        then = datetime.strptime(str(timestamp), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - then).total_seconds()


def cancel_job(job_id):
    """Mark a job cancelling and signal its worker process. Returns fast.

    Idempotent: a job already in a terminal state (done/error/cancelled) is
    returned unchanged, and a job already "cancelling" is returned unchanged
    too (aside from a harmless repeat SIGTERM) -- so double-clicks and
    retried requests from a flaky connection never re-run cleanup or spam
    duplicate "Cancel requested" events. Never blocks on the worker process
    actually exiting; that is reap_job()'s job, driven by status polling.
    """
    job = read_job(job_id)

    if not job:
        return {
            "status": "missing",
            "message": "Job not found.",
            "job_id": job_id,
            "leads": [],
        }

    current_status = str(job.get("status") or "").lower()

    if current_status in {"done", "error", "cancelled"}:
        return job

    if current_status == "cancelling":
        # Already in flight. Re-signal in case the first SIGTERM never
        # reached the process (e.g. sent between fork and exec), but do
        # not touch job state or append another event -- a double-click
        # here must be a pure no-op from the caller's point of view.
        _signal_worker(job.get("worker_pid"), signal.SIGTERM)
        return job

    job["cancel_requested"] = True

    if RUN_SCANS_IN_SUBPROCESS:
        job["status"] = "cancelling"
        job["message"] = "Cancelling scan..."
        job["cancel_requested_at"] = now_iso()
    else:
        # No worker process exists to signal or wait on (test-only escape
        # hatch -- see RUN_SCANS_IN_SUBPROCESS): the in-process thread's own
        # cooperative is_cancel_requested() check is the only thing that
        # will ever stop it, so there is no "cancelling" state to reap --
        # go straight to the terminal state, same as before this fix.
        job["status"] = "cancelled"
        job["message"] = "Scan cancelled."
        job["cancelled_at"] = now_iso()

    job["updated_at"] = now_iso()
    job.setdefault("events", []).append({
        "time": now_iso(),
        "message": "Cancel requested by user.",
    })

    write_job(job)

    if RUN_SCANS_IN_SUBPROCESS:
        _signal_worker(job.get("worker_pid"), signal.SIGTERM)

    return job


def reap_job(job_id):
    """Finalize a "cancelling" job once its worker process has actually
    exited, and escalate to SIGKILL if it hasn't after CANCEL_GRACE_SECONDS.

    Called opportunistically from the live-status poll (already hit ~every
    couple seconds by the UI while a scan is in flight), so no extra thread
    or timer is needed in the web process to drive a scan to a final
    CANCELLED state. A no-op for any job not currently "cancelling".
    """
    job = read_job(job_id)
    if not job:
        return job

    if str(job.get("status") or "").lower() != "cancelling":
        return job

    pid = job.get("worker_pid")

    if not _pid_alive(pid):
        job["status"] = "cancelled"
        job["cancel_requested"] = True
        job["message"] = "Scan cancelled."
        job["cancelled_at"] = now_iso()
        job["updated_at"] = now_iso()
        write_job(job)
        return job

    elapsed = _seconds_since(job.get("cancel_requested_at"))
    if elapsed is not None and elapsed > CANCEL_GRACE_SECONDS:
        _signal_worker(pid, signal.SIGKILL)

    return job


def mark_job_cancelled(job_id, message="Scan cancelled."):
    """Called by the worker itself once it cooperatively notices a cancel
    request between units of work. Goes straight to the terminal CANCELLED
    state -- unlike cancel_job(), there is no process left to wait on."""
    job = read_job(job_id)

    if not job:
        return None

    job["cancel_requested"] = True
    job["status"] = "cancelled"
    job["message"] = message
    job["cancelled_at"] = now_iso()
    job["updated_at"] = now_iso()
    write_job(job)
    return job
# === LEADBOT LIVE CANCEL SUPPORT END ===


def _count_active_live_scans():
    active = 0

    try:
        job_files = list(JOB_DIR.glob("*.json"))
    except OSError:
        return 0

    for path in job_files:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if str(job.get("status") or "").lower() not in {"queued", "running", "cancelling"}:
            continue

        if _pid_alive(job.get("worker_pid")):
            active += 1

    return active


def create_job(params):
    job_id = uuid.uuid4().hex[:16]

    job = {
        "job_id": job_id,
        "status": "queued",
        "message": "Queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "params": params,
        "leads": [],
        "seen_domains": [],
        "errors": [],
        "cancel_requested": False,
        "cancelled_at": "",
        "worker_pid": None,
        "counts": {
            "found": 0,
            "cached": 0,
            "enriched": 0,
            "needs_research": 0,
        },
        "export_file": "",
    }

    if RUN_SCANS_IN_SUBPROCESS and _count_active_live_scans() >= MAX_CONCURRENT_LIVE_SCANS:
        job["status"] = "error"
        job["message"] = (
            "Too many scans are running right now. Please try again in a minute."
        )
        write_job(job)
        return job_id

    write_job(job)

    if not RUN_SCANS_IN_SUBPROCESS:
        # Test-only escape hatch -- see RUN_SCANS_IN_SUBPROCESS. Every real
        # call path (the actual /lead-bot/live-start route) always takes
        # the subprocess branch below.
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()
        return job_id

    # Scan work runs in its own OS process, never on a thread inside the
    # shared FastAPI/Uvicorn worker: a thread here would share this
    # process's GIL and event loop with every other request this server is
    # handling, so heavy or stuck crawl work could starve unrelated routes
    # (this is the mechanism behind the site-wide hang this architecture
    # fixes). start_new_session=True gives the child its own process group
    # so cancellation can signal the whole group, not just one pid.
    proc = subprocess.Popen(
        [sys.executable, str(RUN_JOB_SCRIPT), "--job-id", job_id],
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    job["worker_pid"] = proc.pid
    write_job(job)

    # Nothing else in this process ever calls proc.wait()/.poll() on this
    # child (its pid is looked up later purely from the job file, as a bare
    # int, not through this Popen object) -- without this, a finished child
    # becomes a zombie that os.kill(pid, 0) still reports as alive forever,
    # since the kernel keeps a zombie's PID entry until something reaps it.
    # A dedicated thread blocked on wait() for exactly this one child is
    # the reap: it touches no other subprocess this app spawns elsewhere
    # (unlike a global os.waitpid(-1, ...), which could steal another call
    # site's own child -- e.g. app/main.py's subprocess.check_output() for
    # opening an exports folder -- out from under its own wait()).
    threading.Thread(target=proc.wait, daemon=True).start()

    return job_id


def run_job_in_subprocess(job_id):
    """Entry point for scripts/run_live_lead_job.py -- runs in the isolated
    child process, never in the web process.

    Installs a SIGTERM handler (cancel_job() signals this process) and a
    hard scan-level watchdog timer, then runs the normal run_job() loop.
    Either the SIGTERM handler, the watchdog, or run_job()'s own cooperative
    cancellation checks may end the scan; whichever happens first wins, and
    all three leave the job in a terminal state before the process exits.
    """
    def _on_sigterm(signum, frame):
        try:
            mark_job_cancelled(job_id, message="Scan cancelled.")
        except Exception:
            pass
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        # Only the main thread of the main interpreter may install a signal
        # handler. run_job_in_subprocess() is always called that way in
        # production (it's the whole point of the entry point script), but
        # tests may call it from a worker thread -- degrade gracefully
        # rather than raising, since the watchdog/cooperative checks below
        # still provide cancellation.
        pass

    def _on_timeout():
        try:
            job = read_job(job_id)
            status = str((job or {}).get("status") or "").lower()
            if status not in {"done", "error", "cancelled"}:
                mark_job_cancelled(
                    job_id,
                    message=f"Scan exceeded the {SCAN_TIMEOUT_SECONDS}s maximum runtime and was stopped.",
                )
        except Exception:
            pass
        os._exit(1)

    watchdog = threading.Timer(SCAN_TIMEOUT_SECONDS, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    try:
        run_job(job_id)
    finally:
        watchdog.cancel()


def clean_int(value, default, low, high):
    try:
        value = int(value)
    except Exception:
        value = default

    return max(low, min(value, high))


def lead_to_public(lead):
    emails = lead.get("emails") or lead.get("email") or ""
    if isinstance(emails, list):
        emails = ", ".join([str(e) for e in emails if str(e).strip()])

    return {
        "title": lead.get("title") or "",
        "domain": lead.get("domain") or "",
        "url": lead.get("url") or lead.get("website") or lead.get("link") or "",
        "serp_page": str(lead.get("serp_page") or ""),
        "serp_position": str(lead.get("serp_position") or ""),
        "best_phone": lead.get("best_phone") or lead.get("phone") or "",
        "emails": emails,
        "contact_page_url": lead.get("contact_page_url") or lead.get("contact_page") or "",
        "address": lead.get("address") or lead.get("full_address") or lead.get("business_address") or lead.get("formatted_address") or lead.get("street_address") or lead.get("place_address") or "",
        "full_address": lead.get("full_address") or lead.get("address") or lead.get("business_address") or lead.get("formatted_address") or "",
        "business_address": lead.get("business_address") or lead.get("address") or lead.get("full_address") or lead.get("formatted_address") or "",
        "formatted_address": lead.get("formatted_address") or lead.get("address") or lead.get("full_address") or lead.get("business_address") or "",
        "street_address": lead.get("street_address") or "",
        "address_source": lead.get("address_source") or "",
        "address_status": lead.get("address_status") or "",
        "outreach_status": lead.get("outreach_status") or "needs_manual_research",
        "contact_confidence": str(lead.get("contact_confidence") or 0),
        "final_lead_score": str(lead.get("final_lead_score") or lead.get("score") or 0),
        "contact_flags": lead.get("contact_flags") or "",
    }


def _leadbot_filter_new_export_rows(rows, blocked_domains):
    """Final personal/global guard immediately before creating a new CSV."""
    from agents.leadbot_block_gate import lead_matches_blocked_domains

    return [
        row for row in (rows or [])
        if not lead_matches_blocked_domains(row, set(blocked_domains or ()))
    ]



def normalize_lead_results(raw):
    """
    find_leads may return a list, a dict with leads/results/items,
    or other shapes depending on the older LeadBot path.
    Live jobs need a clean list of dict leads only.
    """
    if raw is None:
        return []

    if isinstance(raw, dict):
        for key in ["leads", "results", "items", "data"]:
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        # Single lead dict shape.
        if raw.get("domain") or raw.get("url") or raw.get("title"):
            return [raw]

        return []

    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]

    return []


def _leadbot_is_page_one_organic_result(lead):
    """Persistence safety guard for newly generated live-job results."""
    if not isinstance(lead, dict):
        return False

    source = str(lead.get("source") or "").strip().lower()
    serp_page = str(lead.get("serp_page") or "").strip().lower()
    if source == "google_places" or serp_page == "google places":
        return False

    try:
        position = int(str(lead.get("serp_position") or "").strip())
    except (TypeError, ValueError):
        return False

    return 1 <= position <= 10



def _leadbot_first_address_value(row):
    if not isinstance(row, dict):
        return ""

    for key in [
        "address",
        "full_address",
        "business_address",
        "formatted_address",
        "street_address",
        "place_address",
        "mailing_address",
        "location_address",
        "google_address",
        "maps_address",
    ]:
        value = str(row.get(key) or "").strip()
        if value and value.lower() not in {"not found", "none", "nan", "null"}:
            return value

    return ""


def _leadbot_apply_address_aliases(row, address, source="live_website"):
    if not isinstance(row, dict):
        return row

    address = str(address or "").strip()
    if not address:
        return row

    for key in ["address", "full_address", "business_address", "formatted_address"]:
        if not str(row.get(key) or "").strip():
            row[key] = address

    row["address_source"] = row.get("address_source") or source
    row["address_status"] = row.get("address_status") or "found"

    return row


def _leadbot_enrich_live_address(row, market=""):
    """
    Lightweight live-scan address polish.

    First uses the existing lead_address_agent, which checks the business site/contact
    page for JSON-LD/schema/footer-style address text. This avoids inventing addresses.
    If that fails, it optionally falls back to address_finding_agent, which already has
    its own market guardrails and quota guards.
    """
    if not isinstance(row, dict):
        return row

    existing = _leadbot_first_address_value(row)
    if existing:
        return _leadbot_apply_address_aliases(row, existing, source=row.get("address_source") or "existing")

    # Website/schema/text pass.
    try:
        from agents.lead_address_agent import enrich_row
        enriched = enrich_row(dict(row))
        found = _leadbot_first_address_value(enriched)
        if found:
            row.update(enriched)
            return _leadbot_apply_address_aliases(row, found, source=row.get("address_source") or "website")
    except Exception as exc:
        try:
            print(f"LEADBOT LIVE ADDRESS WEBSITE PASS ERROR: {exc}", flush=True)
        except Exception:
            pass

    # Existing stronger finder fallback. It has guardrails/quota guards.
    try:
        from agents.address_finding_agent import find_business_address
        found = find_business_address(row, market=market)
        if found:
            return _leadbot_apply_address_aliases(row, found, source=row.get("address_source") or "address_finder")
    except TypeError:
        try:
            from agents.address_finding_agent import find_business_address
            found = find_business_address(row)
            if found:
                return _leadbot_apply_address_aliases(row, found, source=row.get("address_source") or "address_finder")
        except Exception as exc:
            try:
                print(f"LEADBOT LIVE ADDRESS FINDER ERROR: {exc}", flush=True)
            except Exception:
                pass
    except Exception as exc:
        try:
            print(f"LEADBOT LIVE ADDRESS FINDER ERROR: {exc}", flush=True)
        except Exception:
            pass

    return row



def _leadbot_live_norm_domain_for_address_sync(row):
    if not isinstance(row, dict):
        return ""

    value = (
        row.get("domain")
        or row.get("root_domain")
        or row.get("website")
        or row.get("url")
        or row.get("link")
        or ""
    )

    value = str(value or "").strip().lower()
    value = value.replace("https://", "").replace("http://", "")
    value = value.replace("www.", "")
    value = value.split("/")[0]
    return value


def _leadbot_live_first_address_for_csv_sync(row):
    if not isinstance(row, dict):
        return ""

    for key in ["address", "full_address", "business_address", "formatted_address", "street_address", "place_address"]:
        value = str(row.get(key) or "").strip()
        if value and value.lower() not in {"not found", "none", "nan", "null"}:
            return value

    return ""


def _leadbot_sync_live_addresses_to_export(job):
    """
    Live scan cards can collect addresses before/after export rows are built.
    This syncs trusted live job address fields into the dashboard CSV, so
    Open Desktop shows the same addresses the live cards showed.
    """
    try:
        import csv
        from pathlib import Path

        if not isinstance(job, dict):
            return 0

        export_file = str(job.get("export_file") or "").strip()
        leads = job.get("leads") or []

        if not export_file or not leads:
            return 0

        address_by_domain = {}

        for lead in leads:
            domain = _leadbot_live_norm_domain_for_address_sync(lead)
            address = _leadbot_live_first_address_for_csv_sync(lead)

            if domain and address:
                address_by_domain[domain] = address

        if not address_by_domain:
            return 0

        export_path = None
        for base in [Path("exports"), Path("data/exports"), Path("data"), Path(".")]:
            candidate = base / export_file
            if candidate.exists():
                export_path = candidate
                break

        if not export_path:
            for base in [Path("exports"), Path("data/exports"), Path("data"), Path(".")]:
                matches = list(base.rglob(export_file))
                if matches:
                    export_path = matches[0]
                    break

        if not export_path or not export_path.exists():
            return 0

        with export_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])

        for col in ["address", "full_address", "business_address", "formatted_address", "address_status", "address_source"]:
            if col not in fieldnames:
                fieldnames.append(col)

        changed = 0

        for row in rows:
            domain = _leadbot_live_norm_domain_for_address_sync(row)
            address = address_by_domain.get(domain)

            if not address:
                continue

            if _leadbot_live_first_address_for_csv_sync(row):
                continue

            row["address"] = address
            row["full_address"] = address
            row["business_address"] = address
            row["formatted_address"] = address
            row["address_status"] = row.get("address_status") or "found"
            row["address_source"] = row.get("address_source") or "live_scan"
            changed += 1

        if not changed:
            return 0

        with export_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        try:
            print(f"LEADBOT LIVE ADDRESS CSV SYNC: changed={changed} file={export_file}", flush=True)
        except Exception:
            pass

        return changed

    except Exception as exc:
        try:
            print(f"LEADBOT LIVE ADDRESS CSV SYNC ERROR: {exc}", flush=True)
        except Exception:
            pass

        return 0


def run_job(job_id):
    job = read_job(job_id)

    if not job:
        return

    try:
        from agents.lead_finding_agent import find_leads
        from agents.lead_business_cache_agent import apply_cached_business_to_lead, save_business_from_lead
        from agents.lead_export_agent import export_leads_to_csv

        params = job.get("params", {})

        industry = str(params.get("industry") or "").strip()
        market = str(params.get("market") or "Long Island").strip()
        keyword = str(params.get("keyword") or industry).strip()
        own_domain = str(params.get("own_domain") or "").strip()
        from agents.leadbot_block_gate import (
            blocklist_owner_key,
            lead_matches_blocked_domains,
            load_effective_blocked_domains,
        )
        blocked_domains = load_effective_blocked_domains(blocklist_owner_key(params))

        total_limit = clean_int(params.get("limit"), 50, 1, 60)

        # Final card target is total_limit.
        # Raw search capacity must be larger because filters/dedupe remove junk.
        per_batch = clean_int(params.get("per_batch"), 8, 1, 12)
        per_query_limit = clean_int(params.get("per_query_limit"), 8, 1, 12)
        max_queries = clean_int(params.get("max_queries"), 8, 1, 12)

        job["status"] = "running"
        job["message"] = "Starting live scan..."
        job["updated_at"] = now_iso()
        write_job(job)

        # Build expanded query set here so live jobs can run independently.
        # Intent rules:
        # - Industry = business type.
        # - Keyword = service/search focus.
        # - If both exist, combined intent gets searched first.
        # - Backups search keyword-only and industry-only.
        # Long Island is huge, so include Nassau/Suffolk variants instead of only broad LI.

        def clean_query_part(value):
            return " ".join(str(value or "").strip().split())

        industry_clean = clean_query_part(industry)
        keyword_clean = clean_query_part(keyword)

        service_terms = []

        if industry_clean and keyword_clean and industry_clean.lower() != keyword_clean.lower():
            service_terms.append(f"{industry_clean} {keyword_clean}")

        if keyword_clean:
            service_terms.append(keyword_clean)

        if industry_clean and industry_clean.lower() != keyword_clean.lower():
            service_terms.append(industry_clean)

        normalized_seed = " ".join(f"{industry_clean} {keyword_clean}".lower().split())

        if "paint" in normalized_seed:
            service_terms.extend([
                "painting company",
                "house painter",
                "interior painter",
                "exterior painter",
                "residential painter",
                "commercial painter",
                "painting contractor",
            ])

        # Small food/local backfill.
        # Keep this conservative so scans do not hang or get noisy.
        if any(word in normalized_seed for word in ["bagel", "bakery"]):
            service_terms.extend([
                "bagel shop",
                "bakery",
            ])

        deduped_terms = []
        seen_terms = set()
        for term in service_terms:
            term = clean_query_part(term)
            key = term.lower()
            if term and key not in seen_terms:
                seen_terms.add(key)
                deduped_terms.append(term)

        query_items = []

        for index, term in enumerate(deduped_terms):
            is_primary = index == 0

            # Primary combined intent gets the cleanest first pass.
            query_items.append({"query": f"{term} {market}", "category": "primary" if is_primary else "core"})

            # Long Island expansion.
            if "long island" in market.lower():
                query_items.extend([
                    {"query": f"{term} Nassau County NY", "category": "nassau-county"},
                    {"query": f"{term} Suffolk County NY", "category": "suffolk-county"},
                    {"query": f"{term} Nassau NY", "category": "nassau"},
                    {"query": f"{term} Suffolk NY", "category": "suffolk"},
                    {"query": f"{term} Nassau County Long Island", "category": "nassau-long-island"},
                    {"query": f"{term} Suffolk County Long Island", "category": "suffolk-long-island"},
                ])

            # Supporting query shapes.
            query_items.extend([
                {"query": f"{term} near {market}", "category": "near"},
                {"query": f"{market} {term}", "category": "market-first"},
                {"query": f"best {term} {market}", "category": "best"},
            ])

        query_items = [item for item in query_items if item]

        deduped_query_items = []
        seen_queries = set()
        for item in query_items:
            q = str(item.get("query") or "").strip()
            key = q.lower()
            if q and key not in seen_queries:
                seen_queries.add(key)
                deduped_query_items.append(item)

        query_items = deduped_query_items[:max_queries]
        queries = [item["query"] for item in query_items]
        query_categories = {item["query"]: item.get("category", "") for item in query_items}

        if not queries:
            raise RuntimeError("No search query was provided.")

        seen_domains = set(job.get("seen_domains") or [])
        all_rows = []

        # Per-query outcome tally so one query's genuine provider failure
        # doesn't discard results a different query already found. Only
        # PROVIDER_UNAVAILABLE_MARKER counts as a "failed" query here --
        # a market that can't be resolved at all (INVALID_MARKET_LOCATION_
        # MARKER) still aborts the whole scan immediately below, since
        # every remaining query would fail identically anyway.
        queries_attempted = 0
        queries_succeeded = 0
        queries_with_provider_failure = 0

        def _stop_heartbeat_before_write(heartbeat_stop_event, heartbeat_thread):
            """Deterministically silence the heartbeat before this thread's
            own write_job() call, so the two can never race on disk.

            Signals the Event (the heartbeat's wait() wakes immediately,
            not on its next 3s tick) and then joins the heartbeat thread --
            blocking until it has fully exited, including finishing any
            write_job() call that was already in flight. Only after join()
            returns is it guaranteed the heartbeat will never write again,
            so the caller's own write_job() right after this call is safe.

            Always called from run_job()'s own thread, never from the
            heartbeat thread itself, so this can never self-join/deadlock.
            join() on an already-finished thread returns immediately, so
            calling this for every published lead (not just the first) is
            cheap and safe.
            """
            heartbeat_stop_event.set()
            heartbeat_thread.join(timeout=5.0)

        def _publish_candidate(lead, heartbeat_stop_event, heartbeat_thread):
            """Sole publisher of a single accepted lead: domain dedupe,
            guest/logged-in total-limit enforcement, cache application,
            address enrichment, append to job["leads"], write_job().

            Always called on run_job()'s own thread -- either synchronously
            from the post-batch catch-up loop below (as today), or from
            call_find_leads_with_timeout()'s polling loop while it waits on
            the worker thread (new). Never called from the SERP/scoring
            worker thread itself, so this is the only place job/write_job()
            get touched for lead data, preserving the existing single-writer
            guarantee that made write_job() safe before streaming existed.

            Returns True if this lead was newly published, False if it was
            skipped (duplicate domain or over limit) -- so a lead already
            published via early streaming is a safe, cheap no-op when the
            final batch loop reaches it again.
            """
            # Defense in depth for legacy/mocked find_leads call sites:
            # never persist, count, enrich, or export a new page-one organic
            # row even if it bypassed lead_finding_agent's earlier guard.
            if _leadbot_is_page_one_organic_result(lead):
                return False

            # Persistence/streaming guard in case a legacy or mocked finder
            # bypasses the early pre-score block gate.
            if lead_matches_blocked_domains(lead, blocked_domains):
                return False

            if len(job["leads"]) >= total_limit:
                return False

            domain = str(lead.get("domain") or lead.get("url") or "").strip().lower()
            if not domain or domain in seen_domains:
                return False

            seen_domains.add(domain)

            # Real per-lead progress now exists. Deterministically silence
            # the heartbeat -- wait for it to fully exit -- before this
            # thread's own write_job() below, so no heartbeat disk write
            # can ever land after a streamed lead has been written.
            _stop_heartbeat_before_write(heartbeat_stop_event, heartbeat_thread)

            job["message"] = f"Found a lead: {domain}. Checking contact details..."
            job["updated_at"] = now_iso()
            write_job(job)

            try:
                _, cached = apply_cached_business_to_lead(lead)
                if cached:
                    job["counts"]["cached"] += 1
            except Exception:
                cached = False

            try:
                save_business_from_lead(lead, enriched=bool(cached))
            except Exception:
                pass

            # Address polish before the live card/export row is saved.
            try:
                lead = _leadbot_enrich_live_address(lead, market=market)
            except Exception as exc:
                try:
                    print(f"LEADBOT LIVE ADDRESS POLISH ERROR: {domain} {exc}", flush=True)
                except Exception:
                    pass

            public = lead_to_public(lead)

            if public.get("best_phone") or public.get("emails"):
                job["counts"]["enriched"] += 1
            else:
                job["counts"]["needs_research"] += 1

            job["leads"].append(public)
            job["seen_domains"] = list(seen_domains)
            job["counts"]["found"] = len(job["leads"])
            job["updated_at"] = now_iso()
            write_job(job)

            all_rows.append(lead)

            # Small pause lets the UI show progress instead of dumping many
            # cards from the same page/SERP-response at once. Kept after
            # adding real streaming: within one page's results, scoring is
            # still fast/synchronous, so several candidates can still clear
            # the gate within milliseconds of each other.
            time.sleep(0.15)

            return True

        for q_index, query in enumerate(queries, start=1):
            if len(job["leads"]) >= total_limit:
                break

            job["message"] = f"Searching local results for batch {q_index} of {len(queries)}..."
            job["updated_at"] = now_iso()
            write_job(job)

            # Keep the Live Scan UI feeling alive before the first real lead
            # arrives. _publish_candidate() stops this (and waits for it to
            # fully exit) the moment streaming produces its first result.
            heartbeat_stop_event = threading.Event()

            def _leadbot_search_heartbeat():
                heartbeat_messages = [
                    f"Searching local results for batch {q_index} of {len(queries)}...",
                    "Checking business websites...",
                    "Filtering potential leads...",
                    "Still working — slow websites are being skipped when needed...",
                ]

                beat = 0
                while True:
                    # wait() returns True immediately once the event is set
                    # (rather than only after a full tick), and False if the
                    # interval elapsed with no signal -- this is what makes
                    # the deterministic stop-before-write in
                    # _stop_heartbeat_before_write() fast instead of
                    # blocking the first lead's publish for a full interval.
                    if heartbeat_stop_event.wait(timeout=LEADBOT_HEARTBEAT_INTERVAL_SECONDS):
                        break

                    try:
                        hb_job = read_job(job_id)
                        if not hb_job:
                            break

                        if hb_job.get("status") in {"done", "error", "cancelled"}:
                            break

                        if hb_job.get("cancel_requested"):
                            break

                        hb_job["message"] = heartbeat_messages[min(beat, len(heartbeat_messages) - 1)]
                        hb_job["updated_at"] = now_iso()
                        write_job(hb_job)
                        beat += 1
                    except Exception:
                        break

            heartbeat_thread = threading.Thread(target=_leadbot_search_heartbeat, daemon=True)
            heartbeat_thread.start()

            def _stream_publish(lead):
                _publish_candidate(lead, heartbeat_stop_event, heartbeat_thread)

            try:
                leads, search_error = call_find_leads_with_timeout(
                    find_leads,
                    industry=industry,
                    market=market,
                    query=query,
                    own_domain=own_domain,
                    limit=per_query_limit,
                    publish_callback=_stream_publish,
                    blocked_domains=blocked_domains,
                )
            finally:
                # Covers the zero-candidates-this-query case too: guarantees
                # the heartbeat has fully exited before the reload/merge
                # below and before the next query's iteration starts a new
                # heartbeat thread.
                _stop_heartbeat_before_write(heartbeat_stop_event, heartbeat_thread)

            # Reload because the heartbeat may have updated the persisted job
            # file's message/updated_at fields. job["leads"]/counts/seen_domains
            # were only ever written by this thread (via _publish_candidate),
            # so nothing streamed during the call above is lost by this merge.
            reloaded = read_job(job_id)
            if reloaded:
                reloaded["leads"] = job["leads"]
                reloaded["counts"] = job["counts"]
                reloaded["seen_domains"] = job["seen_domains"]
                job = reloaded

            if is_cancel_requested(job_id):
                mark_job_cancelled(job_id)
                return

            if search_error and str(search_error).startswith(INVALID_MARKET_LOCATION_MARKER):
                # The market string couldn't be resolved into a valid
                # provider location (agents.dataforseo_serp_agent.
                # InvalidMarketLocationError) -- a user-input problem, not
                # a provider failure. Every remaining query would fail the
                # exact same way (the market is fixed for the whole job),
                # so stop here rather than retrying. Never represent this
                # as "search_provider_unavailable" or a zero-result scan;
                # any leads already streamed before this point are still
                # preserved (job["leads"] above already carries them).
                job["status"] = "error"
                job["error_code"] = "invalid_market_location"
                job["message"] = INVALID_MARKET_LOCATION_MESSAGE
                job["updated_at"] = now_iso()
                write_job(job)
                return

            if search_error and str(search_error).startswith(INVALID_LOCATION_VALUE_MARKER):
                # DataForSEO itself rejected the location value as invalid
                # (agents.dataforseo_serp_agent.InvalidLocationValueError,
                # e.g. a misspelled city) -- also a user-input problem, not
                # a provider failure. The location value is fixed for the
                # whole job (only the keyword phrasing varies between
                # query variants), so every remaining query would be
                # rejected the exact same way -- stop here rather than
                # burning more paid requests on a location that will never
                # resolve. Any leads already streamed are still preserved.
                job["status"] = "error"
                job["error_code"] = "invalid_location_value"
                job["message"] = INVALID_LOCATION_VALUE_MESSAGE
                job["updated_at"] = now_iso()
                write_job(job)
                return

            queries_attempted += 1

            if search_error and str(search_error).startswith(PROVIDER_UNAVAILABLE_MARKER):
                # This one query variant hit a genuine provider failure
                # (agents.lead_live_job_agent.call_find_leads_with_timeout /
                # business_competitor_finder.SearchProviderUnavailableError).
                # Do not abort the whole scan here -- a different query
                # variant may still succeed. Whether the scan as a whole
                # ends up a total failure, a partial success, or a clean
                # success is decided once after every query has been tried
                # (see "queries_with_provider_failure" below). Never expose
                # the provider name or any numeric status code here.
                queries_with_provider_failure += 1
                job["errors"].append(f"{query}: search temporarily unavailable")
                job["updated_at"] = now_iso()
                write_job(job)
                continue

            if search_error:
                job["errors"].append(f"{query}: {search_error}")
                job["updated_at"] = now_iso()
                write_job(job)
                continue

            # No search_error at all -- a genuine success, whether or not
            # this particular query variant happened to find any leads.
            queries_succeeded += 1

            leads = normalize_lead_results(leads)

            # Safety-net catch-up pass: find_leads() streams every accepted
            # candidate (organic pages and the Google Places supplement
            # alike) via publish_callback as it goes, so in the normal case
            # every lead here was already published and this loop is a
            # cheap no-op. It still exists so no candidate is lost if a
            # given call site's find_leads doesn't accept on_candidate, or a
            # callback invocation raised and was swallowed.
            # _publish_candidate()'s domain dedupe against the shared
            # `seen_domains` set is what makes this safe to re-run: exactly
            # one visible card per domain, never a duplicate.
            for lead in leads:
                if len(job["leads"]) >= total_limit:
                    break
                if is_cancel_requested(job_id):
                    mark_job_cancelled(job_id)
                    return
                _publish_candidate(lead, heartbeat_stop_event, heartbeat_thread)

        # Decide the scan's overall outcome only after every query has been
        # tried, so one query's provider failure never discards a different
        # query's valid results.
        is_total_provider_failure = (
            queries_attempted > 0
            and queries_with_provider_failure > 0
            and queries_succeeded == 0
        )
        is_partial = queries_with_provider_failure > 0 and queries_succeeded > 0

        _leadbot_log_query_outcome(
            job_id,
            queries_attempted=queries_attempted,
            queries_succeeded=queries_succeeded,
            queries_failed=queries_with_provider_failure,
            leads_published=len(job["leads"]),
            is_partial=is_partial,
        )

        if is_total_provider_failure:
            # Every attempted query hit a genuine provider failure and none
            # succeeded -- preserve the existing behavior exactly: no
            # export, no leads published, the standard provider-unavailable
            # message. (Any leads a query streamed via _publish_candidate()
            # before its own later failure are still preserved in
            # job["leads"], same as before this change -- nothing here
            # clears it.)
            job["status"] = "error"
            job["error_code"] = "search_provider_unavailable"
            job["message"] = SEARCH_PROVIDER_UNAVAILABLE_MESSAGE
            job["updated_at"] = now_iso()
            write_job(job)
            return

        job["message"] = "Scan complete. Building dashboard export..."
        job["updated_at"] = now_iso()
        write_job(job)

        all_rows = _leadbot_filter_new_export_rows(all_rows, blocked_domains)

        if all_rows:
            try:
                export = export_leads_to_csv(
                    {
                        "query": " | ".join(queries),
                        "industry": industry,
                        "market": market,
                        "count": len(all_rows),
                        "leads": all_rows,
                    },
                    industry=industry or "leadbot",
                    market=market or "market",
                    only_outreach_ready=False,
                )

                path = export.get("path") or ""
                if path:
                    job["export_file"] = Path(path).name

                    try:
                        from agents.lead_dashboard_agent import record_export_owner
                        record_export_owner(
                            job["export_file"],
                            owner_email=params.get("owner_email", ""),
                            owner_username=params.get("owner_username", ""),
                            is_partial=is_partial,
                        )
                    except Exception as exc:
                        print(f"LEADBOT LIVE EXPORT OWNER RECORD ERROR: {exc}", flush=True)
            except Exception as e:
                job["errors"].append(f"Export failed: {e}")
                job["export_file"] = ""

        if is_cancel_requested(job_id):
            mark_job_cancelled(job_id)
            return

        job["status"] = "done"
        job["partial"] = is_partial
        job["message"] = PARTIAL_RESULTS_WARNING_MESSAGE if is_partial else "Done. Open Desktop is ready."

        try:
            _leadbot_sync_live_addresses_to_export(job)
        except Exception as exc:
            try:
                print(f"LEADBOT LIVE ADDRESS CSV FINAL SYNC ERROR: {exc}", flush=True)
            except Exception:
                pass

        job["updated_at"] = now_iso()
        write_job(job)

    except Exception as e:
        job = read_job(job_id) or {"job_id": job_id, "errors": []}
        job["status"] = "error"
        job["message"] = str(e)
        job.setdefault("errors", []).append(str(e))
        job["updated_at"] = now_iso()
        write_job(job)




# === LEADBOT LIVE CARD META FIELD FIX START ===
# Live scan cards already render Site Title / Meta Description in app/main.py.
# This wrapper makes sure the public live-card payload actually includes those fields.
if "lead_to_public" in globals() and "_leadbot_original_lead_to_public_before_meta_fields" not in globals():
    _leadbot_original_lead_to_public_before_meta_fields = lead_to_public

    def _leadbot_first_value(source, keys):
        if not isinstance(source, dict):
            return ""
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            value = str(value).strip()
            if value and value.lower() not in {"none", "null", "nan", "not found"}:
                return value
        return ""

    def lead_to_public(lead):
        public = _leadbot_original_lead_to_public_before_meta_fields(lead)

        if not isinstance(public, dict):
            return public

        meta_title = _leadbot_first_value(lead, [
            "meta_title",
            "title_tag",
            "seo_title",
            "page_title",
            "site_title",
            "html_title",
            "title",
        ])

        meta_description = _leadbot_first_value(lead, [
            "meta_description",
            "meta_desc",
            "seo_description",
            "page_description",
            "description",
            "snippet",
        ])

        # Keys expected by the live-card JavaScript.
        public["meta_title"] = meta_title
        public["title_tag"] = meta_title
        public["page_title"] = meta_title
        public["site_title"] = meta_title

        public["meta_description"] = meta_description
        public["meta_desc"] = meta_description
        public["page_description"] = meta_description

        return public
# === LEADBOT LIVE CARD META FIELD FIX END ===


# === LEADBOT get_job COMPATIBILITY ALIAS START ===
def get_job(job_id):
    """
    Compatibility wrapper for older LeadBot code that expects get_job().
    The real job reader is read_job().
    """
    return read_job(job_id)
# === LEADBOT get_job COMPATIBILITY ALIAS END ===
