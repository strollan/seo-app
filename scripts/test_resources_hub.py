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


RESOURCE_IMAGES_DIR = Path(__file__).resolve().parent.parent / "app/static/images/resources"


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
    "/how-to-find-website-seo-opportunities-in-a-lead-list",
    "/check-contactability-local-business-leads",
    "/compare-prospect-website-to-outranking-competitor",
    "/local-lead-generation",
    "/lead-list-vs-lead-finder",
)

WITH_LIST_PATH = "/how-to-find-local-business-leads-without-buying-a-lead-list"
HERO_IMAGE_PATH = "/static/images/resources/find-local-business-leads-hero.webp"
CARD_IMAGES = {
    "/how-to-find-local-business-leads-without-buying-a-lead-list": (
        "/static/images/resources/find-local-business-leads-card.webp",
        "Local storefronts connected to search results, a map pin, and verified contact cards.",
    ),
    "/how-to-find-website-seo-opportunities-in-a-lead-list": (
        "/static/images/resources/website-seo-opportunities-lead-list-card.webp",
        "Local lead list dashboard showing website and SEO opportunities for local businesses.",
    ),
    "/check-contactability-local-business-leads": (
        "/static/images/resources/contactability-lead-check-card.webp",
        "LeadMeLeads lead detail showing a local business's phone, email, website, and contact-page information, verified address, and notes on why the lead may be worth contacting.",
    ),
    "/compare-prospect-website-to-outranking-competitor": (
        "/static/images/resources/compare-outranking-competitor-card.webp",
        "Two competing websites shown side by side with a middle column listing differences between them.",
    ),
}
GUIDES_WITHOUT_HEROES = tuple(guide for guide in GUIDES if guide not in CARD_IMAGES)
CARD_TITLES_AND_DESCRIPTIONS = (
    ("What Makes a Good Lead?", "Learn how market fit, contactability, and a specific reason for outreach separate a useful prospect from another name on a list."),
    ("How to Find Local Leads", "Follow a practical process for choosing a market, finding reachable businesses, reviewing websites, and organizing research before outreach."),
    ("Find Local Business Leads Without Buying a List", "Use a focused search-and-review workflow as an alternative to starting with a purchased lead list."),
    ("Verify Local Business Leads Before Outreach", "Confirm business status, websites, contact details, and location before contacting a prospect."),
    ("Find Website and SEO Opportunities in a Lead List", "Audit a local lead list step by step: check websites, search visibility, service gaps, and technical fundamentals, then prioritize what to verify."),
    ("Check for Contactability in Local Business Leads", "Check websites, phone numbers, email and forms, map presence, and social profiles, then sort leads into confidence tiers before outreach."),
    ("Compare a Prospect's Website to the Competitor That Outranks It", "Run a head-to-head comparison against the site winning the searches your prospect wants: what to measure, and how to present the gap without overclaiming."),
    ("Local Lead Generation Without the Giant Lead List", "Compare inbound and outbound methods, referrals, directories, purchased lists, and hands-on business research."),
    ("Lead Lists vs. Lead Finders", "Compare static lead lists and active prospect finding across freshness, targeting, contact data, context, and research time."),
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

    def test_each_card_has_one_stretched_title_link_and_consistent_cta(self):
        cards = re.findall(r'<article class="resource-card">(.*?)</article>', self.body, re.DOTALL)
        self.assertEqual(len(cards), len(GUIDES))
        for card, guide in zip(cards, GUIDES):
            with self.subTest(guide=guide):
                links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', card, re.DOTALL)
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0][0], guide)
                self.assertIn('class="resource-card-title-link"', card)
                title = re.search(r'<h2><a[^>]*>(.*?)</a></h2>', card, re.DOTALL)
                self.assertIsNotNone(title)
                self.assertNotIn("Learn how", links[0][1])
                self.assertIn('<span class="resource-card-cta" aria-hidden="true">Read guide →</span>', card)
        self.assertEqual(self.body.count('class="resource-card-cta"'), len(GUIDES))
        self.assertIn('.resource-card-title-link::after', self.body)
        self.assertIn('position:absolute; inset:0', self.body)
        self.assertIn('.resource-card:hover, .resource-card:focus-within', self.body)
        self.assertIn('.resource-card-title-link:focus-visible', self.body)

    def test_suitable_guide_heroes_map_to_sized_card_images_and_resolve(self):
        cards = re.findall(r'<article class="resource-card">(.*?)</article>', self.body, re.DOTALL)
        self.assertEqual(len(CARD_IMAGES), 4)
        for card, guide in zip(cards, GUIDES):
            image_path, alt = CARD_IMAGES.get(guide, (None, None))
            with self.subTest(guide=guide):
                if image_path:
                    self.assertIn(
                        f'<img class="resource-card-image" src="{image_path}" width="720" height="405" loading="lazy" alt="{alt}">',
                        card,
                    )
                    asset_path = RESOURCE_IMAGES_DIR / Path(image_path).name
                    self.assertTrue(asset_path.is_file())
                    self.assertGreater(asset_path.stat().st_size, 0)
                else:
                    self.assertNotIn('class="resource-card-image"', card)
        self.assertEqual(self.body.count('class="resource-card-image"'), len(CARD_IMAGES))
        self.assertIn("aspect-ratio:16 / 9", self.body)
        self.assertIn("object-fit:cover", self.body)

    def test_existing_lead_list_article_hero_remains_sized_and_served(self):
        article = self.client.get(WITH_LIST_PATH)
        self.assertEqual(article.status_code, 200)
        self.assertIn(
            f'<img src="{HERO_IMAGE_PATH}" width="1280" height="720" '
            'loading="eager" fetchpriority="high" '
            'alt="Local storefronts connected to search results, a map pin, and verified contact cards.">',
            article.text,
        )
        self.assertLess(
            article.text.index(f'<figure class="article-hero">'),
            article.text.index('<p class="lede">'),
        )
        hero_asset = RESOURCE_IMAGES_DIR / Path(HERO_IMAGE_PATH).name
        self.assertTrue(hero_asset.is_file())
        self.assertGreater(hero_asset.stat().st_size, 0)

    def test_guides_without_heroes_have_no_broken_images_and_card_copy_order_remains_unchanged(self):
        cards = re.findall(r'<article class="resource-card">(.*?)</article>', self.body, re.DOTALL)
        self.assertEqual(len(cards), 9)
        for card, (title, description), guide in zip(cards, CARD_TITLES_AND_DESCRIPTIONS, GUIDES):
            with self.subTest(guide=guide):
                self.assertIn(f'<h2><a class="resource-card-title-link" href="{guide}">{title}</a></h2>', card)
                self.assertIn(f'<p>{description}</p>', card)
        for card, guide in zip(cards, GUIDES):
            if guide in GUIDES_WITHOUT_HEROES:
                self.assertNotIn('class="resource-card-image"', card)
        for guide in GUIDES:
            with self.subTest(guide=guide):
                self.assertEqual(self.client.get(guide).status_code, 200)

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
