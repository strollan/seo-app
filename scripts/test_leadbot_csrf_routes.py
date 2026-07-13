"""
Regression tests for the V1 security fix converting state-changing LeadBot
GET routes to POST + CSRF:

  - POST /lead-bot/live-start        (app.main.leadbot_live_start)
  - POST /lead-bot/delete-row/{f}    (app.main.leadbot_delete_row_route)
  - POST /lead-bot/delete-export/{f} (app.main.leadbot_delete_export)
  - POST /lead-bot/delete-row-safe   (app.main.leadbot_delete_row_safe)

Before the fix these were plain GET (or GET+POST, for delete-row-safe)
routes reachable by a simple link click or top-level navigation even with a
SameSite=Lax session cookie (Lax cookies still ride along on top-level GET
navigation), so a crafted link could start a paid scan or delete a lead/
export for a logged-in victim with no confirmation and no token check.

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file so real
session/user data is never touched. Real sessions and real CSRF tokens are
issued through the actual auth_agent flow (not mocked) so these tests
exercise the real CSRF check, not a stand-in for it. Export CSVs used for
the delete-row/delete-export/delete-row-safe tests are written under the
real exports/ directory with randomized filenames (agents/lead_dashboard_agent.py
hardcodes "exports" and "data/leadbot_export_owners.json" as literal paths
in a few places, so redirecting them via monkeypatch isn't practical) and
removed in addCleanup.
"""

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent

VALID_PASSWORD = "correct-horse-battery-staple"


class LeadBotCsrfRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        auth_agent.create_user("user2", VALID_PASSWORD, role="standard", email="user2@example.com")

        self.client = TestClient(appmain.app)

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token


class LiveStartCsrfTests(LeadBotCsrfRouteTestCase):
    def test_get_no_longer_starts_a_scan(self):
        self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.get(
                "/lead-bot/live-start",
                params={"industry": "roofing", "market": "long island"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 405)
        mock_create_job.assert_not_called()

    def test_post_without_valid_csrf_is_rejected(self):
        self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"industry": "roofing", "market": "long island", "csrf_token": "not-a-real-token"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create_job.assert_not_called()

    def test_post_with_valid_csrf_starts_a_scan(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-123") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={
                    "industry": "roofing",
                    "market": "long island",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot/live/job-123")
        mock_create_job.assert_called_once()

    def test_logged_out_post_redirects_to_login_without_starting_a_scan(self):
        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"industry": "roofing", "market": "long island", "csrf_token": ""},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")
        mock_create_job.assert_not_called()


class ExportFixture:
    """Creates a throwaway export CSV (+ owner sidecar) under the real
    exports/ directory and guarantees cleanup, since the ownership-lookup
    helpers in agents/lead_dashboard_agent.py hardcode the "exports" path."""

    def __init__(self, owner_username):
        self.dir = Path("exports")
        self.dir.mkdir(exist_ok=True)
        self.filename = f"test_csrf_fix_{uuid.uuid4().hex}.csv"
        self.path = self.dir / self.filename
        self.owner_sidecar = self.dir / f"{self.filename}.owner.json"

        self.path.write_text("domain,title\nexample.com,Example Biz\n", encoding="utf-8")
        self.owner_sidecar.write_text(json.dumps({"owner_username": owner_username}), encoding="utf-8")

    def cleanup(self):
        for p in (self.path, self.owner_sidecar):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def rows(self):
        if not self.path.exists():
            return []
        return [
            line.strip() for line in self.path.read_text(encoding="utf-8").splitlines()[1:] if line.strip()
        ]


class DeleteRowCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_no_longer_deletes_the_row(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            f"/lead-bot/delete-row/{self.export.filename}",
            params={"domain": "example.com"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": "not-a-real-token"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_owner_post_with_valid_csrf_deletes_the_row(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.export.rows(), [])

    def test_other_user_cannot_delete_row_from_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])


class DeleteExportCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_no_longer_deletes_the_export(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            f"/lead-bot/delete-export/{self.export.filename}",
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertTrue(self.export.path.exists())

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": "not-a-real-token"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self.export.path.exists())

    def test_owner_post_with_valid_csrf_deletes_the_export(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(self.export.path.exists())

    def test_other_user_cannot_delete_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self.export.path.exists())


class DeleteRowSafeCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_is_rejected(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            "/lead-bot/delete-row-safe",
            params={"filename": self.export.filename, "domain": "example.com"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_logged_out_post_is_rejected(self):
        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={"filename": self.export.filename, "domain": "example.com", "csrf_token": ""},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": "not-a-real-token",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_owner_post_with_valid_csrf_deletes_the_row(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(self.export.rows(), [])

    def test_other_user_cannot_delete_row_from_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])


if __name__ == "__main__":
    unittest.main()
