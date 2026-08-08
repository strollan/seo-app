"""Unit tests for the provider-neutral SMTP transport in agents/email_agent.py.

No network calls are made -- smtplib.SMTP is mocked at the module boundary.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.email_agent as email_agent


class IsConfiguredTests(unittest.TestCase):
    def test_blank_host_is_not_configured(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": ""}, clear=False):
            self.assertFalse(email_agent.is_configured())

    def test_missing_host_is_not_configured(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SMTP_HOST", None)
            self.assertFalse(email_agent.is_configured())

    def test_known_placeholder_host_is_not_configured(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": "smtp.example.com"}, clear=False):
            self.assertFalse(email_agent.is_configured())

    def test_real_host_is_configured(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": "smtp.resend.com"}, clear=False):
            self.assertTrue(email_agent.is_configured())


class SendEmailTests(unittest.TestCase):
    def test_send_email_uses_starttls_login_and_sendmail(self):
        env = {
            "SMTP_HOST": "smtp.resend.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "resend",
            "SMTP_PASSWORD": "test-api-key",
            "SMTP_FROM": "noreply@leadmeleads.com",
        }
        with mock.patch.dict("os.environ", env, clear=False), mock.patch.object(
            email_agent.smtplib, "SMTP"
        ) as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value

            email_agent.send_email("user@example.com", "Subject line", "Body text")

            smtp_cls.assert_called_once_with("smtp.resend.com", 587)
            server.starttls.assert_called_once()
            server.login.assert_called_once_with("resend", "test-api-key")
            self.assertEqual(server.sendmail.call_count, 1)
            from_addr, to_addr, raw_message = server.sendmail.call_args.args
            self.assertEqual(from_addr, "noreply@leadmeleads.com")
            self.assertEqual(to_addr, "user@example.com")
            self.assertIn("Subject: Subject line", raw_message)
            self.assertIn("Body text", raw_message)

    def test_send_email_skips_login_when_no_credentials(self):
        env = {
            "SMTP_HOST": "localhost",
            "SMTP_PORT": "1025",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "noreply@leadmeleads.com",
        }
        with mock.patch.dict("os.environ", env, clear=False), mock.patch.object(
            email_agent.smtplib, "SMTP"
        ) as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value

            email_agent.send_email("user@example.com", "Subject", "Body")

            server.login.assert_not_called()
            server.sendmail.assert_called_once()


if __name__ == "__main__":
    unittest.main()
