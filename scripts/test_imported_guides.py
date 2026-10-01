"""Focused coverage for the local-only Slayer guide import seam."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import imported_guides, seo_meta


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300050201f34a24d50000000049454e44ae426082"
)


class ImportedGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package = self.root / "package"
        (self.package / "assets").mkdir(parents=True)
        (self.package / "assets" / "hero.png").write_bytes(PNG)
        (self.package / "assets" / "inline.png").write_bytes(PNG)
        self.content = self.root / "content"
        self.asset_root = self.root / "static" / "images" / "resources" / "imported"

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, **overrides):
        data = {
            "slug": "research-local-service-pages",
            "title": "Research Local Service Pages",
            "dek": "A practical guide to checking local service-page signals.",
            "body_sections": [{
                "heading": "Start with the visible page",
                "paragraphs": ["Review the service, location, and contact information."],
                "bullets": ["Record observable gaps before outreach."],
            }],
            "hero_image": {"path": "assets/hero.png", "alt": "A local service page under review."},
            "inline_images": [{
                "path": "assets/inline.png", "alt": "A checklist for a service-page review.",
                "placement_after_section": 0,
            }],
            "seo_title": "Research Local Service Pages | LeadMeLeads",
            "meta_description": "A practical guide to reviewing visible local service-page signals.",
            "resource_card": {
                "topic": "website-seo",
                "summary": "Review visible service-page signals before outreach.",
            },
        }
        data.update(overrides)
        (self.package / imported_guides.MANIFEST_NAME).write_text(
            json.dumps(data), encoding="utf-8"
        )
        return data

    def _import(self):
        return imported_guides.import_guide_package(
            self.package, content_dir=self.content, asset_root=self.asset_root
        )

    def test_valid_local_import_writes_registry_and_assets_without_external_actions(self):
        self._manifest()
        with mock.patch("os.system") as system:
            result = self._import()
        self.assertEqual(result["local_url"], "http://127.0.0.1:8800/research-local-service-pages")
        record = json.loads((self.content / "research-local-service-pages.json").read_text())
        self.assertEqual(record["hero_image"]["url"], "/static/images/resources/imported/research-local-service-pages/hero.png")
        self.assertEqual(record["inline_images"][0]["url"], "/static/images/resources/imported/research-local-service-pages/inline-1.png")
        self.assertEqual((self.asset_root / "research-local-service-pages" / "hero.png").read_bytes(), PNG)
        self.assertEqual((self.asset_root / "research-local-service-pages" / "inline-1.png").read_bytes(), PNG)
        system.assert_not_called()

    def test_imported_hero_is_copied_recorded_rendered_and_served(self):
        """Keep the package-to-public-image bridge intact end to end."""
        self._manifest()
        self._import()
        guide = imported_guides.load_imported_guides(self.content)["research-local-service-pages"]
        hero_url = "/static/images/resources/imported/research-local-service-pages/hero.png"

        self.assertEqual(guide["hero_image"]["url"], hero_url)
        self.assertEqual(
            (self.asset_root / "research-local-service-pages" / "hero.png").read_bytes(),
            (self.package / "assets" / "hero.png").read_bytes(),
        )

        # Resolve the exact copied file through the same StaticFiles root and
        # public /static URL shape that the production app mounts. A regular
        # file at this resolved path is served as HTTP 200 by StaticFiles.
        static_files = StaticFiles(directory=self.asset_root.parents[2])
        resolved_path, stat_result = static_files.lookup_path(hero_url.removeprefix("/static/"))
        self.assertEqual(Path(resolved_path), self.asset_root / "research-local-service-pages" / "hero.png")
        self.assertIsNotNone(stat_result)
        self.assertGreater(stat_result.st_size, 0)

        templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "app" / "templates")
        request = Request({"type": "http", "method": "GET", "path": f"/{guide['slug']}", "headers": []})
        html = templates.get_template("imported_guide.html").render(
            request=request, guide=guide, logo_url="", user=None, faq=[],
            seo_meta_html="", article_jsonld_html="", faq_jsonld_html="",
        )
        self.assertIn('<figure class="article-hero imported-guide-hero">', html)
        self.assertIn(f'src="{hero_url}"', html)

    def test_imported_hero_uses_a_full_width_16_by_9_frame_without_changing_inline_images(self):
        base = (Path(__file__).resolve().parent.parent / "app" / "templates" / "public_guide_base.html").read_text()
        imported = (Path(__file__).resolve().parent.parent / "app" / "templates" / "imported_guide.html").read_text()

        self.assertIn('class="article-hero imported-guide-hero"', imported)
        hero_start = base.index('.public-guide-content .imported-guide-hero {')
        image_start = base.index('.public-guide-content .imported-guide-hero img {')
        inline_start = base.index('.public-guide-content .article-figure img {')
        hero_rule = base[hero_start:base.index('}', hero_start)]
        image_rule = base[image_start:base.index('}', image_start)]
        inline_rule = base[inline_start:base.index('}', inline_start)]

        self.assertIn('aspect-ratio:16 / 9', hero_rule)
        self.assertIn('overflow:hidden', hero_rule)
        self.assertIn('width:100%', image_rule)
        self.assertIn('height:100%', image_rule)
        self.assertIn('object-fit:cover', image_rule)
        self.assertIn('object-position:center', image_rule)
        self.assertIn('height:auto', inline_rule)
        self.assertNotIn('object-fit', inline_rule)

    def test_duplicate_slug_is_rejected_without_partial_writes(self):
        self._manifest()
        self._import()
        before_record = (self.content / "research-local-service-pages.json").read_bytes()
        before_assets = sorted(p.relative_to(self.asset_root) for p in self.asset_root.rglob("*"))
        with self.assertRaisesRegex(imported_guides.GuideImportError, "already exists"):
            self._import()
        self.assertEqual((self.content / "research-local-service-pages.json").read_bytes(), before_record)
        self.assertEqual(sorted(p.relative_to(self.asset_root) for p in self.asset_root.rglob("*")), before_assets)

    def test_reserved_and_hand_built_slugs_are_rejected_before_writes(self):
        self._manifest(slug="login")
        with self.assertRaisesRegex(imported_guides.GuideImportError, "reserved"):
            self._import()
        self.assertFalse(self.content.exists())

        self._manifest(slug="how-to-find-local-leads")
        with self.assertRaisesRegex(imported_guides.GuideImportError, "hand-built"):
            self._import()
        self.assertFalse(self.content.exists())

    def test_unsafe_slug_or_image_path_is_rejected_before_writes(self):
        self._manifest(slug="../admin")
        with self.assertRaisesRegex(imported_guides.GuideImportError, "slug"):
            self._import()
        self.assertFalse(self.content.exists())
        self._manifest(hero_image={"path": "../outside.png", "alt": "Unsafe"})
        with self.assertRaisesRegex(imported_guides.GuideImportError, "safe relative"):
            self._import()
        self.assertFalse(self.content.exists())

    def test_missing_required_field_is_rejected_before_writes(self):
        self._manifest(meta_description="")
        with self.assertRaisesRegex(imported_guides.GuideImportError, "meta_description is required"):
            self._import()
        self.assertFalse(self.content.exists())
        self.assertFalse(self.asset_root.exists())

    def test_missing_or_malformed_image_is_rejected_before_writes(self):
        self._manifest(hero_image={"path": "assets/missing.png", "alt": "Missing"})
        with self.assertRaisesRegex(imported_guides.GuideImportError, "existing file"):
            self._import()
        self.assertFalse(self.content.exists())

        (self.package / "assets" / "hero.png").write_bytes(b"not an image")
        self._manifest()
        with self.assertRaisesRegex(imported_guides.GuideImportError, "not a supported"):
            self._import()
        self.assertFalse(self.content.exists())

    def test_optional_intro_paragraphs_validate_render_before_first_h2_and_keep_old_manifests_working(self):
        intro = ["This is the first paragraph.", "This is the second paragraph."]
        self._manifest(intro_paragraphs=intro)
        self._import()
        guides = imported_guides.load_imported_guides(self.content)
        self.assertEqual(guides["research-local-service-pages"]["intro_paragraphs"], intro)
        import app.main as appmain

        with (
            mock.patch.object(imported_guides, "load_imported_guides", return_value=guides),
            TestClient(appmain.app) as client,
        ):
            response = client.get("/research-local-service-pages")
        self.assertEqual(response.status_code, 200)
        self.assertLess(response.text.index(intro[0]), response.text.index("Start with the visible page"))
        self.assertNotIn("Introduction</h2>", response.text)

        self._manifest()
        record, _assets = imported_guides.validate_package(self.package)
        self.assertEqual(record["intro_paragraphs"], [])

    def test_intro_paragraphs_must_be_plain_text_list(self):
        self._manifest(intro_paragraphs="not a list")
        with self.assertRaisesRegex(imported_guides.GuideImportError, "intro_paragraphs must be a list"):
            imported_guides.validate_package(self.package)
        self._manifest(intro_paragraphs=[""])
        with self.assertRaisesRegex(imported_guides.GuideImportError, "intro_paragraphs\\[0\\] is required"):
            imported_guides.validate_package(self.package)

    def test_long_body_paragraph_imports_and_is_preserved_exactly(self):
        paragraph = "x" * imported_guides.PARAGRAPH_MAXIMUM
        self.assertEqual(len(paragraph), 4_000)
        self._manifest(body_sections=[{
            "heading": "Start with the visible page",
            "paragraphs": [paragraph],
            "bullets": ["Record observable gaps before outreach."],
        }])

        self._import()

        record = json.loads((self.content / "research-local-service-pages.json").read_text())
        self.assertEqual(record["body_sections"][0]["paragraphs"], [paragraph])

    def test_list_only_section_from_slayer_imports_and_renders_without_losing_bullets(self):
        """Regression: LML-A001's third serialized section is list-only."""
        bullets = [
            "Mobile experience. Check for controls that are hard to tap.",
            "Core information findability. Check the phone number and hours.",
        ]
        self._manifest(body_sections=[{
            "heading": "The Website Itself",
            "paragraphs": [],
            "bullets": bullets,
        }])

        self._import()
        guides = imported_guides.load_imported_guides(self.content)
        guide = guides["research-local-service-pages"]
        self.assertEqual(guide["body_sections"][0]["paragraphs"], [])
        self.assertEqual(guide["body_sections"][0]["bullets"], bullets)

        templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "app" / "templates")
        request = Request({"type": "http", "method": "GET", "path": "/research-local-service-pages", "headers": []})
        html = templates.get_template("imported_guide.html").render(
            request=request, guide=guide, logo_url="", user=None, faq=[],
            seo_meta_html="", article_jsonld_html="", faq_jsonld_html="",
        )
        for bullet in bullets:
            self.assertIn(bullet, html)

    def test_empty_section_is_rejected_even_when_list_only_sections_are_allowed(self):
        self._manifest(body_sections=[{
            "heading": "Empty section",
            "paragraphs": [],
            "bullets": [],
        }])
        with self.assertRaisesRegex(imported_guides.GuideImportError, "needs paragraphs or bullets"):
            imported_guides.validate_package(self.package)

    def test_long_intro_paragraph_within_limit_imports_successfully(self):
        intro = ("Approved introductory paragraph. " * 35).strip()
        self.assertGreater(len(intro), 500)
        self.assertLessEqual(len(intro), imported_guides.PARAGRAPH_MAXIMUM)
        self._manifest(intro_paragraphs=[intro])

        self._import()

        record = json.loads((self.content / "research-local-service-pages.json").read_text())
        self.assertEqual(record["intro_paragraphs"], [intro])

    def test_excessive_body_or_intro_paragraph_is_rejected(self):
        excessive = "x" * (imported_guides.PARAGRAPH_MAXIMUM + 1)
        self._manifest(body_sections=[{
            "heading": "Start with the visible page",
            "paragraphs": [excessive],
            "bullets": [],
        }])
        with self.assertRaisesRegex(imported_guides.GuideImportError, "body_sections\\[0\\].paragraphs exceeds"):
            imported_guides.validate_package(self.package)

        self._manifest(intro_paragraphs=[excessive])
        with self.assertRaisesRegex(imported_guides.GuideImportError, "intro_paragraphs\\[0\\] exceeds"):
            imported_guides.validate_package(self.package)

    def test_imported_guide_integrates_with_route_seo_sitemap_and_resources(self):
        self._manifest()
        self._import()
        guides = imported_guides.load_imported_guides(self.content)
        paths = tuple(f"/{slug}" for slug in guides)
        import app.main as appmain

        with (
            mock.patch.object(imported_guides, "load_imported_guides", return_value=guides),
            mock.patch.object(imported_guides, "public_paths", return_value=paths),
            TestClient(appmain.app) as client,
        ):
            response = client.get("/research-local-service-pages")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Research Local Service Pages", response.text)
            self.assertIn('rel="canonical" href="https://leadmeleads.com/research-local-service-pages"', response.text)
            self.assertIn('name="description" content="A practical guide to reviewing visible local service-page signals."', response.text)
            self.assertNotIn("X-Robots-Tag", response.headers)
            self.assertIn("https://leadmeleads.com/research-local-service-pages", client.get("/sitemap.xml").text)
            resources = client.get("/resources")
            self.assertIn('href="/research-local-service-pages"', resources.text)
            self.assertIn("Review visible service-page signals before outreach.", resources.text)
            # The generic catch-all is registered last, so a hand-built guide
            # still resolves through its established explicit route.
            self.assertEqual(client.get("/how-to-find-local-leads").status_code, 200)


if __name__ == "__main__":
    unittest.main()
