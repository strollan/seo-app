"""
Regression tests for the bounded retry around DataForSEO organic-search
task failures (agents.dataforseo_serp_agent.search_google_organic).

Background: production diagnostics captured a genuine failure with task
status_code 40101 on an otherwise perfectly valid, already-correctly-
resolved market ("Long Island NY" -> "New York,New York,United States").
Per DataForSEO's official task-status-code table:
  - 40101 = Internal SE server error; requested search engine could not
    process the request -- transient/provider-side, safe to retry.
  - 40103 = Task execution failed; DataForSEO's own guidance is to retry.
  - 40102 = No Search Results -- NOT an error, a legitimate empty result,
    and must never enter the retry path or be surfaced as a failure.

This adds a small bounded retry (initial attempt + up to 2 retries, 3
total attempts, ~1s then ~2s backoff) for exactly 40101/40103. Every
other failure -- authentication, malformed request fields, rate limits,
5xx at the HTTP/transport level, timeouts, connection errors -- still
raises on the first attempt, unchanged. A location that fails to resolve
locally (InvalidMarketLocationError) never reaches this retry loop at
all, since _location_name() is called before the loop.

Uses mocked provider calls and mocked time.sleep only -- no real network
call and no real waiting is ever made by this file.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.dataforseo_serp_agent as dfs


def _fake_response(task_status_code, items=None, task_message="x"):
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
                        "result": [{"items": items or []}],
                    }
                ],
            }

    return _FakeResponse()


def _isolate_circuit_breaker_state(testcase):
    """This file's own tests deliberately exhaust 40101/40103 many times
    in a row using real wall-clock time (agents.dataforseo_serp_agent's
    circuit breaker isn't mocked here -- that's covered separately in
    scripts/test_dataforseo_circuit_breaker.py). Run back-to-back, enough
    of those real exhaustions land inside a real 60-second window to trip
    the breaker for a real 120 seconds, which would then leak into and
    break whatever DataForSEO-related test file happens to run next in
    the same process. Save/reset/restore the breaker's module-level state
    around each test so this file can never affect any other."""
    orig = {
        "exhaustion_timestamps": list(dfs._circuit_breaker_state["exhaustion_timestamps"]),
        "opened_until": dfs._circuit_breaker_state["opened_until"],
        "probe_in_progress": dfs._circuit_breaker_state["probe_in_progress"],
    }
    dfs._circuit_breaker_state["exhaustion_timestamps"] = []
    dfs._circuit_breaker_state["opened_until"] = None
    dfs._circuit_breaker_state["probe_in_progress"] = False

    def _restore():
        dfs._circuit_breaker_state["exhaustion_timestamps"] = orig["exhaustion_timestamps"]
        dfs._circuit_breaker_state["opened_until"] = orig["opened_until"]
        dfs._circuit_breaker_state["probe_in_progress"] = orig["probe_in_progress"]

    testcase.addCleanup(_restore)


class DataForSeoRetryTests(unittest.TestCase):
    def setUp(self):
        _isolate_circuit_breaker_state(self)

        self._env_backup = {
            key: os.environ.get(key)
            for key in ("LEADBOT_DATAFORSEO_ENABLED", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
        }
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        os.environ["DATAFORSEO_LOGIN"] = "test-placeholder-login"
        os.environ["DATAFORSEO_PASSWORD"] = "test-placeholder-password"
        self.addCleanup(self._restore_env)

        sleep_patch = mock.patch.object(dfs.time, "sleep")
        self.mock_sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_40101_then_20000_succeeds_on_retry(self):
        responses = [_fake_response(40101), _fake_response(20000)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            rows = dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(rows, [])
        self.assertEqual(mock_post.call_count, 2)
        self.mock_sleep.assert_called_once_with(1)

    def test_40103_then_20000_succeeds_on_retry(self):
        responses = [_fake_response(40103), _fake_response(20000)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            rows = dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(rows, [])
        self.assertEqual(mock_post.call_count, 2)
        self.mock_sleep.assert_called_once_with(1)

    def test_repeated_40101_exhausts_after_three_total_attempts(self):
        responses = [_fake_response(40101), _fake_response(40101), _fake_response(40101)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(self.mock_sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_repeated_40101_exhaustion_surfaces_as_provider_unavailable(self):
        """End-to-end through business_competitor_finder's wrapping, the
        exhausted retry must still become SearchProviderUnavailableError,
        same as any other genuine DataForSEO failure."""
        import business_competitor_finder as bcf

        responses = [_fake_response(40101)] * 3
        with mock.patch.object(dfs.requests, "post", side_effect=responses):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._leadbot_non_serper_search("plumber", location="Albany, NY", page=1, num=10, raise_on_failure=True)

    def test_40102_no_search_results_returns_empty_without_retry(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40102)) as mock_post:
            rows = dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(rows, [])
        mock_post.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_20000_succeeds_without_retry(self):
        items = [{"type": "organic", "url": "https://a-plumber-test.com", "rank_group": 1, "rank_absolute": 1, "title": "A", "description": "b"}]
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000, items=items)) as mock_post:
            rows = dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(len(rows), 1)
        mock_post.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_non_retryable_task_error_does_not_retry(self):
        """40501 Invalid Field is not in the retryable set -- must raise
        on the very first attempt, exactly as before this change."""
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501, task_message="Invalid Field: 'location_name'")) as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        mock_post.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_authentication_http_error_does_not_retry(self):
        import requests as real_requests

        class _AuthFailResponse:
            def raise_for_status(self):
                err = real_requests.exceptions.HTTPError("401 Client Error")
                err.response = mock.Mock(status_code=401)
                raise err

        with mock.patch.object(dfs.requests, "post", return_value=_AuthFailResponse()) as mock_post:
            with self.assertRaises(real_requests.exceptions.HTTPError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        mock_post.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_invalid_market_location_never_enters_retry_loop(self):
        with mock.patch.object(dfs, "requests") as mock_requests:
            with self.assertRaises(dfs.InvalidMarketLocationError):
                dfs.search_google_organic("plumber", "Southampton", depth=10)

        mock_requests.post.assert_not_called()
        self.mock_sleep.assert_not_called()


class RetryDiagnosticLoggingTests(unittest.TestCase):
    """Confirms the sanitized attempt/outcome logging required alongside
    the retry behavior."""

    def setUp(self):
        import tempfile
        import shutil

        _isolate_circuit_breaker_state(self)

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.log_path = Path(self.tmpdir) / "diag.log"
        self._orig_log_path = dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG
        dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG = self.log_path
        self.addCleanup(lambda: setattr(dfs, "_LEADBOT_PROVIDER_DIAGNOSTICS_LOG", self._orig_log_path))

        self._env_backup = {
            key: os.environ.get(key)
            for key in ("LEADBOT_DATAFORSEO_ENABLED", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
        }
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        os.environ["DATAFORSEO_LOGIN"] = "test-placeholder-login"
        os.environ["DATAFORSEO_PASSWORD"] = "test-placeholder-password"
        self.addCleanup(self._restore_env)

        sleep_patch = mock.patch.object(dfs.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _read_records(self):
        import json

        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]

    def test_recovered_after_retry_is_logged_with_attempt_and_outcome(self):
        responses = [_fake_response(40101), _fake_response(20000)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["attempt"], 1)
        self.assertEqual(records[0]["outcome"], "retrying")
        self.assertEqual(records[0]["status_code"], 40101)
        self.assertEqual(records[1]["attempt"], 2)
        self.assertEqual(records[1]["outcome"], "recovered")
        self.assertEqual(records[1]["status_code"], 20000)

    def test_exhausted_after_three_attempts_is_logged_for_each_attempt(self):
        responses = [_fake_response(40103)] * 3
        with mock.patch.object(dfs.requests, "post", side_effect=responses):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_records()
        self.assertEqual(len(records), 3)
        self.assertEqual([r["attempt"] for r in records], [1, 2, 3])
        self.assertEqual([r["outcome"] for r in records], ["retrying", "retrying", "exhausted"])
        self.assertTrue(all(r["status_code"] == 40103 for r in records))

    def test_no_diagnostic_written_on_no_search_results(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40102)):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(self._read_records(), [])

    def test_no_diagnostic_written_on_clean_first_attempt_success(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(self._read_records(), [])

    def test_diagnostic_records_never_contain_secrets(self):
        responses = [_fake_response(40101)] * 3
        with mock.patch.object(dfs.requests, "post", side_effect=responses):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        raw_text = self.log_path.read_text()
        for forbidden in ["test-placeholder-login", "test-placeholder-password", "Authorization", "Basic "]:
            self.assertNotIn(forbidden, raw_text)


if __name__ == "__main__":
    unittest.main()
