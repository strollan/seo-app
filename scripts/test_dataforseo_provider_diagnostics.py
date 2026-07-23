"""
Regression tests for sanitized DataForSEO provider-failure diagnostics
(agents.dataforseo_serp_agent._leadbot_log_provider_diagnostic /
_leadbot_classify_provider_failure).

Background: journalctl is unavailable in production and this app has no
other log file, so repeated "search_provider_unavailable" incidents left
no trace of the actual underlying cause (timeout, rate limit, 5xx,
malformed field, etc.) beyond a generic wrapped message. This closes that
gap by appending one sanitized JSON line per genuine provider failure --
provider name, a best-effort failure category, the numeric DataForSEO
status code, and the location being searched. It never logs raw response
bodies, request headers, credentials, or lead data, and it never changes
what exception propagates or what the user sees -- the caught exception
is always re-raised unchanged.

Confirmed via this session's incident review: identical, already-correct
market strings (e.g. "Long Island NY", an exact alias-dictionary match
unaffected by any location-formatting fix) flipped between success and
failure at different times, which location-formatting logic cannot
explain. This diagnostic logging is meant to capture the real cause the
next time that happens, not to fix the location parser (already covered
by scripts/test_dataforseo_location_formatting.py,
test_dataforseo_city_casing.py, and
test_dataforseo_nassau_suffolk_locations.py).

Uses mocked provider calls only -- no live provider request is made by
this file.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import requests

import agents.dataforseo_serp_agent as dfs


class _DiagnosticLogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, ignore_errors=True)

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

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _read_log_records(self):
        if not self.log_path.exists():
            return []
        lines = [line for line in self.log_path.read_text().splitlines() if line.strip()]
        return [json.loads(line) for line in lines]


class FailureClassificationTests(_DiagnosticLogTestCase):
    def test_timeout_is_logged_and_still_propagates(self):
        with mock.patch.object(dfs.requests, "post", side_effect=requests.exceptions.Timeout("simulated")):
            with self.assertRaises(requests.exceptions.Timeout):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["failure_category"], "timeout")
        self.assertEqual(records[0]["provider"], "dataforseo")
        self.assertEqual(records[0]["location_name"], "Albany,New York,United States")

    def test_connection_error_is_logged_and_still_propagates(self):
        with mock.patch.object(dfs.requests, "post", side_effect=requests.exceptions.ConnectionError("simulated")):
            with self.assertRaises(requests.exceptions.ConnectionError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["failure_category"], "timeout_or_connectivity")

    def test_rate_limit_http_error_is_logged_and_still_propagates(self):
        class FakeResponse:
            def raise_for_status(self):
                err = requests.exceptions.HTTPError("429 Client Error")
                err.response = mock.Mock(status_code=429)
                raise err

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(requests.exceptions.HTTPError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["failure_category"], "rate_limit")

    def test_server_5xx_http_error_is_logged_and_still_propagates(self):
        class FakeResponse:
            def raise_for_status(self):
                err = requests.exceptions.HTTPError("503 Server Error")
                err.response = mock.Mock(status_code=503)
                raise err

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(requests.exceptions.HTTPError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["failure_category"], "provider_5xx")

    def test_auth_http_error_is_logged_and_still_propagates(self):
        class FakeResponse:
            def raise_for_status(self):
                err = requests.exceptions.HTTPError("401 Client Error")
                err.response = mock.Mock(status_code=401)
                raise err

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(requests.exceptions.HTTPError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["failure_category"], "authentication")

    def test_task_level_status_code_is_captured_sanitized(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "status_code": 20000,
                    "tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'location_name'"}],
                }

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["status_code"], 40501)
        self.assertEqual(records[0]["failure_category"], "malformed_request_field")

    def test_envelope_level_status_code_is_captured_when_no_task_code(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"status_code": 40200, "status_message": "Not enough credits"}

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(records[0]["status_code"], 40200)
        self.assertEqual(records[0]["failure_category"], "account_balance")


class NoDiagnosticOnNonProviderOrSuccessTests(_DiagnosticLogTestCase):
    def test_invalid_market_location_is_never_logged_as_a_provider_failure(self):
        with mock.patch.object(dfs, "requests") as mock_requests:
            with self.assertRaises(dfs.InvalidMarketLocationError):
                dfs.search_google_organic("plumber", "Southampton", depth=10)
        mock_requests.post.assert_not_called()
        self.assertEqual(self._read_log_records(), [])

    def test_successful_call_writes_no_diagnostic(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": []}]}]}

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        self.assertEqual(self._read_log_records(), [])


class SanitizedNoSecretsTests(_DiagnosticLogTestCase):
    def test_diagnostic_log_never_contains_credentials_or_headers(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'location_name'"}]}

        with mock.patch.object(dfs.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        raw_log_text = self.log_path.read_text()
        for forbidden in ["test-placeholder-login", "test-placeholder-password", "Authorization", "Basic "]:
            self.assertNotIn(forbidden, raw_log_text)

    def test_diagnostic_log_contains_only_expected_fields(self):
        with mock.patch.object(dfs.requests, "post", side_effect=requests.exceptions.Timeout("simulated")):
            with self.assertRaises(requests.exceptions.Timeout):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        records = self._read_log_records()
        self.assertEqual(
            set(records[0].keys()),
            {"ts", "provider", "failure_category", "status_code", "location_code", "location_name"},
        )

    def test_diagnostic_logging_failure_never_breaks_the_search_path(self):
        """If the log directory can't be written to, the original
        exception must still propagate -- diagnostics are best-effort and
        must never mask or replace the real failure."""
        dfs._LEADBOT_PROVIDER_DIAGNOSTICS_LOG = Path("/nonexistent-root-only-dir/diag.log")
        with mock.patch.object(dfs.requests, "post", side_effect=requests.exceptions.Timeout("simulated")):
            with self.assertRaises(requests.exceptions.Timeout):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)


class ClassifyProviderFailureUnitTests(unittest.TestCase):
    def test_timeout_exception_type(self):
        self.assertEqual(
            dfs._leadbot_classify_provider_failure(requests.exceptions.Timeout("x")),
            "timeout",
        )

    def test_unknown_generic_exception(self):
        self.assertEqual(dfs._leadbot_classify_provider_failure(RuntimeError("something odd")), "unknown")

    def test_message_based_rate_limit_detection(self):
        self.assertEqual(
            dfs._leadbot_classify_provider_failure(RuntimeError("Too Many Requests")),
            "rate_limit",
        )

    def test_message_based_balance_detection(self):
        self.assertEqual(
            dfs._leadbot_classify_provider_failure(RuntimeError("DataForSEO API error: 40200 Not enough credits")),
            "account_balance",
        )


if __name__ == "__main__":
    unittest.main()
