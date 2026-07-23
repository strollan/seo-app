"""
Regression tests for DataForSEO city-name casing normalization.

Root cause this fixes: the location-formatting fix in
scripts/test_dataforseo_location_formatting.py correctly normalizes the
STATE portion of a market string (abbreviation or full name -> DataForSEO's
canonical full state name), but preserved the CITY portion's casing
exactly as the user typed it. DataForSEO's location_name matching is
case-sensitive against its own canonical location list, so a market typed
with irregular casing (e.g. a guest who typed "aLBANY ny", plausibly an
accidental caps-lock slip) produced "aLBANY,New York,United States" --
syntactically well-formed (no duplicate commas), but not DataForSEO's
canonical "Albany,New York,United States", so the task was still rejected.
Confirmed directly against DataForSEO's own /v3/serp/google/locations
reference list in this session's investigation: the mis-cased string is
absent from all 267,107 canonical entries; the properly-cased string is
present (location_code 1022672).

Fix: agents.dataforseo_serp_agent._normalize_city_case() -- if every word
in the parsed city already looks plausibly cased (starts uppercase, isn't
ALL CAPS), it's returned completely unchanged, so correctly typed names
such as "McAllen" or "O'Fallon" are never touched by this fix. Only when
the input looks wrong (all-lowercase, ALL-UPPERCASE, or a clearly
backwards pattern like "aLBANY") does it get renormalized word-by-word,
with special handling for the "Mc" and "O'" prefixes so a full
renormalization pass still produces "McAllen"/"O'Fallon" rather than the
"Mcallen"/"O'fallon" a plain str.title()/str.capitalize() would give.

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


class CityCasingNormalizationTests(unittest.TestCase):
    """agents.dataforseo_serp_agent._location_name(): city-casing coverage,
    pure function, no network involved."""

    def test_backwards_mixed_case_is_normalized(self):
        self.assertEqual(dfs._location_name("aLBANY ny"), "Albany,New York,United States")

    def test_all_uppercase_with_comma_is_normalized(self):
        self.assertEqual(dfs._location_name("ALBANY, NY"), "Albany,New York,United States")

    def test_all_lowercase_no_comma_is_normalized(self):
        self.assertEqual(dfs._location_name("albany ny"), "Albany,New York,United States")

    def test_already_correct_casing_is_preserved(self):
        self.assertEqual(dfs._location_name("Albany, NY"), "Albany,New York,United States")

    def test_lowercase_two_word_city_with_comma(self):
        self.assertEqual(dfs._location_name("san francisco, ca"), "San Francisco,California,United States")

    def test_all_uppercase_two_word_city_no_comma(self):
        self.assertEqual(dfs._location_name("NEW YORK NY"), "New York,New York,United States")

    def test_mcallen_already_correct_is_preserved_verbatim(self):
        """"McAllen" must never be routed through renormalization at all
        when it's already plausible -- this is the case a naive
        str.title() call would break ("McAllen".title() == "Mcallen")."""
        self.assertEqual(dfs._location_name("McAllen, TX"), "McAllen,Texas,United States")

    def test_mcallen_lowercase_is_normalized_correctly(self):
        self.assertEqual(dfs._location_name("mcallen, tx"), "McAllen,Texas,United States")

    def test_ofallon_already_correct_is_preserved_verbatim(self):
        self.assertEqual(dfs._location_name("O'Fallon, MO"), "O'Fallon,Missouri,United States")

    def test_ofallon_lowercase_is_normalized_correctly(self):
        self.assertEqual(dfs._location_name("o'fallon, mo"), "O'Fallon,Missouri,United States")

    def test_winston_salem_already_correct_is_preserved_verbatim(self):
        self.assertEqual(dfs._location_name("Winston-Salem, NC"), "Winston-Salem,North Carolina,United States")

    def test_winston_salem_lowercase_is_normalized_correctly(self):
        self.assertEqual(dfs._location_name("winston-salem, nc"), "Winston-Salem,North Carolina,United States")

    def test_st_louis_already_correct_is_preserved_verbatim(self):
        self.assertEqual(dfs._location_name("St. Louis, MO"), "St. Louis,Missouri,United States")

    def test_valid_zip_code_unaffected_by_city_casing_fix(self):
        self.assertEqual(dfs._location_name("12207"), "12207")

    def test_valid_zip_plus_four_unaffected_by_city_casing_fix(self):
        self.assertEqual(dfs._location_name("12207-1234"), "12207-1234")

    def test_invalid_state_abbreviation_still_rejected(self):
        with self.assertRaises(dfs.InvalidMarketLocationError):
            dfs._location_name("Albany, XX")

    def test_ambiguous_bare_city_still_rejected(self):
        with self.assertRaises(dfs.InvalidMarketLocationError) as ctx:
            dfs._location_name("Southampton")
        self.assertIn(INVALID_MARKET_MESSAGE, str(ctx.exception))

    def test_no_result_ever_contains_consecutive_commas(self):
        markets = [
            "aLBANY ny", "ALBANY, NY", "albany ny", "Albany, NY",
            "san francisco, ca", "NEW YORK NY", "McAllen, TX", "mcallen, tx",
            "O'Fallon, MO", "o'fallon, mo", "Winston-Salem, NC",
            "winston-salem, nc", "St. Louis, MO", "12207", "12207-1234",
        ]
        for market in markets:
            result = dfs._location_name(market)
            self.assertNotIn(",,", result, f"consecutive commas in result for {market!r}: {result!r}")


class NormalizeCityCaseHelperTests(unittest.TestCase):
    """Direct unit coverage of _normalize_city_case() in isolation."""

    def test_plausible_input_returned_unchanged(self):
        for city in ["Albany", "San Francisco", "McAllen", "O'Fallon", "Winston-Salem", "St. Louis"]:
            self.assertEqual(dfs._normalize_city_case(city), city)

    def test_all_lower_is_normalized(self):
        self.assertEqual(dfs._normalize_city_case("albany"), "Albany")

    def test_all_upper_is_normalized(self):
        self.assertEqual(dfs._normalize_city_case("ALBANY"), "Albany")

    def test_backwards_case_is_normalized(self):
        self.assertEqual(dfs._normalize_city_case("aLBANY"), "Albany")

    def test_empty_string_returns_empty(self):
        self.assertEqual(dfs._normalize_city_case(""), "")


class LocationDoesNotCallProviderForInvalidMarketTests(unittest.TestCase):
    """Confirms invalid markets never reach the provider request layer,
    even after the city-casing change."""

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key) for key in ("LEADBOT_DATAFORSEO_ENABLED",)
        }
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
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

    def test_valid_mis_cased_market_reaches_request_layer_with_canonical_casing(self):
        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "status_code": 20000,
                    "tasks": [{"status_code": 20000, "result": [{"items": []}]}],
                }

        self._env_backup.setdefault("DATAFORSEO_LOGIN", os.environ.get("DATAFORSEO_LOGIN"))
        self._env_backup.setdefault("DATAFORSEO_PASSWORD", os.environ.get("DATAFORSEO_PASSWORD"))
        os.environ["DATAFORSEO_LOGIN"] = "test-placeholder-login"
        os.environ["DATAFORSEO_PASSWORD"] = "test-placeholder-password"

        with mock.patch.object(dfs.requests, "post", return_value=_FakeResponse()) as mock_post:
            dfs.search_google_organic("plumber", "aLBANY ny", depth=10)

        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload[0]["location_name"], "Albany,New York,United States")


class RunJobCityCasingTests(unittest.TestCase):
    """agents.lead_live_job_agent.run_job(): end-to-end job state for a
    mis-cased-but-valid market vs. an invalid market vs. a genuine provider
    failure -- using the real _location_name()/search_google_organic call
    chain (only the final HTTP layer is mocked)."""

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
        job_id = "citycase-" + uuid.uuid4().hex[:12]
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

    def test_reported_incident_market_now_normalizes_and_completes(self):
        """The exact market string from the production job that motivated
        this fix ("aLBANY ny") must now normalize to the canonical
        location_name and reach the (mocked) provider, rather than being
        rejected by DataForSEO for a casing mismatch."""
        with mock.patch("agents.dataforseo_serp_agent.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            mock_post.return_value.json.return_value = {
                "status_code": 20000,
                "tasks": [{"status_code": 20000, "result": [{"items": []}]}],
            }
            job_id = self._make_job("plumber", "aLBANY ny")
            job_agent.run_job(job_id)

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload[0]["location_name"], "Albany,New York,United States")

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)

    def test_ambiguous_bare_city_still_ends_as_invalid_market_location(self):
        job_id = self._make_job("candy", "Southampton")
        job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "invalid_market_location")
        self.assertEqual(final_job["message"], INVALID_MARKET_MESSAGE)
        self.assertEqual(final_job["export_file"], "")
        self.assertEqual(self.export_calls, [])

    def test_genuine_dataforseo_failure_still_becomes_search_provider_unavailable(self):
        with mock.patch("agents.dataforseo_serp_agent.requests.post", side_effect=RuntimeError("simulated DataForSEO outage")):
            job_id = self._make_job("plumber", "aLBANY ny")
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "search_provider_unavailable")

    def test_genuine_zero_result_scan_stays_valid_done(self):
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


if __name__ == "__main__":
    unittest.main()
