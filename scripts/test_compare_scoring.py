"""
URL Compare scoring: Your Site and Competitor use one rule set.

Offline. Synthetic scan fixtures only; saved reports and history are never
read or written. The pre-fix ("before") scoring is executed from the git
HEAD copy of strict_final_score_cap, reproducing the old per-slot chains.
Route tests stub every network/OpenAI/file-writing step and block sockets.
"""

import os
import re
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain


def page(url="https://example.com/", **overrides):
    data = {"url": url, "domain": appmain.clean_domain(url), "clean_domain": appmain.clean_domain(url), "final_url": url,
            "title": "Emergency Plumber in Shirley NY | Example Plumbing", "title_length": 50, "title_status": "Good",
            "meta_description": "Licensed plumbers serving Shirley, Mastic and Center Moriches with 24/7 emergency repairs and drain cleaning.",
            "meta_length": 110, "meta_status": "Good", "h1": "Shirley NY Plumbing", "has_h1": True, "h1_count": 1, "h2_count": 4,
            "keywords": ["plumber", "shirley"], "top_terms": [("plumber", 9)], "word_count": 900, "has_meta": True,
            "internal_link_count": 25, "canonical": url, "image_count": 10, "alt_count": 9, "error": None, "score": 0,
            "geo_phrases": []}
    data.update(overrides)
    return data


FIXTURES = {
    "strong page": page(),
    "missing meta description": page(meta_description="", meta_length=0, meta_status="Missing", has_meta=False),
    "meta placeholder ERROR": page(meta_description="ERROR"),
    "images without alt text": page(alt_count=1),
    "no images": page(image_count=0, alt_count=0),
    "thin page (80 words)": page(word_count=80),
    "medium page (250 words)": page(word_count=250),
    "no H1": page(h1="", has_h1=False, h1_count=0),
    "long title (80 chars)": page(title="X" * 80, title_length=80, title_status="Too Long"),
}


def old_scores(site, competitor):
    """The pre-fix /analyze chains, using HEAD's strict_final_score_cap."""
    source = subprocess.run(["git", "show", "HEAD:app/main.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    strict_src = re.search(r"^def strict_final_score_cap\(page, score\):.*?(?=^\S)", source, re.S | re.M).group(0)
    ns = {}
    exec(compile(strict_src, "HEAD:strict_final_score_cap", "exec"), ns)
    strict, caps, nope = ns["strict_final_score_cap"], appmain.apply_score_caps, appmain.no_perfect_display_score
    site, competitor = dict(site), dict(competitor)
    s = nope(site, nope(site, caps(site, appmain.score_page(site)[0])))
    c = appmain.score_page(competitor)[0]
    c = nope(competitor, nope(competitor, caps(competitor, strict(competitor, nope(competitor, nope(competitor, strict(competitor, c)))))))
    competitor["weak_data"] = competitor.get("word_count", 0) < 100 or competitor.get("h1_count", 0) == 0 or not competitor.get("title")
    s = nope(site, nope(site, s))
    c = nope(competitor, nope(competitor, strict(competitor, strict(competitor, caps(competitor, c)))))
    return s, c


def new_score(data):
    return appmain.compare_page_score(data, appmain.score_page(data)[0])


class SlotSymmetryTests(unittest.TestCase):
    def test_identical_scan_data_scores_identically_in_either_slot(self):
        for name, data in FIXTURES.items():
            with self.subTest(name):
                # Same data in the site slot and in the competitor slot (as a separate page).
                self.assertEqual(new_score(dict(data)), new_score(dict(data, url="https://other.example/")))

    def test_the_old_competitor_cap_was_asymmetric(self):
        site, comp = old_scores(FIXTURES["strong page"], FIXTURES["strong page"])
        self.assertEqual((site, comp), (98, 85))  # same page, different slot
        self.assertEqual(new_score(FIXTURES["strong page"]), 98)

    def test_comparison_flags_do_not_change_the_score(self):
        flagged = page(weak_data=True, location_mismatch=True)
        self.assertEqual(new_score(flagged), new_score(page()))


class FieldMappingTests(unittest.TestCase):
    def test_meta_description_field_is_read(self):
        self.assertEqual(appmain.strict_final_score_cap(page(), 100), 100)
        self.assertEqual(appmain.strict_final_score_cap(page(meta_description=""), 100), 85)
        self.assertEqual(appmain.strict_final_score_cap(page(meta_description="ERROR"), 100), 85)
        self.assertEqual(appmain.strict_final_score_cap({"title": "T", "h1": "H", "h1_count": 1, "meta": "legacy meta"}, 100), 100)

    def test_alt_count_field_is_read(self):
        self.assertEqual(appmain.strict_final_score_cap(page(alt_count=9), 100), 100)
        self.assertEqual(appmain.strict_final_score_cap(page(alt_count=4), 100), 95)   # 40% < 50%
        self.assertEqual(appmain.strict_final_score_cap(page(alt_count=5), 100), 100)  # exactly 50%
        self.assertEqual(appmain.strict_final_score_cap(page(image_count=0, alt_count=0), 100), 100)


class BoundaryTests(unittest.TestCase):
    def test_caps_and_boundaries(self):
        expectations = {
            "strong page": 98, "missing meta description": 85, "meta placeholder ERROR": 85,
            "images without alt text": 95, "no images": 98, "thin page (80 words)": 75,
            "medium page (250 words)": 90, "no H1": 85, "long title (80 chars)": 95,
        }
        for name, expected in expectations.items():
            with self.subTest(name):
                self.assertEqual(new_score(FIXTURES[name]), expected)
        for boundary, cap in ((99, 75), (100, 90), (299, 90), (300, 98)):
            self.assertEqual(new_score(page(word_count=boundary)), cap, boundary)


class AnalyzeRouteTests(unittest.TestCase):
    """Full /analyze with every external or file-writing step stubbed."""

    def setUp(self):
        appmain._analyze_rate_limit_attempts.clear()
        original = socket.socket.connect
        def guarded(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("network blocked in tests: " + str(address))
            return original(sock, address)
        self.saved = []
        stubs = {
            "validate_public_url": lambda url: url, "run_seo_agent": lambda results: {}, "enrich_keywords_with_dataforseo": lambda terms: [],
            "auto_save_geo_phrases": lambda phrases: None, "build_agent_action_plan": lambda *a, **k: "", "build_agent_insight_html": lambda *a, **k: "",
            "save_report_snapshot": lambda html, site, competitor, *a, **k: self.saved.append((site["url"], site["score"], competitor["url"], competitor["score"])),
        }
        self.patches = [mock.patch.object(socket.socket, "connect", guarded)] + [mock.patch.object(appmain, name, value) for name, value in stubs.items()]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(appmain.app)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        appmain._analyze_rate_limit_attempts.clear()

    def run_compare(self, site, competitor):
        pages = {site["url"]: site, competitor["url"]: competitor}
        with mock.patch.object(appmain, "fetch_page_data", side_effect=lambda url: dict(pages[url])):
            response = self.client.post("/analyze", data={"url_1": site["url"], "url_2": competitor["url"]})
        self.assertEqual(response.status_code, 200, response.text[:300])
        return self.saved[-1], response.text

    def test_swapping_slots_swaps_scores_exactly(self):
        a = page("https://alpha-plumbing.com/")
        b = page("https://beta-plumbing.com/", meta_description="", alt_count=2, word_count=250)
        (_, a_first, _, b_second), html = self.run_compare(a, b)
        (_, b_first, _, a_second), _ = self.run_compare(b, a)
        self.assertEqual((a_first, b_second), (a_second, b_first))
        self.assertEqual((a_first, b_second), (new_score(a), new_score(b)))
        self.assertEqual((a_first, b_second), (98, 65))  # b: raw 65 (missing meta, low alt, 250 words) is below every cap
        self.assertIn("Requested URL:</strong> https://alpha-plumbing.com/", html)

    def test_same_scan_data_on_two_distinct_sites_ties(self):
        a, b = page("https://alpha-plumbing.com/"), page("https://beta-plumbing.com/")
        (_, site_score, _, comp_score), html = self.run_compare(a, b)
        self.assertEqual(site_score, comp_score)
        self.assertIn("TIED", html)

    def test_redirected_competitor_is_scored_from_its_own_scan(self):
        a = page("https://alpha-plumbing.com/")
        b = page("https://beta-plumbing.com/", final_url="https://www.beta-plumbing.com/home/", meta_description="")
        (_, site_score, _, comp_score), html = self.run_compare(a, b)
        self.assertEqual((site_score, comp_score), (new_score(a), new_score(b)))
        self.assertEqual(comp_score, 85)  # missing-meta cap
        self.assertIn("Scanned URL (after redirect):</strong> https://www.beta-plumbing.com/home/", html)


if __name__ == "__main__":
    unittest.main()
