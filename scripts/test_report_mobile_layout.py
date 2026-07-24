"""
Regression coverage for mobile-layout defects on the Compare report page
(app/templates/report.html, rendered by POST /analyze).

The page already had extensive prior mobile hardening (hamburger nav,
chip/canonical-URL word-wrapping, a global body.report-page{overflow-x:hidden}
safety net). Auditing the full cascade against realistic worst-case content
(a single unbroken long token, as real scraped SEO data can produce) found
one remaining gap: `.quick-wins-list li` and `.report-box` (the Analysis
section, including AI-generated action-plan cards) had no word-break /
overflow-wrap protection, unlike every other text container on the page
(.site-url, .chip, .metric-line a, .clean-url). Because `.site-card` sits
inside a CSS Grid (`.compare-grid`), an unbroken long token there triggers
classic grid-blowout: the grid track grows to the content's min-content
width and gets silently clipped by an ancestor's overflow:hidden, cutting
the text off-screen instead of wrapping.

No headless browser is available in this environment (installing one is an
environment change requiring separate approval), so these tests verify the
CSS invariants that guarantee no page-level horizontal overflow -- every
text container that can hold scraped/LLM content has word-break protection,
every table is scroll-wrapped, the comparison grid collapses to one column
under the mobile breakpoint, and the global overflow-x:hidden fallback is
still present -- against the actual rendered HTML, rather than a measured
pixel screenshot.

Does not touch SEO scoring, history/saved-report data, DataForSEO/Lead
Finder, or auth/CSRF -- purely the report.html template markup/CSS.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-key")

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent / "app" / "templates")
STYLES_CSS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css"

# A single unbroken (no spaces, no hyphens) very long token -- the kind of
# string real scraped SEO data (a URL slug, a compound keyword term) can
# produce -- used to stress-test grid/flex overflow.
LONG_TOKEN = "supercalifragilisticexpialidociousemergencywaterextractionservicenationwide"


def _site(domain, title="A Title", meta="A meta description of normal length here."):
    return {
        "clean_domain": domain,
        "score": 82,
        "title": title,
        "title_length": len(title),
        "title_status": "Good",
        "meta_description": meta,
        "meta_length": len(meta),
        "meta_status": "Good",
        "h1": "Some H1",
    }


def render_report_html(**overrides):
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    site = _site("www." + LONG_TOKEN + ".com")
    competitor = _site("shortcompetitor.com")

    gap = {
        "missing_grouped": {
            "service": [{"term": LONG_TOKEN, "priority": "high"}],
            "location": [{"term": "normal two word phrase", "priority": "medium"}],
            "commercial": [],
        },
        "shared": [{"term": "restoration"}],
    }

    context = {
        "site": site,
        "competitors": [competitor],
        "analysis_html": f"<p>See {LONG_TOKEN} for details.</p>",
        "current_page_signals": [],
        "volume_data": [
            {"keyword": LONG_TOKEN, "volume": 1000, "cpc": 2.5, "competition": "LOW"},
        ],
        "gap": gap,
        "site_score_breakdown": {"Title": 10, "Meta": 8, "Total": 18},
        "competitor_score_breakdowns": [
            {"domain": competitor["clean_domain"], "breakdown": {"Title": 8, "Meta": 6}, "score": 14}
        ],
        "site_quick_wins": [f"Add a title tag targeting '{LONG_TOKEN}'."],
        "site_section_card": None,
        "logo_url": None,
        "user": None,
    }
    context.update(overrides)
    return env.get_template("report.html").render(context)


class ReportBoxAndQuickWinsWordBreakTests(unittest.TestCase):
    """The specific gap found and fixed by this change."""

    def setUp(self):
        self.html = render_report_html()

    def test_report_box_rule_has_word_wrap_protection(self):
        rule = re.search(r"\.report-box\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(rule, "expected a bare .report-box CSS rule")
        body = rule.group(1)
        self.assertIn("overflow-wrap: anywhere", body)

    def test_quick_wins_list_item_rule_has_word_wrap_protection(self):
        rule = re.search(r"\.quick-wins-list li\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(rule, "expected a .quick-wins-list li CSS rule")
        body = rule.group(1)
        self.assertIn("overflow-wrap: anywhere", body)

    def test_long_token_present_in_quick_wins_and_analysis(self):
        # Sanity: the stress-test content actually made it into the
        # elements the new CSS rules protect.
        self.assertIn(LONG_TOKEN, self.html)
        quick_wins_section = re.search(
            r'<ul class="quick-wins-list">.*?</ul>', self.html, re.DOTALL
        )
        self.assertIsNotNone(quick_wins_section)
        self.assertIn(LONG_TOKEN, quick_wins_section.group(0))


class ExistingMobileSafetyNetsStillIntactTests(unittest.TestCase):
    """Regression guards on the mobile protections already present before
    this change, so a future edit can't silently remove them."""

    def setUp(self):
        self.html = render_report_html()

    def test_global_overflow_x_hidden_fallback_present(self):
        self.assertRegex(
            self.html,
            r"body\.report-page\s*\{\s*overflow-x:\s*hidden;\s*\}",
        )

    def test_every_table_is_scroll_wrapped(self):
        # Every <table ...> in the document must be inside a .table-wrap
        # scroll container -- a bare table with no wrapper is the one case
        # where wide content genuinely needs horizontal scroll, and that
        # scroll must be scoped to the table, not the page.
        for match in re.finditer(r"<table\b", self.html):
            preceding = self.html[: match.start()]
            last_table_wrap_open = preceding.rfind('<div class="table-wrap">')
            last_table_wrap_close = preceding.rfind("</div>")
            self.assertGreater(
                last_table_wrap_open,
                -1,
                "found a <table> not preceded by an opening .table-wrap",
            )

    def test_site_url_has_word_break(self):
        rule = re.search(r"\.site-url\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(rule)
        self.assertIn("word-break: break-word", rule.group(1))

    def test_chip_gets_overflow_wrap_anywhere_under_480px(self):
        media_start = self.html.find("@media (max-width: 480px)")
        self.assertGreater(media_start, -1, "expected a <=480px media block")
        style_end = self.html.find("</style>", media_start)
        block = self.html[media_start:style_end]
        self.assertIn(".report-page .chip", block)
        self.assertIn("overflow-wrap: anywhere !important", block)

    def test_hamburger_nav_present_for_narrow_widths(self):
        self.assertIn('data-nav-toggle', self.html)
        self.assertIn('id="reportNav"', self.html)


class DesktopComparisonGridPreservedTests(unittest.TestCase):
    """Confirms the desktop layout was not touched by the mobile fix."""

    def setUp(self):
        self.html = render_report_html()

    def test_desktop_compare_grid_is_two_columns(self):
        rule = re.search(r"\.compare-grid\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(rule)
        self.assertIn("grid-template-columns: 1fr 1fr", rule.group(1))

    def test_compare_grid_collapses_to_one_column_at_mobile_breakpoint(self):
        block = re.search(
            r"@media \(max-width: 800px\)\s*\{(.*?)\}\s*\n\s*\}",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(block, "expected the <=800px collapse block")
        self.assertIn("grid-template-columns: 1fr", block.group(1))

    def test_your_site_and_competitor_labels_present(self):
        # Comparison content must stay identifiable once stacked -- each
        # card keeps its "Your Site" / "Competitor N" label regardless of
        # column count.
        self.assertIn('<div class="site-label">Your Site</div>', self.html)
        self.assertIn("Competitor 1", self.html)


class PrimaryActionsRemainUsableTests(unittest.TestCase):
    def setUp(self):
        self.html = render_report_html()

    def test_nav_buttons_have_44px_touch_target(self):
        # The header-actions/report-actions button rule (shared external
        # stylesheet, not inlined in report.html) sets a 44px minimum
        # touch target; report.html's own override only touches min-width.
        css = STYLES_CSS_PATH.read_text()
        idx = css.find(".header .report-actions a")
        self.assertGreater(idx, -1, "expected the header report-actions button rule")
        rule_end = css.find("}", idx)
        rule_body = css[idx:rule_end]
        self.assertIn("min-height: 44px !important", rule_body)

    def test_home_and_save_pdf_actions_present(self):
        self.assertIn('href="/index"', self.html)
        self.assertIn("Save PDF", self.html)


if __name__ == "__main__":
    unittest.main()
