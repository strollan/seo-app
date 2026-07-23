"""
Regression tests for DataForSEO location-name formatting.

Root cause this fixes: agents.dataforseo_serp_agent._location_name() used
market.split() on whitespace, which left a comma attached to the city
portion for "City, ST"-style input (e.g. "San Francisco, CA" ->
city="San Francisco," with the comma still attached), producing a
malformed location_name like "San Francisco,,California,United States".
DataForSEO rejected this at the task level with status_code 40501,
"Invalid Field: 'location_name'." -- which, before the provider-failure-
surfacing fix, silently looked like a genuine zero-result scan, and after
it, looked like "the lead search service is temporarily unavailable" even
though the account, credentials, balance, and network were all healthy.
Confirmed via a live production diagnostic in this session's incident
investigation (reproduced the exact 40501 error with the tester's own
"plumber"/"Albany, NY" query).

Fix: _location_name() now does explicit final-comma parsing (falling back
to the existing space-separated "City ST" convention when there's no
comma at all), resolves both 2-letter state abbreviations and full state
names as a single unit, preserves existing ZIP-code and alias-map
pass-through behavior unchanged, and raises InvalidMarketLocationError --
a new exception distinct from SearchProviderUnavailableError -- for
markets it can't confidently resolve (e.g. a bare city with no state,
like "Southampton") rather than sending DataForSEO a malformed request or
guessing a state.

Covers, across three layers:
  - agents.dataforseo_serp_agent._location_name(): the parsing itself
  - business_competitor_finder._raw_find_business_competitors() /
    _leadbot_non_serper_search(): that InvalidMarketLocationError is
    never sent to the provider and is never mislabeled as
    SearchProviderUnavailableError
  - agents.lead_live_job_agent.run_job(): job state (status/error_code/
    message/export_file) for an invalid market vs. a genuine provider
    failure vs. a genuine successful/zero-result scan

Uses mocked provider calls only -- no live provider request is made by
this file.
"""

import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.dataforseo_serp_agent as dfs
import business_competitor_finder as bcf
import agents.lead_live_job_agent as job_agent

INVALID_MARKET_MESSAGE = "Enter a City, State or ZIP Code, such as Albany, NY or 12207."


class LocationNameParsingTests(unittest.TestCase):
    """agents.dataforseo_serp_agent._location_name(): pure-function
    coverage of the parsing itself, no network involved."""

    def test_city_comma_space_state(self):
        self.assertEqual(dfs._location_name("Albany, NY"), "Albany,New York,United States")

    def test_city_comma_no_space_state(self):
        self.assertEqual(dfs._location_name("Albany,NY"), "Albany,New York,United States")

    def test_city_space_comma_space_state(self):
        self.assertEqual(dfs._location_name("Albany , NY"), "Albany,New York,United States")

    def test_san_francisco_comma_ca(self):
        self.assertEqual(dfs._location_name("San Francisco, CA"), "San Francisco,California,United States")

    def test_new_york_comma_ny(self):
        """The city name and the state's full name are both "New York" --
        exercises the final-comma split not confusing the two."""
        self.assertEqual(dfs._location_name("New York, NY"), "New York,New York,United States")

    def test_lowercase_state_abbreviation(self):
        self.assertEqual(dfs._location_name("Albany, ny"), "Albany,New York,United States")

    def test_full_state_name_supported(self):
        self.assertEqual(dfs._location_name("Albany, New York"), "Albany,New York,United States")

    def test_full_state_name_multi_word(self):
        self.assertEqual(dfs._location_name("Charlotte, North Carolina"), "Charlotte,North Carolina,United States")

    def test_state_abbreviation_with_periods(self):
        self.assertEqual(dfs._location_name("Albany, N.Y."), "Albany,New York,United States")

    def test_no_comma_space_separated_still_works(self):
        """Preserve the existing (already-working) space-separated
        convention with no comma at all."""
        self.assertEqual(dfs._location_name("Albany NY"), "Albany,New York,United States")

    def test_alias_map_still_works_unchanged(self):
        self.assertEqual(dfs._location_name("Long Island NY"), "New York,New York,United States")

    def test_valid_zip_code_passes_through_unchanged(self):
        self.assertEqual(dfs._location_name("12207"), "12207")

    def test_valid_zip_plus_four_passes_through_unchanged(self):
        self.assertEqual(dfs._location_name("12207-1234"), "12207-1234")

    def test_no_result_ever_contains_consecutive_commas(self):
        markets = [
            "Albany, NY", "Albany,NY", "Albany , NY", "San Francisco, CA",
            "New York, NY", "Albany, ny", "Albany, New York", "Albany NY",
            "Long Island NY", "12207", "12207-1234",
        ]
        for market in markets:
            result = dfs._location_name(market)
            self.assertNotIn(",,", result, f"consecutive commas in result for {market!r}: {result!r}")

    def test_bare_ambiguous_city_is_rejected(self):
        with self.assertRaises(dfs.InvalidMarketLocationError) as ctx:
            dfs._location_name("Southampton")
        self.assertIn(INVALID_MARKET_MESSAGE, str(ctx.exception))

    def test_empty_market_is_rejected(self):
        with self.assertRaises(dfs.InvalidMarketLocationError):
            dfs._location_name("")

    def test_whitespace_only_market_is_rejected(self):
        with self.assertRaises(dfs.InvalidMarketLocationError):
            dfs._location_name("   ")

    def test_unrecognized_state_code_is_rejected_not_guessed(self):
        """"XX" is not a real state abbreviation or state name -- must be
        rejected, not silently passed through as malformed punctuation."""
        with self.assertRaises(dfs.InvalidMarketLocationError):
            dfs._location_name("Albany, XX")

    def test_malformed_trailing_comma_is_normalized_not_sent_malformed(self):
        """A trailing comma with nothing after it (no state at all) must
        be rejected rather than producing a location_name ending in a
        dangling comma."""
        with self.assertRaises(dfs.InvalidMarketLocationError):
            dfs._location_name("Albany,")


class DataForSeoClientDoesNotCallProviderForInvalidMarketTests(unittest.TestCase):
    """search_google_organic() must never reach requests.post for a market
    it can't resolve -- InvalidMarketLocationError is raised while still
    building the payload."""

    def setUp(self):
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

    def test_invalid_market_raises_before_any_request(self):
        with mock.patch.object(dfs, "requests") as mock_requests:
            with self.assertRaises(dfs.InvalidMarketLocationError):
                dfs.search_google_organic("plumber", "Southampton", depth=10)
        mock_requests.post.assert_not_called()

    def test_valid_market_reaches_the_request_layer_with_normalized_location(self):
        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "status_code": 20000,
                    "tasks": [{"status_code": 20000, "result": [{"items": []}]}],
                }

        with mock.patch.object(dfs.requests, "post", return_value=_FakeResponse()) as mock_post:
            dfs.search_google_organic("plumber", "Albany, NY", depth=10)

        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload[0]["location_name"], "Albany,New York,United States")


class RawFindBusinessCompetitorsInvalidMarketTests(unittest.TestCase):
    """_raw_find_business_competitors(): InvalidMarketLocationError must
    never be mislabeled as SearchProviderUnavailableError, in either the
    Serper-disabled (current production config) or Serper-enabled path."""

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("USE_LIVE_SERP", "LEADBOT_DATAFORSEO_ENABLED")
        }
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_serper_disabled_invalid_market_raises_invalid_market_location(self):
        os.environ["USE_LIVE_SERP"] = "0"
        with mock.patch.object(bcf, "google_search") as mock_google_search:
            with self.assertRaises(dfs.InvalidMarketLocationError):
                bcf._raw_find_business_competitors("candy", location="Southampton", limit=5, pages=[1])
        mock_google_search.assert_not_called()

    def test_serper_enabled_fallback_invalid_market_raises_invalid_market_location_not_provider_unavailable(self):
        os.environ["USE_LIVE_SERP"] = "true"
        with mock.patch.object(bcf, "google_search", side_effect=RuntimeError("simulated Serper outage")):
            with self.assertRaises(dfs.InvalidMarketLocationError):
                bcf._raw_find_business_competitors("candy", location="Southampton", limit=5, pages=[1])


class RunJobInvalidMarketLocationTests(unittest.TestCase):
    """agents.lead_live_job_agent.run_job(): end-to-end job state for an
    unresolvable market vs. a genuine provider failure vs. a genuine
    successful scan -- using the real _location_name()/search_google_organic
    call chain (only the final HTTP layer is mocked), so this proves the
    whole pipeline, not just isolated units."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self._orig_job_dir = job_agent.JOB_DIR
        job_agent.JOB_DIR = Path(self.tmpdir)
        self.addCleanup(lambda: setattr(job_agent, "JOB_DIR", self._orig_job_dir))

        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                "USE_LIVE_SERP",
                "LEADBOT_DATAFORSEO_ENABLED",
                "DATAFORSEO_LOGIN",
                "DATAFORSEO_PASSWORD",
            )
        }
        os.environ["USE_LIVE_SERP"] = "0"
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        os.environ["DATAFORSEO_LOGIN"] = "test-placeholder-login"
        os.environ["DATAFORSEO_PASSWORD"] = "test-placeholder-password"
        self.addCleanup(self._restore_env)

        self.export_calls = []

        def fake_export(payload):
            self.export_calls.append(payload)
            return {"path": "/tmp/should-not-be-called.csv"}

        patches = [
            mock.patch("agents.lead_business_cache_agent.apply_cached_business_to_lead", lambda lead: (lead, False)),
            mock.patch("agents.lead_business_cache_agent.save_business_from_lead", lambda lead, enriched=False: None),
            mock.patch("agents.lead_export_agent.export_leads_to_csv", fake_export),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _make_job(self, keyword, market, max_queries=1):
        job_id = "locfmt-" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "queued",
            "message": "",
            "leads": [],
            "errors": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "seen_domains": [],
            "params": {
                "industry": keyword,
                "market": market,
                "keyword": keyword,
                "own_domain": "",
                "limit": 10,
                "per_batch": 8,
                "per_query_limit": 8,
                "max_queries": max_queries,
                "guest_id": "",
            },
            "cancel_requested": False,
            "updated_at": job_agent.now_iso(),
            "export_file": "",
        }
        job_agent.write_job(job)
        return job_id

    def test_bare_ambiguous_city_ends_as_invalid_market_location_no_export(self):
        job_id = self._make_job("candy", "Southampton")
        job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "invalid_market_location")
        self.assertEqual(final_job["message"], INVALID_MARKET_MESSAGE)
        self.assertEqual(final_job["export_file"], "")
        self.assertEqual(self.export_calls, [])

    def test_valid_normalized_market_reaches_dataforseo_and_completes_normally(self):
        fake_results = [{"title": "A Plumber", "link": "https://a-plumber-test.com", "snippet": "plumber services"}]
        with mock.patch("agents.dataforseo_serp_agent.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            mock_post.return_value.json.return_value = {
                "status_code": 20000,
                "tasks": [{"status_code": 20000, "result": [{"items": [
                    {"type": "organic", "url": "https://a-plumber-test.com", "rank_group": 1, "rank_absolute": 1, "title": "A Plumber", "description": "plumber services"},
                ]}]}],
            }
            job_id = self._make_job("plumber", "Albany, NY")
            job_agent.run_job(job_id)

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload[0]["location_name"], "Albany,New York,United States")

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)

    def test_genuine_dataforseo_zero_result_stays_a_valid_done_scan(self):
        with mock.patch("agents.dataforseo_serp_agent.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            mock_post.return_value.json.return_value = {
                "status_code": 20000,
                "tasks": [{"status_code": 20000, "result": [{"items": []}]}],
            }
            job_id = self._make_job("plumber", "Albany, NY")
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertEqual(final_job["leads"], [])
        self.assertEqual(final_job["errors"], [])

    def test_genuine_dataforseo_failure_still_becomes_search_provider_unavailable(self):
        """Confirms the two error types remain properly distinguished
        end-to-end: a *provider* failure (not a location-parsing problem)
        for a perfectly valid, normalized market still surfaces as
        search_provider_unavailable, not invalid_market_location."""
        with mock.patch("agents.dataforseo_serp_agent.requests.post", side_effect=RuntimeError("simulated DataForSEO outage")):
            job_id = self._make_job("plumber", "Albany, NY")
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "search_provider_unavailable")


if __name__ == "__main__":
    unittest.main()
