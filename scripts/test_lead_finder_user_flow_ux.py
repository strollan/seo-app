"""Focused source/render coverage for Lead Finder UX flow copy and controls.

No test starts a job, contacts a provider, or writes an export.
"""

import inspect
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.lead_dashboard_agent as dashboard


class LeadFinderUserFlowUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = dashboard.render_lead_dashboard(
            current_user={"role": "standard", "username": "ux-test"},
            csrf_token="test-token",
        )
        cls.live_source = inspect.getsource(appmain.leadbot_live_page)

    def test_search_form_is_clear_and_required_without_visible_industry(self):
        self.assertIn('for="leadbotKeywordInput">Keyword (Required)</label>', self.dashboard)
        self.assertIn('placeholder="plumber, dentist, web designer" required', self.dashboard)
        self.assertIn(
            'for="leadbotMarketInput">City, State, or ZIP Code (Required)</label>',
            self.dashboard,
        )
        self.assertIn('placeholder="Albany, NY or 10001" required', self.dashboard)
        self.assertIn("Start Lead Finder Scan", self.dashboard)
        self.assertNotIn('name="industry"', self.dashboard)
        self.assertNotIn("<b>Industry</b>", self.dashboard)

    def test_scan_start_and_first_result_states_are_explicit(self):
        self.assertIn('btn.textContent = "Starting Lead Finder..."', self.dashboard)
        self.assertIn('<div id="message"><span class="pulse"></span>Starting...</div>', self.live_source)
        self.assertIn("A lead card is now visible, so the initial waiting message is stale.", self.live_source)
        self.assertIn('waitingMessageEl.style.display = "none"', self.live_source)
        self.assertIn('liveConsoleFirstLine.textContent = "Lead cards are appearing."', self.live_source)

    def test_complete_partial_provider_and_zero_states_remain_distinct(self):
        required = (
            "Scan complete. Your export is ready.",
            "Partial results are ready.",
            "Your partial export is ready.",
            "Lead search temporarily unavailable",
            "This scan could not be completed. Please try again shortly.",
            "The scan completed, but no qualifying leads were found for this search.",
        )
        for copy in required:
            self.assertIn(copy, self.live_source)

    def test_invalid_location_state_remains_specific(self):
        self.assertEqual(
            appmain.MARKET_REQUIRED_MESSAGE,
            "Enter a city, state, or ZIP code.",
        )
        self.assertIn("Enter a city, state, or ZIP code.", self.dashboard)

    def test_result_card_has_core_information_actions_and_four_why_lines(self):
        cards = dashboard.lead_cards(
            [{
                "title": "Example Plumbing With A Very Long Business Name",
                "domain": "a-very-long-example-business-domain.example",
                "url": "https://a-very-long-example-business-domain.example",
                "serp_page": "2",
                "serp_position": "14",
                "best_phone": "555-0100",
                "emails": "hello@example.test",
                "address": "1 Main Street, Albany, NY",
                "contact_page_url": "https://example.test/contact",
                "keyword": "plumber",
                "market": "Albany NY",
            }],
            selected_name="test.csv",
            csrf_token="token",
        )
        for copy in (
            "Example Plumbing",
            "a-very-long-example-business-domain.example",
            "Page 2 · Position 14",
            "<b>Phone</b>",
            "<b>Email</b>",
            "<b>Address</b>",
            "<b>Contact Page</b>",
            "Save Details",
            "Save Address",
            "Why this lead",
        ):
            self.assertIn(copy, cards)
        why = re.search(r'<div class="leadbot-why-note">(.*?)</div>', cards, re.DOTALL).group(1)
        self.assertEqual(len(re.findall(r"<p>.*?</p>", why, re.DOTALL)), 4)

    def test_export_actions_name_csv_and_live_destination(self):
        self.assertIn(">Download CSV</a>", self.dashboard)
        self.assertIn("Open Results &amp; Exports", self.dashboard)
        self.assertIn("Review Results &amp; Export CSV", self.live_source)
        self.assertIn("export-partial-badge", self.dashboard)
        self.assertIn(
            ">Partial</span>",
            inspect.getsource(dashboard.render_lead_dashboard),
        )

    def test_mobile_card_and_export_safeguards_remain(self):
        for css in (
            "@media (max-width: 700px)",
            "overflow-wrap: anywhere",
            "min-width: 0 !important",
            "max-width: 100% !important",
            "width: 100% !important",
            "min-height: 44px !important",
            "grid-template-columns: 1fr !important",
        ):
            self.assertIn(css, self.dashboard)


if __name__ == "__main__":
    unittest.main()
