"""
Focused coverage for the new /what-makes-a-good-lead SEO/AEO landing page
(app/templates/good_lead.html, app/main.py:good_lead_page(),
app.seo_meta.GOOD_LEAD_PAGE / GOOD_LEAD_FAQ / render_faq_jsonld()).

The page reuses the existing /compare page shell (body class
"compare-page", the same #compareNav off-canvas mobile nav markup/CSS,
the same styles.css) rather than introducing a new design, and extends
app.seo_meta.PUBLIC_INDEXABLE_PATHS -- the single source of truth already
covered by scripts/test_seo_technical_foundation.py -- so this page's
noindex/sitemap/robots wiring is exercised generically by that file's
existing EXPECTED-driven tests (updated alongside this change to include
this page). This file covers what's specific to this page: the FAQ
section's schema-matches-visible-copy guarantee, its exact H1/CTA/
internal-link content, and the mobile-safety structural invariants that
prove no page-level horizontal overflow at 320/375px without a headless
browser (see scripts/test_report_mobile_layout.py for the same technique
and its rationale).

Does not touch SEO scoring, history/saved-report data, DataForSEO/Lead
Finder, or auth/CSRF.
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import app.seo_meta as seo_meta

PATH = "/what-makes-a-good-lead"


class GoodLeadPageBasicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.response = cls.client.get(PATH)
        cls.body = cls.response.text

    def test_route_returns_200(self):
        self.assertEqual(self.response.status_code, 200)

    def test_exact_title(self):
        match = re.search(r"<title>([^<]*)</title>", self.body)
        self.assertIsNotNone(match)
        self.assertEqual(
            match.group(1),
            "What Makes a Good Lead? How to Find Leads Worth Contacting",
        )

    def test_exact_meta_description(self):
        self.assertIn(
            f'name="description" content="{seo_meta.GOOD_LEAD_PAGE.description}"',
            self.body,
        )

    def test_exact_canonical(self):
        match = re.search(r'rel="canonical" href="([^"]+)"', self.body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "https://leadmeleads.com/what-makes-a-good-lead")

    def test_indexable_robots_meta(self):
        self.assertIn('name="robots" content="index, follow"', self.body)

    def test_exact_h1(self):
        match = re.search(r"<h1>([^<]*)</h1>", self.body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "What Makes a Good Lead?")

    def test_h1_does_not_cannibalize_homepage_title_or_h1(self):
        homepage_body = self.client.get("/").text
        homepage_h1 = re.search(r"<h1>([^<]*)</h1>", homepage_body).group(1)
        homepage_title = re.search(r"<title>([^<]*)</title>", homepage_body).group(1)
        page_h1 = re.search(r"<h1>([^<]*)</h1>", self.body).group(1)
        page_title = re.search(r"<title>([^<]*)</title>", self.body).group(1)
        self.assertNotEqual(homepage_h1, page_h1)
        self.assertNotEqual(homepage_title, page_title)


class GoodLeadInternalLinksAndCtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body = cls.client.get(PATH).text

    def test_links_to_lead_finder(self):
        self.assertIn('href="/lead-bot"', self.body)

    def test_links_to_compare(self):
        self.assertIn('href="/compare"', self.body)

    def test_has_a_natural_cta_to_lead_finder(self):
        ctas = re.findall(r'<a class="inline-cta" href="/lead-bot">([^<]+)</a>', self.body)
        self.assertGreaterEqual(len(ctas), 1)
        for text in ctas:
            self.assertTrue(text.strip())


class GoodLeadFaqSchemaMatchesVisibleCopyTests(unittest.TestCase):
    """The core AEO/schema requirement: the FAQPage JSON-LD must contain
    exactly the questions/answers actually visible on the page -- neither
    more (invisible schema-only claims) nor fewer (visible content with
    no matching schema entry)."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body = cls.client.get(PATH).text

    def _extract_visible_faq(self):
        items = re.findall(
            r'<div class="faq-item">\s*<h3>(.*?)</h3>\s*<p>(.*?)</p>\s*</div>',
            self.body,
            re.DOTALL,
        )
        self.assertTrue(items, "no visible FAQ items found on the page")
        return [(q.strip(), a.strip()) for q, a in items]

    def _extract_faq_jsonld(self):
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', self.body, re.DOTALL
        )
        faq_scripts = [json.loads(s) for s in scripts if '"FAQPage"' in s]
        self.assertEqual(len(faq_scripts), 1, "expected exactly one FAQPage JSON-LD script")
        return faq_scripts[0]

    def test_visible_faq_matches_seo_meta_source_of_truth(self):
        visible = self._extract_visible_faq()
        expected = [(item["question"], item["answer"]) for item in seo_meta.GOOD_LEAD_FAQ]
        self.assertEqual(visible, expected)

    def test_five_required_faq_questions_are_visible(self):
        visible_questions = {q for q, _ in self._extract_visible_faq()}
        expected_questions = {
            "What makes a good lead?",
            "How do I find good leads?",
            "What is the difference between a good lead and a qualified lead?",
            "Are local leads better?",
            "Is buying a large lead list worth it?",
        }
        self.assertEqual(visible_questions, expected_questions)

    def test_faq_jsonld_is_well_formed_faqpage(self):
        data = self._extract_faq_jsonld()
        self.assertEqual(data["@context"], "https://schema.org")
        self.assertEqual(data["@type"], "FAQPage")
        self.assertIn("mainEntity", data)
        for entry in data["mainEntity"]:
            self.assertEqual(entry["@type"], "Question")
            self.assertEqual(entry["acceptedAnswer"]["@type"], "Answer")

    def test_faq_jsonld_matches_visible_copy_exactly(self):
        visible = self._extract_visible_faq()
        jsonld = self._extract_faq_jsonld()
        schema_pairs = [
            (entry["name"], entry["acceptedAnswer"]["text"])
            for entry in jsonld["mainEntity"]
        ]
        self.assertEqual(schema_pairs, visible)

    def test_faq_jsonld_has_no_extra_or_missing_questions(self):
        jsonld = self._extract_faq_jsonld()
        self.assertEqual(len(jsonld["mainEntity"]), len(seo_meta.GOOD_LEAD_FAQ))

    def test_answers_open_with_a_direct_first_sentence(self):
        # AEO requirement: answer the question directly in the first
        # sentence rather than leading with a rhetorical wind-up.
        for item in seo_meta.GOOD_LEAD_FAQ:
            first_sentence = item["answer"].split(".")[0]
            self.assertGreater(len(first_sentence), 15)
            self.assertFalse(first_sentence.lower().startswith(("well,", "great question")))


class GoodLeadSeoWiringTests(unittest.TestCase):
    """The page is wired into the existing single source of truth
    (app.seo_meta.PUBLIC_INDEXABLE_PATHS) rather than hand-listed in
    multiple places, so sitemap/robots/noindex all stay in sync
    automatically -- verified directly here, and exercised generically
    for every public page (this one included) by the updated
    scripts/test_seo_technical_foundation.py."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)

    def test_path_is_in_public_indexable_paths(self):
        self.assertIn(PATH, seo_meta.PUBLIC_INDEXABLE_PATHS)

    def test_sitemap_includes_this_page(self):
        body = self.client.get("/sitemap.xml").text
        self.assertIn("<loc>https://leadmeleads.com/what-makes-a-good-lead</loc>", body)

    def test_robots_txt_does_not_disallow_this_page(self):
        body = self.client.get("/robots.txt").text
        self.assertNotIn(f"Disallow: {PATH}", body)

    def test_noindex_middleware_does_not_block_this_page(self):
        response = self.client.get(PATH)
        self.assertIsNone(response.headers.get("x-robots-tag"))

    def test_should_apply_noindex_header_is_false_for_this_path(self):
        self.assertFalse(seo_meta.should_apply_noindex_header(PATH))


class GoodLeadMobileLayoutSafetyTests(unittest.TestCase):
    """No headless browser is available in this environment (see
    scripts/test_report_mobile_layout.py for the same limitation and the
    approved static-audit alternative), so these tests verify the CSS/
    structural invariants that guarantee no page-level horizontal
    overflow at 320/375px: the page reuses the already-proven
    .compare-page off-canvas mobile nav (id="compareNav", matching the
    ID-scoped styles.css rules exactly -- a mismatched id here would
    silently drop the off-canvas behavior and let the nav overflow), and
    every prose/FAQ text container has overflow-wrap protection for long
    unbroken content."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body = cls.client.get(PATH).text

    def test_body_reuses_compare_page_mobile_nav_class(self):
        self.assertRegex(self.body, r'<body class="[^"]*\bcompare-page\b[^"]*">')

    def test_nav_toggle_and_panel_ids_match_the_id_scoped_css(self):
        # styles.css's off-canvas panel rules are scoped to the literal
        # ids #compareNav/#historyNav/#reportNav, not a class -- using a
        # different id here would silently lose position:fixed at
        # <=700px and let the nav overflow the viewport.
        self.assertIn('aria-controls="compareNav"', self.body)
        self.assertIn('id="compareNav"', self.body)

    def test_mobile_nav_toggle_and_overlay_present(self):
        self.assertIn("data-nav-toggle", self.body)
        self.assertIn("data-nav-overlay", self.body)
        self.assertIn('src="/static/js/mobile-nav.js"', self.body)

    def test_section_paragraphs_have_overflow_wrap_protection(self):
        rule = re.search(r"\.good-lead-content \.section p\s*\{([^}]*)\}", self.body)
        self.assertIsNotNone(rule)
        self.assertIn("overflow-wrap: anywhere", rule.group(1))

    def test_faq_answers_have_overflow_wrap_protection(self):
        rule = re.search(r"\.good-lead-content \.faq-item p\s*\{([^}]*)\}", self.body)
        self.assertIsNotNone(rule)
        self.assertIn("overflow-wrap: anywhere", rule.group(1))

    def test_narrow_width_padding_reductions_present(self):
        self.assertIn("@media (max-width: 480px)", self.body)
        self.assertIn("@media (max-width: 700px)", self.body)

    def test_no_fixed_pixel_widths_that_could_exceed_320px(self):
        # Any bare `width: <N>px` (not min/max-width) declared in this
        # page's own <style> block without a surrounding max-width safety
        # net could force horizontal overflow on a 320px viewport.
        style_block = re.search(r"<style>(.*?)</style>", self.body, re.DOTALL).group(1)
        for match in re.finditer(r"(?<!-)width:\s*(\d+)px", style_block):
            self.assertLess(
                int(match.group(1)), 320, f"fixed width {match.group(0)!r} could overflow 320px"
            )


if __name__ == "__main__":
    unittest.main()
