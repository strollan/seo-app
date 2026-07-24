"""
Regression tests for restoring a short, factual "Why this lead" note to
Lead Finder result cards (dashboard cards and live-scan cards), after the
old long/inaccurate explanation generator was removed entirely (see
scripts/test_lead_explanation_removed.py).

This is a deliberately narrow re-introduction: a compact two-line note
built only from data already on the row/job (keyword, market, whether a
phone/email was found) -- never scored, never speculative, never routed
through the deleted agents/lead_reason_agent.py or the old
build_lead_reason()/reasons-list logic in agents/lead_finding_agent.py.

These tests prove:
  - agents.lead_dashboard_agent.leadbot_why_note_lines() produces the
    exact required copy for every input combination (keyword+market
    present, either missing, malformed/junk values, contact present vs
    missing), and never leaks a blank/None/malformed keyword or market
  - the dashboard card renderer (lead_cards()) renders the "Why this
    lead" heading exactly once per card, with exactly two body lines,
    using the correct contact-found/contact-missing wording, and no
    trace of the old removed speculative copy
  - the CSV export path still has no explanation column (EXPORT_FIELDS
    and a real export_leads_to_csv() call)
  - agents/lead_reason_agent.py remains deleted
  - a real end-to-end live scan (create_job()/run_job(), no network)
    renders the note correctly in a real browser DOM for both a lead
    with contact details and one without, and does so identically
    whether the job is partial or a full completion -- proving the JS
    renderLead() path (which needed job.params threaded through, since
    keyword/market live on the job, not on each lead) actually works,
    not just the Python side

agents.lead_finding_agent.find_leads is monkeypatched in the live-scan
tests so no real Serper/DataForSEO call is ever made. Playwright/Chromium
tests skip themselves if unavailable rather than failing the suite.
"""

import re
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.lead_dashboard_agent as dash_agent
import agents.lead_export_agent as export_agent
import agents.lead_live_job_agent as job_agent

# Fragments of the old, deliberately-removed generator's speculative/scored
# copy -- must never reappear now that a note has been restored.
OLD_SPECULATIVE_FRAGMENTS = [
    "Direct business domain:",
    "Matches industry signals:",
    "Contains possible wrong-industry terms",
    "Looks like a real service business.",
    "Sweet spot: ranking around position",
    "strong opportunity",
    "likely needs help",
    "poor SEO",
    "high-value lead",
]


class WhyNoteLinesHelperTests(unittest.TestCase):
    def test_keyword_and_market_present_produces_specific_line(self):
        line1, line2 = dash_agent.leadbot_why_note_lines("plumber", "Albany, NY", True)
        self.assertEqual(line1, "Matches your search for plumber in Albany, NY.")
        self.assertEqual(line2, "Website and available contact details are ready to review.")

    def test_missing_keyword_falls_back(self):
        line1, _ = dash_agent.leadbot_why_note_lines("", "Albany, NY", True)
        self.assertEqual(line1, "Matches the search used for this scan.")

    def test_missing_market_falls_back(self):
        line1, _ = dash_agent.leadbot_why_note_lines("plumber", "", True)
        self.assertEqual(line1, "Matches the search used for this scan.")

    def test_malformed_values_fall_back(self):
        for junk in ["None", "null", "NaN", "unknown", "N/A", "  ", "not found"]:
            with self.subTest(junk=junk):
                line1, _ = dash_agent.leadbot_why_note_lines(junk, "Albany, NY", True)
                self.assertEqual(line1, "Matches the search used for this scan.")
                line1b, _ = dash_agent.leadbot_why_note_lines("plumber", junk, True)
                self.assertEqual(line1b, "Matches the search used for this scan.")

    def test_contact_found_wording(self):
        _, line2 = dash_agent.leadbot_why_note_lines("plumber", "Albany, NY", True)
        self.assertEqual(line2, "Website and available contact details are ready to review.")

    def test_contact_missing_wording(self):
        _, line2 = dash_agent.leadbot_why_note_lines("plumber", "Albany, NY", False)
        self.assertEqual(line2, "A website was found; contact details may still need research.")


class DashboardCardWhyNoteRenderingTests(unittest.TestCase):
    def _row(self, extra=None):
        row = {
            "domain": "acmeplumbing.example",
            "title": "Acme Plumbing Co",
            "url": "https://acmeplumbing.example",
            "best_phone": "555-555-0100",
            "emails": "owner@acmeplumbing.example",
            "outreach_status": "call_ready",
            "score": "90",
            "final_lead_score": "90",
            "keyword": "plumber",
            "market": "Albany, NY",
        }
        if extra:
            row.update(extra)
        return row

    def _why_note_body(self, html_out):
        match = re.search(
            r'<div class="leadbot-why-note">(.*?)</div>', html_out, re.S
        )
        self.assertIsNotNone(match, "leadbot-why-note wrapper not found")
        return match.group(1)

    def test_heading_appears_exactly_once(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        self.assertEqual(html_out.count("Why this lead"), 1)

    def test_body_has_exactly_two_lines(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        body = self._why_note_body(html_out)
        self.assertEqual(body.count("<p>"), 2)

    def test_contact_found_wording_when_phone_and_email_present(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        body = self._why_note_body(html_out)
        self.assertIn("Website and available contact details are ready to review.", body)
        self.assertNotIn("may still need research", body)

    def test_contact_missing_wording_when_no_phone_or_email(self):
        html_out = dash_agent.lead_cards(
            [self._row(extra={"best_phone": "", "emails": ""})],
            selected_name="test.csv",
            csrf_token="tok",
        )
        body = self._why_note_body(html_out)
        self.assertIn("A website was found; contact details may still need research.", body)

    def test_missing_keyword_market_uses_safe_fallback(self):
        html_out = dash_agent.lead_cards(
            [self._row(extra={"keyword": "", "market": ""})],
            selected_name="test.csv",
            csrf_token="tok",
        )
        body = self._why_note_body(html_out)
        self.assertIn("Matches the search used for this scan.", body)
        self.assertNotIn("in .", body)

    def test_specific_search_line_uses_real_keyword_and_market(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        body = self._why_note_body(html_out)
        self.assertIn("Matches your search for plumber in Albany, NY.", body)

    def test_no_old_speculative_text_returns(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        for fragment in OLD_SPECULATIVE_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, html_out)

    def test_no_empty_why_note_wrapper(self):
        html_out = dash_agent.lead_cards([self._row()], selected_name="test.csv", csrf_token="tok")
        self.assertNotIn('<div class="leadbot-why-note">\n            </div>', html_out)
        self.assertNotIn('<div class="leadbot-why-note"></div>', html_out)


class CsvExportHasNoExplanationColumnTests(unittest.TestCase):
    def setUp(self):
        self._written_paths = []

    def tearDown(self):
        for p in self._written_paths:
            try:
                Path(p).unlink()
            except FileNotFoundError:
                pass

    def test_export_fields_has_no_reason_or_why_column(self):
        for field in export_agent.EXPORT_FIELDS:
            self.assertNotIn("reason", field.lower())
            self.assertNotIn("why", field.lower())
            self.assertNotIn("explanation", field.lower())

    def test_real_csv_export_has_no_why_note_column(self):
        result = {
            "leads": [
                {
                    "domain": "acmeplumbing.example",
                    "title": "Acme Plumbing Co",
                    "url": "https://acmeplumbing.example",
                    "best_phone": "555-555-0100",
                    "emails": "owner@acmeplumbing.example",
                    "outreach_status": "call_ready",
                    "score": 90,
                    "final_lead_score": 90,
                }
            ]
        }
        export_result = export_agent.export_leads_to_csv(
            result, industry="plumbing", market="albany", only_outreach_ready=True
        )
        path = Path(export_result["path"])
        self._written_paths.append(path)

        header = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("reason", header.lower())
        self.assertNotIn("why", header.lower())


class OldReasonGeneratorStillDeletedTests(unittest.TestCase):
    def test_lead_reason_agent_file_does_not_exist(self):
        repo_root = Path(__file__).resolve().parent.parent
        self.assertFalse((repo_root / "agents" / "lead_reason_agent.py").exists())

    def test_lead_finding_agent_has_no_build_lead_reason(self):
        import agents.lead_finding_agent as finding_agent
        self.assertFalse(hasattr(finding_agent, "build_lead_reason"))


CANNED_LEAD_WITH_CONTACT = {
    "domain": "livewhynote-contact.example",
    "url": "https://livewhynote-contact.example",
    "title": "Live Why Note Contact Plumbing",
    "best_phone": "555-555-0122",
    "emails": "owner@livewhynote-contact.example",
    "outreach_status": "call_ready",
    "contact_confidence": 80,
    "final_lead_score": 90,
}

CANNED_LEAD_NO_CONTACT = {
    "domain": "livewhynote-nocontact.example",
    "url": "https://livewhynote-nocontact.example",
    "title": "Live Why Note No Contact Plumbing",
    "best_phone": "",
    "emails": "",
    "outreach_status": "needs_manual_research",
    "contact_confidence": 0,
    "final_lead_score": 55,
}


def fake_find_leads_both(industry, market, service_keyword=None, own_domain=None, limit=10):
    return {"leads": [dict(CANNED_LEAD_WITH_CONTACT), dict(CANNED_LEAD_NO_CONTACT)]}


class LiveScanWhyNoteBrowserRegressionTests(unittest.TestCase):
    """Drives a real headless browser against a real uvicorn subprocess so
    the client-side renderLead()/leadbotWhyNoteLines() JS actually
    executes -- a route-only TestClient test cannot observe JS-generated
    DOM content. Skips itself if Playwright/Chromium aren't installed.

    Uses a real create_job()/run_job() pass with agents.lead_finding_agent
    .find_leads monkeypatched, so no network call or paid scan ever
    happens. Real job/export files are removed in cleanup.
    """

    PORT = 8793

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
                str(repo_root / "venv" / "bin" / "python3"),
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

    def setUp(self):
        import agents.auth_agent as auth_agent
        self.auth_agent = auth_agent

        suffix = uuid.uuid4().hex[:10]
        self.username = f"whynotetest_{suffix}"
        self.password = "correct-horse-battery-staple"
        auth_agent.create_user(self.username, self.password, role="standard", email=f"{self.username}@example.com")
        self.addCleanup(self._delete_user)

        self._created_job_ids = []
        self._created_export_names = []
        self.addCleanup(self._cleanup_jobs_and_exports)

    def _delete_user(self):
        import sqlite3
        try:
            conn = sqlite3.connect(self.auth_agent.AUTH_DB)
            conn.execute("DELETE FROM users WHERE username = ?", (self.username,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _cleanup_jobs_and_exports(self):
        for job_id in self._created_job_ids:
            try:
                job_agent.job_path(job_id).unlink()
            except FileNotFoundError:
                pass
        for export_name in self._created_export_names:
            for p in (Path("exports") / export_name, Path("exports") / f"{export_name}.owner.json"):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

            owner_index_path = Path("data/leadbot_export_owners.json")
            if owner_index_path.exists():
                try:
                    import json
                    data = json.loads(owner_index_path.read_text(encoding="utf-8") or "{}")
                except Exception:
                    data = {}
                if export_name in data:
                    data.pop(export_name, None)
                    owner_index_path.write_text(
                        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
                    )

    def _run_job(self, partial):
        unique_market = f"Whynotetest {uuid.uuid4().hex[:8]}, NY"
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_both):
            job_id = job_agent.create_job({
                "industry": "",
                "market": unique_market,
                "keyword": "plumber",
                "own_domain": "",
                "limit": 2,
                "per_batch": 2,
                "per_query_limit": 2,
                "max_queries": 1,
                "owner_email": f"{self.username}@example.com",
                "owner_username": self.username,
                "owner_role": "standard",
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
        self.assertEqual(job.get("status"), "done", f"job did not finish cleanly: {job}")

        if partial:
            job["partial"] = True
            job_agent.write_job(job)

        if job.get("export_file"):
            self._created_export_names.append(job["export_file"])

        return job_id, unique_market

    def _login_and_get_page(self):
        context = self._browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base_url}/login")
        page.fill('input[name="username"]', self.username)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        return page

    def _assert_why_notes_render_correctly(self, page, job_id, market):
        page.goto(f"{self.base_url}/lead-bot/live/{job_id}")
        page.wait_for_selector(".leadbot-why-note", timeout=15000)
        page.wait_for_timeout(500)

        notes = page.query_selector_all(".leadbot-why-note")
        self.assertEqual(len(notes), 2, "expected exactly one why-note per rendered card")

        headings = page.query_selector_all(".leadbot-why-note b")
        for heading in headings:
            self.assertEqual(heading.inner_text().strip(), "Why this lead")

        for note in notes:
            paragraphs = note.query_selector_all("p")
            self.assertEqual(len(paragraphs), 2, "each why-note must have exactly two body lines")

        cards_text = page.content()
        self.assertIn(f"Matches your search for plumber in {market}.", cards_text)
        self.assertIn("Website and available contact details are ready to review.", cards_text)
        self.assertIn("A website was found; contact details may still need research.", cards_text)

        for fragment in OLD_SPECULATIVE_FRAGMENTS:
            self.assertNotIn(fragment, cards_text)

    def test_complete_job_renders_why_notes_correctly(self):
        job_id, market = self._run_job(partial=False)
        page = self._login_and_get_page()
        self._assert_why_notes_render_correctly(page, job_id, market)

    def test_partial_job_renders_why_notes_correctly(self):
        job_id, market = self._run_job(partial=True)
        page = self._login_and_get_page()
        self._assert_why_notes_render_correctly(page, job_id, market)

    def test_mobile_viewport_markup_has_no_oversized_empty_wrapper(self):
        job_id, market = self._run_job(partial=False)
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base_url}/login")
        page.fill('input[name="username"]', self.username)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(f"{self.base_url}/lead-bot/live/{job_id}")
        page.wait_for_selector(".leadbot-why-note", timeout=15000)
        page.wait_for_timeout(500)

        notes = page.query_selector_all(".leadbot-why-note")
        self.assertEqual(len(notes), 2)
        for note in notes:
            box = note.bounding_box()
            self.assertIsNotNone(box)
            # A compact two-line note must never balloon into a large
            # empty-looking panel on a narrow mobile viewport.
            self.assertLess(box["height"], 120, "why-note wrapper is unexpectedly tall on mobile")


if __name__ == "__main__":
    unittest.main()
