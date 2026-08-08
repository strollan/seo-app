"""
Regression tests for distinguishing a DataForSEO-confirmed invalid
location value (task status 40501, "Invalid Field: 'location_name'") from
a genuine provider outage.

Incident background: production tester traffic on 2026-07-23/24 showed
repeated "The lead search service is temporarily unavailable... Please
try again shortly" failures. Investigation of the sanitized diagnostics
log (data/leadbot_provider_diagnostics.log) and job files found:
  - the circuit breaker has NEVER opened in production (zero
    "circuit_breaker_open" log entries, and no window of 3 exhausted
    retryable failures within 60s ever occurred) -- ruling out the
    120s cooldown as a cause
  - a confirmed, reproducible cause instead: a misspelled location
    ("Beverley Hills, CA" instead of "Beverly Hills, CA") passes
    agents.dataforseo_serp_agent._location_name()'s casing-only
    normalization unchanged (it's already plausibly capitalized), gets
    sent to DataForSEO, and DataForSEO rejects it with task 40501. This
    is a permanent, non-retryable, user-input problem -- retrying it
    (as the affected job did, 4 times in 4 seconds) can never succeed --
    yet it was surfacing through the exact same generic
    "temporarily unavailable" wording as an actual transient outage.

This change adds agents.dataforseo_serp_agent.InvalidLocationValueError,
raised specifically for task 40501, threaded through
business_competitor_finder.py and agents/lead_live_job_agent.py the same
way the pre-existing InvalidMarketLocationError already is, so run_job()
surfaces it as its own distinct, honest error_code/message instead of
"search_provider_unavailable". Nothing about the retry ladder, the
circuit breaker thresholds/cooldown, scoring, ordering, exports,
ownership, guest limits, CSRF, or authentication is touched -- 40501 was
already excluded from the retryable set and the circuit breaker before
this change, and still is.

Uses mocked provider calls and a mocked circuit-breaker clock throughout
-- no real network call and no real waiting is ever made by this file.
"""

import os
import sys
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


class _InvalidLocationValueTestCase(unittest.TestCase):
    """Same circuit-breaker isolation pattern as
    scripts/test_dataforseo_circuit_breaker.py: resets breaker state
    before/after every test and provides a controllable fake clock."""

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


class SearchGoogleOrganicRaisesDistinctErrorTests(_InvalidLocationValueTestCase):
    def test_40501_raises_invalid_location_value_error_not_generic_runtime_error(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501, "Invalid Field: 'location_name'")):
            with self.assertRaises(dfs.InvalidLocationValueError):
                dfs.search_google_organic("plumber", "Beverley Hills, CA", depth=10)

    def test_40501_makes_exactly_one_attempt_no_retry(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501)) as mock_post:
            with self.assertRaises(dfs.InvalidLocationValueError):
                dfs.search_google_organic("plumber", "Beverley Hills, CA", depth=10)
        self.assertEqual(mock_post.call_count, 1)

    def test_40501_never_opens_the_circuit_breaker(self):
        # Even repeated 40501s (well beyond the retryable-failure
        # threshold of 3) must never open the circuit -- 40501 was
        # already excluded from the retryable set before this change.
        for _ in range(5):
            with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501)):
                with self.assertRaises(dfs.InvalidLocationValueError):
                    dfs.search_google_organic("plumber", "Beverley Hills, CA", depth=10)
            self._advance(1)

        self.assertIsNone(dfs._circuit_breaker_state["opened_until"])
        self.assertEqual(dfs._circuit_breaker_state["exhaustion_timestamps"], [])

        # A subsequent ordinary call must still proceed normally (closed
        # circuit), proving none of those 40501s were mistaken for a
        # retryable exhaustion.
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(20000)) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        mock_post.assert_called_once()

    def test_genuine_40101_still_retries_and_still_raises_generic_runtime_error(self):
        # Unrelated status codes must be completely unaffected by this
        # change.
        responses = [_fake_response(40101), _fake_response(40101), _fake_response(40101)]
        with mock.patch.object(dfs.requests, "post", side_effect=responses) as mock_post:
            with self.assertRaises(RuntimeError):
                dfs.search_google_organic("plumber", "Albany, NY", depth=10)
        self.assertEqual(mock_post.call_count, 3)
        self.assertNotIsInstance(mock_post.side_effect, dfs.InvalidLocationValueError)


class NonSerperSearchPropagatesDistinctErrorTests(_InvalidLocationValueTestCase):
    def test_raises_invalid_location_value_error_not_search_provider_unavailable(self):
        with mock.patch.object(dfs.requests, "post", return_value=_fake_response(40501)):
            with self.assertRaises(bcf.InvalidLocationValueError):
                bcf._leadbot_non_serper_search(
                    "plumber", location="Beverley Hills, CA", raise_on_failure=True
                )


def _find_leads_raises_invalid_location(industry, market, query, own_domain, limit, on_candidate=None):
    raise dfs.InvalidLocationValueError("DataForSEO task error: 40501 Invalid Field: 'location_name'")


class CallFindLeadsWithTimeoutClassificationTests(unittest.TestCase):
    def test_invalid_location_value_error_maps_to_its_own_marker(self):
        leads, search_error = job_agent.call_find_leads_with_timeout(
            _find_leads_raises_invalid_location,
            industry="",
            market="Beverley Hills, CA",
            query="poop Beverley Hills, CA",
            own_domain="",
            limit=5,
        )

        self.assertIsNone(leads)
        self.assertTrue(str(search_error).startswith(job_agent.INVALID_LOCATION_VALUE_MARKER))

    def test_marker_and_message_constants_are_distinct_from_existing_ones(self):
        self.assertNotEqual(job_agent.INVALID_LOCATION_VALUE_MARKER, job_agent.PROVIDER_UNAVAILABLE_MARKER)
        self.assertNotEqual(job_agent.INVALID_LOCATION_VALUE_MARKER, job_agent.INVALID_MARKET_LOCATION_MARKER)
        self.assertNotEqual(job_agent.INVALID_LOCATION_VALUE_MESSAGE, job_agent.SEARCH_PROVIDER_UNAVAILABLE_MESSAGE)
        self.assertNotEqual(job_agent.INVALID_LOCATION_VALUE_MESSAGE, job_agent.INVALID_MARKET_LOCATION_MESSAGE)


def fake_find_leads_raises_invalid_location(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
    # Accepts the exact same shape as the real find_leads() (including
    # on_candidate) so agents.lead_live_job_agent._resolve_find_leads_call()
    # doesn't need its documented TypeError-triggered retry-without-
    # on_candidate fallback -- that fallback is a real, intentional part
    # of production behavior, but it makes a mock's call_count double per
    # logical attempt (the first, richer call fails at argument-binding
    # time before this function's body ever runs, then a second call
    # without on_candidate actually executes) -- an artifact of Mock
    # call-counting, not a second real query attempt.
    raise dfs.InvalidLocationValueError("DataForSEO task error: 40501 Invalid Field: 'location_name'")


class RunJobEndToEndInvalidLocationValueTests(unittest.TestCase):
    """Real create_job()/run_job() pass (background thread, no network):
    agents.lead_finding_agent.find_leads is monkeypatched to raise
    InvalidLocationValueError directly, exercising the real
    call_find_leads_with_timeout() -> run_job() classification path."""

    def setUp(self):
        # create_job() now spawns each scan as its own OS process by
        # default (agents.lead_live_job_agent.RUN_SCANS_IN_SUBPROCESS,
        # part of the P1 cancel-hang fix), which re-imports find_leads
        # fresh from disk and can never see the mock.patch("agents.
        # lead_finding_agent.find_leads", ...) below. Opt back into the
        # pre-fix in-process thread so that patch keeps working.
        subprocess_patch = mock.patch.object(job_agent, "RUN_SCANS_IN_SUBPROCESS", False)
        subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

        self._created_job_ids = []

    def tearDown(self):
        for job_id in self._created_job_ids:
            try:
                job_agent.job_path(job_id).unlink()
            except FileNotFoundError:
                pass

    def test_job_ends_with_distinct_error_code_and_message(self):
        import time

        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_raises_invalid_location):
            job_id = job_agent.create_job({
                "industry": "",
                "market": "Beverley Hills, CA",
                "keyword": "poop",
                "own_domain": "",
                "limit": 25,
                "per_batch": 8,
                "per_query_limit": 6,
                "max_queries": 6,
                "owner_email": "",
                "owner_username": "",
                "owner_role": "",
            })
            self._created_job_ids.append(job_id)

            deadline = time.time() + 10
            job = None
            while time.time() < deadline:
                job = job_agent.read_job(job_id)
                if job and job.get("status") in {"done", "error", "cancelled"}:
                    break
                time.sleep(0.05)

        self.assertIsNotNone(job, "job file was never written")
        self.assertEqual(job.get("status"), "error")
        self.assertEqual(job.get("error_code"), "invalid_location_value")
        self.assertEqual(job.get("message"), job_agent.INVALID_LOCATION_VALUE_MESSAGE)
        self.assertNotEqual(job.get("message"), job_agent.SEARCH_PROVIDER_UNAVAILABLE_MESSAGE)

    def test_job_stops_after_first_query_instead_of_burning_every_variant(self):
        """The location is fixed for the whole job, so every query variant
        would fail identically -- this must stop immediately rather than
        retrying every keyword/location phrasing, unlike a genuine
        per-query provider failure (which does let other variants try)."""
        import time

        with mock.patch(
            "agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_raises_invalid_location
        ) as mock_find_leads:
            job_id = job_agent.create_job({
                "industry": "",
                "market": "Beverley Hills, CA",
                "keyword": "poop",
                "own_domain": "",
                "limit": 25,
                "per_batch": 8,
                "per_query_limit": 6,
                "max_queries": 6,
                "owner_email": "",
                "owner_username": "",
                "owner_role": "",
            })
            self._created_job_ids.append(job_id)

            deadline = time.time() + 10
            job = None
            while time.time() < deadline:
                job = job_agent.read_job(job_id)
                if job and job.get("status") in {"done", "error", "cancelled"}:
                    break
                time.sleep(0.05)

        self.assertIsNotNone(job)
        self.assertEqual(job.get("status"), "error")
        # Only the first query variant should have been attempted before
        # the job stopped -- not one call per configured query variant.
        self.assertEqual(mock_find_leads.call_count, 1)
        self.assertEqual(job.get("errors"), [])


if __name__ == "__main__":
    unittest.main()
