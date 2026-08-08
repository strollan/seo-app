"""
Regression tests for the P1 production incident where a running Lead
Finder scan was associated with the entire public app (leadmeleads.com)
becoming unavailable, and cancel appeared to hang waiting for a "good
spot" before the incident.

Root cause (see agents/lead_live_job_agent.py, scripts/run_live_lead_job.py):
scan work used to run on a plain `threading.Thread` inside the same
FastAPI/Uvicorn process that serves every other route. That thread shared
the process's single GIL and single asyncio event loop with `/`, `/login`,
`/compare`, etc. Cancellation was purely cooperative (a job-file flag
checked once per search-query batch), and a single crawl could be stuck
much longer than its nominal (3, 6) second requests timeout suggests,
because that timeout resets on every chunk a slow/trickling server sends
(agents/crawl_agent.py). A stuck or abandoned crawl thread could never be
forcibly stopped -- only asked nicely, arbitrarily late.

The fix moves scan execution into its own OS process (create_job() spawns
scripts/run_live_lead_job.py) so cancellation is a real, immediate,
OS-level operation (SIGTERM, escalating to SIGKILL) instead of a
cooperative request a stuck thread can ignore indefinitely, and so nothing
a scan does can ever block the web process from answering other users.

Most tests here talk directly to agents.lead_live_job_agent with JOB_DIR
and RUN_JOB_SCRIPT redirected to a temp dir and a small fixture worker
script, so they run fast and deterministically without any real network
call. The concurrency-sensitive tests (unrelated routes staying responsive
while a scan runs/cancels) boot this app as a real `uvicorn` subprocess --
a real separate OS process, real HTTP traffic, real wall-clock timing --
because that is the one property a mocked/in-process test cannot honestly
prove: it's exactly the kind of GIL/event-loop starvation that caused the
incident.
"""

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import requests
from fastapi.testclient import TestClient

import agents.auth_agent as auth_agent
import agents.crawl_agent as crawl_agent
import agents.lead_live_job_agent as ja
import app.main as appmain

VALID_PASSWORD = "correct-horse-battery-staple"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixture worker scripts. Each is a tiny standalone program spawned exactly
# the way agents.lead_live_job_agent.create_job() spawns the real scan
# worker (`python <script> --job-id <id>`), so tests exercise the real
# spawn/signal/reap plumbing without running a real network crawl.
# ---------------------------------------------------------------------------

def _write_fixture(tmpdir, name, source):
    path = Path(tmpdir) / name
    path.write_text(source)
    return path


def _cooperative_worker_source(job_dir, seconds=6):
    """Checks cancellation frequently between small units of work, like a
    well-behaved scan loop. Exits promptly once cancel_requested is set."""
    return f"""
import sys, time, argparse
sys.path.insert(0, {str(REPO_ROOT)!r})
import agents.lead_live_job_agent as ja
from pathlib import Path
ja.JOB_DIR = Path({str(job_dir)!r})

p = argparse.ArgumentParser(); p.add_argument("--job-id", required=True)
args = p.parse_args()

job = ja.read_job(args.job_id)
job["status"] = "running"
ja.write_job(job)

start = time.time()
while time.time() - start < {seconds}:
    if ja.is_cancel_requested(args.job_id):
        ja.mark_job_cancelled(args.job_id)
        sys.exit(0)
    time.sleep(0.05)

job = ja.read_job(args.job_id)
job["status"] = "done"
job["message"] = "done"
ja.write_job(job)
"""


def _stuck_ignores_sigterm_source(job_dir, seconds=30):
    """Simulates a scan wedged inside a single blocking call: ignores
    SIGTERM and never checks the cancellation flag. Only SIGKILL stops it --
    proving cancel_job()/reap_job() do not depend on worker cooperation."""
    return f"""
import sys, time, signal, argparse
sys.path.insert(0, {str(REPO_ROOT)!r})
import agents.lead_live_job_agent as ja
from pathlib import Path
ja.JOB_DIR = Path({str(job_dir)!r})

signal.signal(signal.SIGTERM, signal.SIG_IGN)

p = argparse.ArgumentParser(); p.add_argument("--job-id", required=True)
args = p.parse_args()

job = ja.read_job(args.job_id)
job["status"] = "running"
ja.write_job(job)

time.sleep({seconds})
"""


def _cpu_busy_worker_source(job_dir, seconds=8):
    """Pins one CPU core doing real Python bytecode work (not sleep) for
    `seconds`, checking cancellation between bursts. Used to prove that
    heavy scan work in the isolated child process cannot degrade the web
    process's ability to serve unrelated routes -- the actual mechanism
    behind the production incident."""
    return f"""
import sys, time, argparse
sys.path.insert(0, {str(REPO_ROOT)!r})
import agents.lead_live_job_agent as ja
from pathlib import Path
ja.JOB_DIR = Path({str(job_dir)!r})

p = argparse.ArgumentParser(); p.add_argument("--job-id", required=True)
args = p.parse_args()

job = ja.read_job(args.job_id)
job["status"] = "running"
ja.write_job(job)

start = time.time()
while time.time() - start < {seconds}:
    if ja.is_cancel_requested(args.job_id):
        ja.mark_job_cancelled(args.job_id)
        sys.exit(0)
    x = 0
    for i in range(2_000_000):
        x += i * i

job = ja.read_job(args.job_id)
job["status"] = "done"
ja.write_job(job)
"""


# ---------------------------------------------------------------------------
# In-process tests: talk directly to agents.lead_live_job_agent with
# JOB_DIR/RUN_JOB_SCRIPT redirected, and to the app through TestClient.
# ---------------------------------------------------------------------------

class LeadFinderCancelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.job_dir = Path(self.tmpdir.name) / "jobs"
        self.job_dir.mkdir(parents=True, exist_ok=True)
        job_dir_patch = mock.patch.object(ja, "JOB_DIR", self.job_dir)
        job_dir_patch.start()
        self.addCleanup(job_dir_patch.stop)

        self._spawned_pids = []

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()
        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")

        self.client = TestClient(appmain.app)

    def tearDown(self):
        # Best-effort: never leave a fixture worker running past its test,
        # even if an assertion failed mid-test.
        for pid in self._spawned_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass

    def login(self):
        user = auth_agent.get_user_by_username("user1")
        token = auth_agent.create_session(user)
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token

    def use_fixture_worker(self, source):
        script = _write_fixture(self.tmpdir.name, f"worker_{uuid.uuid4().hex}.py", source)
        patch = mock.patch.object(ja, "RUN_JOB_SCRIPT", script)
        patch.start()
        self.addCleanup(patch.stop)
        return script

    def make_job(self, *, owner_email="user1@example.com", owner_username="user1"):
        job_id = ja.create_job({
            "industry": "test",
            "market": "Test City, NY",
            "owner_email": owner_email,
            "owner_username": owner_username,
        })
        job = ja.read_job(job_id)
        if job and job.get("worker_pid"):
            self._spawned_pids.append(job["worker_pid"])
        return job_id

    def wait_for_status(self, job_id, statuses, timeout=8.0, reap=True):
        deadline = time.time() + timeout
        job = ja.read_job(job_id)
        while time.time() < deadline:
            if reap and str((job or {}).get("status") or "").lower() == "cancelling":
                job = ja.reap_job(job_id)
            else:
                job = ja.read_job(job_id)
            if job and str(job.get("status") or "").lower() in statuses:
                return job
            time.sleep(0.1)
        return job


class CancelEndpointRespondsPromptlyTests(LeadFinderCancelTestCase):
    """Requirement 1: cancel returns quickly and never waits for the crawl
    to actually finish stopping -- proven with a worker that ignores
    SIGTERM entirely, so any blocking-until-dead implementation would be
    caught by the timing assertion below."""

    def test_cancel_returns_promptly_even_for_a_stuck_worker(self):
        _, csrf_token = self.login()
        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))
        job_id = self.make_job()

        self.wait_for_status(job_id, {"running"}, timeout=5)

        start = time.monotonic()
        resp = self.client.post(
            f"/lead-bot/live-cancel/{job_id}",
            data={"csrf_token": csrf_token},
        )
        elapsed = time.monotonic() - start

        self.assertEqual(resp.status_code, 200)
        self.assertLess(elapsed, 1.0, "cancel endpoint must not block on the worker actually stopping")
        self.assertEqual(resp.json()["status"], "cancelling")


class CancelIdempotencyTests(LeadFinderCancelTestCase):
    """Requirements 2 & 3: repeated/rapid cancel calls are harmless."""

    def test_cancel_is_idempotent_after_job_is_terminal(self):
        self.use_fixture_worker(_cooperative_worker_source(self.job_dir, seconds=1))
        job_id = self.make_job()
        self.wait_for_status(job_id, {"done"}, timeout=6)

        first = ja.cancel_job(job_id)
        second = ja.cancel_job(job_id)

        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "done")
        self.assertEqual(first, second)

    def test_cancel_is_idempotent_while_cancelling(self):
        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))
        job_id = self.make_job()
        self.wait_for_status(job_id, {"running"}, timeout=5)

        first = ja.cancel_job(job_id)
        second = ja.cancel_job(job_id)
        third = ja.cancel_job(job_id)

        self.assertEqual(first["status"], "cancelling")
        self.assertEqual(second["status"], "cancelling")
        self.assertEqual(third["status"], "cancelling")

        events = ja.read_job(job_id).get("events") or []
        cancel_events = [e for e in events if "Cancel requested" in e.get("message", "")]
        self.assertEqual(
            len(cancel_events), 1,
            "a repeated cancel call must not append duplicate cleanup/events",
        )

    def test_repeated_rapid_cancel_requests_over_http_are_harmless(self):
        """Real concurrency, not a static assertion: fires many overlapping
        cancel POSTs from different threads at the same job_id through the
        actual ASGI app and asserts none of them error or corrupt state."""
        _, csrf_token = self.login()
        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))
        job_id = self.make_job()
        self.wait_for_status(job_id, {"running"}, timeout=5)

        def fire():
            resp = self.client.post(
                f"/lead-bot/live-cancel/{job_id}",
                data={"csrf_token": csrf_token},
            )
            return resp.status_code, resp.json().get("status")

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: fire(), range(24)))

        for status_code, job_status in results:
            self.assertEqual(status_code, 200)
            self.assertIn(job_status, {"cancelling", "cancelled"})

        final = self.wait_for_status(job_id, {"cancelled"}, timeout=8)
        self.assertEqual(final["status"], "cancelled")

        events = ja.read_job(job_id).get("events") or []
        cancel_events = [e for e in events if "Cancel requested" in e.get("message", "")]
        self.assertEqual(len(cancel_events), 1)


class WorkerCancellationBehaviorTests(LeadFinderCancelTestCase):
    """Requirements 4, 5, 9, 10: the worker observes the cancel flag,
    reaches CANCELLED, cleans up, and leaves no orphan process behind."""

    def test_cooperative_worker_observes_cancel_flag_and_reaches_cancelled(self):
        self.use_fixture_worker(_cooperative_worker_source(self.job_dir, seconds=10))
        job_id = self.make_job()
        self.wait_for_status(job_id, {"running"}, timeout=5)

        ja.cancel_job(job_id)

        final = self.wait_for_status(job_id, {"cancelled"}, timeout=5)
        self.assertEqual(final["status"], "cancelled")
        self.assertTrue(final.get("cancel_requested"))

    def test_stuck_worker_ignoring_sigterm_is_still_force_stopped(self):
        """Requirement 10 (orphan check) against the worst case: a worker
        that never checks the flag and ignores SIGTERM must still end up
        fully gone once the cancel grace period elapses."""
        grace_patch = mock.patch.object(ja, "CANCEL_GRACE_SECONDS", 1)
        grace_patch.start()
        self.addCleanup(grace_patch.stop)

        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))
        job_id = self.make_job()
        self.wait_for_status(job_id, {"running"}, timeout=5)
        pid = ja.read_job(job_id)["worker_pid"]

        ja.cancel_job(job_id)
        final = self.wait_for_status(job_id, {"cancelled"}, timeout=8)

        self.assertEqual(final["status"], "cancelled")
        self.assertFalse(
            ja._pid_alive(pid),
            "no orphan worker process may remain once the job is CANCELLED",
        )

    def test_worker_cleanup_leaves_no_live_process_on_normal_completion(self):
        self.use_fixture_worker(_cooperative_worker_source(self.job_dir, seconds=1))
        job_id = self.make_job()
        final = self.wait_for_status(job_id, {"done"}, timeout=6)
        pid = final["worker_pid"]

        deadline = time.time() + 3
        while ja._pid_alive(pid) and time.time() < deadline:
            time.sleep(0.1)

        self.assertFalse(ja._pid_alive(pid), "worker process must exit (and be reaped) once its job is done")


class BoundedConcurrencyTests(LeadFinderCancelTestCase):
    """SAFETY: a burst of scans cannot spawn unbounded worker processes."""

    def test_scans_beyond_the_concurrency_cap_are_rejected_without_spawning(self):
        cap_patch = mock.patch.object(ja, "MAX_CONCURRENT_LIVE_SCANS", 2)
        cap_patch.start()
        self.addCleanup(cap_patch.stop)

        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))

        job_ids = [self.make_job() for _ in range(4)]
        jobs = [ja.read_job(jid) for jid in job_ids]

        running = [j for j in jobs if j["status"] in {"queued", "running"}]
        rejected = [j for j in jobs if j["status"] == "error"]

        self.assertEqual(len(running), 2)
        self.assertEqual(len(rejected), 2)
        for job in rejected:
            self.assertIsNone(job.get("worker_pid"))

    def test_production_default_allows_exactly_one_scan_at_a_time(self):
        """Production is a 1 vCPU / 1 GB droplet: only one scan subprocess
        may run at once, a second attempt fails immediately with a clear
        message (not a hang or an error dump), cancelling the first frees
        the slot, and a new scan can then start."""
        self.assertEqual(ja.MAX_CONCURRENT_LIVE_SCANS, 1, "production default must stay at 1")

        self.use_fixture_worker(_stuck_ignores_sigterm_source(self.job_dir, seconds=30))

        # 1. first scan starts.
        first_id = self.make_job()
        first = self.wait_for_status(first_id, {"running"}, timeout=5)
        self.assertEqual(first["status"], "running")
        first_pid = first["worker_pid"]

        # 2. second concurrent scan is safely refused, with a clear
        # user-facing message -- no hang, no worker process spawned.
        second_id = self.make_job()
        second = ja.read_job(second_id)
        self.assertEqual(second["status"], "error")
        self.assertIsNone(second.get("worker_pid"))
        self.assertIn("try again", (second.get("message") or "").lower())

        # 3. cancelling the first scan frees the slot.
        ja.cancel_job(first_id)
        cancelled = self.wait_for_status(first_id, {"cancelled"}, timeout=8)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(ja._pid_alive(first_pid))

        # 4. a new scan can start afterward.
        third_id = self.make_job()
        third = self.wait_for_status(third_id, {"running"}, timeout=5)
        self.assertEqual(third["status"], "running")


class CrawlTotalTimeoutTests(unittest.TestCase):
    """Requirement 6 / trace question 8: a slow-trickling response cannot
    hold a crawl open indefinitely, even though requests' own (connect,
    read) timeout resets on every chunk that successfully arrives."""

    def test_slow_trickle_response_is_cut_off_by_a_hard_total_deadline(self):
        class FakeResponse:
            is_redirect = False
            headers = {}
            url = "http://example.com/"
            status_code = 200
            encoding = "utf-8"

            def iter_content(self, chunk_size=16 * 1024):
                # Each individual yield easily beats a (3, 6)s per-call
                # read timeout, but the generator never stops on its own --
                # simulating a server that paces bytes just fast enough to
                # keep resetting that timeout indefinitely.
                while True:
                    time.sleep(0.05)
                    yield b"a"

            def close(self):
                pass

        result = {}

        def run():
            start = time.monotonic()
            resp = crawl_agent.crawl_get("http://example.com/")
            result["elapsed"] = time.monotonic() - start
            result["status_code"] = resp.status_code

        with mock.patch.object(crawl_agent, "validate_public_url", return_value="example.com"), \
             mock.patch.object(crawl_agent, "CRAWL_TOTAL_TIMEOUT_SECONDS", 0.5), \
             mock.patch.object(crawl_agent.requests, "get", return_value=FakeResponse()):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(timeout=5.0)

        self.assertFalse(
            t.is_alive(),
            "crawl_get() did not return within the hard total-timeout deadline "
            "(this is the mechanism that let a single slow-trickle crawl block "
            "a thread far longer than its (3, 6)s timeout suggested)",
        )
        self.assertLess(result.get("elapsed", 999), 2.0)
        self.assertEqual(result.get("status_code"), 200)  # truncated body, not an error


# ---------------------------------------------------------------------------
# Real-server tests: boot this app as a genuine `uvicorn` subprocess (same
# invocation shape as production's run_server.sh) so "unrelated routes stay
# responsive" is proven with real OS-level concurrency, not a mock.
# ---------------------------------------------------------------------------

class LiveServerResponsivenessTests(unittest.TestCase):
    """Regression test for the architectural failure mode identified in the
    incident: heavy Lead Finder scan work running inside the same process
    that serves the rest of the site can starve unrelated routes. Boots a
    real uvicorn server (single worker, exactly like production's
    run_server.sh) with LEADBOT_LIVE_RUN_JOB_SCRIPT pointed at a CPU-pinning
    fixture worker, and drives real concurrent HTTP traffic against it
    while that worker runs and is cancelled, in its own OS process.

    Uses this repo's real data/app_auth.db (same constraint, and same
    approach, as scripts/test_leadbot_csrf_routes.py's browser regression
    tests) with a uniquely-named throwaway account, removed in
    tearDownClass. The scan itself never touches real leads/exports data --
    the busy fixture worker only writes its own job status file, cleaned up
    per-test.
    """

    PORT = 8793

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._busy_script = _write_fixture(
            cls._tmpdir.name,
            "cpu_busy_worker.py",
            _cpu_busy_worker_source(REPO_ROOT / "data" / "leadbot_live_jobs", seconds=8),
        )

        cls._auth_db_patch = mock.patch.object(auth_agent, "AUTH_DB", REPO_ROOT / "data" / "app_auth.db")
        cls._auth_db_patch.start()
        auth_agent.init_auth_db()
        cls._username = f"cancelhang_{uuid.uuid4().hex[:10]}"
        auth_agent.create_user(
            cls._username, VALID_PASSWORD, role="standard", email=f"{cls._username}@example.com"
        )

        cls._proc = subprocess.Popen(
            [
                sys.executable,
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "USE_LIVE_SERP": "false",
                "DATAFORSEO_ENABLED": "0",
                "LEADBOT_DATAFORSEO_ENABLED": "0",
                "LEADBOT_LIVE_RUN_JOB_SCRIPT": str(cls._busy_script),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        cls.base_url = f"http://127.0.0.1:{cls.PORT}"
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                if requests.get(f"{cls.base_url}/login", timeout=1).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        if not up:
            cls._proc.terminate()
            cls._auth_db_patch.stop()
            raise unittest.SkipTest("local dev server did not start in time")

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "_proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        cls._tmpdir.cleanup()
        cls._auth_db_patch.stop()

    def _login_session(self):
        session = requests.Session()
        login_resp = session.post(
            f"{self.base_url}/login",
            data={"username": self._username, "password": VALID_PASSWORD},
            allow_redirects=False,
        )
        self.assertEqual(login_resp.status_code, 303)

        page = session.get(f"{self.base_url}/lead-bot").text
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match, "could not find a csrf_token on /lead-bot")
        return session, match.group(1)

    def _poll_unrelated_route_responsiveness(self, path, duration_seconds, max_single_request_seconds):
        """Hits `path` in a tight loop for `duration_seconds` from this test
        thread while other work runs concurrently in the server process.
        Fails if any single request is slower than `max_single_request_seconds`
        -- the exact symptom (Gateway Timeout on `/`, `/login`, etc.) this
        architecture change exists to prevent."""
        deadline = time.time() + duration_seconds
        slowest = 0.0
        count = 0
        while time.time() < deadline:
            start = time.monotonic()
            resp = requests.get(f"{self.base_url}{path}", timeout=max_single_request_seconds + 5)
            elapsed = time.monotonic() - start
            slowest = max(slowest, elapsed)
            count += 1
            self.assertEqual(resp.status_code, 200)
            self.assertLess(
                elapsed, max_single_request_seconds,
                f"{path} took {elapsed:.2f}s while a scan was active/cancelling -- "
                "an unrelated route must never be blocked by scan/cancel work",
            )
        self.assertGreater(count, 0)
        return slowest

    def test_heavy_scan_work_cannot_block_unrelated_routes(self):
        """Requirements 7 & 8, and the core architectural regression test:
        `/login` must stay fast both while a CPU-pinning "scan" runs in its
        isolated worker process, and while it is being cancelled."""
        session, csrf_token = self._login_session()

        start_resp = session.post(
            f"{self.base_url}/lead-bot/live-start",
            data={
                "market": "Test City, NY",
                "keyword": "test",
                "limit": 5,
                "csrf_token": csrf_token,
            },
            allow_redirects=False,
        )
        self.assertEqual(start_resp.status_code, 303)
        job_id = start_resp.headers["location"].rsplit("/", 1)[-1]
        job_file = REPO_ROOT / "data" / "leadbot_live_jobs" / f"{job_id}.json"
        self.addCleanup(lambda: job_file.unlink(missing_ok=True))

        deadline = time.time() + 5
        while time.time() < deadline and not job_file.exists():
            time.sleep(0.1)
        self.assertTrue(job_file.exists(), "live-start did not create a job file")

        # The busy worker pins a core for ~8s. While it runs, hammer an
        # entirely unrelated route from this test thread and require every
        # single response back well inside a couple seconds -- the pre-fix
        # architecture (scan on a thread in the web process) would degrade
        # badly here.
        slowest = self._poll_unrelated_route_responsiveness(
            "/login", duration_seconds=3.0, max_single_request_seconds=2.0
        )

        # Now cancel it and prove the same holds through the
        # cancel/cancelling/reap window (requirement 8).
        cancel_resp = session.post(
            f"{self.base_url}/lead-bot/live-cancel/{job_id}",
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(cancel_resp.status_code, 200)

        slowest_during_cancel = self._poll_unrelated_route_responsiveness(
            "/login", duration_seconds=3.0, max_single_request_seconds=2.0
        )

        deadline = time.time() + 8
        final_status = None
        while time.time() < deadline:
            status_resp = requests.get(f"{self.base_url}/lead-bot/live-status/{job_id}", cookies=session.cookies)
            final_status = status_resp.json().get("status")
            if final_status == "cancelled":
                break
            time.sleep(0.3)
        self.assertEqual(final_status, "cancelled")

        print(
            f"[responsiveness] slowest /login during scan={slowest:.3f}s, "
            f"during cancel={slowest_during_cancel:.3f}s"
        )

    def test_second_scan_refused_while_unrelated_route_stays_responsive(self):
        """Requirement 5 of the 1-vCPU concurrency follow-up: with
        MAX_CONCURRENT_LIVE_SCANS=1 (the production default), a second
        live-start while the first is still running is refused with a
        clear message -- and `/login` never slows down while that happens,
        proving the refusal itself is cheap (a job-dir scan + a fast
        write), not something that could itself degrade the server."""
        session, csrf_token = self._login_session()

        first_resp = session.post(
            f"{self.base_url}/lead-bot/live-start",
            data={"market": "Test City, NY", "keyword": "test", "limit": 5, "csrf_token": csrf_token},
            allow_redirects=False,
        )
        self.assertEqual(first_resp.status_code, 303)
        first_job_id = first_resp.headers["location"].rsplit("/", 1)[-1]
        first_job_file = REPO_ROOT / "data" / "leadbot_live_jobs" / f"{first_job_id}.json"
        self.addCleanup(lambda: first_job_file.unlink(missing_ok=True))

        deadline = time.time() + 5
        while time.time() < deadline and not first_job_file.exists():
            time.sleep(0.1)
        self.assertTrue(first_job_file.exists(), "first live-start did not create a job file")

        second_resp = session.post(
            f"{self.base_url}/lead-bot/live-start",
            data={"market": "Test City, NY", "keyword": "test", "limit": 5, "csrf_token": csrf_token},
            allow_redirects=False,
        )
        self.assertEqual(second_resp.status_code, 303)
        second_job_id = second_resp.headers["location"].rsplit("/", 1)[-1]
        second_job_file = REPO_ROOT / "data" / "leadbot_live_jobs" / f"{second_job_id}.json"
        self.addCleanup(lambda: second_job_file.unlink(missing_ok=True))

        deadline = time.time() + 5
        second_status = None
        second_message = ""
        while time.time() < deadline:
            status_json = requests.get(
                f"{self.base_url}/lead-bot/live-status/{second_job_id}", cookies=session.cookies
            ).json()
            second_status = status_json.get("status")
            second_message = status_json.get("message") or ""
            if second_status:
                break
            time.sleep(0.1)

        self.assertEqual(second_status, "error", "a second concurrent scan must be refused, not queued/hung")
        self.assertIn("try again", second_message.lower())

        self._poll_unrelated_route_responsiveness(
            "/login", duration_seconds=2.0, max_single_request_seconds=2.0
        )

        cancel_resp = session.post(
            f"{self.base_url}/lead-bot/live-cancel/{first_job_id}",
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(cancel_resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
