"""
Ownership-isolation tests for GET /history/rerun (app.main.rerun_saved_report).

Regression coverage for a bug where /history/rerun had no login check and no
owner check at all, so any visitor (including logged-out ones) could rerun
*any* user's saved report just by guessing/copying a saved_report path.

auth_current_user is monkeypatched so these tests never touch the real auth
DB (no session/user rows are created), and app.main.analyze is monkeypatched
so no real crawl/network request happens (and fails the test if reached:
rerun now only pre-fills /compare) -- only the ownership-check branch
in rerun_saved_report is exercised. history.json is redirected to a temp
file so the real reports/history.json is never read or written.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app.main as appmain

USERS = {
    "user1-token": {"id": 1, "username": "user1", "role": "standard"},
    "user2-token": {"id": 2, "username": "user2", "role": "standard"},
    "admin-token": {"id": 99, "username": "admin", "role": "admin"},
}


def fake_auth_current_user(request):
    token = request.cookies.get(appmain.AUTH_COOKIE_NAME)
    return USERS.get(token)


async def fake_analyze(request, url_1="", url_2=""):
    raise AssertionError("rerun must not run a scan; it only pre-fills Compare")


class HistoryRerunOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmpdir.name) / "history.json"
        history = [
            {
                "saved_report": "/reports/saved/user1-report.html",
                "site_url": "https://user1-site.example",
                "competitor_url": "https://user1-competitor.example",
                "owner_id": 1,
            },
            {
                "saved_report": "/reports/saved/user2-report.html",
                "site_url": "https://user2-site.example",
                "competitor_url": "https://user2-competitor.example",
                "owner_id": 2,
            },
        ]
        self.history_path.write_text(json.dumps(history), encoding="utf-8")

        patches = [
            mock.patch.object(appmain, "history_file_path", lambda: str(self.history_path)),
            mock.patch.object(appmain, "auth_current_user", fake_auth_current_user),
            mock.patch.object(appmain, "analyze", fake_analyze),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(appmain.app)

    def assertPrefilledCompare(self, resp, url_1, url_2):
        # Rerun pre-fills Compare (303); the paid scan runs only when the user clicks Analyze.
        self.assertEqual(resp.status_code, 303)
        location = urlsplit(resp.headers["location"])
        self.assertEqual(location.path, "/compare")
        self.assertEqual(parse_qs(location.query), {"url_1": [url_1], "url_2": [url_2]})

    def test_owner_can_rerun_own_report(self):
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, "user1-token")
        resp = self.client.get(
            "/history/rerun",
            params={"saved_report": "/reports/saved/user1-report.html"},
            follow_redirects=False,
        )
        self.assertPrefilledCompare(resp, "https://user1-site.example", "https://user1-competitor.example")

    def test_other_users_report_is_blocked(self):
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, "user1-token")
        resp = self.client.get(
            "/history/rerun",
            params={"saved_report": "/reports/saved/user2-report.html"},
            follow_redirects=False,
        )
        # Not found for this owner -> falls through to the not-found redirect,
        # never reaching fake_analyze / real analyze.
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/history")

    def test_admin_can_rerun_any_users_report(self):
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, "admin-token")
        resp = self.client.get(
            "/history/rerun",
            params={"saved_report": "/reports/saved/user2-report.html"},
            follow_redirects=False,
        )
        self.assertPrefilledCompare(resp, "https://user2-site.example", "https://user2-competitor.example")

    def test_unauthenticated_request_is_redirected_to_login(self):
        resp = self.client.get(
            "/history/rerun",
            params={"saved_report": "/reports/saved/user1-report.html"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()
