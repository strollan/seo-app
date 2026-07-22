"""
Regression tests for limited guest (unauthenticated) beta testing of Lead
Finder, added so friends/testers can try the core product before creating
an account.

Covers:
  - guest can open the Lead Finder input page (GET /lead-bot)
  - guest can start a limited scan (POST /lead-bot/live-start) with a valid
    guest CSRF cookie pair, and is rejected without one
  - guest scan params are clamped to the small guest ceiling regardless of
    what is submitted
  - guest scan-rate limiting (agents.guest_session_agent)
  - guest cannot reach /history, /settings (admin-only), or another guest's
    /lead-bot/live-status/{job_id} / /lead-bot/live/{job_id}
  - registered-user behavior on these same routes is unchanged
  - login and create-account still work end to end
  - the privacy note renders on the login/create-account pages and the
    guest beta note renders on the homepage and Lead Finder page only when
    logged out

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file, same as
scripts/test_leadbot_csrf_routes.py, so real user/session data is never
touched. agents.lead_live_job_agent.create_job is mocked for the
scan-starting tests (no real scan/external API call). Ownership-matching
tests write a small real job JSON file directly under
data/leadbot_live_jobs/ (same pattern as
scripts/test_lead_live_job_export_ownership.py) and remove it in cleanup.
"""

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
import agents.guest_session_agent as guest_agent
import agents.lead_live_job_agent as job_agent

VALID_PASSWORD = "correct-horse-battery-staple"


class GuestAccessTestCase(unittest.TestCase):
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
        auth_agent.create_user("theadmin", VALID_PASSWORD, role="admin", email="admin@example.com")

        # Isolate the in-process guest rate limiter between tests.
        rl_patch = mock.patch.object(guest_agent, "_guest_scan_rate_limit_attempts", {})
        rl_patch.start()
        self.addCleanup(rl_patch.stop)

        self.client = TestClient(appmain.app)
        self._job_files_to_remove = []

    def tearDown(self):
        for path in self._job_files_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token

    def guest_client(self):
        """A fresh TestClient with no login cookie, used for guest tests."""
        return TestClient(appmain.app)

    def write_fake_job(self, guest_id="", owner_email="", owner_username=""):
        """Write a minimal real job JSON file and return its job_id, same
        on-disk shape agents.lead_live_job_agent.write_job() produces."""
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": "done",
            "message": "Done",
            "params": {
                "guest_id": guest_id,
                "owner_email": owner_email,
                "owner_username": owner_username,
            },
            "leads": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self._job_files_to_remove.append(job_agent.job_path(job_id))
        return job_id


class LeadFinderGuestPageTests(GuestAccessTestCase):
    """1. Guest can open Lead Finder."""

    def test_guest_can_load_lead_bot_page(self):
        client = self.guest_client()
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Run Lead Finder", resp.text)

    def test_guest_page_issues_guest_cookie_pair(self):
        client = self.guest_client()
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(guest_agent.GUEST_ID_COOKIE, resp.cookies)
        self.assertIn(guest_agent.GUEST_CSRF_COOKIE, resp.cookies)

    def test_guest_beta_note_renders_when_logged_out(self):
        """Wording updated to disclose the actual guest ceiling (3 scans/hour,
        limited results) instead of the vaguer "Guest access is limited" --
        see scripts/test_guest_scan_rejection_messaging.py for the rest of
        the guest-rejection-messaging coverage this wording change is part
        of."""
        client = self.guest_client()
        resp = client.get("/lead-bot")
        self.assertIn(
            "Guest preview: up to 3 scans per hour with limited results. "
            "No account required for beta testing, and your contact "
            "information is not required.",
            resp.text,
        )

    def test_beta_note_absent_for_logged_in_user(self):
        client = self.guest_client()
        self.login(client, "user1")
        resp = client.get("/lead-bot")
        self.assertNotIn("No account required for beta testing", resp.text)

    def test_homepage_beta_note_renders_when_logged_out(self):
        client = self.guest_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No account required for beta testing", resp.text)

    def test_homepage_beta_note_absent_for_logged_in_user(self):
        client = self.guest_client()
        self.login(client, "user1")
        resp = client.get("/")
        self.assertNotIn("No account required for beta testing", resp.text)

    def test_guest_page_keeps_keyword_and_market_fields_visible(self):
        """13. Keyword + City, State, or ZIP Code remains the visible input
        flow for guests too, not just registered users."""
        client = self.guest_client()
        resp = client.get("/lead-bot")
        self.assertIn("City, State, or ZIP Code", resp.text)
        self.assertIn("Keyword", resp.text)

    def test_guest_page_does_not_restore_industry_field(self):
        """14. The Industry field is not restored for guests either."""
        client = self.guest_client()
        resp = client.get("/lead-bot")
        self.assertNotIn(">Industry<", resp.text)


class LeadFinderGuestScanStartTests(GuestAccessTestCase):
    """2. Guest can start the permitted limited scan flow."""

    def _get_guest_cookies(self, client):
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        return (
            client.cookies.get(guest_agent.GUEST_ID_COOKIE),
            client.cookies.get(guest_agent.GUEST_CSRF_COOKIE),
        )

    def test_guest_with_valid_csrf_starts_a_scan(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self._get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job", return_value="guest-job-1") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={
                    "keyword": "plumber",
                    "market": "Albany, NY",
                    "csrf_token": guest_csrf,
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot/live/guest-job-1")
        mock_create.assert_called_once()

    def test_guest_scan_params_are_clamped_to_guest_ceiling(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self._get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job", return_value="guest-job-2") as mock_create:
            client.post(
                "/lead-bot/live-start",
                data={
                    "keyword": "plumber",
                    "market": "Albany, NY",
                    "limit": "500",
                    "per_batch": "50",
                    "per_query_limit": "50",
                    "max_queries": "50",
                    "csrf_token": guest_csrf,
                },
                follow_redirects=False,
            )

        called_params = mock_create.call_args[0][0]
        self.assertLessEqual(called_params["limit"], guest_agent.GUEST_SCAN_LIMIT_MAX)
        self.assertLessEqual(called_params["per_batch"], guest_agent.GUEST_SCAN_PER_BATCH_MAX)
        self.assertLessEqual(called_params["per_query_limit"], guest_agent.GUEST_SCAN_PER_QUERY_LIMIT_MAX)
        self.assertLessEqual(called_params["max_queries"], guest_agent.GUEST_SCAN_MAX_QUERIES_MAX)
        self.assertTrue(called_params.get("guest_id"))

    def test_guest_scan_without_csrf_cookie_is_rejected(self):
        client = self.guest_client()  # never loaded /lead-bot, no cookies at all

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": ""},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create.assert_not_called()

    def test_guest_scan_with_mismatched_csrf_is_rejected(self):
        client = self.guest_client()
        self._get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={
                    "keyword": "plumber",
                    "market": "Albany, NY",
                    "csrf_token": "not-the-real-token",
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create.assert_not_called()

    def test_guest_blank_market_is_still_rejected(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self._get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 400)
        mock_create.assert_not_called()


class GuestRateLimitTests(GuestAccessTestCase):
    """8. Guest rate/usage restrictions are enforced."""

    def test_guest_scan_rate_limit_blocks_after_max_attempts(self):
        client = self.guest_client()
        resp = client.get("/lead-bot")
        guest_csrf = client.cookies.get(guest_agent.GUEST_CSRF_COOKIE)

        with mock.patch.object(job_agent, "create_job", return_value="job-rl"):
            for _ in range(guest_agent.GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS):
                resp = client.post(
                    "/lead-bot/live-start",
                    data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": guest_csrf},
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)

            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 429)

    def test_clamp_guest_scan_params_pure_function(self):
        limit, per_batch, per_query, max_q = guest_agent.clamp_guest_scan_params(
            9999, 9999, 9999, 9999
        )
        self.assertEqual(limit, guest_agent.GUEST_SCAN_LIMIT_MAX)
        self.assertEqual(per_batch, guest_agent.GUEST_SCAN_PER_BATCH_MAX)
        self.assertEqual(per_query, guest_agent.GUEST_SCAN_PER_QUERY_LIMIT_MAX)
        self.assertEqual(max_q, guest_agent.GUEST_SCAN_MAX_QUERIES_MAX)

    def test_clamp_guest_scan_params_rejects_non_numeric(self):
        limit, per_batch, per_query, max_q = guest_agent.clamp_guest_scan_params(
            "not-a-number", None, "", object()
        )
        self.assertGreaterEqual(limit, 1)
        self.assertGreaterEqual(per_batch, 1)
        self.assertGreaterEqual(per_query, 1)
        self.assertGreaterEqual(max_q, 1)


class GuestJobOwnershipTests(GuestAccessTestCase):
    """6. Guest cannot access another visitor's saved report; a guest's own
    job stays visible only to that same guest session."""

    def test_guest_can_view_own_job_status(self):
        client = self.guest_client()
        client.get("/lead-bot")  # mint a guest_id cookie
        guest_id = client.cookies.get(guest_agent.GUEST_ID_COOKIE)

        job_id = self.write_fake_job(guest_id=guest_id)

        resp = client.get(f"/lead-bot/live-status/{job_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("job_id"), job_id)

    def test_guest_cannot_view_another_guests_job_status(self):
        other_client = self.guest_client()
        other_client.get("/lead-bot")
        other_guest_id = other_client.cookies.get(guest_agent.GUEST_ID_COOKIE)
        job_id = self.write_fake_job(guest_id=other_guest_id)

        attacker_client = self.guest_client()
        attacker_client.get("/lead-bot")
        resp = attacker_client.get(f"/lead-bot/live-status/{job_id}")

        self.assertEqual(resp.json().get("status"), "auth_required")

    def test_guest_with_no_cookie_at_all_cannot_view_any_job_status(self):
        job_id = self.write_fake_job(guest_id="some-other-guest")

        client = self.guest_client()  # never visited /lead-bot
        resp = client.get(f"/lead-bot/live-status/{job_id}")

        self.assertEqual(resp.json().get("status"), "auth_required")

    def test_guest_cannot_view_a_registered_users_job_status(self):
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")

        client = self.guest_client()
        client.get("/lead-bot")
        resp = client.get(f"/lead-bot/live-status/{job_id}")

        self.assertEqual(resp.json().get("status"), "auth_required")

    def test_logged_in_user_job_status_behavior_unchanged(self):
        """Registered users retain existing (unauthenticated-job-id-based)
        access to live-status -- this test pins that this guest change did
        not narrow or alter it."""
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")

        client = self.guest_client()
        self.login(client, "user2")  # a *different* logged-in user
        resp = client.get(f"/lead-bot/live-status/{job_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("job_id"), job_id)

    def test_guest_cannot_view_another_guests_live_page(self):
        other_client = self.guest_client()
        other_client.get("/lead-bot")
        other_guest_id = other_client.cookies.get(guest_agent.GUEST_ID_COOKIE)
        job_id = self.write_fake_job(guest_id=other_guest_id)

        attacker_client = self.guest_client()
        attacker_client.get("/lead-bot")
        resp = attacker_client.get(f"/lead-bot/live/{job_id}", follow_redirects=False)

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")

    def test_guest_can_view_own_live_page(self):
        client = self.guest_client()
        client.get("/lead-bot")
        guest_id = client.cookies.get(guest_agent.GUEST_ID_COOKIE)
        job_id = self.write_fake_job(guest_id=guest_id)

        resp = client.get(f"/lead-bot/live/{job_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("IS_GUEST = true", resp.text)
        self.assertIn("Want to save this report?", resp.text)

    def test_logged_in_user_live_page_is_not_marked_as_guest(self):
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")

        client = self.guest_client()
        self.login(client, "user1")
        resp = client.get(f"/lead-bot/live/{job_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("IS_GUEST = false", resp.text)


class GuestProtectedRouteTests(GuestAccessTestCase):
    """3, 4, 5. Guest cannot access history, settings, or admin-only
    functionality."""

    def test_guest_cannot_access_history(self):
        client = self.guest_client()
        resp = client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_guest_cannot_access_settings_admin_page(self):
        client = self.guest_client()
        resp = client.get("/settings", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_standard_user_still_blocked_from_admin_settings(self):
        """Confirms this change didn't accidentally loosen the existing
        admin-only boundary for regular logged-in users either."""
        client = self.guest_client()
        self.login(client, "user1")
        resp = client.get("/settings")
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_still_access_settings(self):
        client = self.guest_client()
        self.login(client, "theadmin")
        resp = client.get("/settings")
        self.assertEqual(resp.status_code, 200)

    def test_guest_cannot_access_leadbot_cards_route(self):
        client = self.guest_client()
        resp = client.get("/lead-bot/cards/whatever.csv", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")

    def test_guest_cannot_access_export_route(self):
        """7. Exports remain account-only; a guest is redirected to login
        rather than being able to download any export, their own or
        another user's."""
        client = self.guest_client()
        resp = client.get("/lead-bot/export/whatever.csv", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")

    def test_guest_cannot_access_live_dashboard_export_route(self):
        """7. Same account-only boundary for the "Open Desktop" dashboard
        redirect route."""
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")

        client = self.guest_client()
        resp = client.get(f"/lead-bot/live-dashboard/{job_id}", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")


class RegisteredUserUnchangedBehaviorTests(GuestAccessTestCase):
    """7. Registered users retain existing behavior on the routes this
    change touched."""

    def test_logged_in_user_live_start_unchanged(self):
        _token, csrf_token = self.login(self.client, "user1")

        with mock.patch.object(job_agent, "create_job", return_value="user-job-1") as mock_create:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={
                    "keyword": "plumber",
                    "market": "Albany, NY",
                    "limit": "50",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot/live/user-job-1")
        called_params = mock_create.call_args[0][0]
        # Unlike a guest, a logged-in user's own submitted limit passes
        # through unclamped by the guest ceiling (still subject to
        # run_job()'s existing 1-60 clamp, unchanged by this task).
        self.assertEqual(called_params["limit"], 50)
        self.assertNotIn("guest_id", called_params)

    def test_logged_in_user_live_start_still_requires_csrf(self):
        self.login(self.client, "user1")

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": "bad"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create.assert_not_called()

    def test_history_still_requires_login_for_registered_flow(self):
        resp = self.client.get("/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_logged_in_user_can_reach_history(self):
        self.login(self.client, "user1")
        resp = self.client.get("/history")
        self.assertEqual(resp.status_code, 200)


class LoginAndSignupStillWorkTests(GuestAccessTestCase):
    """9. Login and create-account still work."""

    def test_login_with_correct_password_succeeds(self):
        resp = self.client.post(
            "/login",
            data={"username": "user1", "password": VALID_PASSWORD},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")
        self.assertIn(appmain.AUTH_COOKIE_NAME, resp.cookies)

    def test_login_with_wrong_password_fails(self):
        resp = self.client.post(
            "/login",
            data={"username": "user1", "password": "wrong-password"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Invalid username or password", resp.text)

    def test_create_account_succeeds_for_new_user(self):
        resp = self.client.post(
            "/create-account",
            data={
                "username": "brandnewtester",
                "email": "brandnewtester@example.com",
                "password": VALID_PASSWORD,
                "confirm_password": VALID_PASSWORD,
                "website": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIsNotNone(auth_agent.get_user_by_username("brandnewtester"))


class PrivacyCopyRendersTests(GuestAccessTestCase):
    """10. Privacy text renders where expected."""

    def test_login_page_shows_privacy_note(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain._ACCOUNT_PRIVACY_NOTE, resp.text)

    def test_signup_page_shows_privacy_note(self):
        resp = self.client.get("/create-account")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain._ACCOUNT_PRIVACY_NOTE, resp.text)

    def test_privacy_note_does_not_overpromise_deletion(self):
        # The exact sentence from the task prompt must NOT appear verbatim,
        # since self-service account deletion is not implemented.
        resp = self.client.get("/login")
        self.assertNotIn(
            "You may request deletion of your account and associated test data at any time.",
            resp.text,
        )


if __name__ == "__main__":
    unittest.main()
