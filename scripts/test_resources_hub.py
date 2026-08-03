"""Focused coverage for the public /resources SEO hub."""

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


class AsgiClient:
    def get(self, path):
        async def request():
            transport = httpx.ASGITransport(app=appmain.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(path)
        return asyncio.run(request())


GUIDES = (
    "/what-makes-a-good-lead",
    "/how-to-find-local-leads",
    "/how-to-find-local-business-leads-without-buying-a-lead-list",
    "/how-to-verify-local-business-leads-before-outreach",
    "/local-lead-generation",
    "/lead-list-vs-lead-finder",
)


class ResourcesHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = AsgiClient()
        cls.response = cls.client.get("/resources")
        cls.body = cls.response.text

    def test_route_and_exact_seo_fields(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertIn("<title>Lead Generation Resources | LeadMeLeads</title>", self.body)
        self.assertIn(
            'name="description" content="Explore practical guides to finding, '
            'reviewing, and organizing local business prospects for thoughtful outreach."',
            self.body,
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", self.body, re.DOTALL),
            ["Practical Lead Generation Resources"],
        )
        self.assertIn(
            'rel="canonical" href="https://leadmeleads.com/resources"',
            self.body,
        )

    def test_indexable_and_sitemap_exposed(self):
        self.assertIn('name="robots" content="index, follow"', self.body)
        self.assertNotIn("noindex", self.response.headers.get("X-Robots-Tag", "").lower())
        sitemap = self.client.get("/sitemap.xml").text
        self.assertIn("<loc>https://leadmeleads.com/resources</loc>", sitemap)
        robots = self.client.get("/robots.txt").text
        self.assertIn("Sitemap: https://leadmeleads.com/sitemap.xml", robots)
        self.assertNotIn("Disallow: /resources", robots)

    def test_links_to_all_guides_and_one_lead_finder_cta(self):
        for guide in GUIDES:
            self.assertIn(f'href="{guide}"', self.body)
        self.assertEqual(
            len(re.findall(r'<a class="inline-cta" href="/lead-bot">', self.body)),
            1,
        )

    def test_discovery_and_article_backlinks(self):
        homepage = self.client.get("/").text
        self.assertIn('href="/resources"', homepage)
        for guide in GUIDES:
            with self.subTest(guide=guide):
                guide_response = self.client.get(guide)
                self.assertEqual(guide_response.status_code, 200)
                self.assertIn('href="/resources"', guide_response.text)
                self.assertIn('name="robots" content="index, follow"', guide_response.text)
                self.assertNotIn(
                    "noindex",
                    guide_response.headers.get("X-Robots-Tag", "").lower(),
                )
                self.assertIn(
                    f'rel="canonical" href="{seo_meta.canonical_url(guide)}"',
                    guide_response.text,
                )

    def test_without_list_card_uses_natural_anchor_text(self):
        self.assertIn(
            'href="/how-to-find-local-business-leads-without-buying-a-lead-list">'
            "Learn how to research leads without buying a list</a>",
            self.body,
        )

    def test_verify_leads_card_uses_natural_anchor_text(self):
        self.assertIn(
            'href="/how-to-verify-local-business-leads-before-outreach">'
            "Learn how to verify leads before outreach</a>",
            self.body,
        )
        self.assertEqual(
            self.body.count('href="/how-to-verify-local-business-leads-before-outreach"'),
            1,
        )

    def test_no_faq_schema_on_hub(self):
        self.assertNotIn('"FAQPage"', self.body)

    def test_mobile_safe_markup_for_320_and_375(self):
        self.assertIn(
            'name="viewport" content="width=device-width, initial-scale=1.0"',
            self.body,
        )
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", self.body)
        self.assertIn("@media (max-width:700px)", self.body)
        self.assertIn(".resources-grid { grid-template-columns:1fr; }", self.body)
        self.assertIn("min-width:0", self.body)
        self.assertIn("overflow-wrap:anywhere", self.body)
        self.assertNotRegex(self.body, r'(?<!max-)(?<!min-)width:\s*[4-9]\d{2,}px')


if __name__ == "__main__":
    unittest.main()
