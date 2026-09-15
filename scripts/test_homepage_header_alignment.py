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
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import requests
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
            "@media (max-width: 1000px)",
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


# Breakpoint matrix for the browser audit below: wide desktop (one row),
# the full 701-1000 intermediate band (stacked rows), the 700-and-below
# hamburger tier, and common phone widths.
BREAKPOINT_WIDTHS = [
    1440, 1024, 1001,          # wide desktop: single-row navigation
    1000, 981, 980, 900, 875, 851, 850, 768, 701,  # intermediate: stacked
    700, 480, 375,             # hamburger drawer tier
]

HOMEPAGE_BREAKPOINT = 1000
HAMBURGER_BREAKPOINT = 700

# The browser audit measures geometry via bounding rects; 0.5px tolerates
# subpixel rounding without masking a real overlap.
GEOM_TOLERANCE_PX = 0.5

MEASURE_HEADER_GEOMETRY_JS = """() => {
    const viewport = window.innerWidth;
    const header = document.querySelector('.home-header .header');
    const logo = document.querySelector('.home-header .logo');
    const nav = document.querySelector('.home-header #compareNav');
    const toggle = document.querySelector('.home-header .app-nav-toggle');
    const heroGrid = document.querySelector('.hero-grid');
    const heroCopy = document.querySelector('.hero-copy h1');
    if (!header || !logo || !nav) return {error: 'missing header elements'};
    const hr = header.getBoundingClientRect();
    const lr = logo.getBoundingClientRect();
    const nr = nav.getBoundingClientRect();
    const links = [...nav.querySelectorAll('a')].map(a => {
        const r = a.getBoundingClientRect();
        return {href: a.getAttribute('href'), label: a.textContent.trim(),
                left: r.left, right: r.right, top: r.top, bottom: r.bottom};
    });
    const contained = (inner, outer) =>
        inner.left >= outer.left - TOL && inner.right <= outer.right + TOL &&
        inner.top >= outer.top - TOL && inner.bottom <= outer.bottom + TOL;
    const sameRow = Math.min(lr.bottom, nr.bottom) - Math.max(lr.top, nr.top) > TOL;
    let linkOverlaps = [];
    for (let i = 0; i < links.length; i++) {
        for (let j = i + 1; j < links.length; j++) {
            const a = links[i], b = links[j];
            const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (ox > TOL && oy > TOL) linkOverlaps.push([a.href, b.href]);
        }
    }
    const doc = document.documentElement;
    return {
        viewport,
        headerRect: {left: hr.left, right: hr.right, top: hr.top, bottom: hr.bottom},
        logoRect: {left: lr.left, right: lr.right, top: lr.top, bottom: lr.bottom},
        navRect: {left: nr.left, right: nr.right, top: nr.top, bottom: nr.bottom},
        sameRow,
        siblingGap: +(nr.left - lr.right).toFixed(1),
        logoNavCollision: sameRow && nr.left < lr.right - TOL,
        logoInsideHeader: contained(lr, hr),
        navInsideHeader: contained(nr, hr),
        linksContainedInHeader: links.every(l => contained(l, hr)),
        linkOverlaps,
        navVisibleOnPage: nr.width > 0 && nr.height > 0 && nr.left < viewport && nr.right > 0,
        toggleVisible: toggle ? getComputedStyle(toggle).display !== 'none' : false,
        headerOverlapsHero: heroGrid ? heroGrid.getBoundingClientRect().top < hr.bottom - 1 : null,
        heroTitleContained: heroCopy
            ? heroCopy.scrollWidth <= heroCopy.clientWidth + 1 &&
              heroCopy.getBoundingClientRect().right <= viewport + TOL
            : null,
        docOverflow: doc.scrollWidth > doc.clientWidth + 1,
        docScrollWidth: doc.scrollWidth,
        h1Count: document.querySelectorAll('h1').length,
        logoSrc: (document.querySelector('.home-header .logo') || {}).getAttribute ?
            document.querySelector('.home-header .logo').getAttribute('src') : null,
        links,
    };
}""".replace("TOL", str(GEOM_TOLERANCE_PX))


class HomepageHeaderBreakpointTests(unittest.TestCase):
    """Browser-measured regression coverage for the homepage header
    breakpoints.

    The 850px intermediate breakpoint previously left a broken band
    (~851-950px) where the one-row layout was still active but the
    nowrap nav (670px) + logo (180px) + gap (28px) no longer fit, so the
    nav pills slid left over the logo. Document overflow stayed 0 because
    .home-header has overflow:hidden -- so this audit measures sibling
    collision (logo.right <= nav.left on a shared row) explicitly instead
    of trusting page overflow.

    Skips itself if Playwright/Chromium aren't available (matches
    test_public_guide_header_responsive_layout.py).
    """

    PORT = 8795

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")

        cls._sync_playwright_ctx = sync_playwright()
        cls._playwright = cls._sync_playwright_ctx.__enter__()

        try:
            cls._browser = cls._playwright.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest(f"chromium is not available: {exc}")

        cls._proc = subprocess.Popen(
            [
                sys.executable,
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "test-placeholder-not-a-real-key"),
                "USE_LIVE_SERP": "false",
                "DATAFORSEO_ENABLED": "0",
                "LEADBOT_DATAFORSEO_ENABLED": "0",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        base_url = f"http://127.0.0.1:{cls.PORT}"
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                if requests.get(f"{base_url}/login", timeout=1).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        if not up:
            cls._proc.terminate()
            cls._browser.close()
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest("local dev server did not start in time")

        cls.base_url = base_url

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "_proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        browser = getattr(cls, "_browser", None)
        if browser is not None:
            browser.close()

        ctx = getattr(cls, "_sync_playwright_ctx", None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    def _measure(self, width):
        page = self._browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(f"{self.base_url}/")
            page.wait_for_load_state("networkidle")
            return page.evaluate(MEASURE_HEADER_GEOMETRY_JS)
        finally:
            page.close()

    def _assert_no_sibling_collision(self, width, m):
        self.assertNotIn("error", m, f"measurement failed at {width}px: {m}")
        # Explicit sibling collision check for the intended one-row layout:
        # when logo and navigation share a row, logo.right <= nav.left must
        # hold. Document overflow is NOT evidence here -- .home-header has
        # overflow:hidden, so the old broken band reported zero overflow.
        with self.subTest(width=width):
            self.assertFalse(
                m["logoNavCollision"],
                f"logo/nav overlap {m['siblingGap']}px at {width}px viewport "
                f"(logo.right={m['logoRect']['right']}, nav.left={m['navRect']['left']})",
            )

    def test_wide_desktop_keeps_one_row_navigation(self):
        for width in (1440, 1024, 1001):
            m = self._measure(width)
            self._assert_no_sibling_collision(width, m)
            with self.subTest(width=width):
                self.assertTrue(
                    m["sameRow"],
                    f"expected one-row navigation at {width}px",
                )
                self.assertGreaterEqual(
                    m["siblingGap"], 12,
                    f"logo/nav separation under 12px at {width}px — not enough "
                    "breathing room for the one-row layout",
                )

    def test_intermediate_range_stacks_navigation_below_the_logo(self):
        for width in (1000, 981, 980, 900, 875, 851, 850, 768, 701):
            m = self._measure(width)
            self._assert_no_sibling_collision(width, m)
            with self.subTest(width=width):
                self.assertFalse(
                    m["sameRow"],
                    f"logo and nav still share a row at {width}px",
                )
                self.assertGreaterEqual(
                    m["navRect"]["top"], m["logoRect"]["bottom"] - GEOM_TOLERANCE_PX,
                    f"nav not stacked below the logo at {width}px",
                )
                self.assertTrue(
                    m["navVisibleOnPage"],
                    f"navigation hidden instead of stacked at {width}px",
                )

    def test_logo_and_nav_stay_inside_the_header_outside_drawer_tier(self):
        for width in (1440, 1024, 1001, 1000, 981, 980, 900, 875, 851, 850, 768, 701):
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertNotIn("error", m)
                self.assertTrue(m["logoInsideHeader"], f"logo escapes header at {width}px")
                self.assertTrue(m["navInsideHeader"], f"nav escapes header at {width}px")
                self.assertTrue(
                    m["linksContainedInHeader"],
                    f"nav link escapes header at {width}px",
                )

    def test_nav_links_never_overlap_each_other(self):
        for width in BREAKPOINT_WIDTHS:
            if width <= HAMBURGER_BREAKPOINT:
                continue  # drawer links are column-stacked by construction
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertEqual(
                    m["linkOverlaps"], [],
                    f"nav links overlap each other at {width}px: {m['linkOverlaps']}",
                )

    def test_header_never_overlaps_hero_content(self):
        for width in BREAKPOINT_WIDTHS:
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertFalse(
                    m["headerOverlapsHero"],
                    f"header overlaps hero content at {width}px",
                )

    def test_no_horizontal_document_overflow_at_any_width(self):
        for width in BREAKPOINT_WIDTHS:
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertFalse(
                    m["docOverflow"],
                    f"horizontal document overflow at {width}px "
                    f"(scrollWidth={m['docScrollWidth']})",
                )

    def test_exactly_one_canonical_logo_and_h1_at_every_width(self):
        for width in BREAKPOINT_WIDTHS:
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertNotIn("error", m)
                self.assertEqual(m["h1Count"], 1, f"H1 count wrong at {width}px")
                self.assertEqual(
                    m["logoSrc"], CANONICAL_LOGO,
                    f"canonical logo missing or replaced at {width}px",
                )

    def test_hero_title_and_copy_remain_contained(self):
        for width in BREAKPOINT_WIDTHS:
            m = self._measure(width)
            with self.subTest(width=width):
                self.assertTrue(
                    m["heroTitleContained"],
                    f"hero H1 overflows its box or the viewport at {width}px",
                )

    def test_mobile_drawer_tier_hides_desktop_pills_until_opened(self):
        for width in (700, 480, 375):
            page = self._browser.new_page(viewport={"width": width, "height": 900})
            try:
                page.goto(f"{self.base_url}/")
                page.wait_for_load_state("networkidle")
                with self.subTest(width=width, state="closed"):
                    toggle_visible = page.eval_on_selector(
                        ".home-header .app-nav-toggle",
                        "el => getComputedStyle(el).display !== 'none'",
                    )
                    self.assertTrue(toggle_visible, f"hamburger toggle hidden at {width}px")
                    nav_rect = page.eval_on_selector(
                        ".home-header #compareNav", "el => el.getBoundingClientRect().left"
                    )
                    self.assertGreaterEqual(
                        nav_rect, width - GEOM_TOLERANCE_PX,
                        f"desktop pills visible at {width}px before the drawer opens",
                    )

                page.click(".home-header .app-nav-toggle")
                page.wait_for_timeout(350)
                with self.subTest(width=width, state="open"):
                    drawer = page.eval_on_selector(
                        ".home-header #compareNav",
                        "el => { const r = el.getBoundingClientRect();"
                        " return {left: r.left, right: r.right}; }",
                    )
                    self.assertLessEqual(
                        drawer["right"], width + GEOM_TOLERANCE_PX,
                        f"drawer extends past the viewport at {width}px",
                    )
                    # The drawer is min(78vw, 300px) wide, anchored to the
                    # right edge; "slid in" means it covers a real slice of
                    # the viewport, not just its right border.
                    self.assertLess(
                        drawer["left"], width - 200,
                        f"drawer did not slide in at {width}px",
                    )

                # Close through the overlay, like a real user would.
                page.mouse.click(10, 450)
                page.wait_for_timeout(350)
                with self.subTest(width=width, state="closed-again"):
                    nav_rect = page.eval_on_selector(
                        ".home-header #compareNav", "el => el.getBoundingClientRect().left"
                    )
                    self.assertGreaterEqual(
                        nav_rect, width - GEOM_TOLERANCE_PX,
                        f"drawer did not close via the overlay at {width}px",
                    )
            finally:
                page.close()

    def test_nav_destinations_and_labels_unchanged_in_browser(self):
        page = self._browser.new_page(viewport={"width": 375, "height": 900})
        try:
            page.goto(f"{self.base_url}/")
            page.wait_for_load_state("networkidle")
            links = page.eval_on_selector_all(
                ".home-header #compareNav a",
                "els => els.map(a => [a.getAttribute('href'), a.textContent.trim()])",
            )
            self.assertEqual([tuple(link) for link in links], GUEST_NAV)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
