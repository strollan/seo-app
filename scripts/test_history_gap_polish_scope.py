"""
Regression coverage for a bug in GET /history rendering where the
single-report polish pipeline was also applied to the multi-card history
list page. A roofing domain anywhere in the full page caused generic chip
text such as "Drain Cleaning" to be replaced with "Roof Repair" across
unrelated cards, making different saved reports appear contaminated or
identical.

The fix prevents final_html_report_polish_middleware_v3 from running on the
bare /history list page while preserving its use for /history/rerun and
single-report pages. The separately scoped history-card middleware may still
relabel text inside the roofing card itself, but unrelated cards must keep
their independently rendered text.

auth_current_user and _get_or_create_csrf_token are monkeypatched so this
test never touches the real auth/session DB, and history_file_path is
redirected to a temp file so the real reports/history.json is never read.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app.main as appmain

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


class HistoryGapPolishScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmpdir.name) / "history.json"
        history = [
            {
                "date": "January 01, 2026 09:00 AM",
                "site_domain": "myroofingco.com",
                "competitor_domain": "liroofing.com",
                "site_score": 80,
                "competitor_score": 70,
                "score_difference": 10,
                "top_gaps": ["Drain Cleaning"],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/roofing-report.html",
                "owner_id": 1,
            },
            {
                "date": "January 01, 2026 09:05 AM",
                "site_domain": "smiledental.com",
                "competitor_domain": "bestdental.com",
                "site_score": 60,
                "competitor_score": 55,
                "score_difference": 5,
                "top_gaps": ["Drain Cleaning"],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/dental-report.html",
                "owner_id": 1,
            },
        ]
        self.history_path.write_text(json.dumps(history), encoding="utf-8")

        patches = [
            mock.patch.object(appmain, "history_file_path", lambda: str(self.history_path)),
            mock.patch.object(appmain, "auth_current_user", fake_auth_current_user),
            mock.patch.object(appmain, "_get_or_create_csrf_token", fake_csrf_token),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(appmain.app)
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, "user1-token")

    def test_unrelated_card_chip_text_is_not_relabeled_by_another_cards_industry(self):
        resp = self.client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

        cards = extract_cards(resp.text)
        self.assertEqual(len(cards), 2)

        roofing_card, dental_card = cards
        self.assertIn("myroofingco.com", roofing_card)
        self.assertIn("smiledental.com", dental_card)

        # The card that actually matched "roofing" gets its own chip relabeled.
        self.assertIn("Roof Repair", roofing_card)
        self.assertNotIn("Drain Cleaning", roofing_card)

        # The unrelated dental card must keep its original generic chip text
        # and must not inherit the other card's industry relabeling.
        self.assertIn("Drain Cleaning", dental_card)
        self.assertNotIn("Roof Repair", dental_card)

        # Fields bound per-card by the template (domain, score, date, saved
        # report link) are independently rendered from each history entry and
        # must stay distinct -- only the matched card's chip text should ever
        # change, never the surrounding ownership-rendered card data. This
        # rules out "identical cards" being caused by these fields collapsing
        # together, isolating the symptom to the chip-text rewrite bug.
        self.assertIn("80", roofing_card)
        self.assertIn("liroofing.com", roofing_card)
        self.assertIn("roofing-report.html", roofing_card)
        self.assertIn("January 01, 2026 09:00 AM", roofing_card)

        self.assertIn("60", dental_card)
        self.assertIn("bestdental.com", dental_card)
        self.assertIn("dental-report.html", dental_card)
        self.assertIn("January 01, 2026 09:05 AM", dental_card)


if __name__ == "__main__":
    unittest.main()
