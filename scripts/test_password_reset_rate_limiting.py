"""Focused direct-call tests for password-recovery rate limiting.

No TestClient is used, no real email is sent, and no production database is
opened. Authentication helpers and email delivery are mocked at their module
boundaries.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.auth_agent as auth_agent


VALID_PASSWORD = "correct-horse-battery-staple"


def make_request(client_host="203.0.113.10", path="/forgot-password"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"host", b"testserver")],
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": (client_host, 12345),
        }
    )


class PasswordResetRateLimitTests(unittest.TestCase):
    def setUp(self):
        appmain._forgot_password_rate_limit_attempts.clear()
        appmain._reset_password_rate_limit_attempts.clear()
        self.addCleanup(appmain._forgot_password_rate_limit_attempts.clear)
        self.addCleanup(appmain._reset_password_rate_limit_attempts.clear)

    def test_normal_forgot_password_request_is_allowed_and_email_is_mocked(self):
        user = {"id": 7, "username": "alice", "email": "alice@example.com"}
        with mock.patch.object(
            auth_agent, "create_reset_token", return_value=("raw-test-token", user)
        ), mock.patch.object(appmain, "_show_dev_reset_link", return_value=False), mock.patch.object(
            appmain, "_is_dev_mode", return_value=False
        ), mock.patch.object(appmain, "_send_reset_email") as send_email:
            response = appmain.forgot_password_post(
                make_request(), identifier="alice@example.com"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "If an account exists for that username or email",
            response.body.decode(),
        )
        send_email.assert_called_once()

    def test_repeated_forgot_abuse_is_limited_before_token_or_email_work(self):
        user = {"id": 7, "username": "alice", "email": "alice@example.com"}
        with mock.patch.object(
            auth_agent, "create_reset_token", return_value=("raw-test-token", user)
        ) as create_token, mock.patch.object(
            appmain, "_show_dev_reset_link", return_value=False
        ), mock.patch.object(appmain, "_is_dev_mode", return_value=False), mock.patch.object(
            appmain, "_send_reset_email"
        ) as send_email:
            for _ in range(appmain.FORGOT_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS):
                allowed = appmain.forgot_password_post(
                    make_request(), identifier="alice@example.com"
                )
                self.assertNotIn(appmain.PASSWORD_RESET_RATE_LIMIT_MESSAGE, allowed.body.decode())

            limited = appmain.forgot_password_post(
                make_request(), identifier="alice@example.com"
            )

        self.assertIn(appmain.PASSWORD_RESET_RATE_LIMIT_MESSAGE, limited.body.decode())
        self.assertEqual(create_token.call_count, appmain.FORGOT_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS)
        self.assertEqual(send_email.call_count, appmain.FORGOT_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS)

    def test_existing_and_nonexistent_accounts_keep_same_generic_response(self):
        existing = {"id": 7, "username": "alice", "email": ""}
        with mock.patch.object(
            auth_agent,
            "create_reset_token",
            side_effect=[("token", existing), ("", None)],
        ), mock.patch.object(appmain, "_show_dev_reset_link", return_value=False), mock.patch.object(
            appmain, "_is_dev_mode", return_value=False
        ), mock.patch.object(appmain, "_send_reset_email"):
            existing_response = appmain.forgot_password_post(
                make_request("203.0.113.11"), identifier="alice"
            )
            missing_response = appmain.forgot_password_post(
                make_request("203.0.113.12"), identifier="missing"
            )

        generic = "If an account exists for that username or email, a reset link has been sent."
        self.assertIn(generic, existing_response.body.decode())
        self.assertIn(generic, missing_response.body.decode())
        self.assertNotIn("alice", existing_response.body.decode().lower())
        self.assertNotIn("missing", missing_response.body.decode().lower())

    def test_normal_reset_submission_consumes_token_after_success(self):
        user = {"id": 7, "username": "alice"}
        with mock.patch.object(
            auth_agent, "get_user_for_reset_token", return_value=user
        ), mock.patch.object(auth_agent, "set_user_password") as set_password, mock.patch.object(
            auth_agent, "consume_reset_token"
        ) as consume, mock.patch.object(
            auth_agent, "delete_all_sessions_for_user"
        ) as delete_sessions, mock.patch.object(
            appmain, "_show_dev_reset_link", return_value=False
        ):
            response = appmain.reset_password_post(
                make_request(path="/reset-password"),
                token="raw-test-token",
                password=VALID_PASSWORD,
                confirm_password=VALID_PASSWORD,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?reset=1")
        set_password.assert_called_once_with(7, VALID_PASSWORD)
        consume.assert_called_once_with("raw-test-token")
        delete_sessions.assert_called_once_with(7)

    def test_repeated_reset_attempts_are_limited_before_token_lookup(self):
        with mock.patch.object(
            auth_agent, "get_user_for_reset_token", return_value=None
        ) as lookup:
            for _ in range(appmain.RESET_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS):
                appmain.reset_password_post(
                    make_request(path="/reset-password"),
                    token="invalid",
                    password=VALID_PASSWORD,
                    confirm_password=VALID_PASSWORD,
                )
            limited = appmain.reset_password_post(
                make_request(path="/reset-password"),
                token="invalid",
                password=VALID_PASSWORD,
                confirm_password=VALID_PASSWORD,
            )

        self.assertIn(appmain.PASSWORD_RESET_RATE_LIMIT_MESSAGE, limited.body.decode())
        self.assertEqual(lookup.call_count, appmain.RESET_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS)

    def test_rate_limit_rejection_does_not_consume_valid_token(self):
        blocked_ip = "203.0.113.20"
        other_ip = "203.0.113.21"
        key = appmain._password_reset_rate_limit_key(blocked_ip)
        appmain._reset_password_rate_limit_attempts[key] = [
            appmain.time.time()
        ] * appmain.RESET_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS
        user = {"id": 7, "username": "alice"}

        with mock.patch.object(
            auth_agent, "get_user_for_reset_token", return_value=user
        ) as lookup, mock.patch.object(auth_agent, "set_user_password"), mock.patch.object(
            auth_agent, "consume_reset_token"
        ) as consume, mock.patch.object(
            auth_agent, "delete_all_sessions_for_user"
        ), mock.patch.object(appmain, "_show_dev_reset_link", return_value=False):
            limited = appmain.reset_password_post(
                make_request(blocked_ip, "/reset-password"),
                token="valid-token",
                password=VALID_PASSWORD,
                confirm_password=VALID_PASSWORD,
            )
            allowed = appmain.reset_password_post(
                make_request(other_ip, "/reset-password"),
                token="valid-token",
                password=VALID_PASSWORD,
                confirm_password=VALID_PASSWORD,
            )

        self.assertIn(appmain.PASSWORD_RESET_RATE_LIMIT_MESSAGE, limited.body.decode())
        self.assertEqual(allowed.status_code, 303)
        lookup.assert_called_once_with("valid-token")
        consume.assert_called_once_with("valid-token")

    def test_existing_login_and_signup_limits_are_unchanged_and_scopes_independent(self):
        self.assertEqual(auth_agent.LOGIN_MAX_ATTEMPTS, 5)
        self.assertEqual(auth_agent.LOGIN_WINDOW_SECONDS, 10 * 60)
        self.assertEqual(appmain.SIGNUP_RATE_LIMIT_MAX_ATTEMPTS, 5)
        self.assertEqual(appmain.SIGNUP_RATE_LIMIT_WINDOW_SECONDS, 10 * 60)

        for _ in range(appmain.FORGOT_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS):
            appmain.password_reset_record_attempt("forgot", "203.0.113.30")
        self.assertTrue(
            appmain.password_reset_is_rate_limited("forgot", "203.0.113.30")
        )
        self.assertFalse(
            appmain.password_reset_is_rate_limited("reset", "203.0.113.30")
        )

    def test_limiter_response_and_logs_contain_no_identifiers_or_tokens(self):
        client_host = "203.0.113.40"
        key = appmain._password_reset_rate_limit_key(client_host)
        appmain._forgot_password_rate_limit_attempts[key] = [
            appmain.time.time()
        ] * appmain.FORGOT_PASSWORD_RATE_LIMIT_MAX_ATTEMPTS
        with mock.patch("builtins.print") as printed, mock.patch.object(
            auth_agent, "create_reset_token"
        ) as create_token, mock.patch.object(appmain, "_send_reset_email") as send_email:
            response = appmain.forgot_password_post(
                make_request(client_host), identifier="secret-user@example.com"
            )

        body = response.body.decode()
        self.assertNotIn("secret-user@example.com", body)
        self.assertNotIn(client_host, body)
        printed.assert_not_called()
        create_token.assert_not_called()
        send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
