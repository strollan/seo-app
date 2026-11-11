"""Offline tests for the Lead snapshot panel (score, organic rank, Maps rank, website).

Display only: no network, no paid calls. Run: python -m unittest scripts.test_lead_snapshot
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import lead_dashboard_agent as dashboard  # noqa: E402
from agents import lead_snapshot_agent as snap  # noqa: E402


def organic(**kw):
    row = dict(title="Alpha Bagels", domain="alphabagels.example", url="https://alphabagels.example/",
               serp_page="4", serp_position="34", final_lead_score="52", keyword="bagels", market="Shirley, NY")
    row.update(kw)
    return row


def maps(**kw):
    row = dict(title="Maps Bagels", domain="mapsbagels.example", url="https://mapsbagels.example/",
               website_status="verified", website_verified="true", place_id="ChIJ_x", cid="",
               serp_page="Google Maps", serp_position="7", maps_position="7", source="dataforseo_maps",
               final_lead_score="67", keyword="bagels", market="Shirley, NY")
    row.update(kw)
    return row


class SnapshotTests(unittest.TestCase):
    def test_results_page_math(self):
        for pos, page in ((1, 1), (10, 1), (11, 2), (31, 4), (34, 4), (40, 4), (41, 5), (100, 10)):
            self.assertEqual(snap.results_page(pos), page, pos)

    def test_organic_rank_and_score(self):
        out = snap.snapshot_html(organic())
        self.assertIn("<b>52</b> / 100", out)
        self.assertIn('style="width:52%"', out)
        self.assertIn('aria-label="Lead score 52 out of 100"', out)
        self.assertIn("#34 · Page 4", out)
        self.assertNotIn('class="ls-label">Maps<', out)

    def test_unmeasured_organic_rank(self):
        for row in (organic(serp_position=""), organic(serp_position="manual"), organic(serp_position="0")):
            out = snap.snapshot_html(row)
            self.assertIn("Organic rank not checked.", out)
            self.assertNotIn("· Page", out)

    def test_maps_position_is_never_organic(self):
        out = snap.snapshot_html(maps())
        self.assertIn("Organic rank not checked.", out)
        self.assertNotIn("· Page", out)
        self.assertIn('Maps<span class="ls-long"> Position</span></span><span class="ls-value">#7', out)
        # Organic-export Google Places rows: not organic, and not a Maps listing rank.
        places = snap.snapshot_html(organic(serp_page="Google Places", serp_position="2", source="google_places"))
        self.assertIn("Organic rank not checked.", places)
        self.assertNotIn('class="ls-label">Maps<', places)

    def test_score_missing_or_clamped(self):
        self.assertIn("Not scored", snap.snapshot_html(organic(final_lead_score="", score="")))
        self.assertIn("<b>100</b> / 100", snap.snapshot_html(organic(final_lead_score="140")))
        self.assertIn("<b>40</b> / 100", snap.snapshot_html(organic(final_lead_score="", score="40")))

    def test_website_states(self):
        verified = snap.snapshot_html(maps())
        self.assertIn('Yes · <span class="ls-tag ls-verified">Verified</span>', verified)
        self.assertIn('<a class="ls-url" href="https://mapsbagels.example/" title="https://mapsbagels.example/" '
                      'target="_blank" rel="noopener noreferrer nofollow">mapsbagels.example</a>', verified)
        unverified = snap.snapshot_html(maps(website_verified="false", website_status="unverified"))
        self.assertIn("Yes · <span class=\"ls-tag ls-unverified\">Not verified</span>", unverified)
        self.assertNotIn(">Verified<", unverified)
        self.assertIn(">Not verified<", snap.snapshot_html(organic()))
        for row in (maps(url="", domain="", website_status="missing", website_verified="false"),
                    organic(url="", domain=""),
                    organic(url="https://www.google.com/url?q=https://x.example")):
            out = snap.snapshot_html(row)
            self.assertIn("No website listed", out)
            self.assertNotIn("ls-url", out)
            self.assertNotIn("<a ", out)
        # The domain is never substituted for a missing URL.
        self.assertNotIn("alphabagels.example", snap.snapshot_html(organic(url="")))
        # A recorded non-http value is shown as text, never linked.
        self.assertNotIn("<a ", snap.snapshot_html(organic(url="javascript:alert(1)")))

    def test_display_url(self):
        self.assertEqual(snap.display_url("https://www.example.com/"), "example.com")
        self.assertEqual(snap.display_url("http://example.com/menu/"), "example.com/menu/")

    def test_score_basis_tooltip(self):
        out = snap.snapshot_html(maps(score_basis='Phone +15 · Verified website +20 "x"'))
        self.assertIn('title="Why: Phone +15 · Verified website +20 &quot;x&quot;"', out)
        self.assertNotIn("title=\"Why", snap.snapshot_html(organic()))

    def test_escaping(self):
        out = snap.snapshot_html(organic(url='https://x.example/"><script>alert(1)</script>'))
        self.assertNotIn("<script>", out)


class CardIntegrationTests(unittest.TestCase):
    def test_saved_cards_keep_every_action(self):
        out = dashboard.lead_cards([organic(), maps()], selected_name="x.csv", csrf_token="t")
        self.assertEqual(out.count('class="lead-snapshot"'), 2)
        self.assertEqual(out.count('class="lead-add-crm"'), 2)
        self.assertEqual(out.count('class="lead-top-competitor"'), 2)
        self.assertIn('<div class="lead-head">', out)

    def test_live_page_mirror_and_css(self):
        src = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("function leadSnapshotHtml(lead)", src)
        self.assertEqual(src.count("${{leadSnapshotHtml(lead)}}"), 2)  # organic + Maps live cards
        self.assertIn("/static/lead-snapshot.css?v=", src)
        self.assertIn("/static/lead-snapshot.css?v=",
                      (ROOT / "agents/lead_dashboard_agent.py").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "app/static/lead-snapshot.css").exists())


if __name__ == "__main__":
    unittest.main()
