"""Focused conversion-content and responsive-safety checks for the homepage."""

import asyncio
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import httpx

import app.main as appmain
import app.seo_meta as seo_meta
from agents import lead_export_agent


class AsgiClient:
    def get(self, path):
        async def request():
            transport = httpx.ASGITransport(app=appmain.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(path)
        return asyncio.run(request())


class HomepageConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = AsgiClient()
        cls.response = cls.client.get("/")
        cls.body = cls.response.text
        cls.visible = " ".join(re.sub(r"<[^>]+>", " ", cls.body).split())

    def test_homepage_and_h1(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", self.body, re.DOTALL),
            ["Find local leads worth contacting."],
        )

    def test_consistent_primary_ctas_resolve(self):
        ctas = re.findall(
            r'<a class="(?:btn btn-primary|dark)" href="/lead-bot">([^<]+)</a>',
            self.body,
        )
        self.assertGreaterEqual(len(ctas), 2)
        self.assertTrue(all(text.strip() == "Find Local Leads" for text in ctas))
        self.assertTrue(any(route.path == "/lead-bot" for route in appmain.app.routes))
        primary_css = re.search(r"\.btn-primary\s*\{(.*?)\}", self.body, re.DOTALL).group(1)
        secondary_css = re.search(r"\.btn-secondary\s*\{(.*?)\}", self.body, re.DOTALL).group(1)
        self.assertIn("background: #ffffff", primary_css)
        self.assertIn("background: rgba(15, 23, 42, 0.58)", secondary_css)

    def test_how_it_works_three_steps(self):
        self.assertIn('id="how-it-works-heading">How It Works</h2>', self.body)
        for number, title in (("1", "Search"), ("2", "Review"), ("3", "Export")):
            self.assertIn(f'<span class="step-number">{number}</span>', self.body)
            self.assertIn(f"<strong>{title}</strong>", self.body)

    def test_what_you_get_real_capabilities_and_caveat(self):
        self.assertIn('id="what-you-get-heading">What You Get</h2>', self.body)
        required = (
            "Real businesses found from local searches",
            "Original Google search position",
            "Phone and email when available",
            "Business address and contact-page information when found",
            "&ldquo;Why This Lead&rdquo; context",
            "Exportable prospect lists",
            "Not every prospect will have every contact field",
        )
        for copy in required:
            self.assertIn(copy, self.body)

    def test_export_copy_matches_real_csv_fields(self):
        copy = (
            "Export prospect lists as CSV with business details, keyword and market, "
            "search position, and phone, email, and contact-page URL when available."
        )
        self.assertIn(copy, self.visible)
        for field in (
            "title", "domain", "url", "keyword", "market", "serp_position",
            "best_phone", "emails", "contact_page_url",
        ):
            self.assertIn(field, lead_export_agent.EXPORT_FIELDS)
        self.assertNotIn("Why This Lead", copy)

    def test_homepage_owns_worth_contacting_title_and_h1_intent(self):
        self.assertEqual(
            seo_meta.HOME_PAGE.title,
            "LeadMeLeads — Find Local Leads Worth Contacting",
        )
        guide_expectations = {
            "/how-to-find-local-leads": (
                "How to Find Local Leads: A Step-by-Step Process | LeadMeLeads",
                "How to Find Local Leads",
            ),
            "/what-makes-a-good-lead": (
                "What Makes a Good Lead? A Practical Guide | LeadMeLeads",
                "What Makes a Good Lead?",
            ),
        }
        for path, (title, h1) in guide_expectations.items():
            page = self.client.get(path).text
            self.assertIn(f"<title>{title}</title>", page)
            self.assertEqual(re.findall(r"<h1>(.*?)</h1>", page, re.DOTALL), [h1])
            self.assertNotIn("worth contacting", title.lower())
            self.assertNotIn("worth contacting", h1.lower())
            self.assertIn(
                f'rel="canonical" href="{seo_meta.canonical_url(path)}"',
                page,
            )

    def test_balanced_google_comparison_and_quality_positioning(self):
        self.assertIn("Google helps you find businesses.", self.body)
        self.assertIn("Helps you search through businesses one by one.", self.body)
        self.assertIn(
            "Organizes businesses, contact information, search context, and "
            "prospect-review signals in one place.",
            self.visible,
        )
        self.assertIn("More names are not necessarily better leads", self.body)
        self.assertIn("easier to overlook in ordinary search results", self.visible)

    def test_no_unsupported_intent_or_guarantee_claims(self):
        lower = self.visible.lower()
        forbidden = (
            "sales intent",
            "buying intent",
            "guaranteed opportunity",
            "guaranteed qualification",
            "ready to buy",
            "qualified buyer",
            "every lead has",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, lower)

    def test_resources_and_good_lead_links_resolve(self):
        for path in ("/resources", "/what-makes-a-good-lead"):
            self.assertIn(f'href="{path}"', self.body)
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_responsive_safeguards_cover_requested_widths(self):
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', self.body)
        self.assertIn("* {\n    box-sizing: border-box;", self.body)
        self.assertIn("@media (max-width: 940px)", self.body)
        self.assertIn("@media (max-width: 620px)", self.body)
        self.assertIn("@media (max-width: 560px)", self.body)
        self.assertIn("minmax(0, 1fr)", self.body)
        self.assertIn("min-width: 0", self.body)
        self.assertNotRegex(self.body, r'(?<!max-)(?<!min-)width:\s*[4-9]\d{2,}px')


if __name__ == "__main__":
    unittest.main()
