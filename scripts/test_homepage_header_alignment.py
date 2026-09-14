"""
Regression coverage for the homepage/canonical public header alignment
("fix: align homepage with canonical public header").

The homepage (/) previously rendered its own .topbar with a stretched
logo and translucent dark nav pills, which made it visually diverge from
the canonical header (_public_header.html + the header rules in
app/static/css/styles.css) shared by /resources, /what-makes-a-good-lead,
and /compare. The homepage now renders the canonical header chrome: the
same logo markup, the same white-pill nav styling (mirrored into the
page's inline CSS because the homepage cannot load styles.css without its
page-global !important locks breaking the marketing content below the
fold), and -- decisively for keeping the two from drifting -- the exact
same nav markup partial, app/templates/_public_nav.html, included by both
_public_header.html and index.html, plus the shared drawer JS
(/static/js/mobile-nav.js).

The hero H1 stays in .hero-copy: the header is header-only mode and must
never emit an H1 of its own.
"""

import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import app.seo_meta as seo_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_LOGO = "/static/leadmeleads-logo-blue-transparent.png?v=transparent-1"

GUEST_NAV = [
    ("/", "Home"),
    ("/lead-bot", "Lead Finder"),
    ("/compare", "Compare"),
    ("/login", "Login"),
    ("/create-account", "Create Account"),
]

AUTHED_EXTRA = [
    ("/history", "History"),
    ("/settings", "Settings"),
    ("/logout", "Logout"),
]


def render_homepage(user=None):
    return appmain.templates.env.get_template("index.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/"), query_params={}),
        user=user,
        logo_url=CANONICAL_LOGO,
        seo_meta_html=seo_meta.render_seo_meta_html(seo_meta.HOME_PAGE),
        seo_jsonld_html=seo_meta.render_homepage_jsonld(),
    )


def render_resources(user=None):
    return appmain.templates.env.get_template("resources.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/resources"), query_params={}),
        user=user,
        logo_url=CANONICAL_LOGO,
        seo_meta_html="",
        seo_jsonld_html="",
        article_jsonld_html="",
        faq_jsonld_html="",
        faq=[],
    )


class HomepageHeaderAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_homepage()
        cls.soup = BeautifulSoup(cls.html, "html.parser")

    def _header(self, soup=None):
        return (soup or self.soup).select_one(".home-header .header")

    def test_header_carries_exactly_one_canonical_logo_linked_home(self):
        header = self._header()
        logos = header.select(f'img[src="{CANONICAL_LOGO}"]')
        self.assertEqual(len(logos), 1)
        logo = logos[0]
        self.assertEqual(logo.get("alt"), "LeadMeLeads")
        self.assertEqual(logo.get("width"), "2090")
        self.assertEqual(logo.get("height"), "511")
        self.assertIsNotNone(logo.find_parent("a", href="/"))
        self.assertIn("logo", logo.get("class") or [])
        # Exactly one canonical logo across the whole page (no second copy
        # left behind in the body/hero).
        self.assertEqual(
            len(self.soup.select(f'img[src="{CANONICAL_LOGO}"]')), 1
        )

    def test_header_has_no_text_wordmark_or_logo_recreation(self):
        header = self._header()
        self.assertNotIn("wordmark", str(header))
        self.assertEqual(header.select(".brand-logo"), [])
        # The only text inside the header is the nav itself; no stray
        # "LeadMeLeads" text node recreation.
        texts = [t.strip() for t in header.find_all(string=True) if t.strip()]
        allowed = {label for _, label in GUEST_NAV}
        self.assertTrue(set(texts).issubset(allowed), f"unexpected header text: {texts}")

    def test_guest_nav_uses_standardized_canonical_labels_and_routes(self):
        nav = self.soup.select_one(".home-header #compareNav")
        links = [(a.get("href"), a.get_text(strip=True)) for a in nav.find_all("a")]
        self.assertEqual(links, GUEST_NAV)

    def test_authenticated_nav_behavior_preserved(self):
        soup = BeautifulSoup(render_homepage(user=SimpleNamespace()), "html.parser")
        nav = soup.select_one(".home-header #compareNav")
        links = [(a.get("href"), a.get_text(strip=True)) for a in nav.find_all("a")]
        self.assertEqual(links, GUEST_NAV[:3] + AUTHED_EXTRA)
        self.assertNotIn("/login", [href for href, _ in links])
        self.assertNotIn("/create-account", [href for href, _ in links])

    def test_homepage_nav_markup_is_the_shared_canonical_partial(self):
        """The homepage and the canonical guide header must render the
        identical toggle + nav DOM, so future label/aria edits land once."""
        home_nav = BeautifulSoup(render_homepage(), "html.parser")
        guide_nav = BeautifulSoup(render_resources(), "html.parser")
        self.assertEqual(
            str(home_nav.select_one(".home-header .app-nav-toggle")),
            str(guide_nav.select_one(".hero .app-nav-toggle")),
        )
        self.assertEqual(
            str(home_nav.select_one("#compareNav")),
            str(guide_nav.select_one("#compareNav")),
        )

    def test_both_hosts_include_the_shared_nav_partial(self):
        header_tpl = (REPO_ROOT / "app/templates/_public_header.html").read_text()
        index_tpl = (REPO_ROOT / "app/templates/index.html").read_text()
        self.assertIn('{% include "_public_nav.html" %}', header_tpl)
        self.assertIn('{% include "_public_nav.html" %}', index_tpl)
        # Neither host may re-inline the nav links again.
        self.assertNotIn('<a href="/lead-bot">Lead Finder</a>', header_tpl)
        self.assertNotIn('<a href="/lead-bot">Lead Finder</a>', index_tpl)

    def test_drawer_harness_present(self):
        header = self.soup.select_one(".home-header")
        self.assertIsNotNone(header.select_one("button.app-nav-toggle[data-nav-toggle]"))
        toggle = header.select_one("[data-nav-toggle]")
        self.assertEqual(toggle.get("aria-controls"), "compareNav")
        self.assertEqual(toggle.get("aria-expanded"), "false")
        self.assertIsNotNone(header.select_one(".app-nav-overlay[data-nav-overlay]"))
        self.assertIn("/static/js/mobile-nav.js", str(self.soup))

    def test_exactly_one_h1_and_it_stays_in_the_hero(self):
        h1s = self.soup.find_all("h1")
        self.assertEqual(len(h1s), 1)
        self.assertEqual(h1s[0].get_text(strip=True), "Find local leads worth contacting.")
        hero_copy = self.soup.select_one(".home-header ~ .hero-grid .hero-copy h1, .hero-copy h1")
        self.assertIsNotNone(hero_copy)
        self.assertIsNone(self._header().find("h1"), "header-only mode must not emit an H1")

    def test_inline_css_mirrors_the_canonical_header_values(self):
        style = "\n".join(s.get_text() for s in self.soup.find_all("style"))
        for token in [
            ".home-header .logo",
            "height: 81px",
            "max-width: 180px",
            "object-fit: contain",
            ".home-header .nav a",
            "background: #ffffff",
            "color: #0f2f7f",
            "border-radius: 8px",
            "min-height: 44px",
            "min-width: 118px",
            "font-weight: 900",
            "@media (max-width: 850px)",
            "@media (max-width: 700px)",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, style)

    def test_legacy_topbar_fully_removed(self):
        self.assertNotIn("topbar", self.html)
        self.assertNotIn("brand-logo", self.html)
        self.assertNotIn("Compare URL", self.soup.select_one(".home-header").get_text())

    def test_homepage_routes_ctas_and_below_fold_content_intact(self):
        body = self.soup.get_text(" ", strip=True)
        for phrase in [
            "Find local leads worth contacting.",
            "Stop hunting. Start finding.",
            "Open Lead Finder",
            "Compare Two Websites",
            "Start with a search, not a spreadsheet.",
            "lead generation resources",
            "Contact us",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)
        hrefs = {a.get("href") for a in self.soup.find_all("a")}
        for route in ["/lead-bot", "/compare", "/resources", "/what-makes-a-good-lead", "/contact"]:
            self.assertIn(route, hrefs)

    def test_route_renders_guest_nav_via_live_app(self):
        async def fetch():
            import httpx
            transport = httpx.ASGITransport(app=appmain.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/")
        response = asyncio.run(fetch())
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")
        links = [(a.get("href"), a.get_text(strip=True)) for a in soup.select(".home-header #compareNav a")]
        self.assertEqual(links, GUEST_NAV)
        self.assertEqual(len(soup.find_all("h1")), 1)


if __name__ == "__main__":
    unittest.main()
