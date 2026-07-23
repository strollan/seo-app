"""
Regression tests for preserving partial Lead Finder results when some
(but not all) internally-generated query variants hit a genuine
DataForSEO provider failure.

Background: a Lead Finder scan runs several query variants for the same
keyword/market (e.g. "plumber Albany, NY", "plumber near Albany, NY",
"Albany, NY plumber", "best plumber Albany, NY"). Before this fix,
agents.lead_live_job_agent.run_job() aborted the ENTIRE scan the moment
ANY single query variant raised SearchProviderUnavailableError -- marking
the whole job "search_provider_unavailable" and discarding real, already-
published leads from query variants that succeeded earlier in the same
loop. Confirmed directly in production: one query recovered via the
40101/40103 retry logic and returned real results, another query
exhausted its retries, and the entire scan was thrown away.

Fix: run_job()'s per-query loop no longer aborts on the first provider
failure. It tracks queries_attempted / queries_succeeded (results or a
legitimate zero-result outcome) / queries_with_provider_failure across
the whole loop, and only after every query has been tried does it decide
the scan's overall outcome:
  - every query failed -> unchanged existing behavior: status "error",
    error_code "search_provider_unavailable", no leads, no export.
  - at least one query succeeded (even with zero results) and at least
    one failed -> status "done", job["partial"] = True, message is the
    partial-results warning -- reusing the existing "done" status.
  - no query ever hit a provider failure -> unchanged existing behavior,
    whether or not any leads were found.

A market that can't be resolved at all (InvalidMarketLocationError) is
untouched by this fix -- every query would fail identically, so it still
aborts immediately, exactly as before.

Integrated on top of the DataForSEO circuit breaker
(agents/dataforseo_serp_agent.py): a circuit_breaker_open failure is not
special-cased here at all -- it reaches this loop through the exact same
PROVIDER_UNAVAILABLE_MARKER/SearchProviderUnavailableError chain as any
other provider failure, so it participates in the partial/total-failure
accounting identically. See CircuitBreakerInteractionTests below.

Also adds, on top of the original fix: app/main.py's polling JS now shows
partial-specific wording (never the three complete-success phrases) when
job.partial is true, and completed exports record a "partial" flag in the
existing data/leadbot_export_owners.json metadata file (via
agents.lead_dashboard_agent.record_export_owner()'s new is_partial
parameter / _leadbot_export_is_partial() reader) so partial exports can be
labeled in the dashboard file list. See PartialUiWordingTests and
PartialExportMetadataTests below.

Uses mocked find_leads() calls only (the exact function run_job() calls
once per query variant) -- no real network call is made by this file.
The lower-level 40101/40103 retry mechanism itself is covered separately
by scripts/test_dataforseo_retry.py, which this file does not duplicate
or modify.
"""

import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.lead_live_job_agent as job_agent
import business_competitor_finder as bcf

PARTIAL_MESSAGE = "Some search requests could not be completed. The results shown may be incomplete."
PROVIDER_FAILURE_MESSAGE = (
    "The lead search service is temporarily unavailable. This scan did not "
    "complete. Please try again shortly."
)


def _isolate_circuit_breaker_state(testcase):
    """Some tests in this file (CircuitBreakerInteractionTests) directly
    manipulate agents.dataforseo_serp_agent's module-level circuit-breaker
    state to simulate an already-open circuit. Without resetting it
    before and restoring it after every test in this file, that state
    (set using real wall-clock time, not a mocked clock) could otherwise
    leak into and break whatever DataForSEO-related test file happens to
    run next in the same process -- the same class of bug already fixed
    once for scripts/test_dataforseo_retry.py."""
    import agents.dataforseo_serp_agent as dfs

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


class _PartialResultsTestCase(unittest.TestCase):
    def setUp(self):
        _isolate_circuit_breaker_state(self)

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self._orig_job_dir = job_agent.JOB_DIR
        job_agent.JOB_DIR = Path(self.tmpdir)
        self.addCleanup(lambda: setattr(job_agent, "JOB_DIR", self._orig_job_dir))

        self._orig_outcome_log = job_agent._LEADBOT_QUERY_OUTCOME_LOG
        job_agent._LEADBOT_QUERY_OUTCOME_LOG = Path(self.tmpdir) / "query_outcomes.log"
        self.addCleanup(lambda: setattr(job_agent, "_LEADBOT_QUERY_OUTCOME_LOG", self._orig_outcome_log))

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

    def _make_job(self, max_queries=4):
        job_id = "partial-" + uuid.uuid4().hex[:12]
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
                "market": "Test City",
                "keyword": "plumber",
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

    def _read_outcome_log(self):
        import json

        path = job_agent._LEADBOT_QUERY_OUTCOME_LOG
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class OneSucceedsWithLeadsOneFailsTests(_PartialResultsTestCase):
    def test_valid_leads_are_returned_and_scan_is_not_total_failure(self):
        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                lead = {
                    "domain": "first-lead.com", "url": "https://first-lead.com", "title": "First Co",
                    "final_lead_score": 90, "best_phone": "555-1", "emails": [],
                }
                if on_candidate:
                    on_candidate(lead)
                return {"leads": [lead], "count": 1}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertTrue(final_job["partial"])
        self.assertEqual(final_job["message"], PARTIAL_MESSAGE)

        domains = [lead.get("domain") for lead in final_job["leads"]]
        self.assertIn("first-lead.com", domains)

    def test_export_is_still_created_for_the_valid_lead(self):
        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                lead = {
                    "domain": "first-lead.com", "url": "https://first-lead.com", "title": "First Co",
                    "final_lead_score": 90, "best_phone": "555-1", "emails": [],
                }
                if on_candidate:
                    on_candidate(lead)
                return {"leads": [lead], "count": 1}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertTrue(final_job["export_file"])
        self.assertEqual(len(self.export_calls), 1)


class OneSucceedsWithZeroResultsOneFailsTests(_PartialResultsTestCase):
    def test_zero_leads_with_partial_warning_not_provider_unavailable(self):
        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"leads": [], "count": 0}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertTrue(final_job["partial"])
        self.assertEqual(final_job["message"], PARTIAL_MESSAGE)
        self.assertEqual(final_job["leads"], [])

    def test_no_fabricated_leads(self):
        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"leads": [], "count": 0}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["leads"], [])
        self.assertEqual(self.export_calls, [])


class EveryQueryFailsTests(_PartialResultsTestCase):
    def test_total_provider_failure_unchanged(self):
        def failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise bcf.SearchProviderUnavailableError("simulated total outage")

        job_id = self._make_job(max_queries=3)
        with mock.patch("agents.lead_finding_agent.find_leads", failing_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "search_provider_unavailable")
        self.assertEqual(final_job["message"], PROVIDER_FAILURE_MESSAGE)
        self.assertEqual(final_job["leads"], [])
        self.assertEqual(final_job["export_file"], "")
        self.assertEqual(self.export_calls, [])
        self.assertNotIn("partial", final_job)


class EveryQuerySucceedsWithZeroResultsTests(_PartialResultsTestCase):
    def test_normal_zero_result_outcome_no_partial_no_error(self):
        def empty_but_healthy_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            return {"leads": [], "count": 0}

        job_id = self._make_job(max_queries=3)
        with mock.patch("agents.lead_finding_agent.find_leads", empty_but_healthy_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertFalse(final_job.get("partial"))
        self.assertEqual(final_job["message"], "Done. Open Desktop is ready.")
        self.assertEqual(final_job["leads"], [])


class AllQueriesSucceedWithLeadsTests(_PartialResultsTestCase):
    def test_normal_success_unchanged_no_partial_warning(self):
        def healthy_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            slug = abs(hash(str(service_keyword))) % 100000
            lead = {
                "domain": f"lead-{slug}.example.com",
                "url": f"https://lead-{slug}.example.com",
                "title": "A Lead", "final_lead_score": 80, "best_phone": "555-0", "emails": [],
            }
            if on_candidate:
                on_candidate(lead)
            return {"leads": [lead], "count": 1}

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", healthy_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertFalse(final_job.get("partial"))
        self.assertEqual(final_job["message"], "Done. Open Desktop is ready.")
        self.assertGreater(len(final_job["leads"]), 0)


class QueryOutcomeDiagnosticsTests(_PartialResultsTestCase):
    def test_partial_outcome_is_logged_with_sanitized_counts(self):
        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                lead = {
                    "domain": "first-lead.com", "url": "https://first-lead.com", "title": "First Co",
                    "final_lead_score": 90, "best_phone": "555-1", "emails": [],
                }
                if on_candidate:
                    on_candidate(lead)
                return {"leads": [lead], "count": 1}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        records = self._read_outcome_log()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["job_id"], job_id)
        self.assertEqual(record["queries_attempted"], 2)
        self.assertEqual(record["queries_succeeded"], 1)
        self.assertEqual(record["queries_failed"], 1)
        self.assertEqual(record["leads_published"], 1)
        self.assertTrue(record["partial"])

    def test_diagnostic_log_contains_no_secrets_or_query_text(self):
        def failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise bcf.SearchProviderUnavailableError(
                "Serper 429 quota exceeded for key sk-abcdef1234"
            )

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", failing_find_leads):
            job_agent.run_job(job_id)

        raw_text = job_agent._LEADBOT_QUERY_OUTCOME_LOG.read_text()
        for forbidden in ["sk-abcdef1234", "Serper", "plumber", "Test City"]:
            self.assertNotIn(forbidden, raw_text)


class PartialExportMetadataTests(_PartialResultsTestCase):
    """A partial scan's export must be marked in the existing
    data/leadbot_export_owners.json metadata file (reused rather than a
    new system), and a complete scan's export must default to
    non-partial. An owner (email/username) is required for this call
    site to write anything at all -- unchanged, pre-existing behavior --
    so this is exercised with an authenticated owner in job params."""

    def _make_owned_job(self, max_queries=2):
        job_id = self._make_job(max_queries=max_queries)
        job = job_agent.read_job(job_id)
        job["params"]["owner_email"] = "owner@example.com"
        job["params"]["owner_username"] = "owner1"
        job_agent.write_job(job)
        return job_id

    def test_partial_scan_export_is_marked_partial(self):
        import agents.lead_dashboard_agent as dash

        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                lead = {
                    "domain": "first-lead.com", "url": "https://first-lead.com", "title": "First Co",
                    "final_lead_score": 90, "best_phone": "555-1", "emails": [],
                }
                if on_candidate:
                    on_candidate(lead)
                return {"leads": [lead], "count": 1}
            raise bcf.SearchProviderUnavailableError("simulated outage on second query")

        job_id = self._make_owned_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertTrue(final_job["export_file"])
        self.assertTrue(dash._leadbot_export_is_partial(final_job["export_file"]))

    def test_complete_scan_export_defaults_to_non_partial(self):
        import agents.lead_dashboard_agent as dash

        def healthy_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            lead = {
                "domain": "complete-lead.com", "url": "https://complete-lead.com", "title": "Complete Co",
                "final_lead_score": 80, "best_phone": "555-2", "emails": [],
            }
            if on_candidate:
                on_candidate(lead)
            return {"leads": [lead], "count": 1}

        job_id = self._make_owned_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", healthy_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertTrue(final_job["export_file"])
        self.assertFalse(dash._leadbot_export_is_partial(final_job["export_file"]))

    def test_old_export_with_no_metadata_record_defaults_to_non_partial(self):
        import agents.lead_dashboard_agent as dash

        self.assertFalse(dash._leadbot_export_is_partial("leads_never_recorded_by_any_job.csv"))

    def test_existing_owner_fields_still_recorded_alongside_partial_flag(self):
        import agents.lead_dashboard_agent as dash
        import json

        dash.record_export_owner("leads_x.csv", owner_email="a@example.com", owner_username="a", is_partial=True)
        data = json.loads(Path("data/leadbot_export_owners.json").read_text())
        self.assertEqual(data["leads_x.csv"]["email"], "a@example.com")
        self.assertEqual(data["leads_x.csv"]["owner_username"], "a")
        self.assertTrue(data["leads_x.csv"]["partial"])


class PartialUiWordingTests(unittest.TestCase):
    """The polling JS shipped with the live-scan page (app/main.py) must
    gate the three complete-success phrases behind job.partial !== true,
    and offer the approved partial-specific phrases inside that guard.
    Since the actual job.partial value is only known at runtime (fetched
    client-side via /lead-bot/live-status/{job_id}), this checks the
    served page's static JS source/structure -- the same rigor level the
    existing LiveScanPageProviderFailureCopyTests in
    scripts/test_search_provider_failure_surfacing.py uses for the
    provider-failure copy, not full browser DOM simulation."""

    PARTIAL_PRIMARY = "Partial results are ready."
    PARTIAL_SECONDARY = "Some searches could not be completed, but the leads found so far are available."
    PARTIAL_EXPORT_LINE = "Your partial export is ready."
    COMPLETE_PRIMARY = "Scan complete. Your export is ready."
    COMPLETE_SECONDARY = "Search complete."
    COMPLETE_EXPORT_LINE = "Dashboard is ready."

    @classmethod
    def setUpClass(cls):
        import tempfile
        import agents.auth_agent as auth_agent
        import app.main as appmain
        from fastapi.testclient import TestClient

        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.auth_db_path = Path(cls._tmpdir.name) / "test_auth.db"
        cls._db_patch = mock.patch.object(auth_agent, "AUTH_DB", cls.auth_db_path)
        cls._db_patch.start()
        auth_agent.init_auth_db()
        auth_agent.create_user("uiuser1", "correct-horse-battery-staple", role="standard", email="uiuser1@example.com")

        cls.client = TestClient(appmain.app)
        user = auth_agent.get_user_by_username("uiuser1")
        token = auth_agent.create_session(user)
        cls.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

    @classmethod
    def tearDownClass(cls):
        cls._db_patch.stop()
        cls._tmpdir.cleanup()

    def setUp(self):
        job_id = "ui-" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "done",
            "message": PARTIAL_MESSAGE,
            "partial": True,
            "params": {"guest_id": "", "owner_email": "uiuser1@example.com", "owner_username": "uiuser1"},
            "leads": [{"domain": "x.com"}],
            "counts": {"found": 1, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self.addCleanup(lambda: job_agent.job_path(job_id).unlink(missing_ok=True))
        self.job_id = job_id

    def test_partial_wording_present_and_gated_behind_job_partial_check(self):
        resp = self.client.get(f"/lead-bot/live/{self.job_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.text

        self.assertIn(self.PARTIAL_PRIMARY, body)
        self.assertIn(self.PARTIAL_SECONDARY, body)
        self.assertIn(self.PARTIAL_EXPORT_LINE, body)

        guard_index = body.find("job.partial === true")
        partial_text_index = body.find(self.PARTIAL_PRIMARY)
        self.assertNotEqual(guard_index, -1, "the job.partial === true guard must exist in the served JS")
        self.assertLess(guard_index, partial_text_index, "the partial wording must appear after (inside) the guard")

    def test_complete_success_phrases_are_gated_in_the_else_branch(self):
        resp = self.client.get(f"/lead-bot/live/{self.job_id}")
        body = resp.text

        partial_text_index = body.find(self.PARTIAL_PRIMARY)
        else_index = body.find("} else {", partial_text_index)
        complete_text_index = body.find(self.COMPLETE_PRIMARY)

        self.assertNotEqual(else_index, -1, "an else branch must follow the partial block")
        self.assertGreater(complete_text_index, else_index, "complete-success wording must be in the else branch, after the partial block")

    def test_no_provider_or_circuit_breaker_details_in_served_page(self):
        resp = self.client.get(f"/lead-bot/live/{self.job_id}")
        body_lower = resp.text.lower()
        for forbidden in ["dataforseo", "circuit breaker", "40101", "40102", "40103", "serper"]:
            self.assertNotIn(forbidden, body_lower)


class NormalCompletedJobRetainsExistingWordingTests(unittest.TestCase):
    """A completed job with no partial flag at all (the common case, and
    every pre-existing job file) must keep showing the exact existing
    complete-success wording, unchanged."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import agents.auth_agent as auth_agent
        import app.main as appmain
        from fastapi.testclient import TestClient

        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.auth_db_path = Path(cls._tmpdir.name) / "test_auth.db"
        cls._db_patch = mock.patch.object(auth_agent, "AUTH_DB", cls.auth_db_path)
        cls._db_patch.start()
        auth_agent.init_auth_db()
        auth_agent.create_user("uiuser2", "correct-horse-battery-staple", role="standard", email="uiuser2@example.com")

        cls.client = TestClient(appmain.app)
        user = auth_agent.get_user_by_username("uiuser2")
        token = auth_agent.create_session(user)
        cls.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

    @classmethod
    def tearDownClass(cls):
        cls._db_patch.stop()
        cls._tmpdir.cleanup()

    def test_job_with_no_partial_key_at_all_still_serves_complete_wording_gate(self):
        """Simulates a job file written before this fix existed -- no
        "partial" key present at all."""
        job_id = "ui-legacy-" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "done",
            "message": "Done. Open Desktop is ready.",
            "params": {"guest_id": "", "owner_email": "uiuser2@example.com", "owner_username": "uiuser2"},
            "leads": [{"domain": "x.com"}],
            "counts": {"found": 1, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self.addCleanup(lambda: job_agent.job_path(job_id).unlink(missing_ok=True))

        resp = self.client.get(f"/lead-bot/live/{job_id}")
        self.assertEqual(resp.status_code, 200)
        # The served JS is static regardless of job_id -- confirms the
        # complete-success phrases are still shipped (the "job.partial
        # === true" strict check means a job with no "partial" key at all
        # evaluates falsy and falls through to this branch at runtime).
        self.assertIn("Scan complete. Your export is ready.", resp.text)
        self.assertIn("job.partial === true", resp.text)


class CircuitBreakerInteractionTests(_PartialResultsTestCase):
    """Confirms a circuit_breaker_open failure participates in the
    partial-results accounting exactly like any other provider failure,
    and that the partial-results loop cannot bypass, reset, or weaken the
    breaker -- the two features only ever communicate through the
    existing PROVIDER_UNAVAILABLE_MARKER/SearchProviderUnavailableError
    chain, with no direct coupling."""

    def test_run_job_never_references_circuit_breaker_internals(self):
        import inspect

        source = inspect.getsource(job_agent)
        self.assertNotIn("circuit_breaker", source.lower())

    def test_circuit_breaker_open_counts_as_a_provider_failure_in_partial_accounting(self):
        # This test's own state mutation is isolated by the base class's
        # _isolate_circuit_breaker_state() (reset before, restored after)
        # -- see _PartialResultsTestCase.setUp().
        import agents.dataforseo_serp_agent as dfs

        # Force the circuit open directly (unit-level control), rather
        # than exercising the full 3-exhaustion sequence -- that sequence
        # itself is covered by scripts/test_dataforseo_circuit_breaker.py.
        dfs._circuit_breaker_state["opened_until"] = dfs._circuit_breaker_now() + 120

        call_count = {"n": 0}

        def flaky_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                lead = {
                    "domain": "first-lead.com", "url": "https://first-lead.com", "title": "First Co",
                    "final_lead_score": 90, "best_phone": "555-1", "emails": [],
                }
                if on_candidate:
                    on_candidate(lead)
                return {"leads": [lead], "count": 1}
            # Simulate what actually happens when the second query reaches
            # the (now open) breaker: SearchProviderUnavailableError, same
            # as any other provider failure from run_job()'s perspective.
            raise bcf.SearchProviderUnavailableError("DataForSEO circuit breaker is open")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", flaky_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertTrue(final_job["partial"])
        domains = [lead.get("domain") for lead in final_job["leads"]]
        self.assertIn("first-lead.com", domains)


if __name__ == "__main__":
    unittest.main()
