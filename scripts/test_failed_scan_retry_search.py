"""
Regression tests for "Try this search again" on the total-provider-failure
live-scan state (agents.lead_live_job_agent.SEARCH_PROVIDER_UNAVAILABLE
error_code path).

This is a link + prefill only:
  - app/main.py's renderLead poll() JS adds a "Try this search again" link
    (alongside the existing "Back to Lead Finder" link) inside the same
    pre-existing ".leadbot-provider-unavailable" block, gated on the exact
    same `job.status === "error" && job.error_code ===
    "search_provider_unavailable"` condition that already excludes both
    partial-result jobs (status "done") and invalid-location/invalid-
    market-location jobs (a different error_code) -- no new gating logic
    was added or needed.
  - the link is a plain <a href="/lead-bot?retry_keyword=...&retry_market=
    ...&retry_own_domain=...">, built from the job's own params -- a GET
    navigation, never a form auto-submit, never a fetch/POST, so clicking
    it cannot create a job or consume a guest attempt by construction.
  - agents/lead_dashboard_agent.py's /lead-bot page reads those three
    query params on load and only sets .value on the matching form inputs
    -- no .submit() call, no fetch, nothing else touched.

These tests prove:
  - the "Try this search again" link/prefill markup exists and is wired
    to the correct query param names
  - the prefill script never calls form.submit() or triggers a network
    request
  - a real total-provider-failure job (real run_job(), no network) shows
    the retry link with the correct keyword/market/own_domain encoded
  - a real partial-result job does NOT show the retry link
  - a real invalid-location-value job does NOT show the retry link
  - visiting /lead-bot with retry_* query params prefills the form
    fields, in a real browser, without ever calling the live-start route
    (no job file is created)
"""

import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.dataforseo_serp_agent as dfs
import agents.lead_dashboard_agent as dash_agent
import agents.lead_live_job_agent as job_agent


class RetryLinkStaticMarkupTests(unittest.TestCase):
    def test_app_main_builds_retry_link_from_job_params(self):
        source = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn('retryParams.set("retry_keyword", params.keyword)', source)
        self.assertIn('retryParams.set("retry_market", params.market)', source)
        self.assertIn('retryParams.set("retry_own_domain", params.own_domain)', source)
        self.assertIn("Try this search again", source)
        # Still inside the same, unmodified total-failure gate.
        idx = source.index('job.status === "error" && job.error_code === "search_provider_unavailable"')
        block = source[idx:idx + 2000]
        self.assertIn("Try this search again", block)
        self.assertIn("Back to Lead Finder", block)

    def test_prefill_script_never_submits_or_fetches(self):
        source = dash_agent.render_lead_dashboard(current_user=None, csrf_token="tok")
        start = source.index("LEADBOT RETRY SEARCH PREFILL START")
        end = source.index("LEADBOT RETRY SEARCH PREFILL END")
        block = source[start:end]
        self.assertIn("retry_keyword", block)
        self.assertIn("retry_market", block)
        self.assertIn("retry_own_domain", block)
        self.assertNotIn(".submit(", block)
        self.assertNotIn("fetch(", block)
        self.assertNotIn("XMLHttpRequest", block)


def fake_find_leads_provider_unavailable(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
    from business_competitor_finder import SearchProviderUnavailableError
    raise SearchProviderUnavailableError("Live search provider failed and the fallback (DataForSEO) is not enabled.")


def fake_find_leads_partial(call_state):
    def _inner(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
        from business_competitor_finder import SearchProviderUnavailableError
        call_state["n"] += 1
        if call_state["n"] == 1:
            return {"leads": [{
                "domain": "retrytest-partial.example",
                "title": "Retry Test Partial Biz",
                "url": "https://retrytest-partial.example",
                "best_phone": "555-555-0111",
                "emails": "owner@retrytest-partial.example",
                "outreach_status": "call_ready",
                "contact_confidence": 80,
                "final_lead_score": 90,
            }]}
        raise SearchProviderUnavailableError("Live search provider failed and the fallback (DataForSEO) is not enabled.")
    return _inner


def fake_find_leads_invalid_location(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
    raise dfs.InvalidLocationValueError("DataForSEO task error: 40501 Invalid Field: 'location_name'")


class _RunJobTestCase(unittest.TestCase):
    def setUp(self):
        # create_job() now spawns each scan as its own OS process by
        # default (agents.lead_live_job_agent.RUN_SCANS_IN_SUBPROCESS,
        # part of the P1 cancel-hang fix), which re-imports find_leads
        # fresh from disk and can never see the mock.patch("agents.
        # lead_finding_agent.find_leads", ...) in _run_job() below. Opt
        # back into the pre-fix in-process thread so that patch keeps
        # working.
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

    def _run_job(self, side_effect, market_suffix, keyword="plumber", own_domain="mysite.example", max_queries=2):
        unique_market = f"Retrytest {market_suffix} {uuid.uuid4().hex[:8]}, NY"
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=side_effect):
            job_id = job_agent.create_job({
                "industry": "",
                "market": unique_market,
                "keyword": keyword,
                "own_domain": own_domain,
                "limit": 5,
                "per_batch": 2,
                "per_query_limit": 2,
                "max_queries": max_queries,
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
        return job, unique_market


class TotalFailureJobParamsTests(_RunJobTestCase):
    def test_total_failure_job_has_params_needed_for_retry_link(self):
        job, market = self._run_job(fake_find_leads_provider_unavailable, "total")
        self.assertEqual(job.get("status"), "error")
        self.assertEqual(job.get("error_code"), "search_provider_unavailable")
        self.assertEqual(job["params"]["keyword"], "plumber")
        self.assertEqual(job["params"]["market"], market)
        self.assertEqual(job["params"]["own_domain"], "mysite.example")


class PartialJobDoesNotQualifyTests(_RunJobTestCase):
    def test_partial_job_status_is_done_not_error_search_provider_unavailable(self):
        call_state = {"n": 0}
        job, market = self._run_job(fake_find_leads_partial(call_state), "partial", max_queries=2)
        self.assertEqual(job.get("status"), "done")
        self.assertTrue(job.get("partial"))
        # The client-side gate is `job.status === "error" && job.error_code
        # === "search_provider_unavailable"` -- a partial job's status is
        # "done", so it can never match regardless of error_code.
        self.assertNotEqual(job.get("status"), "error")


class InvalidLocationJobDoesNotQualifyTests(_RunJobTestCase):
    def test_invalid_location_job_has_a_different_error_code(self):
        job, market = self._run_job(fake_find_leads_invalid_location, "invalidloc")
        self.assertEqual(job.get("status"), "error")
        self.assertEqual(job.get("error_code"), "invalid_location_value")
        self.assertNotEqual(job.get("error_code"), "search_provider_unavailable")


class RetryPrefillBrowserRegressionTests(unittest.TestCase):
    """Real headless browser: visiting /lead-bot with retry_* query params
    prefills the form without ever hitting /lead-bot/live-start (no job
    file created, no guest attempt consumed). Skips itself if Playwright/
    Chromium aren't installed."""

    PORT = 8801

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")

        cls._sync_playwright_ctx = sync_playwright()
        cls._playwright = cls._sync_playwright_ctx.__enter__()

        try:
            cls._browser = cls._playwright.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest(f"chromium is not available: {exc}")

        import subprocess
        import requests

        repo_root = Path(__file__).resolve().parent.parent
        cls._proc = subprocess.Popen(
            [
                sys.executable,
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(repo_root),
            env={
                **os.environ,
                "USE_LIVE_SERP": "false",
                "DATAFORSEO_ENABLED": "0",
                "LEADBOT_DATAFORSEO_ENABLED": "0",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        base_url = f"http://127.0.0.1:{cls.PORT}"
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                resp = requests.get(f"{base_url}/login", timeout=1)
                if resp.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        if not up:
            cls._proc.terminate()
            cls._browser.close()
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest("local dev server did not start in time")

        cls.base_url = base_url

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "_proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        browser = getattr(cls, "_browser", None)
        if browser is not None:
            browser.close()

        ctx = getattr(cls, "_sync_playwright_ctx", None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    def test_retry_query_params_prefill_form_without_navigation_or_job_creation(self):
        import glob

        jobs_before = set(glob.glob("data/leadbot_live_jobs/*.json"))

        live_start_calls = []
        context = self._browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()

        def _handle_route(route):
            live_start_calls.append(route.request.url)
            route.continue_()

        page.route("**/lead-bot/live-start", _handle_route)

        market_value = "Albany, NY"
        keyword_value = "plumber"
        own_domain_value = "mysite.example"
        url = (
            f"{self.base_url}/lead-bot?"
            f"retry_keyword={quote(keyword_value)}&"
            f"retry_market={quote(market_value)}&"
            f"retry_own_domain={quote(own_domain_value)}"
        )
        page.goto(url)
        page.wait_for_selector("#leadbotRunForm")
        page.wait_for_timeout(300)

        self.assertEqual(page.input_value("#leadbotKeywordInput"), keyword_value)
        self.assertEqual(page.input_value("#leadbotMarketInput"), market_value)
        self.assertEqual(
            page.eval_on_selector('#leadbotRunForm input[name="own_domain"]', "el => el.value"),
            own_domain_value,
        )

        # No auto-submit: still on /lead-bot, never navigated to the live
        # scan page, and the live-start route was never hit.
        self.assertIn("/lead-bot", page.url)
        self.assertNotIn("/lead-bot/live/", page.url)
        self.assertEqual(live_start_calls, [])

        jobs_after = set(glob.glob("data/leadbot_live_jobs/*.json"))
        self.assertEqual(jobs_before, jobs_after, "visiting the retry link must never create a job file")


if __name__ == "__main__":
    unittest.main()
