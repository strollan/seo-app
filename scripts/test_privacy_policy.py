"""Regression coverage for the public LeadMeLeads privacy-policy page."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import httpx

import app.main as appmain


class TestClient:
    def get(self, path):
        async def request():
            transport = httpx.ASGITransport(app=appmain.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(path)

        return asyncio.run(request())


class PrivacyPolicyPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.response = TestClient().get("/privacy-policy")
        cls.body = cls.response.text

    def test_page_is_public_and_has_required_metadata(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertIsNone(self.response.headers.get("x-robots-tag"))
        self.assertIn("<title>Privacy Policy | LeadMeLeads</title>", self.body)
        self.assertIn(
            'name="description" content="Read the LeadMeLeads privacy policy, including how Slayer Status uses WhatsApp Business Platform information."',
            self.body,
        )
        self.assertIn('href="https://leadmeleads.com/privacy-policy"', self.body)

    def test_policy_copy_and_footer_link_are_present(self):
        for text in (
            "Last updated: September 24, 2026",
            "WhatsApp and Slayer Status",
            "We do not sell WhatsApp message data or use it for advertising.",
            "leadmeleads@gmail.com",
        ):
            with self.subTest(text=text):
                self.assertIn(text, self.body)
        self.assertIn('href="/privacy-policy" aria-current="page">Privacy Policy</a>', self.body)

    def test_route_is_in_the_public_seo_allowlist(self):
        self.assertIn("/privacy-policy", appmain.seo_meta.PUBLIC_INDEXABLE_PATHS)


if __name__ == "__main__":
    unittest.main()
