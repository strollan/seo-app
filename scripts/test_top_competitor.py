"""Top-competitor selection from saved Lead Finder results, plus paid-scan guards.

Offline. The fixture mirrors the real Shirley, NY pizza export's search data
(public business domains, titles, queries and positions; no contact details).
Route tests stub every network/paid step and block sockets.
"""
import csv
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
from agents.top_competitor_agent import reusable_report, select_top_competitor

BEST, NEAR, PLAIN = "best pizza Shirley,NY Shirley,NY", "pizza near Shirley,NY Shirley,NY", "Shirley,NY pizza Shirley,NY"
SEARCH = [  # (domain, url, page, position, query, title)
    ("groupon.com", "https://www.groupon.com/local/shirley-ny/italian-restaurants", 6, 56, NEAR, "Italian Restaurants in Shirley - Deals"),
    ("valpak.com", "https://www.valpak.com/coupons/printable/restaurants/shirley-ny", 3, 29, NEAR, "Shirley, NY Printable Free Online Restaurants Coupons"),
    ("jcpizzeriany.com", "https://jcpizzeriany.com/", 3, 23, NEAR, "https://jcpizzeriany.com/"),
    ("thepizzaweb.com", "https://thepizzaweb.com/", 4, 34, NEAR, "The Slice Pizzeria - 456 William Floyd Parkway Shirley NY"),
    ("herohavenpizzany.com", "https://herohavenpizzany.com/", 3, 25, PLAIN, "Hero Haven Pizza & More - Shirley, NY"),
    ("louiespizzeria.com", "https://louiespizzeria.com/", 4, 37, BEST, "Louie's Pizzeria - Italian Restaurant"),
    ("joespizzarestuarantshirleyny.com", "https://www.joespizzarestuarantshirleyny.com/", 2, 11, BEST, "Joes Pizza Restaurant (Shirley NY) - Shirley, NY"),
    ("slicelife.com", "https://slicelife.com/restaurants/ny/shirley/11967/bella-pizzeria/menu", 2, 15, BEST, "Bella Pizzeria - View Menu & Order Online"),
    ("locations.pizzahut.com", "https://locations.pizzahut.com/ny/shirley/123-william-floyd", 4, 36, BEST, "Pizza Deals in Shirley, NY"),
    ("carlosinshirley.com", "https://carlosinshirley.com/", 3, 30, BEST, "Carlo's Pizzeria and Ristorante in Shirley"),
]
FIELDS = ["industry", "market", "keyword", "title", "domain", "url", "serp_page", "serp_position", "base_keyword",
          "search_location", "scan_date", "query_used", "source", "lead_source_label"]


def write_export(directory, extra=()):
    path = Path(directory) / "leads_pizza_shirleyny_20261023_234902.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in [dict(zip(("domain", "url", "serp_page", "serp_position", "query_used", "title"), item)) for item in SEARCH] + list(extra):
            writer.writerow({"industry": "pizza", "market": "Shirley,NY", "keyword": "pizza Shirley,NY", "base_keyword": "pizza",
                             "search_location": "Shirley,NY", "scan_date": "2026-10-23 23:47:31", "source": "dataforseo",
                             "lead_source_label": "Google Organic"} | row)
    return path


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.export = write_export(self.tmp.name)

    def test_carlos_gets_the_highest_ranked_eligible_business_from_its_own_query(self):
        result = select_top_competitor(self.export, "carlosinshirley.com")
        self.assertEqual((result["status"], result["basis"]), ("selected", "same_query"))
        c = result["competitor"]
        self.assertEqual((c["domain"], c["position"], c["page"], c["ranking_type"], c["query"]),
                         ("joespizzarestuarantshirleyny.com", 11, 2, "organic", BEST))
        self.assertEqual((c["keyword"], c["location"], c["scan_date"]), ("pizza", "Shirley,NY", "2026-10-23 23:47:31"))
        reasons = {e["domain"]: e["reason"] for e in result["excluded"]}
        self.assertEqual(reasons["slicelife.com"], "online ordering platform")  # outranks Carlo's but is not a business site
        self.assertEqual(reasons["locations.pizzahut.com"], "national chain location page")
        self.assertEqual(reasons["valpak.com"], "coupon site")
        self.assertIn("groupon.com", reasons)

    def test_top_ranked_prospect_gets_the_best_competitor_below_it(self):
        result = select_top_competitor(self.export, "joespizzarestuarantshirleyny.com")
        self.assertEqual(result["competitor"]["domain"], "carlosinshirley.com")
        self.assertTrue(result["prospect_outranks"])

    def test_other_queries_are_only_a_labelled_fallback(self):
        result = select_top_competitor(self.export, "jcpizzeriany.com")
        self.assertEqual((result["basis"], result["competitor"]["domain"], result["competitor"]["position"]), ("same_query", "thepizzaweb.com", 34))
        lone = select_top_competitor(self.export, "herohavenpizzany.com")
        self.assertEqual((lone["basis"], lone["competitor"]["domain"]), ("related_query", "joespizzarestuarantshirleyny.com"))
        self.assertIn("not directly comparable", lone["note"])

    def test_other_query_rank_cannot_displace_the_best_same_query_competitor(self):
        extra = [
            {"domain": "joespizzarestuarantshirleyny.com", "url": "https://www.joespizzarestuarantshirleyny.com/",
             "serp_page": "1", "serp_position": "1", "query_used": NEAR, "title": "Joe's on another query"},
            {"domain": "rival-pizza.example", "url": "https://rival-pizza.example/",
             "serp_page": "2", "serp_position": "15", "query_used": BEST, "title": "Rival Pizza"},
        ]
        export = write_export(tempfile.mkdtemp(dir=self.tmp.name), extra)
        with export.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        for ordered_rows in (rows, list(reversed(rows))):
            with self.subTest(reversed=ordered_rows[0] == rows[-1]):
                with export.open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerows(ordered_rows)
                result = select_top_competitor(export, "carlosinshirley.com")
                self.assertEqual(result["basis"], "same_query")
                competitor = result["competitor"]
                self.assertEqual((competitor["domain"], competitor["query"], competitor["position"]),
                                 ("joespizzarestuarantshirleyny.com", BEST, 11))

    def test_fallback_query_is_not_chosen_by_comparing_unrelated_positions(self):
        extra = [{"domain": "jcpizzeriany.com", "url": "https://jcpizzeriany.com/",
                  "serp_page": "1", "serp_position": "1", "query_used": NEAR, "title": "JC Pizza"}]
        export = write_export(tempfile.mkdtemp(dir=self.tmp.name), extra)
        result = select_top_competitor(export, "herohavenpizzany.com")
        self.assertEqual((result["basis"], result["competitor"]["domain"], result["competitor"]["position"]),
                         ("related_query", "joespizzarestuarantshirleyny.com", 11))
        self.assertIsNone(result["prospect_outranks"])
        self.assertIn("not directly comparable", result["note"])

    def test_maps_results_are_never_ranked_against_organic_and_positions_are_never_invented(self):
        maps = [{"domain": "mapsonly-pizza.com", "url": "https://mapsonly-pizza.com/", "serp_page": "", "serp_position": "1",
                 "query_used": BEST, "title": "Maps Pizza", "source": "google_places", "lead_source_label": "Google Maps"}]
        export = write_export(tempfile.mkdtemp(dir=self.tmp.name), maps)
        result = select_top_competitor(export, "carlosinshirley.com")
        self.assertEqual(result["competitor"]["domain"], "joespizzarestuarantshirleyny.com")
        self.assertIn(("mapsonly-pizza.com", "nonorganic result (prospect is organic); rankings are not comparable"),
                      [(e["domain"], e["reason"]) for e in result["excluded"]])
        as_maps = select_top_competitor(export, "mapsonly-pizza.com")
        self.assertEqual(as_maps["status"], "none")  # no Maps competitor with a saved position
        self.assertIsNone(as_maps["prospect"]["position"])

    def test_missing_urls_duplicates_and_the_prospect_itself_are_excluded(self):
        extra = [{"domain": "", "url": "", "serp_page": "1", "serp_position": "2", "query_used": BEST, "title": "No website"},
                 {"domain": "joespizzarestuarantshirleyny.com", "url": "https://joespizzarestuarantshirleyny.com/menu", "serp_page": "3",
                  "serp_position": "27", "query_used": BEST, "title": "Joes menu page"},
                 {"domain": "shop.carlosinshirley.com", "url": "https://shop.carlosinshirley.com/", "serp_page": "1",
                  "serp_position": "3", "query_used": BEST, "title": "Carlo's shop"}]
        export = write_export(tempfile.mkdtemp(dir=self.tmp.name), extra)
        result = select_top_competitor(export, "carlosinshirley.com")
        self.assertEqual((result["competitor"]["domain"], result["competitor"]["position"]), ("joespizzarestuarantshirleyny.com", 11))
        reasons = [e["reason"] for e in result["excluded"]]
        self.assertIn("no usable website URL", reasons)
        self.assertIn("duplicate website", reasons)
        self.assertNotIn("shop.carlosinshirley.com", [result["competitor"]["domain"]])  # same site as the prospect

    def test_unknown_prospect_and_other_searches(self):
        self.assertEqual(select_top_competitor(self.export, "not-in-export.com")["status"], "not_found")
        other = [{"domain": "plumber.com", "url": "https://plumber.com/", "serp_page": "1", "serp_position": "1", "query_used": BEST,
                  "title": "Plumber", "base_keyword": "plumber"}]
        export = write_export(tempfile.mkdtemp(dir=self.tmp.name), other)
        self.assertNotEqual(select_top_competitor(export, "carlosinshirley.com")["competitor"]["domain"], "plumber.com")


class ReuseTests(unittest.TestCase):
    def entry(self, owner=1, stamp="2026-11-03_18-19-11-122180", site="https://joespizzarestuarantshirleyny.com", comp="https://carlosinshirley.com"):
        return {"owner_id": owner, "timestamp": stamp, "site_url": site, "competitor_url": comp,
                "site_final_url": "https://www.joespizzarestuarantshirleyny.com/", "saved_report": f"/reports/saved/{stamp}_x.html",
                "date": "November 03, 2026 06:19:11.122 PM · ID 180"}

    def test_reuses_only_the_owners_recent_matching_comparison(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        history = [self.entry(), self.entry(stamp="2026-11-03_18-10-47-993365"), self.entry(owner=2, stamp="2026-11-03_19-00-00-000001"),
                   self.entry(stamp="2026-09-01_10-00-00-000001")]
        found = reusable_report(history, 1, "https://www.joespizzarestuarantshirleyny.com/", "https://carlosinshirley.com/", now=now)
        self.assertEqual(found["timestamp"], "2026-11-03_18-19-11-122180")  # newest of the owner's, not user 2's
        self.assertIsNone(reusable_report(history, 1, "https://carlosinshirley.com/", "https://joespizzarestuarantshirleyny.com/", now=now))
        self.assertIsNone(reusable_report(history, None, "https://joespizzarestuarantshirleyny.com/", "https://carlosinshirley.com/", now=now))
        later = datetime(2026, 12, 20, tzinfo=timezone.utc)
        self.assertIsNone(reusable_report(history, 1, "https://joespizzarestuarantshirleyny.com/", "https://carlosinshirley.com/", now=later))


def page(url):
    return {"url": url, "domain": appmain.clean_domain(url), "clean_domain": appmain.clean_domain(url), "final_url": url,
            "title": "Pizza in Shirley NY", "title_length": 20, "title_status": "Good", "meta_description": "Pizza.", "meta_length": 6,
            "meta_status": "Too Short", "h1": "Pizza", "has_h1": True, "h1_count": 1, "h2_count": 1, "keywords": [], "top_terms": [],
            "word_count": 400, "has_meta": True, "internal_link_count": 3, "canonical": url, "image_count": 0, "alt_count": 0,
            "error": None, "score": 0, "geo_phrases": []}


class RouteTests(unittest.TestCase):
    USER = {"id": 1, "username": "owner@example.com", "role": "standard"}

    def setUp(self):
        appmain._analyze_rate_limit_attempts.clear(); appmain._analyze_submissions.clear()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.export = write_export(self.tmp.name)
        original = socket.socket.connect
        def guarded(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("network blocked in tests: " + str(address))
            return original(sock, address)
        self.fetched, self.saved, self.user = [], [], None
        stubs = {
            "validate_public_url": lambda url: url, "run_seo_agent": lambda results: {}, "enrich_keywords_with_dataforseo": lambda terms: [],
            "auto_save_geo_phrases": lambda phrases: None, "build_agent_action_plan": lambda *a, **k: "", "build_agent_insight_html": lambda *a, **k: "",
            "fetch_page_data": lambda url: (self.fetched.append(url), page(url))[1],
            "save_report_snapshot": lambda html, site, comp, *a, **k: (self.saved.append(site["url"]), {"saved_report": "/reports/saved/new-report.html"})[1],
            "auth_current_user": lambda request: self.user,
            "_login_required_user_or_response": lambda request: (self.user, None) if self.user else (None, appmain.RedirectResponse("/login", status_code=303)),
            "leadbot_user_can_access_export": lambda filename, request: bool(self.user) and filename == self.export.name,
            "load_report_history": lambda: [],
        }
        patches = [mock.patch.object(socket.socket, "connect", guarded)] + [mock.patch.object(appmain, k, v) for k, v in stubs.items()]
        patches.append(mock.patch("agents.lead_dashboard_agent.safe_export_file", lambda name: self.export if name == self.export.name else None))
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.client = TestClient(appmain.app)

    def top(self, domain="carlosinshirley.com", file=None):
        return self.client.get("/lead-bot/compare-top-competitor", params={"source_file": file or self.export.name, "source_domain": domain}, follow_redirects=False)

    def test_prefills_both_urls_and_explains_the_choice_without_scanning(self):
        self.user = self.USER
        response = self.top()
        self.assertEqual(response.status_code, 200, response.text[:300])
        html = response.text
        self.assertIn('value="https://carlosinshirley.com/"', html)
        self.assertIn('value="https://www.joespizzarestuarantshirleyny.com/"', html)
        for text in ("Why this competitor", "Google organic #11", "(page 2)", "best pizza Shirley,NY Shirley,NY", "Shirley,NY", "2026-10-23",
                     "online ordering platform", "Nothing runs until you click"):
            self.assertIn(text, html)
        self.assertIn('name="submission_id"', html)
        self.assertEqual((self.fetched, self.saved), ([], []))

    def test_offers_the_existing_report_instead_of_a_new_scan(self):
        self.user = self.USER
        existing = {"owner_id": 1, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S") + "-000001",
                    "site_url": "https://joespizzarestuarantshirleyny.com", "competitor_url": "https://carlosinshirley.com",
                    "saved_report": "/reports/saved/existing.html", "date": "November 03, 2026 06:19:11.122 PM · ID 180"}
        with mock.patch.object(appmain, "load_report_history", lambda: [existing]):
            html = self.top("joespizzarestuarantshirleyny.com").text
        self.assertIn("You already compared these two websites", html)
        self.assertIn('href="/reports/saved/existing.html"', html)

    def test_requires_sign_in_and_export_ownership(self):
        self.assertEqual(self.top().status_code, 303)
        self.user = self.USER
        self.assertEqual(self.top(file="someone-elses.csv").status_code, 403)
        self.assertEqual(self.top(file="../" + self.export.name).status_code, 403)
        self.assertEqual(self.top(domain="not-in-export.com").status_code, 404)

    def analyze(self, submission_id, url_2="https://carlosinshirley.com/"):
        return self.client.post("/analyze", data={"url_1": "https://www.joespizzarestuarantshirleyny.com/", "url_2": url_2,
                                                  "submission_id": submission_id}, follow_redirects=False)

    def test_refresh_never_repeats_a_scan(self):
        first = self.analyze("A" * 24)
        self.assertEqual(first.status_code, 200, first.text[:300])
        self.assertEqual(len(self.fetched), 2)
        again = self.analyze("A" * 24)  # browser refresh/resubmit of the same form
        self.assertEqual((again.status_code, len(self.fetched)), (409, 2))
        self.assertIn("was not run again", again.text)
        self.user = self.USER
        appmain._analyze_submissions.clear()
        self.analyze("B" * 24)
        repeat = self.analyze("B" * 24)
        self.assertEqual((repeat.status_code, repeat.headers["location"], len(self.fetched)), (303, "/reports/saved/new-report.html", 4))
        self.analyze("B" * 24, url_2="https://thepizzaweb.com/")  # changed URLs on the same page are a new comparison
        self.assertEqual(len(self.fetched), 6)

    def test_forms_without_an_id_still_work(self):
        self.client.post("/analyze", data={"url_1": "https://a-pizza.com/", "url_2": "https://b-pizza.com/"})
        self.client.post("/analyze", data={"url_1": "https://a-pizza.com/", "url_2": "https://b-pizza.com/"})
        self.assertEqual(len(self.fetched), 4)

    def test_run_again_opens_the_prefilled_form_instead_of_scanning(self):
        self.user = self.USER
        entry = {"owner_id": 1, "saved_report": "/reports/saved/x.html", "site_url": "https://a-pizza.com/", "competitor_url": "https://b-pizza.com/"}
        with mock.patch.object(appmain, "load_report_history", lambda: [entry]):
            response = self.client.get("/history/rerun", params={"saved_report": "/reports/saved/x.html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/compare?url_1=https%3A%2F%2Fa-pizza.com%2F&url_2="))
        self.assertEqual(self.fetched, [])

    def test_compare_page_has_a_fresh_submission_id_each_time(self):
        import re
        ids = [re.search(r'name="submission_id" value="([^"]+)"', self.client.get("/compare").text).group(1) for _ in range(2)]
        self.assertNotEqual(ids[0], ids[1])
        self.assertNotIn("Why this competitor", self.client.get("/compare").text)


class CardTests(unittest.TestCase):
    def test_saved_lead_card_offers_compare_with_top_competitor(self):
        from agents.lead_dashboard_agent import lead_cards
        row = {"domain": "carlosinshirley.com", "url": "https://carlosinshirley.com/", "title": "Carlo's", "serp_position": "30", "serp_page": "3"}
        html = lead_cards([row], selected_name="leads_pizza_shirleyny_20261023_234902.csv", csrf_token="t")
        self.assertIn('class="lead-top-competitor"', html)
        self.assertIn("/lead-bot/compare-top-competitor?source_file=leads_pizza_shirleyny_20261023_234902.csv&amp;source_domain=carlosinshirley.com", html)
        self.assertNotIn("lead-top-competitor", lead_cards([row], selected_name="", csrf_token="t"))


if __name__ == "__main__":
    unittest.main()
