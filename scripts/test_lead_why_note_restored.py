"""
Regression tests for the "Why this lead" note on Lead Finder result cards
(dashboard cards and live-scan cards).

History: the old long/speculative explanation generator was removed
entirely (see scripts/test_lead_explanation_removed.py), then a compact
four-line factual note was restored (search, contact, website, verification).
This file now covers the *upgraded* version of that note: line 1 prefers
a real, already-validated SERP position when one exists; line 2 picks the
single most useful verified signal already available on the row/job
(empty-contact gap, then a genuine SEO content gap -- meta description or
page title missing -- then the specific contact-availability shape),
never scored, never speculative, never routed through the deleted
agents/lead_reason_agent.py or the old build_lead_reason()/reasons-list
logic in agents/lead_finding_agent.py.

Verified fields used (inspected before writing this upgrade):
  - serp_position / serp_page -- already validated by the pre-existing
    real_serp_value()/realSerpValue() guards (reject "manual", "?", "not
    found", "none", "null", "nan") in both the dashboard and live-scan
    paths, so line 1 reuses that same validated value rather than
    re-deriving its own.
  - keyword / market -- row CSV columns (dashboard) / job.params
    (live-scan), guarded against blank/None/junk values exactly as
    before.
  - best_phone / emails -- already used for the card's own Phone/Email
    fields and for calculate_contact_confidence().
  - meta_description -- dashboard: get_value() with no fallback to the
    business title, so a genuine absence is reliably detectable via the
    existing is_missing_meta_description() helper; live-scan: the same
    OR-chain used to build the SEO Snapshot's own meta-description
    display, mirrored by a new leadbotWhyNoteIsMissingMetaDescription()
    JS helper.
  - page_title -- dashboard: read WITHOUT the "or title" business-name
    fallback used for the SEO Snapshot's own display (that fallback
    would make "missing" undetectable), so a separate page_title_raw is
    used just for this signal; live-scan: metaTitle already has no such
    fallback.

These tests prove:
  - the heading appears exactly once per card, with exactly four body
    lines
  - line 1 uses a real SERP position when present, falls back to the
    keyword/market phrasing when position is unavailable, and falls back
    further to a fully generic line when keyword/market are unavailable
  - a fake/manual/null/unknown position, keyword, or market never leaks
    into the copy
  - line 2 correctly reflects contact-found / contact-missing /
    meta-description-missing / page-title-missing signals, choosing the
    single most useful one
  - none of the old speculative/scored phrases ever reappear
  - the CSV export path still has no explanation column
  - agents/lead_reason_agent.py remains deleted
  - a real end-to-end live scan (create_job()/run_job(), no network)
    renders the upgraded note correctly in a real browser DOM, and does
    so identically whether the job is partial or a full completion

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
# copy -- must never reappear.
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
    "likely needs SEO",
]


class WhyNoteLinesHelperTests(unittest.TestCase):
    def _call(self, keyword="plumber", market="Albany, NY", serp_position="", has_phone=True,
               has_email=True, meta_missing=False, title_missing=False,
               has_address=False, has_contact_page=False):
        return dash_agent.leadbot_why_note_lines(
            keyword=keyword,
            market=market,
            serp_position=serp_position,
            has_phone=has_phone,
            has_email=has_email,
            meta_description_missing=meta_missing,
            page_title_missing=title_missing,
            has_address=has_address,
            has_contact_page=has_contact_page,
        )

    def test_exactly_four_distinct_signal_categories(self):
        lines = self._call(serp_position="17", has_address=True, meta_missing=True)
        self.assertEqual(lines, (
            'Found for "plumber" in Albany, NY at position 17.',
            "Phone and email are available for outreach.",
            "Meta description is missing.",
            "A verified business address is available to help confirm the prospect.",
        ))
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(set(lines)), 4)

    def test_real_position_present_uses_specific_position_line(self):
        line1 = self._call(serp_position="7")[0]
        self.assertEqual(line1, 'Found for "plumber" in Albany, NY at position 7.')

    def test_missing_position_falls_back_to_keyword_market_line(self):
        line1 = self._call(serp_position="")[0]
        self.assertEqual(line1, 'Found in your "plumber" search for Albany, NY.')

    def test_fake_or_manual_position_never_appears(self):
        for junk in ["manual", "?", "not found", "None", "null", "NaN", "unknown", "outside page one", "-1", "0"]:
            with self.subTest(junk=junk):
                line1 = self._call(serp_position=junk)[0]
                self.assertNotIn(junk, line1)
                self.assertEqual(line1, 'Found in your "plumber" search for Albany, NY.')

    def test_missing_keyword_or_market_falls_back_fully(self):
        line1 = self._call(keyword="", serp_position="7")[0]
        self.assertEqual(line1, "Found during this Lead Finder scan.")
        line1b = self._call(market="", serp_position="7")[0]
        self.assertEqual(line1b, "Found during this Lead Finder scan.")

    def test_malformed_keyword_or_market_falls_back(self):
        for junk in ["None", "null", "NaN", "unknown", "N/A", "  ", "not found"]:
            with self.subTest(junk=junk):
                line1 = self._call(keyword=junk)[0]
                self.assertEqual(line1, "Found during this Lead Finder scan.")
                line1b = self._call(market=junk)[0]
                self.assertEqual(line1b, "Found during this Lead Finder scan.")

    def test_contact_permutations_are_factual(self):
        cases = [
            ((True, True), "Phone and email are available for outreach."),
            ((True, False), "Phone found, but no email was located."),
            ((False, True), "Email found, but no phone number was located."),
            ((False, False), "No direct contact details were found yet."),
        ]
        for (has_phone, has_email), expected in cases:
            with self.subTest(has_phone=has_phone, has_email=has_email):
                self.assertEqual(
                    self._call(has_phone=has_phone, has_email=has_email)[1],
                    expected,
                )

    def test_no_contact_does_not_replace_seo_line(self):
        line2, line3 = self._call(
            has_phone=False, has_email=False, meta_missing=True, title_missing=True
        )[1:3]
        self.assertEqual(line2, "No direct contact details were found yet.")
        self.assertEqual(line3, "Meta description is missing.")

    def test_meta_missing_is_selected_for_website_signal(self):
        self.assertEqual(self._call(meta_missing=True)[2], "Meta description is missing.")

    def test_title_missing_used_when_meta_present(self):
        self.assertEqual(
            self._call(meta_missing=False, title_missing=True)[2],
            "Page title information is missing.",
        )

    def test_title_and_meta_present_is_objective_website_signal(self):
        self.assertEqual(
            self._call(meta_missing=False, title_missing=False)[2],
            "Title and meta description are both present.",
        )

    def test_address_preferred_then_contact_page_then_safe_fallback(self):
        self.assertEqual(
            self._call(has_address=True, has_contact_page=True)[3],
            "A verified business address is available to help confirm the prospect.",
        )
        self.assertEqual(
            self._call(has_contact_page=True)[3],
            "A contact page was found for further review.",
        )
        self.assertEqual(
            self._call()[3],
            "Review the website to confirm fit before outreach.",
        )

    def test_no_speculative_phrases_in_any_combination(self):
        for has_phone in (True, False):
            for has_email in (True, False):
                for meta_missing in (True, False):
                    for title_missing in (True, False):
                        lines = self._call(
                            has_phone=has_phone, has_email=has_email,
                            meta_missing=meta_missing, title_missing=title_missing,
                        )
                        for fragment in OLD_SPECULATIVE_FRAGMENTS:
                            for line in lines:
                                self.assertNotIn(fragment, line)


class DashboardCardWhyNoteRenderingTests(unittest.TestCase):
    def _row(self, extra=None):
        row = {
            "domain": "acmeplumbing.example",
            "title": "Acme Plumbing Co",
            "url": "https://acmeplumbing.example",
            "best_phone": "555-555-0100",
            "emails": "owner@acmeplumbing.test",
            "outreach_status": "call_ready",
            "score": "90",
            "final_lead_score": "90",
            "keyword": "plumber",
            "market": "Albany, NY",
            "page_title": "Acme Plumbing | Official Site",
            "meta_description": "Fast, licensed plumbing service in Albany, NY.",
        }
        if extra:
            row.update(extra)
        return row

    def _cards(self, rows):
        return dash_agent.lead_cards(rows, selected_name="test.csv", csrf_token="tok")

    def _why_note_body(self, html_out):
        match = re.search(r'<div class="leadbot-why-note">(.*?)</div>', html_out, re.S)
        self.assertIsNotNone(match, "leadbot-why-note wrapper not found")
        return match.group(1)

    def test_heading_appears_exactly_once(self):
        html_out = self._cards([self._row()])
        self.assertEqual(html_out.count("Why this lead"), 1)

    def test_body_has_exactly_four_lines(self):
        html_out = self._cards([self._row()])
        body = self._why_note_body(html_out)
        self.assertEqual(body.count("<p>"), 4)

    def test_real_serp_position_used_when_available(self):
        html_out = self._cards([self._row(extra={"serp_position": "12"})])
        body = self._why_note_body(html_out)
        self.assertIn("in Albany, NY at position 12.", body)
        self.assertIn("plumber", body)

    def test_manual_or_null_position_never_appears(self):
        for junk in ["manual", "?", "not found", "None", "null"]:
            with self.subTest(junk=junk):
                html_out = self._cards([self._row(extra={"serp_position": junk})])
                body = self._why_note_body(html_out)
                self.assertNotIn(f"position {junk}", body)
                self.assertIn('Found in your', body)

    def test_missing_position_falls_back_safely(self):
        html_out = self._cards([self._row(extra={"serp_position": ""})])
        body = self._why_note_body(html_out)
        self.assertIn('Found in your', body)
        self.assertNotIn("at position", body)

    def test_contact_missing_wording_when_no_phone_or_email(self):
        html_out = self._cards([self._row(extra={"best_phone": "", "emails": ""})])
        body = self._why_note_body(html_out)
        self.assertIn("No direct contact details were found yet.", body)

    def test_seo_gap_signal_used_only_when_genuinely_present(self):
        # Meta description present (row default) -> not shown.
        html_out = self._cards([self._row()])
        body = self._why_note_body(html_out)
        self.assertNotIn("Meta description is missing.", body)
        self.assertIn("Title and meta description are both present.", body)

        # Meta description genuinely missing -> shown instead of the
        # (already-visible-elsewhere) contact-status line.
        html_out2 = self._cards([self._row(extra={"meta_description": ""})])
        body2 = self._why_note_body(html_out2)
        self.assertIn("Meta description is missing.", body2)

    def test_title_missing_signal(self):
        html_out = self._cards([self._row(extra={
            "page_title": "", "meta_title": "", "title": "Acme Plumbing Co",
        })])
        body = self._why_note_body(html_out)
        self.assertIn("Page title information is missing.", body)

    def test_address_then_contact_page_then_fourth_line_fallback(self):
        address_body = self._why_note_body(self._cards([self._row(extra={
            "address": "10 State St, Albany, NY",
            "contact_page_url": "https://acmeplumbing.example/contact",
        })]))
        self.assertIn("A verified business address is available", address_body)
        self.assertNotIn("A contact page was found", address_body)

        contact_body = self._why_note_body(self._cards([self._row(extra={
            "address": "", "contact_page_url": "https://acmeplumbing.example/contact",
        })]))
        self.assertIn("A contact page was found for further review.", contact_body)

        fallback_body = self._why_note_body(self._cards([self._row(extra={
            "address": "unknown", "contact_page_url": "null",
        })]))
        self.assertIn("Review the website to confirm fit before outreach.", fallback_body)
        self.assertNotIn(">unknown<", fallback_body)
        self.assertNotIn(">null<", fallback_body)

    def test_missing_keyword_market_uses_safe_fallback(self):
        html_out = self._cards([self._row(extra={"keyword": "", "market": ""})])
        body = self._why_note_body(html_out)
        self.assertIn("Found during this Lead Finder scan.", body)

    def test_null_unknown_values_never_leak(self):
        html_out = self._cards([self._row(extra={
            "keyword": "None", "market": "unknown", "serp_position": "null",
        })])
        body = self._why_note_body(html_out)
        self.assertNotIn("None", body)
        self.assertNotIn("null", body)
        self.assertIn("Found during this Lead Finder scan.", body)

    def test_no_old_speculative_text_returns(self):
        html_out = self._cards([self._row()])
        for fragment in OLD_SPECULATIVE_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, html_out)

    def test_no_empty_why_note_wrapper(self):
        html_out = self._cards([self._row()])
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
                    "emails": "owner@acmeplumbing.test",
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


CANNED_LEAD_WITH_CONTACT_AND_POSITION = {
    "domain": "livewhynote-contact.example",
    "url": "https://livewhynote-contact.example",
    "title": "Live Why Note Contact Plumbing",
    "best_phone": "555-555-0122",
    "emails": "owner@livewhynotecontact.test",
    "outreach_status": "call_ready",
    "contact_confidence": 80,
    "final_lead_score": 90,
    "serp_position": "9",
    "meta_description": "Live why note test meta description.",
    "page_title": "Live Why Note Contact Plumbing | Home",
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
    "serp_position": "",
}


def fake_find_leads_both(industry, market, service_keyword=None, own_domain=None, limit=10):
    return {"leads": [dict(CANNED_LEAD_WITH_CONTACT_AND_POSITION), dict(CANNED_LEAD_NO_CONTACT)]}


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

    def setUp(self):
        # job_agent.create_job() below now spawns each scan as its own OS
        # process by default (agents.lead_live_job_agent.
        # RUN_SCANS_IN_SUBPROCESS, part of the P1 cancel-hang fix), which
        # re-imports find_leads fresh from disk and can never see the
        # mock.patch("agents.lead_finding_agent.find_leads", ...) this test
        # relies on. Opt back into the pre-fix in-process thread so that
        # patch keeps working; the browser subprocess started in
        # setUpClass only ever reads the already-completed job file this
        # produces, it never runs find_leads itself.
        subprocess_patch = mock.patch.object(job_agent, "RUN_SCANS_IN_SUBPROCESS", False)
        subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

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
            self.assertEqual(len(paragraphs), 4, "each why-note must have exactly four body lines")

        cards_text = page.content()
        # Lead with a real SERP position -> specific position line.
        self.assertIn(f'Found for "plumber" in {market} at position 9.', cards_text)
        # Lead with no SERP position -> keyword/market fallback, never a
        # fake position.
        self.assertIn(f'Found in your "plumber" search for {market}.', cards_text)
        # Contact-with-SEO-data lead: meta description and title are both
        # present, so line 2 falls through to the contact-availability
        # shape.
        self.assertIn("Phone and email are available for outreach.", cards_text)
        self.assertIn("Title and meta description are both present.", cards_text)
        self.assertIn("Review the website to confirm fit before outreach.", cards_text)
        # No-contact lead.
        self.assertIn("No direct contact details were found yet.", cards_text)

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
            # A compact four-line note must never balloon into a large
            # empty-looking panel on a narrow mobile viewport.
            self.assertLess(box["height"], 150, "why-note wrapper is unexpectedly tall on mobile")


if __name__ == "__main__":
    unittest.main()
