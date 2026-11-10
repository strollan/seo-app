"""
Regression tests: URL Compare must not recommend another industry's keywords.

Production bug (2026-11-09): an ice cream shop's Compare report recommended
"Professional Contractor Services", "Free Contractor Estimate", "Contractor
Quote", ... Root cause, reproduced offline with these fixtures:

1. keyword_gap only accepts phrases containing SERVICE_TERMS (SEO-agency and
   plumbing words), so ice cream / restaurant / remodeling pages got an empty
   gap.
2. On an empty gap, final_replace_empty_gap_buckets_with_ideas_html filled the
   buckets with generic placeholders for unknown industries, including
   "Local Contractor".
3. final_industry_weighted_keyword_terms_html detected the industry by scanning
   the whole report HTML -- including that injected chip -- matched
   "contractor", and rewrote every placeholder into contractor terms.

Every test is offline: page fetches come from scripts/fixtures/compare_industry,
requests/OpenAI are blocked, DataForSEO is disabled, and report saving and the
shared geo-phrase list are stubbed so no data is written.
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")
os.environ["DATAFORSEO_ENABLED"] = "0"

from fastapi.testclient import TestClient

import app.main as appmain

FIXTURES = ROOT / "scripts" / "fixtures" / "compare_industry"
CLIENT_URL = "https://client.example/"
COMPETITOR_URL = "https://competitor.example/"

SECTIONS = (
    "Service Keywords",
    "Location Keywords",
    "Commercial Keywords",
    "Shared Topic Signals",
    "Service Terms to Strengthen",
    "Location Terms to Strengthen",
    "Buyer-Intent Terms to Strengthen",
    "Additional Keyword Ideas",
    "Primary Target",
)

INVENTED_TERMS = (
    "contractor",
    "professional services",
    "residential services",
    "commercial services",
    "emergency services",
    "free estimate",
    "service quote",
    "local service provider",
    "primary service keyword",
)


class _Response:
    def __init__(self, url, text):
        self.url = url
        self.text = text
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        pass


def _blocked(*args, **kwargs):
    raise AssertionError("network call attempted during an offline test")


def keyword_sections(html):
    """{section title: [chip terms]} for the keyword sections of a report."""
    sections = {}
    pattern = re.compile(
        r'<h3 class="keyword-group-title">([^<]+)</h3>(.*?)'
        r'(?=<h3 class="keyword-group-title">|<div class="section">|$)',
        re.S,
    )
    for match in pattern.finditer(html):
        title = re.sub(r"\s*\(\d+\)$", "", match.group(1).strip())
        if title not in SECTIONS:
            continue
        chips = re.findall(
            r'<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>',
            match.group(2),
        )
        sections.setdefault(title, []).extend(c.strip().lower() for c in chips)
    return sections


def all_terms(html):
    return [t for terms in keyword_sections(html).values() for t in terms]


class OfflineCompare(unittest.TestCase):
    def setUp(self):
        self.openai_calls = 0

        def fake_agent(*args, **kwargs):
            self.openai_calls += 1
            return {}

        patches = [
            mock.patch("requests.get", _blocked),
            mock.patch("requests.post", _blocked),
            mock.patch("requests.Session.request", _blocked),
            mock.patch.object(appmain, "validate_public_url", lambda url: url),
            mock.patch.object(appmain, "run_seo_agent", fake_agent),
            mock.patch.object(appmain, "save_report_snapshot", lambda *a, **k: {}),
            mock.patch.object(appmain, "auto_save_geo_phrases", lambda *a, **k: None),
            mock.patch.object(appmain, "auth_current_user", lambda request: {"id": 1, "email": "t@example.com", "role": "admin"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(appmain.app)

    def compare(self, kind):
        pages = {
            CLIENT_URL: (FIXTURES / f"{kind}_client.html").read_text(encoding="utf-8"),
            COMPETITOR_URL: (FIXTURES / f"{kind}_competitor.html").read_text(encoding="utf-8"),
        }

        def fake_crawl(url, **kwargs):
            return _Response(url, pages[appmain.sanitize_url(url).rstrip("/") + "/"])

        with mock.patch.object(appmain, "crawl_get", fake_crawl):
            response = self.client.post("/analyze", data={"url_1": CLIENT_URL, "url_2": COMPETITOR_URL})
        self.assertEqual(response.status_code, 200)
        return response.text

    def assert_no_invented_terms(self, html):
        for term in all_terms(html):
            for bad in INVENTED_TERMS:
                self.assertNotIn(bad, term, f"invented/foreign keyword {term!r} in report")


class CompareIndustryRecommendationTests(OfflineCompare):
    def test_ice_cream_shop_gets_ice_cream_terms_not_contractor_terms(self):
        html = self.compare("icecream")
        self.assert_no_invented_terms(html)
        terms = all_terms(html)
        self.assertTrue(terms, "ice cream report has no keyword recommendations")
        self.assertTrue(all("ice cream" in t for t in terms), terms)
        self.assertIn("homemade ice cream", keyword_sections(html).get("Shared Topic Signals", []))

    def test_restaurant_gets_restaurant_terms(self):
        html = self.compare("restaurant")
        self.assert_no_invented_terms(html)
        terms = all_terms(html)
        self.assertIn("italian restaurant", terms)
        for term in terms:
            self.assertNotIn("village", term, "place-name fragment used as a keyword")

    def test_contractor_gets_contractor_terms_from_its_pages(self):
        html = self.compare("contractor")
        terms = all_terms(html)
        self.assertIn("kitchen remodeling", terms)
        self.assertIn("bathroom remodeling", terms)
        for template in ("professional contractor services", "free contractor estimate", "contractor quote"):
            self.assertNotIn(template, terms)

    def test_unknown_business_gets_no_invented_keywords(self):
        html = self.compare("unknown")
        self.assertEqual(all_terms(html), [], keyword_sections(html))
        self.assertIn("No service keyword gaps found.", html)

    def test_every_recommendation_appears_on_a_scanned_page(self):
        for kind in ("icecream", "contractor", "restaurant", "unknown"):
            pages = " ".join(
                re.sub(r"[^a-z0-9]+", " ", (FIXTURES / f"{kind}_{side}.html").read_text(encoding="utf-8").lower())
                for side in ("client", "competitor")
            )
            for term in all_terms(self.compare(kind)):
                self.assertIn(" " + term + " ", " " + pages + " ", f"{kind}: {term!r} is not on either page")

    def test_reports_do_not_leak_between_clients(self):
        # A contractor report first, then an ice cream report in the same process.
        self.compare("contractor")
        html = self.compare("icecream")
        self.assert_no_invented_terms(html)
        self.assertFalse([t for t in all_terms(html) if "remodel" in t or "kitchen" in t])

    def test_compare_makes_no_openai_call(self):
        self.compare("icecream")
        self.assertEqual(self.openai_calls, 0)

    def test_full_competitor_page_report_is_not_blank(self):
        # "icecreamfull": same client, competitor page over 100 words, so the
        # report is NOT in weak-competitor mode and the Priority Recommendations
        # merge runs. That merge used to return None -> blank 200 report.
        html = self.compare("icecreamfull")
        self.assertNotIn("Use a stronger competitor page", html)
        self.assertGreater(len(html), 10000)
        self.assertIn("Priority Recommendations", html)
        self.assert_no_invented_terms(html)
        self.assertIn("homemade ice cream", keyword_sections(html).get("Primary Target", []))

    def test_primary_target_is_filled_from_page_content(self):
        for kind in ("icecream", "restaurant", "contractor", "icecreamfull"):
            html = self.compare(kind)
            self.assertNotIn("Primary Service Keyword", html, kind)
            self.assertIsNone(
                re.search(r'Primary Target</h3>\s*<div class="chip-wrap">\s*</div>', html),
                f"{kind}: empty Primary Target heading",
            )
            self.assertTrue(keyword_sections(html).get("Primary Target"), kind)


class IndustryDetectorTests(unittest.TestCase):
    def test_detector_ignores_its_own_generated_chips(self):
        html = (
            '<p>Homemade ice cream in Port Jefferson.</p>'
            '<div class="keyword-group"><h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
            '<div class="chip-wrap"><span class="chip chip-medium">⭐ Professional Services</span></div></div>'
            '<div class="keyword-group"><h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
            '<div class="chip-wrap"><span class="chip chip-medium">⭐ Free Estimate</span>'
            '<span class="chip chip-medium">⭐ Local Contractor</span></div></div>'
        )
        out = appmain.final_industry_weighted_keyword_terms_html(html)
        self.assertNotIn("Contractor Services", out)
        self.assertNotIn("Free Contractor Estimate", out)

    def test_detector_still_uses_real_page_content(self):
        html = (
            '<p>Licensed plumber for drain cleaning.</p>'
            '<div class="keyword-group"><h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
            '<div class="chip-wrap"><span class="chip chip-medium">⭐ Professional Services</span></div></div>'
        )
        out = appmain.final_industry_weighted_keyword_terms_html(html)
        self.assertIn("Professional Plumbing Services", out)

    def test_unknown_industry_empty_buckets_are_left_empty(self):
        html = (
            '<p>No major missing keyword gaps were found.</p>'
            '<div class="keyword-group"><h3 class="keyword-group-title">Service Keywords (0)</h3>'
            '<p class="empty-note">No service keyword gaps found.</p></div>'
        )
        self.assertEqual(appmain.final_replace_empty_gap_buckets_with_ideas_html(html), html)
        self.assertNotIn("chip", appmain.final_add_additional_keyword_ideas_html(html))

    def test_known_industry_no_gap_fallback_is_unchanged(self):
        html = (
            '<p>Emergency plumber and drain cleaning. No major missing keyword gaps were found.</p>'
            '<div class="keyword-group"><h3 class="keyword-group-title">Service Keywords (0)</h3>'
            '<p class="empty-note">No service keyword gaps found.</p></div>'
        )
        out = appmain.final_replace_empty_gap_buckets_with_ideas_html(html)
        self.assertIn("Drain Cleaning", out)


class EmptyPrimaryTargetTests(unittest.TestCase):
    EMPTY = (
        '<div class="section"><h2>{heading}</h2><div class="keyword-box">'
        '<div class="keyword-group"><h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap">\n   </div></div>'
        '<div class="keyword-group"><h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">{items}</ul></div></div></div>'
    )

    def test_no_placeholder_when_nothing_supports_a_primary(self):
        html = '<p>Hand-thrown pottery and glaze workshops.</p>' + self.EMPTY.format(
            heading="Recommended Target Keywords", items="")
        out = appmain.final_fix_empty_recommended_keywords_html(html)
        self.assertNotIn("Primary Service Keyword", out)
        self.assertNotIn("Primary Target", out)

    def test_roof_fallback_needs_the_word_roof(self):
        # Compact empty chip-wrap, the exact form the old code also matched.
        html = '<p>Waterproof cones and a rooftop patio.</p>' + self.EMPTY.format(
            heading="Recommended Target Keywords", items="").replace('<div class="chip-wrap">\n   </div>', '<div class="chip-wrap"></div>')
        self.assertNotIn("Roof Repair", appmain.final_fix_empty_recommended_keywords_html(html))

    def test_provisional_block_is_filled_from_secondary(self):
        html = self.EMPTY.format(heading="Provisional Keyword Ideas", items="<li>Italian Restaurant</li><li>Private Parties</li>")
        out = appmain.final_fix_empty_recommended_keywords_html(html)
        self.assertIn("🔥 Italian Restaurant</span>", out)

    def test_quickwins_layout_always_returns_html(self):
        html = (
            '<html><head></head><body>'
            '<div class="section"><h2>Recommended Target Keywords</h2><div class="keyword-box">'
            '<div class="keyword-group"><h3 class="keyword-group-title">Primary Target</h3>'
            '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 Homemade Ice Cream</span></div></div>'
            '</div></div>'
            '<div class="section">\n<h2>Quick Wins</h2><div class="compare-grid quick-wins-grid">'
            '<div class="site-card"><ul class="quick-wins-list"><li>Add a menu page.</li></ul></div></div></div>'
            '</body></html>'
        )
        out = appmain.final_target_keywords_quickwins_layout_html(html)
        self.assertIsInstance(out, str)
        self.assertIn("Priority Recommendations", out)
        self.assertIn("Homemade Ice Cream", out)


class ContentSupportedGapTests(unittest.TestCase):
    def test_existing_engine_result_is_not_replaced(self):
        gap = {"missing_grouped": {"service": [{"term": "drain cleaning", "priority": "high"}], "location": [], "commercial": []}, "shared": []}
        with mock.patch.object(appmain, "_keyword_gap_before_content_supported_fallback", lambda s, c: gap):
            self.assertIs(appmain.keyword_gap({}, [{}]), gap)

    def test_no_competitor_gives_no_fallback(self):
        self.assertEqual(appmain.content_supported_keyword_gap({"keywords": ["ice cream"]}, []), {"service": [], "shared": []})


if __name__ == "__main__":
    unittest.main()
