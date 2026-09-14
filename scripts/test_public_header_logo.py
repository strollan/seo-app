"""Regression coverage for the canonical logo in every public site header."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import app.seo_meta as seo_meta
import agents.lead_dashboard_agent as dashboard_agent


CANONICAL_LOGO = "/static/leadmeleads-logo-blue-transparent.png?v=transparent-1"
PUBLIC_ROUTES = (
    "/",
    "/lead-bot",
    "/compare",
    "/what-makes-a-good-lead",
    "/how-to-find-local-leads",
    "/how-to-find-local-business-leads-without-buying-a-lead-list",
    "/how-to-verify-local-business-leads-before-outreach",
    "/local-lead-generation",
    "/lead-list-vs-lead-finder",
    "/how-to-find-website-seo-opportunities-in-a-lead-list",
    "/check-contactability-local-business-leads",
    "/compare-prospect-website-to-outranking-competitor",
    "/resources",
)


class PublicHeaderLogoTests(unittest.TestCase):
    def _header(self, path):
        if path == "/lead-bot":
            html = dashboard_agent.render_lead_dashboard(current_user=None, csrf_token="test-token")
        else:
            template_name = "index.html" if path == "/" else "resources.html" if path == "/resources" else {
                "/compare": "compare.html",
                "/what-makes-a-good-lead": "good_lead.html",
                "/how-to-find-local-leads": "find_local_leads.html",
                "/how-to-find-local-business-leads-without-buying-a-lead-list": "how_to_find_local_business_leads_without_buying_a_lead_list.html",
                "/how-to-verify-local-business-leads-before-outreach": "how_to_verify_local_business_leads_before_outreach.html",
                "/local-lead-generation": "local_lead_generation.html",
                "/lead-list-vs-lead-finder": "lead_list_vs_finder.html",
                "/how-to-find-website-seo-opportunities-in-a-lead-list": "how_to_find_website_seo_opportunities_in_a_lead_list.html",
                "/check-contactability-local-business-leads": "check_contactability_local_business_leads.html",
                "/compare-prospect-website-to-outranking-competitor": "compare_prospect_website_to_outranking_competitor.html",
            }[path]
            html = appmain.templates.env.get_template(template_name).render(
                request=SimpleNamespace(url=SimpleNamespace(path=path), query_params={}),
                logo_url=CANONICAL_LOGO,
                user=None,
                seo_meta_html="",
                seo_jsonld_html="",
                article_jsonld_html="",
                faq_jsonld_html="",
                faq=[],
            )
        soup = BeautifulSoup(html, "html.parser")
        # "/" now renders the canonical header chrome too (.home-header >
        # .header, homepage-header-alignment fix), so every non-lead-bot
        # public route exposes its header through ".header".
        selector = ".leadbot-brand" if path == "/lead-bot" else ".header"
        header = soup.select_one(selector)
        self.assertIsNotNone(header, f"missing public header on {path}")
        return soup, header

    def test_public_route_inventory_matches_the_indexable_route_allowlist(self):
        self.assertEqual(PUBLIC_ROUTES, seo_meta.PUBLIC_INDEXABLE_PATHS)

    def test_canonical_logo_asset_exists_in_the_static_mount(self):
        asset = Path(appmain.static_dir, "leadmeleads-logo-blue-transparent.png")
        self.assertTrue(asset.is_file())

    def test_each_public_header_has_exactly_one_canonical_home_linked_logo(self):
        for path in PUBLIC_ROUTES:
            with self.subTest(path=path):
                soup, header = self._header(path)
                logos = header.select(f'img[src="{CANONICAL_LOGO}"]')
                self.assertEqual(len(logos), 1, f"expected one canonical header logo on {path}")
                logo = logos[0]
                self.assertEqual(logo.get("alt"), "LeadMeLeads")
                self.assertEqual(logo.get("width"), "2090")
                self.assertEqual(logo.get("height"), "511")
                self.assertIsNotNone(logo.find_parent("a", href="/"))
                self.assertEqual(len(header.select('a[href="/"] img')), 1)
                self.assertEqual(len(soup.find_all("h1")), 1)

    def test_obsolete_homepage_text_wordmark_is_absent(self):
        _soup, header = self._header("/")
        self.assertEqual(header.select(".wordmark"), [])
        self.assertNotIn("wordmark", str(header))


if __name__ == "__main__":
    unittest.main()
