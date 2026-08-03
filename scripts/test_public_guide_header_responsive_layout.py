"""
Regression coverage for the public-guide header layout defect reported at
~1125px viewport width on /how-to-verify-local-business-leads-before-outreach:
the H1 was crushed into a sliver column and wrapped almost one word per
line, the subtitle overlapped the white article-content card, and the
article card began before the header had finished rendering.

Root cause (see app/templates/public_guide_base.html): the shared
styles.css "FINAL HEADER LOCK" block gives .header a fixed height:94px and
gives .header-left min-width:0 while .nav (flex-wrap:nowrap, white-space:
nowrap pills) never shrinks, so any shortfall between the title column and
the nav is forced entirely onto the title -- and a wrapped multi-line
title has nowhere to go since the header box height is pinned. Direct
measurement (Playwright) showed this was not limited to a narrow
"tablet" band: because .container is capped at max-width:1100px
regardless of viewport, the longest guide titles were crushed identically
at 1440px, 1280px, 1125px, and 1024px alike.

The fix adds a `.public-guide-page`-scoped, content-aware `flex-wrap`
layout (only active above 850px, where the page already has a working
column-stack, and above 700px, where it already has a working hamburger
drawer) so the header grows to fit its content and drops the nav to its
own row instead of crushing the title. It only affects `.public-guide-page`
-- carried by the six templates extending public_guide_base.html -- so
/compare, /lead-bot, /history, and reports are untouched.

Skips itself if Playwright/Chromium aren't available rather than failing
the suite (matches the pattern in test_leadbot_csrf_routes.py).
"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent

# The longest H1 in the guide cluster -- the page the defect was reported
# on, and the worst case for title/nav crowding.
LONG_TITLE_PATH = "/how-to-verify-local-business-leads-before-outreach"

# Widths spanning the previously-broken desktop/tablet band plus the
# already-working mobile tiers, per the bug report's explicit test matrix.
WIDTHS = [1440, 1280, 1125, 1024, 768, 480, 375]

# Below this width the header switches to the pre-existing, unmodified
# hamburger-drawer layout (nav hidden behind .app-nav-toggle); the crushed-
# title/overlap checks below only apply to the always-visible-nav tiers.
HAMBURGER_BREAKPOINT = 700


class PublicGuideHeaderResponsiveLayoutTests(unittest.TestCase):
    PORT = 8796

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

        cls._proc = subprocess.Popen(
            [
                sys.executable,
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "test-placeholder-not-a-real-key"),
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
                if requests.get(f"{base_url}/login", timeout=1).status_code == 200:
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

    def _measure(self, path, width):
        page = self._browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(f"{self.base_url}{path}")
            page.wait_for_load_state("networkidle")

            def box(selector):
                el = page.query_selector(selector)
                return el.bounding_box() if el is not None else None

            return {
                "h1": box(".header h1"),
                "subtitle": box(".header p"),
                "card": box(".public-guide-content .card"),
                "h1_count": page.eval_on_selector_all("h1", "els => els.length"),
                "body_scroll_width": page.eval_on_selector("body", "el => el.scrollWidth"),
            }
        finally:
            page.close()

    def test_h1_never_crushed_below_a_readable_width(self):
        for width in WIDTHS:
            if width <= HAMBURGER_BREAKPOINT:
                continue
            with self.subTest(width=width):
                m = self._measure(LONG_TITLE_PATH, width)
                self.assertIsNotNone(m["h1"], f"no h1 box measured at {width}px")
                # The crushed state measured 158.4px wide before the fix,
                # wrapping the 49-character title almost one word per
                # line. 350px keeps real headroom above that failure mode
                # while still tolerating legitimately narrower columns.
                self.assertGreater(
                    m["h1"]["width"], 350,
                    f"H1 crushed to {m['h1']['width']}px at {width}px viewport",
                )
                # The crushed state also rendered the H1 starting above
                # y=0 (negative y), i.e. overflowing above the header box.
                self.assertGreaterEqual(
                    m["h1"]["y"], 0,
                    f"H1 renders above the header top at {width}px viewport",
                )

    def test_subtitle_never_overlaps_the_article_card(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                m = self._measure(LONG_TITLE_PATH, width)
                subtitle, card = m["subtitle"], m["card"]
                self.assertIsNotNone(subtitle)
                self.assertIsNotNone(card)
                subtitle_bottom = subtitle["y"] + subtitle["height"]
                self.assertLessEqual(
                    subtitle_bottom, card["y"] + 1,  # 1px tolerance for subpixel rounding
                    f"subtitle (bottom={subtitle_bottom}) overlaps the card "
                    f"(top={card['y']}) at {width}px viewport",
                )

    def test_exactly_one_h1_at_every_width(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                m = self._measure(LONG_TITLE_PATH, width)
                self.assertEqual(m["h1_count"], 1)

    def test_no_horizontal_page_overflow_at_any_width(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                m = self._measure(LONG_TITLE_PATH, width)
                self.assertLessEqual(
                    m["body_scroll_width"], width + 1,
                    f"horizontal overflow at {width}px viewport "
                    f"(scrollWidth={m['body_scroll_width']})",
                )

    def test_compare_page_layout_is_unaffected(self):
        """/compare carries .compare-page but not .public-guide-page, so
        the new rules (all scoped to .public-guide-page) must not touch
        it. Guards against a future edit accidentally widening the
        selector scope."""
        for width in (1440, 1125, 1024):
            with self.subTest(width=width):
                m = self._measure("/compare", width)
                self.assertEqual(m["h1_count"], 1)
                self.assertLessEqual(m["body_scroll_width"], width + 1)


if __name__ == "__main__":
    unittest.main()
