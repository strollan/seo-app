"""
Regression tests for surfacing live search-provider failures instead of
silently masking them as a genuine zero-result scan.

Root cause this fixes: business_competitor_finder._raw_find_business_competitors()
caught any Serper exception locally and fell back to
_leadbot_non_serper_search() (DataForSEO) -- which, with
LEADBOT_DATAFORSEO_ENABLED at its default of "0", returned [] immediately
with no exception ever propagating. agents.lead_live_job_agent.run_job()
then completed the job as status="done" with leads=[] and errors=[] --
indistinguishable from an honest "searched, found nothing" result. This was
confirmed as the cause of a production incident where every guest scan in
a window returned zero leads in ~1 second (see the incident investigation
in this session).

Fix: SearchProviderUnavailableError is now raised (not swallowed) when the
primary provider fails and no working fallback can produce results. A
provider that runs successfully and legitimately finds nothing is
unaffected -- it still returns [] normally.

Covers, in three layers:
  - business_competitor_finder._leadbot_non_serper_search() /
    _raw_find_business_competitors(): the exact raise-vs-return-[] decision
  - agents.lead_live_job_agent.run_job(): job state (status/error_code/
    message/export_file/preserved leads) when the provider fails
  - the rendered /lead-bot/live/{job_id} page: user-facing copy, and that
    it never leaks provider names/secrets/stack traces

Uses mocked providers only -- no live provider calls are made by this file.
"""

import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent
import agents.lead_live_job_agent as job_agent
import business_competitor_finder as bcf

VALID_PASSWORD = "correct-horse-battery-staple"

PROVIDER_FAILURE_MESSAGE = (
    "The lead search service is temporarily unavailable. This scan did not "
    "complete. Please try again shortly."
)

# Strings that must never reach a user-facing message or the rendered page
# for a provider failure -- provider names, secrets, raw errors.
FORBIDDEN_LEAK_STRINGS = (
    "Serper", "DataForSEO", "dataforseo", "serper",
    "Traceback", "api_key", "API_KEY", "sk-", "429",
)


class EnvVarSandbox(unittest.TestCase):
    """Base class: saves/restores the small set of env vars this feature
    reads, so tests can't leak configuration into each other or into the
    rest of the suite."""

    ENV_KEYS = ("USE_LIVE_SERP", "LEADBOT_DATAFORSEO_ENABLED", "DATAFORSEO_DEPTH")

    def setUp(self):
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NonSerperSearchStrictModeTests(EnvVarSandbox):
    """_leadbot_non_serper_search()'s raise_on_failure behavior in
    isolation -- the exact unit this fix changes."""

    def test_disabled_and_not_strict_returns_empty_unchanged(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "0"
        result = bcf._leadbot_non_serper_search("plumber", location="Test City", page=1)
        self.assertEqual(result, [])

    def test_disabled_and_strict_raises_provider_unavailable(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "0"
        with self.assertRaises(bcf.SearchProviderUnavailableError):
            bcf._leadbot_non_serper_search("plumber", location="Test City", page=1, raise_on_failure=True)

    def test_enabled_and_working_returns_results_when_strict(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        fake_results = [{"title": "A Plumber", "link": "https://a-plumber-test.com"}]
        with mock.patch("agents.dataforseo_serp_agent.search_google_organic", return_value=fake_results):
            result = bcf._leadbot_non_serper_search(
                "plumber", location="Test City", page=1, raise_on_failure=True
            )
        self.assertEqual(result, fake_results)

    def test_enabled_but_call_fails_raises_when_strict(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        with mock.patch("agents.dataforseo_serp_agent.search_google_organic", side_effect=RuntimeError("boom")):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._leadbot_non_serper_search(
                    "plumber", location="Test City", page=1, raise_on_failure=True
                )

    def test_enabled_but_call_fails_returns_empty_when_not_strict(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        with mock.patch("agents.dataforseo_serp_agent.search_google_organic", side_effect=RuntimeError("boom")):
            result = bcf._leadbot_non_serper_search("plumber", location="Test City", page=1)
        self.assertEqual(result, [])


class RawFindBusinessCompetitorsProviderFailureTests(EnvVarSandbox):
    """_raw_find_business_competitors(): the primary-provider-raises
    scenarios exactly as they occur in the real call path."""

    def setUp(self):
        super().setUp()
        os.environ["USE_LIVE_SERP"] = "true"

    def test_primary_raises_fallback_disabled_raises_not_empty_list(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "0"
        with mock.patch.object(bcf, "google_search", side_effect=RuntimeError("simulated Serper outage")):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])

    def test_primary_raises_fallback_enabled_and_working_succeeds_normally(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        fake_results = [{"title": "A Plumber", "link": "https://a-plumber-test.com", "snippet": "plumber services"}]
        with mock.patch.object(bcf, "google_search", side_effect=RuntimeError("simulated Serper outage")), \
             mock.patch("agents.dataforseo_serp_agent.search_google_organic", return_value=fake_results):
            results = bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])
        self.assertIsInstance(results, list)

    def test_primary_raises_fallback_enabled_but_also_fails_raises(self):
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        with mock.patch.object(bcf, "google_search", side_effect=RuntimeError("simulated Serper outage")), \
             mock.patch("agents.dataforseo_serp_agent.search_google_organic", side_effect=RuntimeError("dataforseo also down")):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])

    def test_primary_succeeds_with_zero_results_is_still_a_genuine_empty_result(self):
        """The critical non-regression: a provider that runs fine and
        legitimately finds nothing must NOT be turned into a failure."""
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "0"
        with mock.patch.object(bcf, "google_search", return_value={"organic": []}), \
             mock.patch.object(bcf, "get_organic_results", return_value=[]):
            results = bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])
        self.assertEqual(results, [])


class RawFindBusinessCompetitorsSerperDisabledTests(EnvVarSandbox):
    """
    _raw_find_business_competitors() with USE_LIVE_SERP=0 -- production's
    actual current configuration. DataForSEO is the *direct* (not fallback)
    path here (the `else` branch), which previously called
    _leadbot_non_serper_search() without raise_on_failure=True -- so a real
    DataForSEO failure with Serper disabled could return [] silently and
    look like a genuine zero-result scan, exactly the gap this fix closes.
    """

    def setUp(self):
        super().setUp()
        os.environ["USE_LIVE_SERP"] = "0"
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"

    def test_dataforseo_succeeds_with_results_and_serper_is_never_called(self):
        fake_results = [{"title": "A Plumber", "link": "https://a-plumber-test.com", "snippet": "plumber services"}]
        with mock.patch.object(bcf, "google_search") as mock_google_search, \
             mock.patch("agents.dataforseo_serp_agent.search_google_organic", return_value=fake_results):
            results = bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])

        self.assertIsInstance(results, list)
        mock_google_search.assert_not_called()

    def test_dataforseo_legitimate_zero_result_stays_a_valid_empty_result(self):
        with mock.patch.object(bcf, "google_search") as mock_google_search, \
             mock.patch("agents.dataforseo_serp_agent.search_google_organic", return_value=[]):
            results = bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])

        self.assertEqual(results, [])
        mock_google_search.assert_not_called()

    def test_dataforseo_failure_raises_provider_unavailable_not_empty_list(self):
        with mock.patch.object(bcf, "google_search") as mock_google_search, \
             mock.patch("agents.dataforseo_serp_agent.search_google_organic", side_effect=RuntimeError("simulated DataForSEO outage")):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._raw_find_business_competitors("plumber", location="Test City", limit=5, pages=[1])

        mock_google_search.assert_not_called()


class RunJobProviderFailureTests(unittest.TestCase):
    """agents.lead_live_job_agent.run_job(): job state when the provider
    fails vs. a genuine zero-result scan vs. an ordinary per-query error."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self._orig_job_dir = job_agent.JOB_DIR
        job_agent.JOB_DIR = Path(self.tmpdir)
        self.addCleanup(lambda: setattr(job_agent, "JOB_DIR", self._orig_job_dir))

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

    def _make_job(self, max_queries=1, guest_id=""):
        job_id = "provfail-" + uuid.uuid4().hex[:12]
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
                "guest_id": guest_id,
            },
            "cancel_requested": False,
            "updated_at": job_agent.now_iso(),
            "export_file": "",
        }
        job_agent.write_job(job)
        return job_id

    def test_provider_failure_marks_job_error_with_sanitized_code_and_message(self):
        def failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise bcf.SearchProviderUnavailableError("simulated total outage")

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", failing_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "error")
        self.assertEqual(final_job["error_code"], "search_provider_unavailable")
        self.assertEqual(final_job["message"], PROVIDER_FAILURE_MESSAGE)

    def test_provider_failure_creates_no_export(self):
        def failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise bcf.SearchProviderUnavailableError("simulated total outage")

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", failing_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["export_file"], "")
        self.assertEqual(self.export_calls, [])

    def test_provider_failure_message_leaks_no_provider_names_or_secrets(self):
        def failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise bcf.SearchProviderUnavailableError(
                "Serper 429 quota exceeded for key sk-abcdef1234 -- Traceback (most recent call last)"
            )

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", failing_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        for banned in FORBIDDEN_LEAK_STRINGS:
            self.assertNotIn(banned, final_job["message"])

    def test_partial_published_leads_survive_a_later_provider_failure(self):
        """Updated by the partial-results fix (scripts.
        test_partial_provider_results.py): a query's provider failure no
        longer aborts the whole scan as search_provider_unavailable when a
        different query already succeeded in the same job -- the scan now
        ends "done" with job["partial"] = True and the partial-results
        warning message, and the lead found by the successful query is
        still published. This test previously asserted the old
        total-abort behavior, which was exactly the bug that fix
        addresses. Full coverage of the partial-outcome behavior itself
        (export creation, zero-result variants, total-failure-unchanged,
        diagnostics) lives in test_partial_provider_results.py -- this
        test is kept here only to confirm the lead-survival guarantee in
        this file's existing fixture/mock style."""
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
        self.assertTrue(final_job["partial"])
        domains = [lead.get("domain") for lead in final_job["leads"]]
        self.assertIn("first-lead.com", domains, "a lead published before the failure must survive it")

    def test_genuine_zero_result_scan_is_unaffected_by_this_fix(self):
        def empty_but_healthy_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            return {"leads": [], "count": 0}

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", empty_but_healthy_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertNotIn("error_code", final_job)
        self.assertEqual(final_job["leads"], [])
        self.assertEqual(final_job["errors"], [])

    def test_ordinary_generic_search_error_keeps_its_existing_behavior(self):
        """A plain (non-provider-unavailable) exception from find_leads must
        keep the pre-existing per-query error + continue + eventual "done"
        behavior -- this fix only changes handling for the specific
        provider-unavailable signal, nothing else."""
        def generic_failing_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise RuntimeError("some unrelated bug")

        job_id = self._make_job(max_queries=1)
        with mock.patch("agents.lead_finding_agent.find_leads", generic_failing_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertTrue(any("some unrelated bug" in e for e in final_job["errors"]))


class LiveScanPageProviderFailureCopyTests(unittest.TestCase):
    """The rendered /lead-bot/live/{job_id} page for a provider-failure job:
    correct heading/message/link, distinct from the genuine zero-result
    copy, and no leaked provider names/secrets."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        self.client = TestClient(appmain.app)

        self._job_files_to_remove = []

    def tearDown(self):
        for path in self._job_files_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

    def _write_job(self, status, error_code=None, message="", leads=None):
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": status,
            "message": message,
            "params": {"guest_id": "", "owner_email": "user1@example.com", "owner_username": "user1"},
            "leads": leads if leads is not None else [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        if error_code is not None:
            job["error_code"] = error_code
        job_agent.write_job(job)
        self._job_files_to_remove.append(job_agent.job_path(job_id))
        return job_id

    def test_provider_failure_page_shows_required_heading_message_and_link(self):
        job_id = self._write_job(
            status="error", error_code="search_provider_unavailable", message=PROVIDER_FAILURE_MESSAGE
        )
        self.login(self.client, "user1")

        resp = self.client.get(f"/lead-bot/live/{job_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Lead search temporarily unavailable", resp.text)
        self.assertIn("This scan could not be completed. Please try again shortly.", resp.text)
        self.assertIn('href="/lead-bot"', resp.text)

    def test_provider_failure_and_zero_result_branches_are_mutually_exclusive_by_construction(self):
        """
        AGENT_NOTE: this repo has no browser/JS test runner (same
        limitation noted in scripts/test_guest_scan_rejection_messaging.py
        and scripts/test_export_delete_admin_and_advanced_settings.py).
        TestClient fetches the static page source, which contains the code
        for *every* possible job state (the branch actually taken is
        decided client-side, at runtime, from the polled job JSON) -- so
        asserting "genuine zero-result HTML doesn't contain the
        provider-failure string" against the static source is not a
        meaningful check; both strings are always present in the source
        for every job.

        What's actually verifiable here is the *condition* each branch is
        gated on: the provider-failure block only evaluates true when
        job.status === "error" AND job.error_code === "search_provider_unavailable",
        while the zero-result block is nested strictly inside
        job.status === "done". A job can't be both "error" and "done" at
        once, so these two branches can never both fire for the same job --
        proven structurally rather than by executing the JS.
        """
        job_id = self._write_job(
            status="error", error_code="search_provider_unavailable", message=PROVIDER_FAILURE_MESSAGE
        )
        self.login(self.client, "user1")
        resp = self.client.get(f"/lead-bot/live/{job_id}")
        source = resp.text

        provider_failure_gate = 'if (job.status === "error" && job.error_code === "search_provider_unavailable")'
        self.assertIn(provider_failure_gate, source)

        done_block_match = re.search(r'if \(job\.status === "done"\) \{(.*?)\n        \}\n\n        if \(job\.status === "error"', source, re.S)
        self.assertIsNotNone(done_block_match, "could not locate the done-status block containing the zero-result branch")
        self.assertIn("leadbotZeroResultsEmpty", done_block_match.group(1))
        self.assertIn("The scan completed, but no qualifying leads were found for this search.", done_block_match.group(1))

    def test_rendered_page_source_has_no_leaked_provider_names_or_secrets_in_new_copy(self):
        """The new provider-failure branch's literal user-facing copy must
        never mention a provider name or leak a secret-shaped string --
        checked on the branch's actual source text, not the whole page
        (which legitimately mentions DataForSEO elsewhere, e.g. the
        admin-only toggle button, out of scope for this fix)."""
        job_id = self._write_job(
            status="error", error_code="search_provider_unavailable", message=PROVIDER_FAILURE_MESSAGE
        )
        self.login(self.client, "user1")
        resp = self.client.get(f"/lead-bot/live/{job_id}")
        source = resp.text

        branch_match = re.search(
            r'if \(job\.status === "error" && job\.error_code === "search_provider_unavailable"\) \{(.*?)\n        \}\n\n        \(job\.leads',
            source,
            re.S,
        )
        self.assertIsNotNone(branch_match, "could not locate the provider-failure JS branch")
        branch_source = branch_match.group(1)
        self.assertIn("Lead search temporarily unavailable", branch_source)
        for banned in FORBIDDEN_LEAK_STRINGS:
            self.assertNotIn(banned, branch_source)


class LeadBotCacheHeaderTests(unittest.TestCase):
    """/lead-bot must return no-store/no-cache headers for every role."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        auth_agent.create_user("theadmin", VALID_PASSWORD, role="admin", email="admin@example.com")

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

    def _assert_no_cache_headers(self, resp):
        self.assertEqual(resp.headers.get("cache-control"), "no-store, private, max-age=0")
        self.assertEqual(resp.headers.get("pragma"), "no-cache")
        self.assertEqual(resp.headers.get("expires"), "0")

    def test_guest_gets_no_cache_headers(self):
        client = TestClient(appmain.app)
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        self._assert_no_cache_headers(resp)

    def test_standard_user_gets_no_cache_headers(self):
        client = TestClient(appmain.app)
        self.login(client, "user1")
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        self._assert_no_cache_headers(resp)

    def test_admin_gets_no_cache_headers(self):
        client = TestClient(appmain.app)
        self.login(client, "theadmin")
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        self._assert_no_cache_headers(resp)


if __name__ == "__main__":
    unittest.main()
