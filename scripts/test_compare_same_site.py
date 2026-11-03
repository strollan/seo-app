"""
URL Compare: same-website rejection and clear compared-URL labels.

Offline. /analyze route tests patch app.main.validate_public_url to a no-op
and app.main.fetch_page_data to a fake, so no URL is fetched, and they stop
the pipeline with a sentinel right after the same-website checks, so no
scoring, SEO agent, keyword-volume or SERP code runs.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
from agents.same_site import SAME_WEBSITE_MESSAGE, same_website, site_identity

SAME = [
    ("example.com", "example.com"),
    ("https://example.com/", "http://www.example.com/services"),
    ("EXAMPLE.com", "https://Example.COM."),
    ("https://www.example.com", "https://shop.example.com/page"),
    ("https://blog.example.co.uk", "example.co.uk"),
    ("https://a.example.com.au", "https://www.example.com.au"),
    ("https://joe.github.io/a", "https://joe.github.io/b"),
    ("https://sites.google.com/view/joes-pizza", "https://sites.google.com/view/joes-pizza/menu"),
    ("https://www.facebook.com/joespizza", "https://m.facebook.com/joespizza/about"),
    ("http://203.0.113.5/x", "203.0.113.5"),
]
DIFFERENT = [
    ("longislandroofing.com", "liroofing.com"),
    ("https://aroundtheclockplumbinghsv.com/", "https://www.1tomplumber.com/huntsville/"),
    ("example.com", "example.net"),
    ("example.com", "notexample.com"),
    ("https://alpha.co.uk", "https://beta.co.uk"),
    ("https://alpha.com.au", "https://beta.com.au"),
    ("https://shop-a.com.sg", "https://shop-b.com.sg"),
    ("https://joe.github.io", "https://ann.github.io"),
    ("https://joes.wixsite.com/pizza", "https://anns.wixsite.com/pizza"),
    ("https://sites.google.com/view/joes-pizza", "https://sites.google.com/view/anns-bakery"),
    ("https://www.facebook.com/joespizza", "https://www.facebook.com/annsbakery"),
    ("http://203.0.113.5", "http://203.0.113.6"),
]


class SameWebsiteRuleTests(unittest.TestCase):
    def test_same_websites_are_detected(self):
        for a, b in SAME:
            with self.subTest(a=a, b=b):
                self.assertTrue(same_website(a, b))
                self.assertTrue(same_website(b, a))

    def test_unrelated_websites_are_not_merged(self):
        for a, b in DIFFERENT:
            with self.subTest(a=a, b=b):
                self.assertFalse(same_website(a, b))

    def test_redirect_lists_and_missing_hosts(self):
        self.assertTrue(same_website(["https://a.com", "https://a.com/"], ["https://b.com", "https://www.a.com/home"]))
        self.assertFalse(same_website(["https://a.com", ""], ["https://b.com", None]))
        self.assertIsNone(site_identity(""))
        self.assertFalse(same_website("", ""))


class Sentinel(Exception):
    pass


def fake_page(url, final_url=None):
    page = {key: value for key, value in {"url": url, "domain": appmain.clean_domain(url), "clean_domain": appmain.clean_domain(url)}.items()}
    page.update(final_url=final_url or url, title="T", meta_description="M", h1="H", word_count=500, h1_count=1, score=0)
    return page


class AnalyzeRouteTests(unittest.TestCase):
    def setUp(self):
        appmain._analyze_rate_limit_attempts.clear()
        self.client = TestClient(appmain.app)
        self.patches = [mock.patch.object(appmain, "validate_public_url", lambda url: url),
                        mock.patch.object(appmain, "save_report_snapshot", side_effect=AssertionError("no report may be saved"))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        appmain._analyze_rate_limit_attempts.clear()

    def post(self, url_1, url_2):
        return self.client.post("/analyze", data={"url_1": url_1, "url_2": url_2})

    def test_same_website_is_rejected_before_any_fetch_or_rate_limit_attempt(self):
        for a, b in (("https://example.com", "http://www.example.com/"), ("example.com", "shop.example.com")):
            with self.subTest(a=a, b=b), mock.patch.object(appmain, "fetch_page_data", side_effect=AssertionError("must not fetch")):
                response = self.post(a, b)
                self.assertEqual(response.status_code, 400)
                self.assertIn(SAME_WEBSITE_MESSAGE, response.text)
                self.assertIn("/compare?", response.text)
        self.assertEqual(appmain._analyze_rate_limit_attempts, {})

    def test_redirect_onto_the_same_website_is_rejected_after_fetch(self):
        pages = {"https://client.com": fake_page("https://client.com"),
                 "https://other-name.com": fake_page("https://other-name.com", final_url="https://www.client.com/")}
        with mock.patch.object(appmain, "fetch_page_data", side_effect=lambda url: pages[url]), \
             mock.patch.object(appmain, "score_page", side_effect=AssertionError("must stop before scoring")):
            response = self.post("https://client.com", "https://other-name.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn(SAME_WEBSITE_MESSAGE, response.text)

    def test_distinct_competitor_proceeds_to_scoring_with_the_entered_url(self):
        fetched = []
        def fetch(url):
            fetched.append(url)
            return fake_page(url)
        with mock.patch.object(appmain, "fetch_page_data", side_effect=fetch), \
             mock.patch.object(appmain, "score_page", side_effect=Sentinel("reached scoring")), \
             mock.patch.object(appmain, "find_business_competitors", side_effect=AssertionError("no automatic search")):
            with self.assertRaises(Sentinel):
                self.post("https://www.longislandroofing.com/", "https://liroofing.com/")
        self.assertEqual(fetched, ["https://www.longislandroofing.com/", "https://liroofing.com/"])

    def test_compare_page_ships_the_same_rules_and_message(self):
        page = self.client.get("/compare").text
        self.assertIn("SAME_SITE_RULES", page)
        self.assertIn(json.dumps(SAME_WEBSITE_MESSAGE), page)
        self.assertIn("github.io", page)


class ReportLabelTests(unittest.TestCase):
    def render(self, site, competitor):
        context = {"request": None, "user": None, "site": site, "competitors": [competitor], "analysis_html": "", "current_page_signals": {},
                   "volume_data": [], "gap": {"missing_grouped": {"service": [], "location": [], "commercial": []}, "shared": [], "missing": []}, "site_score_breakdown": {}, "competitor_score_breakdowns": [], "site_quick_wins": [],
                   "competitor_quick_wins": [], "site_section_card": {}, "competitor_section_cards": [], "generated_at": "today", "logo_url": None}
        return appmain.templates.env.get_template("report.html").render(context)

    def test_both_requested_urls_and_redirect_targets_are_shown(self):
        html = self.render(fake_page("https://www.longislandroofing.com/"),
                           fake_page("https://liroofing.com/roofing", final_url="https://www.liroofing.com/roofing-services/"))
        self.assertIn("Requested URL:</strong> https://www.longislandroofing.com/", html)
        self.assertIn("Requested URL:</strong> https://liroofing.com/roofing", html)
        self.assertIn("Scanned URL (after redirect):</strong> https://www.liroofing.com/roofing-services/", html)
        self.assertEqual(html.count("Scanned URL (after redirect)"), 1)  # no label when nothing redirected

    def test_trailing_slash_alone_is_not_reported_as_a_redirect(self):
        html = self.render(fake_page("https://a.com"), fake_page("https://b.com", final_url="https://b.com/"))
        self.assertNotIn("Scanned URL (after redirect)", html)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BrowserParityTests(unittest.TestCase):
    def test_form_check_matches_the_server_rules(self):
        page = TestClient(appmain.app).get("/compare").text
        start = page.index("const SAME_SITE_RULES")
        end = page.index('document.getElementById("url_2").addEventListener("input"')
        cases = SAME + DIFFERENT
        script = page[start:end] + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([a, b]) => sameWebsite(a, b))));"
        output = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True).stdout
        self.assertEqual(json.loads(output), [True] * len(SAME) + [False] * len(DIFFERENT))


if __name__ == "__main__":
    unittest.main()
