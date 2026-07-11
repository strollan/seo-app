"""
Regression coverage for a bug in app.main.save_report_snapshot() where the
saved-report filename and displayed history date were both built from a
second-precision timestamp ("%Y-%m-%d_%H-%M-%S" / "%I:%M:%S %p"). Two reports
generated for the same site/competitor pair within the same second (a double
form submit, or a quick "Run Again") produced the exact same filename -- so
the second save silently overwrote the first saved report file on disk -- and
the resulting /history entries carried the exact same displayed date, making
the two history cards render as visually identical.

The fix adds microsecond precision to both the on-disk filename and the
displayed "date" field, so concurrent saves never collide and same-second
(even same-millisecond) entries remain visually distinct on the /history
page.

reports_dir_path and history_file_path are monkeypatched to a temp directory
so this test never touches the real reports/ tree, and datetime is
monkeypatched to a fake clock so the "same second" collision case is
deterministic rather than timing-dependent.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# agents.seo_agent (imported by app.main) requires OPENAI_API_KEY at import
# time; set a placeholder so this offline test can import app.main without a
# real key when one isn't already present in the environment/.env.
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


class _FixedClock:
    """Stand-in for the datetime module exposing only what main.py uses."""

    def __init__(self, values):
        self._values = list(values)

    def now(self):
        return self._values.pop(0)


class HistorySnapshotUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        reports_dir = Path(self.tmpdir.name) / "reports"
        saved_dir = reports_dir / "saved"
        saved_dir.mkdir(parents=True)
        self.history_path = reports_dir / "history.json"

        patches = [
            mock.patch.object(appmain, "reports_dir_path", lambda: str(reports_dir)),
            mock.patch.object(appmain, "history_file_path", lambda: str(self.history_path)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.site = {"clean_domain": "example.com", "url": "https://example.com", "score": 80}
        self.competitor = {"clean_domain": "rival.com", "url": "https://rival.com", "score": 60}

    def test_exact_same_datetime_saves_get_distinct_filenames_and_preserve_contents(self):
        exact_same_time = datetime(2026, 7, 11, 9, 0, 0, 111111)

        with mock.patch.object(appmain, "datetime", _FixedClock([exact_same_time])):
            first = appmain.save_report_snapshot(
                "<html>first</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        with mock.patch.object(appmain, "datetime", _FixedClock([exact_same_time])):
            second = appmain.save_report_snapshot(
                "<html>second</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        self.assertNotEqual(first["saved_report"], second["saved_report"])
        self.assertNotEqual(first["timestamp"], second["timestamp"])
        self.assertTrue(second["timestamp"].endswith("-2"))

        first_path = Path(first["saved_path"])
        second_path = Path(second["saved_path"])

        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(
            first_path.read_text(encoding="utf-8"),
            "<html>first</html>",
        )
        self.assertEqual(
            second_path.read_text(encoding="utf-8"),
            "<html>second</html>",
        )

    def test_exact_same_datetime_saves_get_distinct_display_labels(self):
        exact_same_time = datetime(2026, 7, 11, 9, 0, 0, 111111)

        with mock.patch.object(appmain, "datetime", _FixedClock([exact_same_time])):
            first = appmain.save_report_snapshot(
                "<html>first</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        with mock.patch.object(appmain, "datetime", _FixedClock([exact_same_time])):
            second = appmain.save_report_snapshot(
                "<html>second</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        self.assertEqual(
            first["date"],
            "July 11, 2026 09:00:00.111 AM · ID 111",
        )
        self.assertEqual(
            second["date"],
            "July 11, 2026 09:00:00.111 AM · ID 111-2",
        )
        self.assertNotEqual(first["date"], second["date"])

    def test_same_millisecond_saves_get_distinct_display_labels(self):
        first_time = datetime(2026, 7, 11, 9, 0, 0, 111111)
        second_time = datetime(2026, 7, 11, 9, 0, 0, 111222)

        with mock.patch.object(appmain, "datetime", _FixedClock([first_time])):
            first = appmain.save_report_snapshot(
                "<html>first</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        with mock.patch.object(appmain, "datetime", _FixedClock([second_time])):
            second = appmain.save_report_snapshot(
                "<html>second</html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        self.assertEqual(
            first["date"],
            "July 11, 2026 09:00:00.111 AM · ID 111",
        )
        self.assertEqual(
            second["date"],
            "July 11, 2026 09:00:00.111 AM · ID 222",
        )
        self.assertNotEqual(first["date"], second["date"])

    def test_displayed_date_uses_readable_milliseconds_and_short_id(self):
        now = datetime(2026, 7, 11, 9, 0, 5, 111222)

        with mock.patch.object(appmain, "datetime", _FixedClock([now])):
            item = appmain.save_report_snapshot(
                "<html></html>",
                self.site,
                self.competitor,
                {},
                [],
                owner_id=1,
            )

        self.assertEqual(
            item["date"],
            "July 11, 2026 09:00:05.111 AM · ID 222",
        )


class HistorySnapshotRenderingTests(unittest.TestCase):
    """Confirms the same-second collision fix is actually visible on the
    rendered /history page, not just in the underlying data structure."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmpdir.name) / "history.json"
        history = [
            {
                "date": "July 11, 2026 09:00:00.111 AM",
                "site_domain": "example.com",
                "competitor_domain": "rival.com",
                "site_score": 80,
                "competitor_score": 60,
                "score_difference": 20,
                "top_gaps": [],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/first-report.html",
                "owner_id": 1,
            },
            {
                "date": "July 11, 2026 09:00:00.222 AM",
                "site_domain": "example.com",
                "competitor_domain": "rival.com",
                "site_score": 80,
                "competitor_score": 60,
                "score_difference": 20,
                "top_gaps": [],
                "volume_opportunities": [],
                "saved_report": "/reports/saved/second-report.html",
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

    def test_same_second_history_cards_render_distinct_date_labels(self):
        resp = self.client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

        cards = extract_cards(resp.text)
        self.assertEqual(len(cards), 2)

        first_card, second_card = cards
        self.assertIn("July 11, 2026 09:00:00.111 AM", first_card)
        self.assertIn("July 11, 2026 09:00:00.222 AM", second_card)
        self.assertNotIn("July 11, 2026 09:00:00.222 AM", first_card)
        self.assertNotIn("July 11, 2026 09:00:00.111 AM", second_card)


if __name__ == "__main__":
    unittest.main()
