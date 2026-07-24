"""
Regression tests for the DataForSEO circuit breaker and the diagnostic-log
size cap.

Background: agents.dataforseo_serp_agent.search_google_organic() already
retries the two DataForSEO task codes confirmed transient/provider-side
(40101 "Internal SE server error", 40103 "Task execution failed") up to 3
total attempts with ~1s/~2s backoff (scripts/test_dataforseo_retry.py).
During an actual DataForSEO degradation, every query variant across a
whole scan can independently exhaust its own 3-attempt budget -- up to
24 requests for a guest scan, 96-144 for a logged-in scan (4 SERP pages
per query x up to 3 attempts each). This circuit breaker stops that
multiplication once the provider has clearly shown it is degraded: after
3 exhausted retryable operations within 60 seconds, it opens for 120
seconds, failing every subsequent operation fast with zero further paid
requests, then allows exactly one probe operation through.

Process model: production runs a single uvicorn process (no --workers
flag; confirmed against the deployed systemd unit and process list), so
this is a thread-safe in-process breaker (module-level state guarded by
one threading.Lock in agents/dataforseo_serp_agent.py) -- no file/SQLite/
Redis-backed shared state, since there's only one process to share it
across.

Uses mocked provider calls, mocked agents.dataforseo_serp_agent.time.sleep,
and a mocked agents.dataforseo_serp_agent._circuit_breaker_now() (in place
of real time.monotonic()) throughout -- no real network call and no real
waiting is ever made by this file.
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.dataforseo_serp_agent as dfs
import agents.lead_live_job_agent as job_agent
import business_competitor_finder as bcf


def _fake_response(task_status_code, task_message="x"):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status_code": 20000,
                "tasks": [
                    {
                        "status_code": task_status_code,
                        "status_message": task_message,
                        "result": [{"items": []}],
                    }
                ],
            }

    return _FakeResponse()


class _CircuitBreakerTestCase(unittest.TestCase):
    """Resets all circuit-breaker module state before/after every test so
    tests can never leak state into each other, and provides a fully
    controllable fake clock (no real sleeping, no real time.monotonic())."""

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("LEADBOT_DATAFORSEO_ENABLED", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
        }
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        os.environ["DATAFORSEO_LOGIN"] = "test-placeholder-login"
        os.environ["DATAFORSEO_PASSWORD"] = "test-placeholder-password"
        self.addCleanup(self._restore_env)

        self._orig_state = {
            "exhaustion_timestamps": list(dfs._circuit_breaker_state["exhaustion_timestamps"]),
            "opened_until": dfs._circuit_breaker_state["opened_until"],
            "probe_in_progress": dfs._circuit_breaker_state["probe_in_progress"],
        }
        dfs._circuit_breaker_state["exhaustion_timestamps"] = []
        dfs._circuit_breaker_state["opened_until"] = None
        dfs._circuit_breaker_state["probe_in_progress"] = False
        self.addCleanup(self._restore_circuit_state)

        self.fake_time = {"t": 1_000_000.0}
        now_patch = mock.patch.object(dfs, "_circuit_breaker_now", side_effect=lambda: self.fake_time["t"])
        now_patch.start()
        self.addCleanup(now_patch.stop)

        sleep_patch = mock.patch.object(dfs.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, ignore_errors=True)
        self.log_path = Path(self.tmpdir) / "diag.log"
        self._orig_log_path = dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG
        dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG = self.log_path
        self.addCleanup(lambda: setattr(dfs, "_LEADBOT_PROVIDER_DIAGNOSTICS_LOG", self._orig_log_path))

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _restore_circuit_state(self):
        dfs._circuit_breaker_state["exhaustion_timestamps"] = self._orig_state["exhaustion_timestamps"]
        dfs._circuit_breaker_state["opened_until"] = self._orig_state["opened_until"]
        dfs._circuit_breaker_state["probe_in_progress"] = self._orig_state["probe_in_progress"]

    def _advance(self, seconds):
        self.fake_time["t"] += seconds

    def _exhaust_once(self, code=40101):
        """Run one operation that exhausts all 3 attempts on a retryable
        code. Returns nothing; raises RuntimeError, caught by caller."""
        responses = [_fake_response(code), _fake_response(code), _fake_response(code)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

    def _read_log_records(self):
        import json

        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]


class IsolatedRetryUnaffectedByBreakerTests(_CircuitBreakerTestCase):
    def test_isolated_40101_still_uses_max_three_attempts(self):
        responses = [_fake_response(40101), _fake_response(40101), _fake_response(40101)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        self.assertEqual(mock_post.call_count, 3)

    def test_isolated_40103_still_uses_max_three_attempts(self):
        responses = [_fake_response(40103), _fake_response(40103), _fake_response(40103)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        self.assertEqual(mock_post.call_count, 3)

    def test_single_exhaustion_does_not_open_circuit(self):
        with self.assertRaises(RuntimeError):
            self._exhaust_once()
        # A second, unrelated operation must still be allowed to make a
        # real request -- the circuit only opens at the threshold (3).
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()


class CircuitOpensAtThresholdTests(_CircuitBreakerTestCase):
    def test_three_exhaustions_within_60s_opens_circuit(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        with mock.patch.object(dfs.requests, "post") as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_not_called()

    def test_open_circuit_makes_zero_paid_requests(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        with mock.patch.object(dfs.requests, "post") as mock_post:
            for _ in range(5):
                with self.assertRaises(RuntimeError):
                    dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_not_called()

    def test_open_circuit_fails_fast_as_provider_unavailable_end_to_end(self):
        """Confirms the existing SearchProviderUnavailableError /
        job error_code="search_provider_unavailable" path still applies,
        reusing the existing mechanism rather than a new one."""
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        with mock.patch.object(dfs.requests, "post") as mock_post:
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._leadbot_non_serper_search("plumber", location="Albany, NY", page=1, num=10, raise_on_failure=True)
        mock_post.assert_not_called()

    def test_exhaustions_outside_60s_window_do_not_accumulate(self):
        with self.assertRaises(RuntimeError):
            self._exhaust_once()
        self._advance(61)  # outside the 60s window -- resets accumulation
        with self.assertRaises(RuntimeError):
            self._exhaust_once()
        self._advance(1)
        with self.assertRaises(RuntimeError):
            self._exhaust_once()

        # Only 2 exhaustions within the current 60s window -- still closed.
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()


class CooldownAndProbeTests(_CircuitBreakerTestCase):
    def _open_circuit(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

    def test_cooldown_allows_exactly_one_probe(self):
        self._open_circuit()
        self._advance(121)  # past the 120s cooldown

        gate1 = dfs._circuit_breaker_gate(self.fake_time["t"])
        gate2 = dfs._circuit_breaker_gate(self.fake_time["t"])
        self.assertEqual(gate1, "probe")
        self.assertEqual(gate2, "open")

    def test_concurrent_operations_during_probe_fail_fast(self):
        self._open_circuit()
        self._advance(121)

        # Claim the probe slot as a different "in-flight" caller would.
        first_gate = dfs._circuit_breaker_gate(self.fake_time["t"])
        self.assertEqual(first_gate, "probe")

        # A second, concurrent caller must fail fast while the probe is
        # still unresolved.
        with mock.patch.object(dfs.requests, "post") as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_not_called()

    def test_successful_probe_closes_and_resets_circuit(self):
        self._open_circuit()
        self._advance(121)

        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

        self.assertIsNone(dfs._circuit_breaker_state["opened_until"])
        self.assertFalse(dfs._circuit_breaker_state["probe_in_progress"])
        self.assertEqual(dfs._circuit_breaker_state["exhaustion_timestamps"], [])

        # Circuit fully closed -- an immediate further call proceeds normally.
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

    def test_failed_probe_reopens_circuit(self):
        self._open_circuit()
        self._advance(121)

        with self.assertRaises(RuntimeError):
            self._exhaust_once()  # this call is the probe, and it fails

        # Immediately after, the circuit must be open again -- fail fast.
        with mock.patch.object(dfs.requests, "post") as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_not_called()

        # And only after another full 120s cooldown does a new probe open up.
        self._advance(121)
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

    def test_non_retryable_error_during_probe_releases_probe_without_reopening(self):
        """A permanent error landing on the probe attempt must not count
        toward the breaker or reopen it -- but the probe slot still has
        to be released so a future call can try again."""
        self._open_circuit()
        self._advance(121)

        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501, "Invalid Field: 'location_name'")):
            with self.assertRaises(dfs.InvalidLocationValueError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertFalse(dfs._circuit_breaker_state["probe_in_progress"])
        # Not reopened for a fresh 120s -- opened_until should not have
        # been pushed forward by this non-retryable error.
        self.assertLess(dfs._circuit_breaker_state["opened_until"], self.fake_time["t"] + 1)


class NonContributingFailureTests(_CircuitBreakerTestCase):
    def test_permanent_errors_do_not_contribute_to_threshold(self):
        for _ in range(5):
            with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501, "Invalid Field: 'location_name'")):
                with self.assertRaises(dfs.InvalidLocationValueError):
                    dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        # Circuit must still be closed -- a normal request proceeds.
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

    def test_40102_genuine_zero_results_does_not_contribute_to_threshold(self):
        for _ in range(5):
            with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40102)):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

    def test_healthy_response_clears_stale_failure_history(self):
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        # A healthy response in between clears the accumulated history.
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(dfs._circuit_breaker_state["exhaustion_timestamps"], [])

        # A further single exhaustion right after must not open the
        # circuit -- the prior 2 were cleared by the healthy response.
        with self.assertRaises(RuntimeError):
            self._exhaust_once()

        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()


class GuestAttemptAndJobIntegrityTests(_CircuitBreakerTestCase):
    """Confirms the circuit breaker cannot affect guest-attempt
    accounting or create duplicate jobs/exports, and that a circuit-open
    scan still reaches the existing search_provider_unavailable job
    state (not a new one)."""

    def setUp(self):
        super().setUp()
        self._orig_job_dir = job_agent.JOB_DIR
        new_job_dir = Path(self.tmpdir) / "jobs"
        new_job_dir.mkdir(parents=True, exist_ok=True)
        job_agent.JOB_DIR = new_job_dir
        self.addCleanup(lambda: setattr(job_agent, "JOB_DIR", self._orig_job_dir))

        self.export_calls = []

        def fake_export(payload, **kwargs):
            self.export_calls.append(payload)
            return {"path": "/tmp/should-not-matter.csv"}

        patches = [
            mock.patch("agents.lead_business_cache_agent.apply_cached_business_to_lead", lambda lead: (lead, False)),
            mock.patch("agents.lead_business_cache_agent.save_business_from_lead", lambda lead, enriched=False: None),
            mock.patch("agents.lead_export_agent.export_leads_to_csv", fake_export),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _make_job(self, max_queries=1):
        job_id = "cbreaker-" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "queued",
            "message": "",
            "leads": [],
            "errors": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "seen_domains": [],
            "params": {
                "industry": "plumber",
                "market": "Albany, NY",
                "keyword": "plumber",
                "own_domain": "",
                "limit": 10,
                "per_batch": 8,
                "per_query_limit": 8,
                "max_queries": max_queries,
                "guest_id": "guest-test",
            },
            "cancel_requested": False,
            "updated_at": job_agent.now_iso(),
            "export_file": "",
        }
        job_agent.write_job(job)
        return job_id

    def test_open_circuit_scan_ends_as_existing_provider_unavailable_state_no_duplicate_job_or_export(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        job_id = self._make_job(max_queries=1)
        with mock.patch.object(dfs.requests, "post") as mock_post:
            job_agent.run_job(job_id)
        mock_post.assert_not_called()

        job_files = list((Path(self.tmpdir) / "jobs").glob("*.json"))
        self.assertEqual(len(job_files), 1, "exactly one job file must exist -- no duplicate job")

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "search_provider_unavailable")
        self.assertEqual(final_job["export_file"], "")
        self.assertEqual(self.export_calls, [], "no duplicate (or any) export for a total provider failure")
        self.assertNotIn("partial", final_job, "bac7096 (parked partial-results behavior) must not be present")


class DiagnosticsSafetyTests(_CircuitBreakerTestCase):
    def test_circuit_breaker_open_is_recorded_with_sanitized_category(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        with mock.patch.object(dfs.requests, "post"):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertTrue(any(r["failure_category"] == "circuit_breaker_open" and r["outcome"] == "circuit_open" for r in records))

    def test_no_secrets_credentials_or_headers_in_diagnostics(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self._exhaust_once()
            self._advance(1)

        with mock.patch.object(dfs.requests, "post"):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        raw_text = self.log_path.read_text()
        for forbidden in [
            "test-placeholder-login", "test-placeholder-password", "Authorization", "Basic ",
            "cookie", "Cookie", "@", "127.0.0.1",
        ]:
            self.assertNotIn(forbidden, raw_text)


class DiagnosticLogRotationTests(_CircuitBreakerTestCase):
    def test_log_stays_size_bounded(self):
        self.log_path.write_text("x" * (dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES + 500))

        dfs._leadbot_log_provider_diagnostic(failure_category="timeout", status_code=None, location_name="Albany,New York,United States")

        self.assertLess(self.log_path.stat().st_size, dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES)

        backup = self.log_path.parent / (self.log_path.name + ".1")
        self.assertTrue(backup.exists())
        self.assertGreaterEqual(backup.stat().st_size, dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES)

    def test_rotation_replaces_older_backup(self):
        backup = self.log_path.parent / (self.log_path.name + ".1")
        backup.write_text("old backup contents")
        self.log_path.write_text("x" * (dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES + 500))

        dfs._leadbot_log_provider_diagnostic(failure_category="timeout", status_code=None, location_name="")

        self.assertNotIn("old backup contents", backup.read_text())

    def test_rotation_failure_cannot_break_provider_handling(self):
        """If rotation itself raises (e.g. a filesystem error), the real
        search must still complete/fail exactly as it would otherwise --
        diagnostics (and their rotation) are always best-effort."""
        with mock.patch.object(dfs.Path, "replace", side_effect=OSError("simulated disk error")):
            with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
                self.log_path.write_text("x" * (dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG_MAX_BYTES + 500))
                rows = dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        self.assertEqual(rows, [])
        mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
