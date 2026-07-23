"""
Regression tests for the homepage content improvement (Phase 2 of the SEO
foundation work). Covers:

  - the approved hero H1 is preserved exactly, and remains the only H1
  - both tools (Lead Finder, Website Comparison Tool) are clearly
    represented with correct CTA destinations
  - no unsupported claims were introduced (guarantees, "every industry",
    nationwide coverage, conversion/revenue claims, permanent-free
    pricing, unverified AI-powered-scoring claims)
  - no obvious keyword stuffing of the five target phrases
  - the Phase 1 SEO metadata (title/description/canonical/robots/OG/
    Twitter/JSON-LD) is still intact on the homepage after the copy edit

Does not touch Lead Finder scanning, DataForSEO, scoring, exports, guest
limits, or CSRF behavior -- copy/markup only.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain

UNSUPPORTED_CLAIMS = [
    "guarantee",
    "guaranteed",
    "every industry",
    "any industry",
    "nationwide",
    "conversion rate",
    "increase revenue",
    "boost revenue",
    "free forever",
    "forever free",
    "ai-powered",
    "ai powered",
    "powered by ai",
]

UNRELATED_KEYWORD_TARGETS = [
    "construction lead",
    "plumbing lead",
    "antivirus",
    "toxic backlink",
    "word-count score",
    "word count score",
]


class HeroHeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body = cls.client.get("/").text

    def test_approved_h1_text_is_preserved_exactly(self):
        match = re.search(r"<h1[^>]*>(.*?)</h1>", self.body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "Find local leads worth contacting.")

    def test_exactly_one_h1_on_the_homepage(self):
        h1s = re.findall(r"<h1[^>]*>", self.body)
        self.assertEqual(len(h1s), 1)

    def test_h2_headings_are_real_section_boundaries_not_empty(self):
        h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", self.body)
        self.assertGreaterEqual(len(h2s), 4)
        for heading in h2s:
            self.assertTrue(heading.strip())


class BothToolsRepresentedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body = cls.client.get("/").text

    def test_lead_finder_heading_present(self):
        self.assertIn("<h2>Lead Finder</h2>", self.body)

    def test_website_comparison_tool_heading_present(self):
        self.assertIn("<h2>Website Comparison Tool</h2>", self.body)

    def test_primary_hero_cta_points_to_lead_bot(self):
        self.assertIn('href="/lead-bot">Find Local Leads</a>', self.body)

    def test_secondary_hero_cta_points_to_compare(self):
        self.assertIn('href="/compare">Compare Two Websites</a>', self.body)

    def test_lead_finder_card_cta_points_to_lead_bot(self):
        self.assertIn('class="dark" href="/lead-bot"', self.body)

    def test_compare_card_cta_points_to_compare(self):
        self.assertIn('class="dark" href="/compare"', self.body)

    def test_final_cta_points_to_lead_bot(self):
        matches = re.findall(r'<a class="btn btn-primary" href="([^"]+)">', self.body)
        self.assertIn("/lead-bot", matches)

    def test_no_cta_link_has_empty_text(self):
        for href, text in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', self.body):
            if href in ("/lead-bot", "/compare"):
                with self.subTest(href=href, text=text):
                    self.assertTrue(text.strip(), f"empty link text for href={href}")


class NoUnsupportedClaimsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.body_lower = cls.client.get("/").text.lower()

    def test_no_unsupported_claims_present(self):
        for phrase in UNSUPPORTED_CLAIMS:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.body_lower)

    def test_no_unrelated_forced_keyword_targets(self):
        for phrase in UNRELATED_KEYWORD_TARGETS:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.body_lower)

    def test_contact_detail_honesty_language_present(self):
        """The copy must not promise complete contact information."""
        self.assertIn("do not always", self.body_lower)

    def test_old_unsupported_half_outdated_claim_is_gone(self):
        """"Half of it is outdated" was an unsupported specific-fraction
        claim about purchased lists, corrected to more accurate wording.
        Must never reappear."""
        self.assertNotIn("half of it is outdated", self.body_lower)


class ProblemSectionAndFinalCtaCorrectedCopyTests(unittest.TestCase):
    """Locks in the exact, explicitly-requested copy correction to the
    "Why not just buy a list?" section and the final CTA. Whitespace is
    normalized before comparison so incidental re-wrapping of the
    template's HTML doesn't make this brittle -- the wording itself is
    still checked exactly."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        body = cls.client.get("/").text
        cls.normalized = " ".join(body.split())

    def test_problem_section_first_paragraph_exact_text(self):
        self.assertIn(
            "Buying a giant, generic lead list can look like the easy route. The data may be "
            "stale, the businesses may not fit your market, and you still have to figure out who "
            "is actually worth contacting.",
            self.normalized,
        )

    def test_problem_section_second_paragraph_exact_text(self):
        self.assertIn(
            "LeadMeLeads takes a different approach: search by keyword and location, then review "
            "local businesses that match the search instead of starting with a recycled "
            "spreadsheet.",
            self.normalized,
        )

    def test_final_cta_exact_text(self):
        self.assertIn(
            "Enter a keyword and a location to find leads worth contacting &mdash; no purchased "
            "list, less guesswork.",
            self.normalized,
        )


class NoKeywordStuffingTests(unittest.TestCase):
    """Each target phrase should appear naturally (a small handful of
    times at most, not stuffed throughout the page).

    "generate sales leads" and "sales lead generation" were dropped from
    this list following an explicit copy correction to the "Why not just
    buy a list?" section (requested directly, verbatim) that removed both
    phrases in favor of more accurate wording -- not a stuffing/coverage
    regression, so they are no longer required here."""

    TARGET_PHRASES = [
        "local leads",
        "business leads",
        "find leads",
    ]

    MAX_OCCURRENCES = 3

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        full_body = cls.client.get("/").text
        # Restrict to visible <body> content -- <head> metadata (title,
        # Open Graph, Twitter card) legitimately repeats the page title/
        # description by design (Phase 1 SEO foundation), which is not
        # keyword stuffing in the sense this check cares about.
        match = re.search(r"<body[^>]*>(.*)</body>", full_body, re.DOTALL)
        assert match, "could not locate <body> in homepage HTML"
        cls.body_lower = match.group(1).lower()

    def test_target_phrases_appear_at_least_once(self):
        for phrase in self.TARGET_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertGreaterEqual(self.body_lower.count(phrase), 1)

    def test_target_phrases_are_not_stuffed(self):
        for phrase in self.TARGET_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertLessEqual(self.body_lower.count(phrase), self.MAX_OCCURRENCES)


class Phase1MetadataStillIntactTests(unittest.TestCase):
    """The Phase 1 SEO foundation must survive the Phase 2 copy edit
    untouched."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmain.app)
        cls.response = cls.client.get("/")
        cls.body = cls.response.text

    def test_title_unchanged(self):
        self.assertIn(
            "<title>LeadMeLeads — Find Local Leads Worth Contacting</title>",
            self.body,
        )

    def test_meta_description_unchanged(self):
        self.assertIn('name="description" content="Find local business leads', self.body)

    def test_canonical_unchanged(self):
        self.assertIn('rel="canonical" href="https://leadmeleads.com/"', self.body)

    def test_robots_meta_unchanged(self):
        self.assertIn('name="robots" content="index, follow"', self.body)

    def test_no_x_robots_tag_header_on_homepage(self):
        self.assertIsNone(self.response.headers.get("x-robots-tag"))

    def test_jsonld_still_present(self):
        self.assertIn("application/ld+json", self.body)
        self.assertIn('"@type":"Organization"', self.body)
        self.assertIn('"@type":"WebSite"', self.body)


if __name__ == "__main__":
    unittest.main()
