"""
Regression tests for the short "value explanation" copy added on the
/lead-bot dashboard page (agents/lead_dashboard_agent.py::render_lead_dashboard()),
directly below the hero/headline and directly above the "Run Lead Finder"
search form.

Placement note: this sits in the plain page background (full container
width, ~900px max), not inside the dark-gradient hero bar alongside the
logo/nav -- an earlier attempt at this same task placed it inside the
narrow ".leadbot-brand-left" flex row (competing with the logo for
space), which caused the two-sentence copy to wrap onto 4 lines at normal
desktop widths instead of the required 2. Placing it as its own block
below the hero, with a generous max-width, fixes that (confirmed via a
real headless browser: exactly 2 lines at 1280px/1024px/768px, natural
multi-line wrapping only at a 375px mobile width).

This is copy-only: a plain <p> styled with the same small/muted
"secondary text" values already used by ".help" elsewhere on this exact
page (color #64748b, font-size 13px) -- no new color palette, no card,
alert, banner, or icon. The form's fields and scan-start logic were not
touched.

These tests prove:
  - the exact required copy (two sentences, one per line via a single
    <br>) appears exactly once
  - it appears after the hero/headline and before the "Run Lead Finder"
    form in the page source
  - it is not wrapped in any alert/banner/card-style element, and its
    own CSS rule adds no background/border/box-shadow
  - the search form's fields (keyword, market, own_domain, scan-size
    controls) are completely unchanged
  - it renders correctly for every viewer: guest, standard user, admin
  - in a real browser: exactly two lines at normal desktop widths
    (1280px, 1024px, 768px), and it still renders (with natural
    multi-line wrapping, no horizontal overflow) at a 375px mobile width
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.lead_dashboard_agent as dash_agent

EXPECTED_LINE_1 = "Google helps you find businesses. LeadMeLeads helps you decide which are worth contacting."
EXPECTED_LINE_2 = "We organize potential leads and contact details in one place, ready to review and export."
EXPECTED_FULL_MARKUP = f'<p class="leadbot-value-explanation">{EXPECTED_LINE_1}<br>{EXPECTED_LINE_2}</p>'


class ValueExplanationMarkupTests(unittest.TestCase):
    def _render(self, current_user=None):
        return dash_agent.render_lead_dashboard(current_user=current_user, csrf_token="tok")

    def test_exact_copy_present_once_for_guest(self):
        source = self._render(current_user=None)
        self.assertEqual(source.count(EXPECTED_FULL_MARKUP), 1)

    def test_exact_copy_present_once_for_standard_user(self):
        source = self._render(current_user={"role": "standard", "username": "user1"})
        self.assertEqual(source.count(EXPECTED_FULL_MARKUP), 1)

    def test_exact_copy_present_once_for_admin(self):
        source = self._render(current_user={"role": "admin", "username": "theadmin"})
        self.assertEqual(source.count(EXPECTED_FULL_MARKUP), 1)

    def test_each_line_appears_exactly_once(self):
        source = self._render(current_user=None)
        self.assertEqual(source.count(EXPECTED_LINE_1), 1)
        self.assertEqual(source.count(EXPECTED_LINE_2), 1)

    def test_body_has_exactly_one_line_break_between_the_two_sentences(self):
        source = self._render(current_user=None)
        start = source.index('<p class="leadbot-value-explanation">')
        end = source.index("</p>", start)
        block = source[start:end]
        self.assertEqual(block.count("<br>"), 1)

    def test_appears_after_hero_and_above_the_run_lead_finder_form(self):
        source = self._render(current_user=None)
        headline_index = source.index("<h1>Lead Finder Dashboard</h1>")
        explanation_index = source.index(EXPECTED_LINE_1)
        form_index = source.index('<form id="leadbotRunForm"')
        self.assertLess(headline_index, explanation_index)
        self.assertLess(explanation_index, form_index)

    def test_not_wrapped_in_alert_banner_or_card_styling(self):
        source = self._render(current_user=None)
        start = source.index('<p class="leadbot-value-explanation">')
        window = source[max(0, start - 200):start]
        for bad_class in ["alert", "banner", "callout", "notice-box", "leadbot-card", "panel-highlight"]:
            self.assertNotIn(bad_class, window)
        self.assertNotIn('role="alert"', source[start:start + len(EXPECTED_FULL_MARKUP) + 20])

    def test_form_fields_unchanged(self):
        source = self._render(current_user=None)
        self.assertIn('name="keyword"', source)
        self.assertIn('name="market"', source)
        self.assertIn('name="own_domain"', source)
        self.assertIn('id="scanSizePreset"', source)
        self.assertIn('id="leadbotStartScanButton"', source)


class ValueExplanationCssTests(unittest.TestCase):
    def test_new_rule_adds_no_card_alert_or_banner_styling(self):
        source = dash_agent.render_lead_dashboard(current_user=None, csrf_token="tok")
        start = source.index(".leadbot-value-explanation {")
        end = source.index("}", start)
        rule_body = source[start:end]
        for bad_prop in ["background", "border", "box-shadow", "border-radius"]:
            self.assertNotIn(bad_prop, rule_body)


class ValueExplanationBrowserRegressionTests(unittest.TestCase):
    """Confirms real rendered line count at normal desktop widths, and
    natural wrapping at a mobile width, in a real headless browser.
    Skips itself if Playwright/Chromium aren't installed. Loads only the
    static dashboard page -- no scan is ever started."""

    PORT = 8799

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

    def _note_box(self, width, height=900):
        context = self._browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base_url}/lead-bot")
        page.wait_for_selector(".leadbot-value-explanation")
        note = page.query_selector(".leadbot-value-explanation")
        self.assertIsNotNone(note)
        return note.bounding_box(), page

    def test_exactly_two_lines_at_1280px(self):
        box, _ = self._note_box(1280)
        # 13px font * 1.45 line-height =~ 18.85px/line; two lines should
        # sit well under 45px, comfortably distinguishing 2 lines from 3+.
        self.assertLess(box["height"], 45)

    def test_exactly_two_lines_at_1024px(self):
        box, _ = self._note_box(1024)
        self.assertLess(box["height"], 45)

    def test_exactly_two_lines_at_768px_tablet_width(self):
        box, _ = self._note_box(768)
        self.assertLess(box["height"], 45)

    def test_mobile_width_wraps_naturally_without_horizontal_overflow(self):
        box, _ = self._note_box(375)
        self.assertIsNotNone(box)
        self.assertLessEqual(box["width"], 375)
        # More than two lines is expected and fine on a narrow mobile
        # viewport -- the requirement is natural wrapping, not a fixed
        # line count.
        self.assertGreater(box["height"], 45)

    def test_note_visible_alongside_headline_and_form(self):
        box, page = self._note_box(1280)
        self.assertTrue(page.is_visible(".leadbot-value-explanation"))
        self.assertTrue(page.is_visible("h1"))
        self.assertTrue(page.is_visible("#leadbotRunForm"))
        self.assertIn(EXPECTED_LINE_1, page.content())


if __name__ == "__main__":
    unittest.main()
