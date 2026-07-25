"""Focused SEO/AEO coverage for the three local-lead editorial guides."""

import asyncio
import html
import json
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


PAGES = {
    "/how-to-find-local-leads": {
        "h1": "How to Find Local Leads Worth Contacting",
        "page": seo_meta.FIND_LOCAL_LEADS_PAGE,
        "faq": seo_meta.FIND_LOCAL_LEADS_FAQ,
    },
    "/local-lead-generation": {
        "h1": "Local Lead Generation Without the Giant Lead List",
        "page": seo_meta.LOCAL_LEAD_GENERATION_PAGE,
        "faq": seo_meta.LOCAL_LEAD_GENERATION_FAQ,
    },
    "/lead-list-vs-lead-finder": {
        "h1": "Lead Lists vs. Lead Finders",
        "page": seo_meta.LEAD_LIST_VS_FINDER_PAGE,
        "faq": seo_meta.LEAD_LIST_VS_FINDER_FAQ,
    },
}


class LocalLeadContentClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = AsgiClient()
        cls.responses = {path: cls.client.get(path) for path in PAGES}
        cls.bodies = {path: response.text for path, response in cls.responses.items()}

    def test_routes_return_200_and_existing_editorial_page_still_works(self):
        for path, response in self.responses.items():
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/what-makes-a-good-lead").status_code, 200)

    def test_unique_titles_descriptions_canonicals_and_exact_h1s(self):
        titles, descriptions, canonicals = [], [], []
        for path, expected in PAGES.items():
            body = self.bodies[path]
            titles.append(re.search(r"<title>([^<]+)</title>", body).group(1))
            descriptions.append(re.search(r'<meta name="description" content="([^"]+)"', body).group(1))
            canonicals.append(re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1))
            self.assertEqual(titles[-1], expected["page"].title)
            self.assertEqual(html.unescape(descriptions[-1]), expected["page"].description)
            self.assertEqual(canonicals[-1], seo_meta.canonical_url(path))
            self.assertEqual(re.findall(r"<h1>(.*?)</h1>", body, re.DOTALL), [expected["h1"]])
        self.assertEqual(len(set(titles)), 3)
        self.assertEqual(len(set(descriptions)), 3)
        self.assertEqual(len(set(canonicals)), 3)

    def test_pages_are_indexable_without_noindex_header(self):
        for path, response in self.responses.items():
            with self.subTest(path=path):
                self.assertIn('name="robots" content="index, follow"', response.text)
                self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", "").lower())

    def test_sitemap_contains_all_three_pages(self):
        sitemap = self.client.get("/sitemap.xml").text
        for path in PAGES:
            self.assertIn(f"<loc>{seo_meta.canonical_url(path)}</loc>", sitemap)

    def test_visible_faq_exactly_matches_jsonld(self):
        for path, expected in PAGES.items():
            body = self.bodies[path]
            visible = [
                (html.unescape(re.sub(r"<[^>]+>", "", q)).strip(), html.unescape(re.sub(r"<[^>]+>", "", a)).strip())
                for q, a in re.findall(
                    r'<div class="faq-item"><h3>(.*?)</h3><p>(.*?)</p></div>',
                    body,
                    re.DOTALL,
                )
            ]
            scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
            faq_data = [json.loads(item) for item in scripts if '"FAQPage"' in item]
            self.assertEqual(len(faq_data), 1)
            schema = [
                (item["name"], item["acceptedAnswer"]["text"])
                for item in faq_data[0]["mainEntity"]
            ]
            source = [(item["question"], item["answer"]) for item in expected["faq"]]
            self.assertEqual(visible, source)
            self.assertEqual(schema, visible)

    def test_internal_links_resolve_and_each_page_has_one_cta(self):
        allowed = set(seo_meta.PUBLIC_INDEXABLE_PATHS)
        for path, body in self.bodies.items():
            links = re.findall(r'href="(/[^"#?]*)"', body)
            for target in links:
                if target.startswith("/static/"):
                    continue
                with self.subTest(page=path, target=target):
                    self.assertIn(target, allowed | {"/login", "/create-account", "/history", "/settings", "/logout"})
            self.assertEqual(
                re.findall(r'<a class="inline-cta" href="/lead-bot">', body),
                ['<a class="inline-cta" href="/lead-bot">'],
            )
            self.assertIn('href="/"', body)
            self.assertIn('href="/lead-bot"', body)
            self.assertIn('href="/compare"', body)
            self.assertIn('href="/what-makes-a-good-lead"', body)

    def test_mobile_safe_markup_and_overflow_guards(self):
        base = (Path(__file__).parent.parent / "app/templates/public_guide_base.html").read_text()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', base)
        self.assertIn("* { box-sizing: border-box; }", base)
        self.assertIn("@media (max-width:480px)", base)
        self.assertIn("max-width:100%", base)
        self.assertIn("overflow-wrap:anywhere", base)
        for body in self.bodies.values():
            self.assertNotRegex(body, r'(?<!max-)width:\s*[4-9]\d{2,}px')


if __name__ == "__main__":
    unittest.main()
