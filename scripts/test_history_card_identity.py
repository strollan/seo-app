"""
Regression coverage for /history report cards that "appear identical."

Root cause (confirmed against production's real reports/history.json):
NOT a template/data-binding bug -- app.main.save_report_snapshot() and the
history.html template already correctly persist and render each entry's
own distinct site_domain/competitor_domain/score/date (see
scripts/test_history_snapshot_uniqueness.py, a prior fix for a real
timestamp-collision bug in this same area). Production's history.json
shows the same "bhdeli.com vs natenals.com" pair saved three separate
times (8:11 PM, 8:31 PM, 8:47 PM) with identical scores and empty
top_gaps/volume_opportunities each time -- these are genuine, real repeat
comparisons of the same two sites, not a display bug fabricating
duplicates. Per the task, these must never be silently deleted or
deduped.

What was genuinely weak: with no gap/volume chips to differentiate them,
the ONLY thing distinguishing three such cards was a small, muted,
14px-gray "date" line below a large domain heading -- easy to miss at a
glance, making genuinely-different (by timestamp and full URL) reports
look identical.

The fix is display-only:
  - the full compared site_url/competitor_url (not just the bare domain)
    now renders as its own compact line
  - the date renders as a prominent pill-style badge (matching the
    existing .score-pill visual language) instead of small muted text

No deletion, no dedup, no changes to save_report_snapshot()'s persistence
logic, filename/collision handling, or the score/gap computation itself.

These tests prove:
  - two cards for the same domain pair (same scores, same empty gaps --
    the exact real-world "looks like a duplicate" shape) still render
    visibly distinct full URLs and date pills
  - two cards for genuinely different site/competitor pairs render their
    own distinct domains, URLs, dates, and scores
  - a legacy history entry missing site_url/competitor_url (old schema,
    before those fields existed) still renders without error and simply
    omits the compared-urls line
  - the existing "View Saved Report" / "Run Again" / "Delete" actions are
    still present and per-item correct
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-key")

import app.main as appmain
from fastapi.testclient import TestClient

USER = {"id": 1, "username": "user1", "role": "standard"}


def fake_auth_current_user(request):
    if request.cookies.get(appmain.AUTH_COOKIE_NAME) == "user1-token":
        return USER
    return None


def fake_csrf_token(request):
    return "test-csrf-token"


def extract_cards(html):
    return re.findall(
        r"<!-- history-card:start -->(.*?)<!-- history-card:end -->",
        html,
        re.DOTALL,
    )


class _HistoryPageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.history_path = Path(self.tmpdir.name) / "history.json"

    def _serve(self, history):
        self.history_path.write_text(json.dumps(history), encoding="utf-8")

        patches = [
            mock.patch.object(appmain, "history_file_path", lambda: str(self.history_path)),
            mock.patch.object(appmain, "auth_current_user", fake_auth_current_user),
            mock.patch.object(appmain, "_get_or_create_csrf_token", fake_csrf_token),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        client = TestClient(appmain.app)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, "user1-token")
        return client


class RepeatedDomainPairCardsRemainDistinctTests(_HistoryPageTestCase):
    """The exact real-world shape found in production: same domains, same
    scores, empty gaps/volume -- three genuine repeat runs."""

    def test_repeat_runs_of_the_same_pair_show_distinct_urls_and_dates(self):
        history = [
            {
                "date": "July 10, 2026 08:47:19.000 PM",
                "timestamp": "2026-07-10_20-47-19",
                "site_url": "https://www.bhdeli.com/",
                "competitor_url": "https://www.natenals.com/",
                "site_domain": "bhdeli.com",
                "competitor_domain": "natenals.com",
                "site_score": 80,
                "competitor_score": 55,
                "score_difference": 25,
                "top_gaps": [],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/2026-07-10_20-47-19_bhdeli-com_vs_natenals-com.html",
                "owner_id": 1,
            },
            {
                "date": "July 10, 2026 08:31:31.000 PM",
                "timestamp": "2026-07-10_20-31-31",
                "site_url": "https://www.bhdeli.com/",
                "competitor_url": "https://www.natenals.com/",
                "site_domain": "bhdeli.com",
                "competitor_domain": "natenals.com",
                "site_score": 80,
                "competitor_score": 55,
                "score_difference": 25,
                "top_gaps": [],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/2026-07-10_20-31-31_bhdeli-com_vs_natenals-com.html",
                "owner_id": 1,
            },
        ]
        client = self._serve(history)

        resp = client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

        cards = extract_cards(resp.text)
        self.assertEqual(len(cards), 2)
        first_card, second_card = cards

        self.assertIn("July 10, 2026 08:47:19.000 PM", first_card)
        self.assertIn("July 10, 2026 08:31:31.000 PM", second_card)
        self.assertNotIn("08:31:31", first_card)
        self.assertNotIn("08:47:19", second_card)

        # Each card also carries its own saved-report link, which is
        # unique per entry even when domains/scores are identical.
        self.assertIn("2026-07-10_20-47-19_bhdeli-com_vs_natenals-com.html", first_card)
        self.assertIn("2026-07-10_20-31-31_bhdeli-com_vs_natenals-com.html", second_card)
        self.assertNotIn("20-31-31", first_card)
        self.assertNotIn("20-47-19", second_card)

        for card in cards:
            self.assertIn('class="date-pill"', card)
            self.assertIn("https://www.bhdeli.com/", card)
            self.assertIn("https://www.natenals.com/", card)


class DistinctPairsRenderDistinctCardsTests(_HistoryPageTestCase):
    def test_two_different_comparisons_render_their_own_content(self):
        history = [
            {
                "date": "July 24, 2026 10:00:00.000 AM",
                "timestamp": "2026-07-24_10-00-00",
                "site_url": "https://site-a.example/",
                "competitor_url": "https://rival-a.example/",
                "site_domain": "site-a.example",
                "competitor_domain": "rival-a.example",
                "site_score": 70,
                "competitor_score": 40,
                "score_difference": 30,
                "top_gaps": ["widget"],
                "volume_opportunities": [{"keyword": "widget repair", "volume": 100}],
                "saved_report": "/reports/saved/aaa.html",
                "owner_id": 1,
            },
            {
                "date": "July 24, 2026 11:00:00.000 AM",
                "timestamp": "2026-07-24_11-00-00",
                "site_url": "https://site-b.example/",
                "competitor_url": "https://rival-b.example/",
                "site_domain": "site-b.example",
                "competitor_domain": "rival-b.example",
                "site_score": 30,
                "competitor_score": 90,
                "score_difference": -60,
                "top_gaps": ["gadget"],
                "volume_opportunities": [{"keyword": "gadget install", "volume": 200}],
                "saved_report": "/reports/saved/bbb.html",
                "owner_id": 1,
            },
        ]
        client = self._serve(history)

        resp = client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        cards = extract_cards(resp.text)
        self.assertEqual(len(cards), 2)
        first_card, second_card = cards

        self.assertIn("site-a.example vs rival-a.example", first_card)
        self.assertIn("site-b.example vs rival-b.example", second_card)
        self.assertNotIn("site-b.example", first_card)
        self.assertNotIn("site-a.example", second_card)

        self.assertIn("widget", first_card)
        self.assertIn("gadget", second_card)
        self.assertNotIn("gadget", first_card)
        self.assertNotIn("widget", second_card)

        self.assertIn("aaa.html", first_card)
        self.assertIn("bbb.html", second_card)

        # Existing per-item actions still present and correctly scoped.
        for card, saved in ((first_card, "aaa.html"), (second_card, "bbb.html")):
            self.assertIn("View Saved Report", card)
            self.assertIn("Run Again", card)
            self.assertIn("Delete", card)
            self.assertIn(saved, card)


class LegacyHistoryEntryCompatibilityTests(_HistoryPageTestCase):
    def test_legacy_entry_without_site_url_or_competitor_url_renders_without_error(self):
        legacy_entry = {
            "date": "July 10, 2026 08:47 PM",
            "timestamp": "2026-07-10_20-47-19",
            "site_domain": "legacy-site.example",
            "competitor_domain": "legacy-rival.example",
            "site_score": 65,
            "competitor_score": 45,
            "score_difference": 20,
            "top_gaps": [],
            "volume_opportunities": [],
            "saved_report": "/reports/saved/legacy.html",
            "owner_id": 1,
            # No site_url / competitor_url keys at all -- pre-dates that
            # field being added to the saved history schema.
        }
        client = self._serve([legacy_entry])

        resp = client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

        cards = extract_cards(resp.text)
        self.assertEqual(len(cards), 1)
        card = cards[0]

        self.assertIn("legacy-site.example vs legacy-rival.example", card)
        self.assertIn('class="date-pill"', card)
        self.assertIn("July 10, 2026 08:47 PM", card)
        self.assertNotIn('class="compared-urls"', card)
        self.assertIn("legacy.html", card)


if __name__ == "__main__":
    unittest.main()
