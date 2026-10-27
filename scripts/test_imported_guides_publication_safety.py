"""Focused regression coverage for locally imported Slayer guides."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main, seo_meta


GUIDE = {
    "version": 1,
    "slug": "imported-guide-check",
    "title": "Imported Guide Check",
    "dek": "A safe local import.",
    "intro_paragraphs": ["Intro."],
    "body_sections": [{"heading": "Check", "paragraphs": ["Body."], "bullets": []}],
    "seo_title": "Imported Guide Check | LeadMeLeads",
    "meta_description": "A safe local import.",
    "hero_image": {
        "url": "/static/images/resources/imported/imported-guide-check/hero.png",
        "alt": "Guide hero",
    },
    "inline_images": [],
    "resource_card": {"topic": "verification", "summary": "A safe local import."},
    "modified_at": "2026-09-30T12:00:00+00:00",
}


class ImportedGuidePublicationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.guides = {GUIDE["slug"]: GUIDE}
        self.request = SimpleNamespace(url=SimpleNamespace(path="/imported-guide-check"))

    def _render_imported_guide(self):
        page = seo_meta.SeoPage(
            title=GUIDE["seo_title"], description=GUIDE["meta_description"],
            canonical_path=f'/{GUIDE["slug"]}', og_type="article",
        )
        return main.templates.get_template("imported_guide.html").render(
            request=self.request, logo_url="/static/logo.png", user=None, guide=GUIDE, faq=[],
            seo_meta_html=seo_meta.render_seo_meta_html(page),
            article_jsonld_html=seo_meta.render_article_jsonld(
                page, modified_at=GUIDE["modified_at"],
                image_url=seo_meta.canonical_url(GUIDE["hero_image"]["url"]),
            ),
            faq_jsonld_html="",
        )

    def test_imported_guide_has_resolved_site_metadata_without_publication_date(self):
        html = self._render_imported_guide()
        self.assertNotRegex(html, r"\[\[[A-Z_]+\]\]")
        schema = json.loads(re.search(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        ).group(1))
        self.assertEqual(schema["author"]["name"], seo_meta.SITE_AUTHOR)
        self.assertEqual(schema["mainEntityOfPage"], "https://leadmeleads.com/imported-guide-check")
        self.assertEqual(schema["image"], "https://leadmeleads.com/static/images/resources/imported/imported-guide-check/hero.png")
        self.assertEqual(schema["publisher"]["logo"]["url"], seo_meta.PUBLISHER_LOGO_URL)
        self.assertEqual(schema["dateModified"], GUIDE["modified_at"])
        self.assertNotIn("datePublished", schema)
        self.assertNotIn('<p class="article-published">', html)

    def test_every_public_guide_has_a_verified_publication_date(self):
        guide_paths = {
            path for path in seo_meta.public_indexable_paths()
            if path not in {"/", "/lead-bot", "/compare", "/resources", "/privacy-policy"}
        }
        self.assertEqual(len(guide_paths), 16)
        self.assertEqual(set(seo_meta.GUIDE_PUBLISHED_AT), guide_paths)

    def test_published_imported_guide_shows_date_matching_schema(self):
        from fastapi.testclient import TestClient

        path = "/identifying-website-maintenance-clues-during-prospecting"
        body = TestClient(main.app).get(path).text
        schema = json.loads(re.search(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        ).group(1))
        self.assertEqual(schema["datePublished"], "2026-10-27T17:09:11+00:00")
        self.assertRegex(
            body,
            r'<h1>Identifying Website Maintenance Clues During Prospecting</h1>\s*'
            r'<p class="article-published">Published <time datetime="2026-10-27">October 27, 2026</time></p>',
        )

    def test_resources_card_links_to_the_imported_route_without_changing_hand_built_links(self):
        html = main.templates.get_template("resources.html").render(
            request=SimpleNamespace(url=SimpleNamespace(path="/resources")),
            logo_url="/static/logo.png", user=None, faq=[],
            seo_meta_html="", article_jsonld_html="", faq_jsonld_html="",
            imported_guides=list(self.guides.values()),
        )
        self.assertIn('href="/imported-guide-check"', html)
        self.assertIn('href="/how-to-verify-local-business-leads-before-outreach"', html)


if __name__ == "__main__":
    unittest.main()
