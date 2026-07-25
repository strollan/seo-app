"""Direct-route tests for minimal admin user management.

Uses a temporary auth database and mocked SMTP. No TestClient, production
database, production account, or real email is involved.
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.auth_agent as auth


PASSWORD = "correct-horse-battery-staple"


def request(session_token="", path="/admin/users"):
    headers = [(b"host", b"testserver")]
    if session_token:
        headers.append(
            (b"cookie", f"{appmain.AUTH_COOKIE_NAME}={session_token}".encode())
        )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("203.0.113.10", 12345),
        }
    )


class AdminUserManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = mock.patch.object(
            auth, "AUTH_DB", Path(self.temp.name) / "auth.db"
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        auth.init_auth_db()

        auth.create_user("admin", PASSWORD, role="admin", email="admin@example.com")
        auth.create_user("member", PASSWORD, role="standard", email="member@example.com")
        self.admin = auth.get_user_by_username("admin")
        self.member = auth.get_user_by_username("member")
        self.admin_session = auth.create_session(self.admin)
        self.member_session = auth.create_session(self.member)

    def admin_request_and_csrf(self, path="/admin/users"):
        req = request(self.admin_session, path)
        return req, auth.issue_csrf_token(self.admin_session)

    def test_mutations_are_post_only_and_accept_no_identity_fields(self):
        paths = {
            "/admin/users/{user_id}/toggle-active",
            "/admin/users/{user_id}/send-reset",
        }
        routes = {
            route.path: route.methods
            for route in appmain.app.routes
            if getattr(route, "path", None) in paths
        }
        self.assertEqual(set(routes), paths)
        self.assertEqual(routes["/admin/users/{user_id}/toggle-active"], {"POST"})
        self.assertEqual(routes["/admin/users/{user_id}/send-reset"], {"POST"})
        for function in (
            appmain.admin_toggle_user_active,
            appmain.admin_send_user_reset,
        ):
            names = set(__import__("inspect").signature(function).parameters)
            self.assertNotIn("role", names)
            self.assertNotIn("username", names)
            self.assertNotIn("email", names)
            self.assertNotIn("is_active", names)

    def test_authorization_for_list_and_both_actions(self):
        anonymous = request()
        standard = request(self.member_session)
        self.assertEqual(appmain.admin_users_page(anonymous).status_code, 303)
        self.assertEqual(appmain.admin_users_page(standard).status_code, 403)
        self.assertEqual(
            appmain.admin_toggle_user_active(self.member["id"], anonymous).status_code,
            303,
        )
        self.assertEqual(
            appmain.admin_toggle_user_active(self.member["id"], standard).status_code,
            403,
        )
        self.assertEqual(
            appmain.admin_send_user_reset(self.member["id"], anonymous).status_code,
            303,
        )
        self.assertEqual(
            appmain.admin_send_user_reset(self.member["id"], standard).status_code,
            403,
        )
        self.assertEqual(
            appmain.admin_users_page(request(self.admin_session)).status_code,
            200,
        )

    def test_list_renders_safe_fields_and_mobile_guards_only(self):
        raw_session = self.member_session
        _raw_reset, _user = auth.create_reset_token("member")
        with contextlib.closing(auth.connect()) as conn:
            stored = conn.execute(
                """
                SELECT users.password_hash, sessions.token_hash,
                       password_reset_tokens.token_hash AS reset_hash
                FROM users
                JOIN sessions ON sessions.user_id = users.id
                JOIN password_reset_tokens ON password_reset_tokens.user_id = users.id
                WHERE users.id = ?
                """,
                (self.member["id"],),
            ).fetchone()

        response = appmain.admin_users_page(request(self.admin_session))
        body = response.body.decode()
        self.assertIn("member", body)
        self.assertIn("member@example.com", body)
        self.assertIn("Standard", body)
        self.assertIn("Active", body)
        self.assertIn(str(self.member["created_at"]), body)
        self.assertNotIn(stored["password_hash"], body)
        self.assertNotIn(stored["token_hash"], body)
        self.assertNotIn(stored["reset_hash"], body)
        self.assertNotIn(raw_session, body)
        self.assertNotIn("password_hash", body)
        self.assertNotIn("reset_hash", body)
        self.assertIn("@media(max-width:420px)", body)
        self.assertIn("overflow-wrap:anywhere", body)
        self.assertIn("width:100%", body)

    def test_csrf_required_for_toggle_and_reset(self):
        req = request(self.admin_session)
        toggle = appmain.admin_toggle_user_active(self.member["id"], req)
        reset = appmain.admin_send_user_reset(self.member["id"], req)
        self.assertEqual(toggle.status_code, 403)
        self.assertEqual(reset.status_code, 403)
        self.assertIsNotNone(auth.get_user_by_username("member"))

    def test_disable_removes_sessions_and_invalidates_auth_and_csrf(self):
        member_csrf = auth.issue_csrf_token(self.member_session)
        req, csrf = self.admin_request_and_csrf()
        with mock.patch.object(
            auth,
            "delete_all_sessions_for_user",
            wraps=auth.delete_all_sessions_for_user,
        ) as delete_sessions:
            response = appmain.admin_toggle_user_active(
                self.member["id"], req, csrf_token=csrf
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("result=disabled", response.headers["location"])
        delete_sessions.assert_called_once_with(self.member["id"])
        self.assertIsNone(auth.authenticate_user("member", PASSWORD))
        self.assertIsNone(auth.get_user_from_token(self.member_session))
        self.assertFalse(auth.verify_csrf_token(self.member_session, member_csrf))
        with contextlib.closing(auth.connect()) as conn:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                (self.member["id"],),
            ).fetchone()[0]
        self.assertEqual(session_count, 0)

    def test_reenable_restores_authentication_without_creating_session(self):
        req, csrf = self.admin_request_and_csrf()
        appmain.admin_toggle_user_active(self.member["id"], req, csrf_token=csrf)
        req, csrf = self.admin_request_and_csrf()
        response = appmain.admin_toggle_user_active(
            self.member["id"], req, csrf_token=csrf
        )
        self.assertIn("result=enabled", response.headers["location"])
        self.assertIsNotNone(auth.authenticate_user("member", PASSWORD))
        with contextlib.closing(auth.connect()) as conn:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                (self.member["id"],),
            ).fetchone()[0]
        self.assertEqual(session_count, 0)

    def test_admin_cannot_disable_self(self):
        req, csrf = self.admin_request_and_csrf()
        response = appmain.admin_toggle_user_active(
            self.admin["id"], req, csrf_token=csrf
        )
        self.assertIn("error=self_disable", response.headers["location"])
        self.assertIsNotNone(auth.get_user_by_username("admin"))

    def test_last_active_admin_guard_is_server_authoritative(self):
        fake_actor = {"id": 999, "username": "service-admin", "role": "admin"}
        with mock.patch.object(appmain, "auth_current_user", return_value=fake_actor), mock.patch.object(
            appmain, "_csrf_token_valid", return_value=True
        ):
            response = appmain.admin_toggle_user_active(
                self.admin["id"], request(), csrf_token="valid"
            )
        self.assertIn("error=last_admin", response.headers["location"])
        self.assertIsNotNone(auth.get_user_by_username("admin"))

    def test_one_admin_can_disable_another_when_two_are_active(self):
        auth.create_user("admin2", PASSWORD, role="admin", email="admin2@example.com")
        admin2 = auth.get_user_by_username("admin2")
        req, csrf = self.admin_request_and_csrf()
        response = appmain.admin_toggle_user_active(
            admin2["id"], req, csrf_token=csrf
        )
        self.assertIn("result=disabled", response.headers["location"])
        self.assertIsNone(auth.get_user_by_username("admin2"))
        self.assertEqual(auth.count_active_admins(), 1)

    def test_admin_reset_uses_existing_token_pipeline_and_mocked_email(self):
        prior_token, _ = auth.create_reset_token("member")
        req, csrf = self.admin_request_and_csrf()
        with mock.patch.object(appmain, "_send_reset_email") as send_email:
            response = appmain.admin_send_user_reset(
                self.member["id"], req, csrf_token=csrf
            )

        self.assertIn("result=reset_sent", response.headers["location"])
        send_email.assert_called_once()
        recipient, reset_url = send_email.call_args.args
        self.assertEqual(recipient, "member@example.com")
        new_token = parse_qs(urlparse(reset_url).query)["token"][0]
        self.assertIsNone(auth.get_user_for_reset_token(prior_token))
        self.assertIsNotNone(auth.get_user_for_reset_token(new_token))
        self.assertNotIn(new_token, response.body.decode())
        self.assertNotIn(reset_url, response.body.decode())

        with contextlib.closing(auth.connect()) as conn:
            row = conn.execute(
                """
                SELECT created_at, expires_at FROM password_reset_tokens
                WHERE token_hash = ?
                """,
                (auth.hash_token(new_token),),
            ).fetchone()
        lifetime = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(
            row["created_at"]
        )
        self.assertEqual(int(lifetime.total_seconds()), 60 * 60)

    def test_smtp_failure_is_safe_and_returns_no_sensitive_detail(self):
        req, csrf = self.admin_request_and_csrf()
        output = io.StringIO()
        with mock.patch.object(
            appmain,
            "_send_reset_email",
            side_effect=RuntimeError("smtp-password=secret-value"),
        ), contextlib.redirect_stdout(output):
            response = appmain.admin_send_user_reset(
                self.member["id"], req, csrf_token=csrf
            )
        self.assertIn("error=reset_failed", response.headers["location"])
        self.assertNotIn("secret-value", response.body.decode())
        self.assertNotIn("secret-value", output.getvalue())
        self.assertEqual(output.getvalue().strip(), "ADMIN USER RESET EMAIL ERROR")


if __name__ == "__main__":
    unittest.main()
