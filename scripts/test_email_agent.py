"""Focused direct-call tests for agents/email_agent.py (Resend HTTPS transport).

No real email is ever sent -- requests.post is mocked at its module
boundary in every test that reaches the network call.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.email_agent import EmailSendError, is_configured, send_email


CONFIGURED_ENV = {
    "RESEND_API_KEY": "test-key",
    "SMTP_FROM": "no-reply@leadmeleads.com",
}


class IsConfiguredTests(unittest.TestCase):
    def test_true_when_api_key_set(self):
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "abc"}):
            self.assertTrue(is_configured())

    def test_false_when_api_key_blank_or_unset(self):
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": ""}):
            self.assertFalse(is_configured())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESEND_API_KEY", None)
            self.assertFalse(is_configured())


class SendEmailSuccessTests(unittest.TestCase):
    def test_successful_send_posts_expected_request_and_returns(self):
        fake_response = mock.Mock(status_code=200)
        fake_response.json.return_value = {"id": "email_abc123"}
        with mock.patch("requests.post", return_value=fake_response) as post, \
             mock.patch.dict(os.environ, CONFIGURED_ENV):
            send_email("to@example.com", "Subject line", "Body text")

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.resend.com/emails")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-key"})
        self.assertEqual(
            kwargs["json"],
            {
                "from": "no-reply@leadmeleads.com",
                "to": ["to@example.com"],
                "subject": "Subject line",
                "text": "Body text",
            },
        )
        self.assertEqual(kwargs["timeout"], 10)


class SendEmailFailureTests(unittest.TestCase):
    def test_missing_api_key_raises_without_any_network_call(self):
        with mock.patch("requests.post") as post, \
             mock.patch.dict(os.environ, {"RESEND_API_KEY": "", "SMTP_FROM": "a@b.com"}):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")
        post.assert_not_called()

    def test_missing_from_address_raises_without_any_network_call(self):
        with mock.patch("requests.post") as post, \
             mock.patch.dict(os.environ, {"RESEND_API_KEY": "key", "SMTP_FROM": ""}):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")
        post.assert_not_called()

    def test_timeout_raises_email_send_error(self):
        with mock.patch(
            "requests.post", side_effect=requests.exceptions.Timeout("timed out")
        ) as post, mock.patch.dict(os.environ, CONFIGURED_ENV):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")
        post.assert_called_once()

    def test_connection_error_raises_email_send_error(self):
        with mock.patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ), mock.patch.dict(os.environ, CONFIGURED_ENV):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")

    def test_non_2xx_response_raises_email_send_error(self):
        fake_response = mock.Mock(status_code=422, text="Invalid `to` field")
        with mock.patch("requests.post", return_value=fake_response), \
             mock.patch.dict(os.environ, CONFIGURED_ENV):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")

    def test_response_without_id_is_not_treated_as_success(self):
        fake_response = mock.Mock(status_code=200)
        fake_response.json.return_value = {"message": "queued, maybe"}
        with mock.patch("requests.post", return_value=fake_response), \
             mock.patch.dict(os.environ, CONFIGURED_ENV):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")

    def test_non_json_response_raises_email_send_error(self):
        fake_response = mock.Mock(status_code=200)
        fake_response.json.side_effect = ValueError("not json")
        with mock.patch("requests.post", return_value=fake_response), \
             mock.patch.dict(os.environ, CONFIGURED_ENV):
            with self.assertRaises(EmailSendError):
                send_email("to@example.com", "subject", "body")


if __name__ == "__main__":
    unittest.main()
