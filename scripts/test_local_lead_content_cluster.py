"""Focused SEO/AEO coverage for the local-lead editorial guides."""

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
        "h1": "How to Find Local Leads",
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

WITHOUT_LIST_PATH = "/how-to-find-local-business-leads-without-buying-a-lead-list"
VERIFY_LEADS_PATH = "/how-to-verify-local-business-leads-before-outreach"
OPPORTUNITIES_PATH = "/how-to-find-website-seo-opportunities-in-a-lead-list"
CONTACTABILITY_PATH = "/check-contactability-local-business-leads"
COMPARE_OUTRANKING_PATH = "/compare-prospect-website-to-outranking-competitor"

# Utility routes that are intentional noindex pages -- deliberately absent
# from PUBLIC_INDEXABLE_PATHS -- yet are legitimate internal link targets
# (e.g. the "Contact us" link in the shared guide footer). Enumerated
# narrowly: anything not listed here or in PUBLIC_INDEXABLE_PATHS still
# fails the link-resolution assertions below.
UTILITY_NOINDEX_PATHS = frozenset({"/contact"})


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
        self.assertEqual(self.client.get(WITHOUT_LIST_PATH).status_code, 200)
        self.assertEqual(self.client.get(VERIFY_LEADS_PATH).status_code, 200)

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
                    self.assertIn(
                        target,
                        allowed
                        | UTILITY_NOINDEX_PATHS
                        | {"/login", "/create-account", "/history", "/settings", "/logout"},
                    )
            self.assertEqual(
                re.findall(r'<a class="inline-cta" href="/lead-bot">', body),
                ['<a class="inline-cta" href="/lead-bot">'],
            )
            self.assertIn('href="/"', body)
            self.assertIn('href="/lead-bot"', body)
            self.assertIn('href="/compare"', body)
            self.assertIn('href="/what-makes-a-good-lead"', body)

    def test_without_list_article_links_to_five_intended_routes(self):
        body = self.client.get(WITHOUT_LIST_PATH).text
        expected = {
            "/lead-bot": "LeadMeLeads Lead Finder",
            "/compare": "Website Comparison Tool",
            "/what-makes-a-good-lead": "what makes a good lead",
            "/lead-list-vs-lead-finder": "lead lists vs. lead finders",
            "/how-to-find-local-leads": "how to find local leads",
        }
        for route, anchor in expected.items():
            self.assertIn(f'href="{route}">{anchor}</a>', body)

    def test_without_list_article_schema_and_approved_copy(self):
        response = self.client.get(WITHOUT_LIST_PATH)
        self.assertEqual(response.status_code, 200)
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        articles = [json.loads(item) for item in scripts if '"Article"' in item]
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["@type"], "Article")
        self.assertEqual(
            articles[0]["mainEntityOfPage"],
            seo_meta.canonical_url(WITHOUT_LIST_PATH),
        )
        self.assertNotIn('"FAQPage"', response.text)
        approved_copy = (
            "Why Purchased Lead Lists Become Outdated",
            "Purchased lead lists can become outdated quickly. Businesses close, move, change ownership, or update their contact information.",
            "LeadMeLeads finds businesses currently appearing in search results and collects publicly available contact and website information. That gives you a more useful starting point than a static list.",
            "The results are still prospects to investigate—not guaranteed buyers. Contact details may be incomplete, so each business should be reviewed and verified before outreach.",
        )
        for exact_text in approved_copy:
            self.assertEqual(response.text.count(exact_text), 1)

    def test_without_list_template_has_no_draft_or_placeholder_markup(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/how_to_find_local_business_leads_without_buying_a_lead_list.html"
        ).read_text(encoding="utf-8")
        self.assertTrue(source.startswith('{% extends "public_guide_base.html" %}'))
        for forbidden in ("<!DOCTYPE html>", "<style>", "draft-banner", "placeholder-", "DRAFT"):
            self.assertNotIn(forbidden, source)

    def test_verify_leads_exact_metadata_and_single_h1(self):
        response = self.client.get(VERIFY_LEADS_PATH)
        body = response.text
        self.assertEqual(response.status_code, 200)
        title = re.search(r"<title>([^<]+)</title>", body).group(1)
        description = html.unescape(
            re.search(r'<meta name="description" content="([^"]+)"', body).group(1)
        )
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
        self.assertEqual(title, seo_meta.VERIFY_LOCAL_BUSINESS_LEADS_PAGE.title)
        self.assertEqual(title, "Verify Local Business Leads Before Outreach | LeadMeLeads")
        self.assertEqual(description, seo_meta.VERIFY_LOCAL_BUSINESS_LEADS_PAGE.description)
        self.assertEqual(canonical, seo_meta.canonical_url(VERIFY_LEADS_PATH))
        self.assertEqual(
            canonical,
            "https://leadmeleads.com/how-to-verify-local-business-leads-before-outreach",
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", body, re.DOTALL),
            ["How to Verify Local Business Leads Before Outreach"],
        )
        self.assertIn('name="robots" content="index, follow"', body)
        self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", "").lower())

    def test_verify_leads_sitemap_contains_page_exactly_once(self):
        sitemap = self.client.get("/sitemap.xml").text
        loc = f"<loc>{seo_meta.canonical_url(VERIFY_LEADS_PATH)}</loc>"
        self.assertEqual(sitemap.count(loc), 1)

    def test_verify_leads_resources_hub_lists_article_once(self):
        resources = self.client.get("/resources").text
        self.assertEqual(
            resources.count(f'href="{VERIFY_LEADS_PATH}"'),
            1,
        )

    def test_verify_leads_schema_and_no_faq(self):
        response = self.client.get(VERIFY_LEADS_PATH)
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        articles = [json.loads(item) for item in scripts if '"Article"' in item]
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["@type"], "Article")
        self.assertEqual(
            articles[0]["mainEntityOfPage"],
            seo_meta.canonical_url(VERIFY_LEADS_PATH),
        )
        self.assertEqual(articles[0]["headline"], seo_meta.VERIFY_LOCAL_BUSINESS_LEADS_PAGE.title)
        self.assertNotIn('"FAQPage"', response.text)

    def test_verify_leads_discloses_public_data_and_independent_verification(self):
        body = self.client.get(VERIFY_LEADS_PATH).text
        self.assertIn(
            "LeadMeLeads compiles publicly available business and website "
            "information as a starting point for this kind of research, not "
            "as a finished, confirmed contact list. Each business still needs "
            "to be independently verified before you reach out.",
            body,
        )

    def test_verify_leads_duplicate_resolution_uses_balanced_multi_source_guidance(self):
        body = self.client.get(VERIFY_LEADS_PATH).text
        self.assertIn(
            "The business's own website is often a useful first-party source, "
            "but it isn't automatically the newest or most accurate one—when "
            "listings disagree, compare the website against other current "
            "business profiles or confirm the detail directly with the "
            "business.",
            body,
        )
        self.assertNotIn(
            "Treat the version published on the business's own website as the "
            "most current source",
            body,
        )

    def test_verify_leads_links_to_intended_routes(self):
        body = self.client.get(VERIFY_LEADS_PATH).text
        expected = {
            "/lead-bot": "LeadMeLeads Lead Finder",
            "/compare": "Website Comparison Tool",
            WITHOUT_LIST_PATH: "how to find local business leads without buying a list",
        }
        for route, anchor in expected.items():
            self.assertIn(f'href="{route}">{anchor}</a>', body)

    def test_verify_leads_links_resolve_to_indexable_paths(self):
        allowed = set(seo_meta.PUBLIC_INDEXABLE_PATHS)
        body = self.client.get(VERIFY_LEADS_PATH).text
        links = re.findall(r'href="(/[^"#?]*)"', body)
        for target in links:
            if target.startswith("/static/"):
                continue
            with self.subTest(target=target):
                self.assertIn(
                    target,
                    allowed
                    | UTILITY_NOINDEX_PATHS
                    | {"/login", "/create-account", "/history", "/settings", "/logout"},
                )

    def test_verify_leads_template_has_no_draft_or_placeholder_markup(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/how_to_verify_local_business_leads_before_outreach.html"
        ).read_text(encoding="utf-8")
        self.assertTrue(source.startswith('{% extends "public_guide_base.html" %}'))
        for forbidden in ("<!DOCTYPE html>", "<style>", "draft-banner", "placeholder-", "DRAFT"):
            self.assertNotIn(forbidden, source)

    def test_verify_leads_word_count_in_target_range(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/how_to_verify_local_business_leads_before_outreach.html"
        ).read_text(encoding="utf-8")
        start = source.index("{% block content %}") + len("{% block content %}")
        end = source.index("{% endblock %}", start)
        plain = re.sub(r"<[^>]+>", " ", source[start:end])
        word_count = len(plain.split())
        self.assertGreaterEqual(word_count, 900)
        self.assertLessEqual(word_count, 1200)

    def test_opportunities_article_exact_metadata_and_single_h1(self):
        response = self.client.get(OPPORTUNITIES_PATH)
        body = response.text
        self.assertEqual(response.status_code, 200)
        title = re.search(r"<title>([^<]+)</title>", body).group(1)
        description = html.unescape(
            re.search(r'<meta name="description" content="([^"]+)"', body).group(1)
        )
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
        self.assertEqual(
            title,
            seo_meta.FIND_WEBSITE_SEO_OPPORTUNITIES_IN_LEAD_LIST_PAGE.title,
        )
        self.assertEqual(
            title,
            "How to Find Website and SEO Opportunities in a Local Lead List | LeadMeLeads",
        )
        self.assertEqual(
            description,
            seo_meta.FIND_WEBSITE_SEO_OPPORTUNITIES_IN_LEAD_LIST_PAGE.description,
        )
        self.assertEqual(description, (
            "A practical framework for auditing a local lead list to spot "
            "website and SEO gaps worth prioritizing before outreach."
        ))
        self.assertEqual(canonical, seo_meta.canonical_url(OPPORTUNITIES_PATH))
        self.assertEqual(
            canonical,
            "https://leadmeleads.com/how-to-find-website-seo-opportunities-in-a-lead-list",
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", body, re.DOTALL),
            ["How to Find Website and SEO Opportunities in a Local Lead List"],
        )
        self.assertIn('name="robots" content="index, follow"', body)
        self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", "").lower())

    def test_opportunities_sitemap_contains_page_exactly_once(self):
        sitemap = self.client.get("/sitemap.xml").text
        loc = f"<loc>{seo_meta.canonical_url(OPPORTUNITIES_PATH)}</loc>"
        self.assertEqual(sitemap.count(loc), 1)

    def test_opportunities_resources_hub_lists_article_once(self):
        resources = self.client.get("/resources").text
        self.assertEqual(resources.count(f'href="{OPPORTUNITIES_PATH}"'), 1)

    def test_opportunities_article_has_hero_and_inline_images_with_alt_text(self):
        body = self.client.get(OPPORTUNITIES_PATH).text
        hero_src = "/static/images/resources/website-seo-opportunities-lead-list-hero.png"
        inline_src = "/static/images/resources/website-seo-opportunities-lead-review.png"
        self.assertEqual(body.count(hero_src), 1)
        self.assertEqual(body.count(inline_src), 1)
        imgs_by_src = {}
        for tag in re.findall(r"<img\b[^>]*>", body):
            src_match = re.search(r'src="([^"]+)"', tag)
            if not src_match:
                continue
            alt_match = re.search(r'alt="([^"]*)"', tag)
            imgs_by_src[src_match.group(1)] = alt_match.group(1) if alt_match else None
        self.assertEqual(
            imgs_by_src.get(hero_src),
            "Local lead list dashboard showing website and SEO "
            "opportunities for local businesses",
        )
        self.assertEqual(
            imgs_by_src.get(inline_src),
            "LeadMeLeads local business lead detail showing website, "
            "contact and search information",
        )

    def test_opportunities_schema_and_no_faq(self):
        response = self.client.get(OPPORTUNITIES_PATH)
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        articles = [json.loads(item) for item in scripts if '"Article"' in item]
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["@type"], "Article")
        self.assertEqual(
            articles[0]["mainEntityOfPage"],
            seo_meta.canonical_url(OPPORTUNITIES_PATH),
        )
        self.assertEqual(
            articles[0]["headline"],
            seo_meta.FIND_WEBSITE_SEO_OPPORTUNITIES_IN_LEAD_LIST_PAGE.title,
        )
        self.assertNotIn('"FAQPage"', response.text)

    def test_opportunities_links_resolve_to_indexable_paths(self):
        allowed = set(seo_meta.PUBLIC_INDEXABLE_PATHS)
        body = self.client.get(OPPORTUNITIES_PATH).text
        links = re.findall(r'href="(/[^"#?]*)"', body)
        for target in links:
            if target.startswith("/static/"):
                continue
            with self.subTest(target=target):
                self.assertIn(
                    target,
                    allowed
                    | UTILITY_NOINDEX_PATHS
                    | {"/login", "/create-account", "/history", "/settings", "/logout"},
                )

    def test_opportunities_article_has_no_links_to_unpublished_articles(self):
        body = self.client.get(OPPORTUNITIES_PATH).text
        self.assertNotIn("/how-to-decide-which-local-leads-to-contact-first", body)
        self.assertNotIn("/website-seo-gaps-personalized-outreach", body)

    def test_opportunities_keeps_lead_finder_cta_and_approved_copy(self):
        body = self.client.get(OPPORTUNITIES_PATH).text
        self.assertIn('href="/lead-bot">LeadMeLeads\' Lead Finder</a>', body)
        approved_copy = (
            "Why This Matters More Than List Size",
            "Step 1: Confirm the Website Actually Exists and Loads",
            "Don't filter out an active business just because its website is missing, broken, parked, or hard to find.",
            "Step 7: Prioritize the List Based on What You Found",
            "A Note on What This Process Doesn't Tell You",
        )
        for exact_text in approved_copy:
            self.assertEqual(body.count(exact_text), 1)

    def test_opportunities_template_has_no_draft_placeholder_or_editorial_scaffolding(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/how_to_find_website_seo_opportunities_in_a_lead_list.html"
        ).read_text(encoding="utf-8")
        self.assertTrue(source.startswith('{% extends "public_guide_base.html" %}'))
        for forbidden in (
            "<!DOCTYPE html>",
            "<style>",
            "draft-banner",
            "placeholder-",
            "DRAFT",
            "SEO Title:",
            "Meta Description:",
            "Slug:",
            "Target keyword:",
            "Topics:",
            "Internal Link Opportunities",
            "Image Concepts",
            "Source / Verification Notes",
            "[[CANONICAL_URL]]",
            "[[IMAGE_URL]]",
            "[[PUBLISHER_LOGO_URL]]",
            "[[PUBLISH_DATE]]",
        ):
            self.assertNotIn(forbidden, source)

    def test_opportunities_word_count_in_target_range(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/how_to_find_website_seo_opportunities_in_a_lead_list.html"
        ).read_text(encoding="utf-8")
        start = source.index("{% block content %}") + len("{% block content %}")
        end = source.index("{% endblock %}", start)
        plain = re.sub(r"<[^>]+>", " ", source[start:end])
        word_count = len(plain.split())
        self.assertGreaterEqual(word_count, 1300)
        self.assertLessEqual(word_count, 1700)

    def test_contactability_article_exact_metadata_and_single_h1(self):
        response = self.client.get(CONTACTABILITY_PATH)
        body = response.text
        self.assertEqual(response.status_code, 200)
        title = re.search(r"<title>([^<]+)</title>", body).group(1)
        description = html.unescape(
            re.search(r'<meta name="description" content="([^"]+)"', body).group(1)
        )
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
        self.assertEqual(
            title,
            seo_meta.CHECK_CONTACTABILITY_LOCAL_BUSINESS_LEADS_PAGE.title,
        )
        self.assertEqual(
            title,
            "How to Check for Contactability in Local Business Leads | LeadMeLeads",
        )
        self.assertEqual(
            description,
            seo_meta.CHECK_CONTACTABILITY_LOCAL_BUSINESS_LEADS_PAGE.description,
        )
        self.assertEqual(description, (
            "A practical, diagnostic guide to checking contactability in local "
            "business leads so you can prioritize outreach and research using "
            "observable contact signals."
        ))
        self.assertEqual(canonical, seo_meta.canonical_url(CONTACTABILITY_PATH))
        self.assertEqual(
            canonical,
            "https://leadmeleads.com/check-contactability-local-business-leads",
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", body, re.DOTALL),
            ["How to Check for Contactability in Local Business Leads"],
        )
        self.assertIn('name="robots" content="index, follow"', body)
        self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", "").lower())

    def test_contactability_sitemap_contains_page_exactly_once(self):
        sitemap = self.client.get("/sitemap.xml").text
        loc = f"<loc>{seo_meta.canonical_url(CONTACTABILITY_PATH)}</loc>"
        self.assertEqual(sitemap.count(loc), 1)

    def test_contactability_resources_hub_lists_article_once(self):
        resources = self.client.get("/resources").text
        self.assertEqual(resources.count(f'href="{CONTACTABILITY_PATH}"'), 1)

    def test_contactability_article_has_hero_and_inline_images_with_alt_text(self):
        body = self.client.get(CONTACTABILITY_PATH).text
        hero_src = "/static/images/resources/contactability-lead-check-hero.png"
        inline_src = "/static/images/resources/contactability-cross-check.png"
        self.assertEqual(body.count(hero_src), 1)
        self.assertEqual(body.count(inline_src), 1)
        imgs_by_src = {}
        for tag in re.findall(r"<img\b[^>]*>", body):
            src_match = re.search(r'src="([^"]+)"', tag)
            if not src_match:
                continue
            alt_match = re.search(r'alt="([^"]*)"', tag)
            imgs_by_src[src_match.group(1)] = alt_match.group(1) if alt_match else None
        self.assertEqual(
            imgs_by_src.get(hero_src),
            "LeadMeLeads lead detail showing a local business's phone, email, "
            "website, and contact-page information, verified address, and "
            "notes on why the lead may be worth contacting.",
        )
        self.assertEqual(
            imgs_by_src.get(inline_src),
            "Comparing a business website contact page and map listing to "
            "cross-check phone number, address, and contact details.",
        )

    def test_contactability_inline_image_follows_the_repeatable_check_section(self):
        body = self.client.get(CONTACTABILITY_PATH).text
        section_heading = "<h2>A repeatable contactability check</h2>"
        next_heading = "<h2>Reading the signals: present versus usable</h2>"
        self.assertEqual(body.count(section_heading), 1)
        self.assertLess(
            body.index(section_heading), body.index("/static/images/resources/contactability-cross-check.png")
        )
        self.assertLess(
            body.index("/static/images/resources/contactability-cross-check.png"),
            body.index(next_heading),
        )

    def test_contactability_schema_and_no_faq(self):
        response = self.client.get(CONTACTABILITY_PATH)
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        articles = [json.loads(item) for item in scripts if '"Article"' in item]
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["@type"], "Article")
        self.assertEqual(
            articles[0]["mainEntityOfPage"],
            seo_meta.canonical_url(CONTACTABILITY_PATH),
        )
        self.assertEqual(
            articles[0]["headline"],
            seo_meta.CHECK_CONTACTABILITY_LOCAL_BUSINESS_LEADS_PAGE.title,
        )
        self.assertNotIn('"FAQPage"', response.text)

    def test_contactability_links_resolve_to_indexable_paths(self):
        allowed = set(seo_meta.PUBLIC_INDEXABLE_PATHS)
        body = self.client.get(CONTACTABILITY_PATH).text
        links = re.findall(r'href="(/[^"#?]*)"', body)
        for target in links:
            if target.startswith("/static/"):
                continue
            with self.subTest(target=target):
                self.assertIn(
                    target,
                    allowed
                    | UTILITY_NOINDEX_PATHS
                    | {"/login", "/create-account", "/history", "/settings", "/logout"},
                )

    def test_contactability_article_has_no_links_to_unpublished_articles(self):
        body = self.client.get(CONTACTABILITY_PATH).text
        self.assertNotIn("/compare-prospect-website-to-outranking-competitor", body)
        self.assertNotIn("/how-to-decide-which-local-leads-to-contact-first", body)
        self.assertNotIn("/website-seo-gaps-personalized-outreach", body)
        self.assertNotIn("/shortlist-best-prospects-50-lead-search", body)

    def test_contactability_keeps_lead_finder_cta_and_approved_copy(self):
        body = self.client.get(CONTACTABILITY_PATH).text
        self.assertIn('href="/lead-bot">Open Lead Finder</a>', body)
        approved_copy = (
            "Why contactability is a prioritization input, not a verdict",
            "What counts as a contact signal",
            "A repeatable contactability check",
            "Reading the signals: present versus usable",
            "Turning your findings into a simple priority order",
            "Where LeadMeLeads fits in the workflow",
            "Use LeadMeLeads to keep your contactability notes organized and compare local leads side by side before you reach out.",
        )
        for exact_text in approved_copy:
            self.assertEqual(body.count(exact_text), 1)

    def test_contactability_template_has_no_draft_placeholder_or_editorial_scaffolding(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/check_contactability_local_business_leads.html"
        ).read_text(encoding="utf-8")
        self.assertTrue(source.startswith('{% extends "public_guide_base.html" %}'))
        for forbidden in (
            "<!DOCTYPE html>",
            "<style>",
            "draft-banner",
            "placeholder-",
            "DRAFT",
            "SEO Title:",
            "Meta Description:",
            "Slug:",
            "Target keyword:",
            "Topics:",
            "Internal Link Opportunities",
            "Image Concepts",
            "Source / Verification Notes",
            "[[AUTHOR_NAME]]",
            "[[CANONICAL_URL]]",
            "[[DATE_MODIFIED]]",
            "[[DATE_PUBLISHED]]",
            "[[IMAGE_URL]]",
            "[[PUBLISHER_NAME]]",
        ):
            self.assertNotIn(forbidden, source)

    def test_contactability_word_count_in_target_range(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/check_contactability_local_business_leads.html"
        ).read_text(encoding="utf-8")
        start = source.index("{% block content %}") + len("{% block content %}")
        end = source.index("{% endblock %}", start)
        plain = re.sub(r"<[^>]+>", " ", source[start:end])
        word_count = len(plain.split())
        self.assertGreaterEqual(word_count, 1600)
        self.assertLessEqual(word_count, 1800)

    def test_compare_outranking_article_exact_metadata_and_single_h1(self):
        response = self.client.get(COMPARE_OUTRANKING_PATH)
        body = response.text
        self.assertEqual(response.status_code, 200)
        title = html.unescape(re.search(r"<title>([^<]+)</title>", body).group(1))
        description = html.unescape(
            re.search(r'<meta name="description" content="([^"]+)"', body).group(1)
        )
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
        self.assertEqual(
            title,
            seo_meta.COMPARE_PROSPECT_WEBSITE_TO_OUTRANKING_COMPETITOR_PAGE.title,
        )
        self.assertEqual(
            title,
            "Comparing a Prospect's Website to the Competitor That Outranks "
            "It | LeadMeLeads",
        )
        self.assertEqual(
            description,
            seo_meta.COMPARE_PROSPECT_WEBSITE_TO_OUTRANKING_COMPETITOR_PAGE.description,
        )
        self.assertEqual(description, (
            "How to compare a sales prospect's site against the competitor "
            "outranking it in search: what to measure, and how to present "
            "the gap without overclaiming."
        ))
        self.assertEqual(canonical, seo_meta.canonical_url(COMPARE_OUTRANKING_PATH))
        self.assertEqual(
            canonical,
            "https://leadmeleads.com/compare-prospect-website-to-outranking-competitor",
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", body, re.DOTALL),
            ["Comparing a Prospect's Website to the Competitor That Outranks It"],
        )
        self.assertIn('name="robots" content="index, follow"', body)
        self.assertNotIn("noindex", response.headers.get("X-Robots-Tag", "").lower())

    def test_compare_outranking_sitemap_contains_page_exactly_once(self):
        sitemap = self.client.get("/sitemap.xml").text
        loc = f"<loc>{seo_meta.canonical_url(COMPARE_OUTRANKING_PATH)}</loc>"
        self.assertEqual(sitemap.count(loc), 1)

    def test_compare_outranking_resources_hub_lists_article_once(self):
        resources = self.client.get("/resources").text
        self.assertEqual(resources.count(f'href="{COMPARE_OUTRANKING_PATH}"'), 1)

    def test_compare_outranking_article_has_exactly_two_images_with_alt_text(self):
        body = self.client.get(COMPARE_OUTRANKING_PATH).text
        hero_src = "/static/images/resources/compare-outranking-competitor-hero.png"
        inline_src = (
            "/static/images/resources/"
            "compare-outranking-competitor-what-to-compare.png"
        )
        self.assertEqual(body.count(hero_src), 1)
        self.assertEqual(body.count(inline_src), 1)
        article_imgs = [
            tag
            for tag in re.findall(r"<img\b[^>]*>", body)
            if "/static/images/resources/" in tag
        ]
        self.assertEqual(len(article_imgs), 2)
        imgs_by_src = {}
        for tag in article_imgs:
            src_match = re.search(r'src="([^"]+)"', tag)
            alt_match = re.search(r'alt="([^"]*)"', tag)
            imgs_by_src[src_match.group(1)] = alt_match.group(1) if alt_match else None
        self.assertEqual(
            imgs_by_src.get(hero_src),
            "Two competing websites shown side by side with a middle column "
            "listing differences between them",
        )
        self.assertEqual(
            imgs_by_src.get(inline_src),
            "Comparison worksheet listing what to compare between a prospect "
            "website and the competitor that outranks it: title and meta, H1 "
            "and service focus, reviews and trust signals, content depth, "
            "and mobile speed and usability",
        )

    def test_compare_outranking_inline_image_follows_the_complete_what_to_compare_section(self):
        body = self.client.get(COMPARE_OUTRANKING_PATH).text
        section_heading = "<h2>What to Compare</h2>"
        last_subsection = "<h3>Brand and entity signals</h3>"
        next_heading = "<h2>Turn the Comparison Into Something the Prospect Can Act On</h2>"
        inline = "/static/images/resources/compare-outranking-competitor-what-to-compare.png"
        self.assertEqual(body.count(section_heading), 1)
        self.assertEqual(body.count(last_subsection), 1)
        self.assertLess(body.index(section_heading), body.index(last_subsection))
        self.assertLess(body.index(last_subsection), body.index(inline))
        self.assertLess(body.index(inline), body.index(next_heading))

    def test_compare_outranking_schema_and_no_faq(self):
        response = self.client.get(COMPARE_OUTRANKING_PATH)
        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        articles = [json.loads(item) for item in scripts if '"Article"' in item]
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["@type"], "Article")
        self.assertEqual(
            articles[0]["mainEntityOfPage"],
            seo_meta.canonical_url(COMPARE_OUTRANKING_PATH),
        )
        self.assertEqual(
            articles[0]["headline"],
            seo_meta.COMPARE_PROSPECT_WEBSITE_TO_OUTRANKING_COMPETITOR_PAGE.title,
        )
        self.assertNotIn('"FAQPage"', response.text)

    def test_compare_outranking_links_resolve_to_indexable_paths(self):
        allowed = set(seo_meta.PUBLIC_INDEXABLE_PATHS)
        body = self.client.get(COMPARE_OUTRANKING_PATH).text
        links = re.findall(r'href="(/[^"#?]*)"', body)
        for target in links:
            if target.startswith("/static/"):
                continue
            with self.subTest(target=target):
                self.assertIn(target, allowed | UTILITY_NOINDEX_PATHS | {"/login", "/create-account", "/history", "/settings", "/logout"})

    def test_compare_outranking_article_has_no_links_to_unpublished_articles(self):
        body = self.client.get(COMPARE_OUTRANKING_PATH).text
        self.assertNotIn("/how-to-decide-which-local-leads-to-contact-first", body)
        self.assertNotIn("/website-seo-gaps-personalized-outreach", body)
        self.assertNotIn("/shortlist-best-prospects-50-lead-search", body)

    def test_compare_outranking_keeps_lead_finder_cta_and_approved_copy(self):
        body = self.client.get(COMPARE_OUTRANKING_PATH).text
        self.assertIn('href="/lead-bot">Open Lead Finder</a>', body)
        approved_copy = (
            "Why a Head-to-Head Comparison Beats a Generic Audit",
            "Pick the Competitor That Actually Outranks the Prospect",
            'Define "Outranks" Precisely Before You Present It',
            "What to Compare",
            "Turn the Comparison Into Something the Prospect Can Act On",
            "Avoid These Traps When You Present the Gap",
            "A Repeatable Comparison Workflow",
            "The Bottom Line",
            "Building these comparisons for every prospect by hand is slow. "
            "See how LeadMeLeads assembles the ranking snapshot and gap "
            "analysis for you.",
        )
        for exact_text in approved_copy:
            self.assertEqual(body.count(exact_text), 1)

    def test_compare_outranking_template_has_no_draft_placeholder_or_editorial_scaffolding(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/compare_prospect_website_to_outranking_competitor.html"
        ).read_text(encoding="utf-8")
        self.assertTrue(source.startswith('{% extends "public_guide_base.html" %}'))
        for forbidden in (
            "<!DOCTYPE html>",
            "<style>",
            "draft-banner",
            "placeholder-",
            "DRAFT",
            "SEO Title:",
            "Meta Description:",
            "Slug:",
            "Target keyword:",
            "Topics:",
            "Internal Link Opportunities",
            "Image Concepts",
            "Source / Verification Notes",
            "[[AUTHOR_NAME]]",
            "[[CANONICAL_URL]]",
            "[[DATE_MODIFIED]]",
            "[[DATE_PUBLISHED]]",
            "[[IMAGE_URL]]",
            "[[PUBLISHER_NAME]]",
            "concept card",
        ):
            self.assertNotIn(forbidden, source)

    def test_compare_outranking_word_count_in_target_range(self):
        source = (
            Path(__file__).parent.parent
            / "app/templates/compare_prospect_website_to_outranking_competitor.html"
        ).read_text(encoding="utf-8")
        start = source.index("{% block content %}") + len("{% block content %}")
        end = source.index("{% endblock %}", start)
        plain = re.sub(r"<[^>]+>", " ", source[start:end])
        word_count = len(plain.split())
        self.assertGreaterEqual(word_count, 1450)
        self.assertLessEqual(word_count, 1700)

    def test_articles_link_to_each_other(self):
        without_list_body = self.client.get(WITHOUT_LIST_PATH).text
        verify_body = self.client.get(VERIFY_LEADS_PATH).text
        self.assertIn(
            f'href="{VERIFY_LEADS_PATH}">how to verify local business leads before outreach</a>',
            without_list_body,
        )
        self.assertIn(
            f'href="{WITHOUT_LIST_PATH}">how to find local business leads without buying a list</a>',
            verify_body,
        )

    def test_mobile_safe_markup_and_overflow_guards(self):
        base = (Path(__file__).parent.parent / "app/templates/public_guide_base.html").read_text()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', base)
        self.assertIn("* { box-sizing: border-box; }", base)
        self.assertIn("@media (max-width:480px)", base)
        self.assertIn("max-width:100%", base)
        self.assertIn("overflow-wrap:anywhere", base)
        for body in self.bodies.values():
            self.assertNotRegex(body, r'(?<!max-)(?<!min-)width:\s*[4-9]\d{2,}px')


if __name__ == "__main__":
    unittest.main()
