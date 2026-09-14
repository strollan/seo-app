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
        # Every primary-styled CTA pointing at Lead Finder must use one of
        # the two approved labels: "Find Local Leads" on the hero, product,
        # and final-CTA conversion path, and the contextual "Open Lead
        # Finder" inside the "Which tool should you use?" card. The primary
        # label must appear repeatedly so the conversion wording stays
        # consistent, and every one of these links must resolve.
        ctas = re.findall(
            r'<a class="(?:btn btn-primary|dark)" href="/lead-bot">([^<]+)</a>',
            self.body,
        )
        approved = {"Find Local Leads", "Open Lead Finder"}
        self.assertGreaterEqual(len(ctas), 2)
        self.assertTrue(
            all(text.strip() in approved for text in ctas),
            f"unexpected Lead Finder CTA label(s): {ctas}",
        )
        self.assertGreaterEqual(
            sum(1 for text in ctas if text.strip() == "Find Local Leads"),
            2,
            "the primary conversion CTA wording is no longer consistent",
        )
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
        # The messaging was refined (f326d3b); what must keep existing is
        # the capability checklist and the honest caveat, not the old
        # sentence shapes.
        self.assertIn('id="what-you-get-heading">What You Get</h2>', self.body)
        capabilities = (
            "Business name and local search position",
            "Website, address, phone, and email when available",
            "Contact-page URL when found",
            "&ldquo;Why This Lead&rdquo; note",
            "Keyword and market attached to each result",
            "CSV export for signed-in users",
        )
        for copy in capabilities:
            self.assertIn(copy, self.body)
        # Real-businesses-from-local-search proof point must survive in the
        # mini-proof strip, and the caveat that contact data is incomplete
        # must remain visible so the list is not oversold.
        self.assertIn("Found from real local searches", self.body)
        self.assertIn("Public information varies", self.visible)
        self.assertIn("we do not always find every contact detail", self.visible)

    def test_export_copy_matches_real_csv_fields(self):
        # The refined messaging describes exports generically ("available
        # business, search, and contact fields"). What still matters: the
        # homepage export claims must match what the CSV writer actually
        # emits, the signed-in requirement must be real, and the CSV
        # description must not promise the "Why This Lead" note (that is a
        # dashboard context line, not an export field).
        self.assertIn(
            "CSV exports include the available business, search, and contact fields",
            self.visible,
        )
        self.assertIn(
            "export a CSV containing the available business, "
            "market, search-position, and contact fields",
            self.visible,
        )
        self.assertIn("CSV export for signed-in users", self.body)
        for field in (
            "title", "domain", "url", "keyword", "market", "serp_position",
            "best_phone", "emails", "contact_page_url",
        ):
            self.assertIn(field, lead_export_agent.EXPORT_FIELDS)
        self.assertTrue(
            any(route.path == "/lead-bot/export/{filename}" for route in appmain.app.routes)
        )
        # The "Why This Lead" note is dashboard context, not a CSV field:
        # the exporter must not emit one, so no export copy line may claim
        # the note is exported.
        self.assertFalse(
            any("why" in field or "reason" in field for field in lead_export_agent.EXPORT_FIELDS),
            "CSV exporter gained a why/reason field; revisit the export copy claims",
        )
        for line in self.body.splitlines():
            if "CSV" in line:
                self.assertNotIn("Why This Lead", line)

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
        # Copy refined in f326d3b replaced the pinned sentences. The
        # requirements they guarded must still hold: Google is credited as
        # the starting point, the comparison section is fair (no disparaging
        # filler), LeadMeLeads claims only a reviewable list built from
        # public data, and quality-over-volume positioning survives.
        self.assertIn("Google helps you find businesses.", self.body)
        section = re.search(
            r'id="google-comparison-heading".*?</section>', self.body, re.DOTALL
        )
        self.assertIsNotNone(section, "Google comparison section was removed")
        section_text = section.group(0)
        self.assertIn("LeadMeLeads and Google Search", section_text)
        self.assertIn(
            "Returns search results for you to open, assess, and track yourself.",
            section_text,
        )
        self.assertIn("reviewable list", section_text)
        self.assertIn("search position, public contact details", section_text)
        lower_section = re.sub(r"<[^>]+>", " ", section_text).lower()
        for slight in ("fails", "worse", "can't", "cannot", "clunky", "manual slog"):
            self.assertNotIn(slight, lower_section)
        self.assertIn("More names are not necessarily better leads", self.body)
        self.assertIn("leaves room for judgment", self.visible)
        self.assertIn("your outreach and discovery qualify the opportunity", self.visible)

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
