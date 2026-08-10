"""Focused direct-call tests for password-reset email transport.

Confirms _send_reset_email() now delivers over the Resend HTTPS API (not
SMTP -- production cannot reach outbound SMTP ports at all) and that a
stalled/failed Resend call still surfaces as an ordinary exception, exactly
as the existing forgot-password/admin-reset callers already expect. No
change is made to those callers or to any other auth/session behavior; only
the mail-delivery function itself.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.auth_agent as auth_agent
from agents.email_agent import EmailSendError


RESEND_ENV = {
    "RESEND_API_KEY": "test-key",
    "SMTP_FROM": "no-reply@leadmeleads.com",
}


def make_request(client_host="203.0.113.60"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/forgot-password",
            "headers": [(b"host", b"testserver")],
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": (client_host, 12345),
        }
    )


class SendResetEmailTransportTests(unittest.TestCase):
    def test_successful_reset_send_calls_resend_not_smtp(self):
        fake_response = mock.Mock(status_code=200)
        fake_response.json.return_value = {"id": "email_456"}
        with mock.patch("requests.post", return_value=fake_response) as post, \
             mock.patch("smtplib.SMTP") as smtp_cls, \
             mock.patch.dict(os.environ, RESEND_ENV):
            appmain._send_reset_email(
                "alice@example.com",
                "https://leadmeleads.com/reset-password?token=abc",
            )

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.resend.com/emails")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["json"]["to"], ["alice@example.com"])
        self.assertIn("reset-password?token=abc", kwargs["json"]["text"])
        smtp_cls.assert_not_called()

    def test_timed_out_reset_send_raises_and_touches_no_smtp(self):
        with mock.patch(
            "requests.post",
            side_effect=requests.exceptions.Timeout("timed out"),
        ) as post, mock.patch("smtplib.SMTP") as smtp_cls, \
             mock.patch.dict(os.environ, RESEND_ENV):
            with self.assertRaises(EmailSendError):
                appmain._send_reset_email(
                    "alice@example.com",
                    "https://leadmeleads.com/reset-password?token=abc",
                )

        post.assert_called_once()
        smtp_cls.assert_not_called()


class ForgotPasswordFlowStillHandlesFailureTests(unittest.TestCase):
    """Callers already wrap _send_reset_email in try/except; a Resend
    failure must keep behaving the same way -- no hang, same generic
    response -- as any other mail failure did before this transport swap.
    """

    def setUp(self):
        appmain._forgot_password_rate_limit_attempts.clear()
        self.addCleanup(appmain._forgot_password_rate_limit_attempts.clear)

    def test_forgot_password_swallows_resend_timeout_like_any_other_failure(self):
        user = {"id": 9, "username": "bob", "email": "bob@example.com"}
        with mock.patch.object(
            auth_agent, "create_reset_token", return_value=("raw-token", user)
        ), mock.patch.object(appmain, "_show_dev_reset_link", return_value=False), \
             mock.patch.object(appmain, "_is_dev_mode", return_value=False), \
             mock.patch(
                 "requests.post",
                 side_effect=requests.exceptions.Timeout("timed out"),
             ) as post, \
             mock.patch("smtplib.SMTP") as smtp_cls, \
             mock.patch.dict(os.environ, RESEND_ENV):
            response = appmain.forgot_password_post(
                make_request(), identifier="bob@example.com"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "If an account exists for that username or email",
            response.body.decode(),
        )
        post.assert_called_once()
        smtp_cls.assert_not_called()

    def test_forgot_password_succeeds_via_resend(self):
        user = {"id": 9, "username": "bob", "email": "bob@example.com"}
        fake_response = mock.Mock(status_code=200)
        fake_response.json.return_value = {"id": "email_789"}
        with mock.patch.object(
            auth_agent, "create_reset_token", return_value=("raw-token", user)
        ), mock.patch.object(appmain, "_show_dev_reset_link", return_value=False), \
             mock.patch.object(appmain, "_is_dev_mode", return_value=False), \
             mock.patch("requests.post", return_value=fake_response) as post, \
             mock.patch("smtplib.SMTP") as smtp_cls, \
             mock.patch.dict(os.environ, RESEND_ENV):
            response = appmain.forgot_password_post(
                make_request(), identifier="bob@example.com"
            )

        self.assertEqual(response.status_code, 200)
        post.assert_called_once()
        smtp_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
