"""Focused direct-call tests for the /contact report form.

No real email is ever sent -- app.main._send_contact_report_email is mocked
at its module boundary in every test that reaches the send step. No real
database is opened; authenticated-user tests mock app.main.auth_current_user
directly instead of exercising the session/auth system.
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


def make_request(client_host="203.0.113.10", referer=None):
    headers = [(b"host", b"testserver")]
    if referer:
        headers.append((b"referer", referer.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/contact",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": (client_host, 12345),
        }
    )


class ContactFormTestCase(unittest.TestCase):
    def setUp(self):
        appmain._contact_rate_limit_attempts.clear()
        self.addCleanup(appmain._contact_rate_limit_attempts.clear)

    def submit(self, client_host="203.0.113.10", email="reporter@example.com",
               reason="Report a Problem", message="Something looks wrong.",
               page_url="", website=""):
        return appmain.contact_post(
            make_request(client_host),
            email=email,
            reason=reason,
            message=message,
            page_url=page_url,
            website=website,
        )


class ContactPageRenderTests(unittest.TestCase):
    def test_get_contact_renders_form(self):
        response = appmain.contact_get(make_request(referer=None))
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Contact LeadMeLeads", body)
        self.assertIn('name="email"', body)
        self.assertIn('name="reason"', body)
        self.assertIn('name="message"', body)
        self.assertIn('name="page_url"', body)
        self.assertIn('name="website"', body)

    def test_get_contact_prefills_page_url_from_same_origin_referer(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/contact",
                "headers": [
                    (b"host", b"testserver"),
                    (b"referer", b"https://testserver/lead-bot"),
                ],
                "query_string": b"",
                "scheme": "https",
                "server": ("testserver", 443),
                "client": ("203.0.113.10", 12345),
            }
        )
        response = appmain.contact_get(request)
        self.assertIn('value="/lead-bot"', response.body.decode())

    def test_get_contact_ignores_cross_origin_referer(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/contact",
                "headers": [
                    (b"host", b"testserver"),
                    (b"referer", b"https://evil.example/spam"),
                ],
                "query_string": b"",
                "scheme": "https",
                "server": ("testserver", 443),
                "client": ("203.0.113.10", 12345),
            }
        )
        response = appmain.contact_get(request)
        self.assertNotIn("evil.example", response.body.decode())


class ValidSubmissionTests(ContactFormTestCase):
    def test_valid_submission_sends_and_shows_success(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            response = self.submit()

        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Thank you &mdash; your report has been received. "
            "We&rsquo;ll look into the issue as soon as possible.",
            body,
        )
        self.assertIn('class="auth-success"', body)
        send_email.assert_called_once()
        to_email, subject, sent_body = send_email.call_args[0]
        self.assertEqual(to_email, "admin@leadmeleads.com")
        self.assertIn("Report a Problem", subject)
        self.assertIn("reporter@example.com", sent_body)
        self.assertIn("Something looks wrong.", sent_body)
        self.assertIn("Account: anonymous", sent_body)

    def test_optional_page_url_is_included_when_provided(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            self.submit(page_url="/lead-bot?x=1")

        sent_body = send_email.call_args[0][2]
        self.assertIn("/lead-bot?x=1", sent_body)

    def test_optional_page_url_absent_reports_not_provided(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            self.submit(page_url="")

        sent_body = send_email.call_args[0][2]
        self.assertIn("Page URL: Not provided", sent_body)

    def test_whitespace_in_email_and_message_is_trimmed(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            self.submit(email="  reporter@example.com  ", message="   Padded message.   ")

        sent_body = send_email.call_args[0][2]
        self.assertIn("Reporter email: reporter@example.com", sent_body)
        self.assertIn("Padded message.", sent_body)
        self.assertNotIn("  Padded message.", sent_body)


class RequiredFieldTests(ContactFormTestCase):
    def test_missing_message_is_rejected(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email:
            response = self.submit(message="   ")

        self.assertIn("Message is required.", response.body.decode())
        send_email.assert_not_called()

    def test_invalid_email_is_rejected(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email:
            response = self.submit(email="not-an-email")

        self.assertIn("Enter a valid email address.", response.body.decode())
        send_email.assert_not_called()

    def test_invalid_reason_is_rejected(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email:
            response = self.submit(reason="Not A Real Reason")

        self.assertIn("Please choose a valid reason.", response.body.decode())
        send_email.assert_not_called()

    def test_message_over_max_length_is_rejected(self):
        too_long = "x" * (appmain.CONTACT_MAX_MESSAGE_LENGTH + 1)
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email:
            response = self.submit(message=too_long)

        body = response.body.decode()
        self.assertNotIn("your report has been received", body)
        send_email.assert_not_called()


class HoneypotTests(ContactFormTestCase):
    def test_honeypot_submission_shows_success_but_does_not_send(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email:
            response = self.submit(website="http://spam-bot.example")

        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("your report has been received", body)
        send_email.assert_not_called()


class RateLimitTests(ContactFormTestCase):
    def test_repeated_submissions_are_rate_limited(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            for _ in range(appmain.CONTACT_RATE_LIMIT_MAX_ATTEMPTS):
                response = self.submit(client_host="198.51.100.5")
                self.assertNotIn(appmain.CONTACT_RATE_LIMIT_MESSAGE, response.body.decode())

            limited = self.submit(client_host="198.51.100.5")

        self.assertIn(appmain.CONTACT_RATE_LIMIT_MESSAGE, limited.body.decode())
        self.assertEqual(send_email.call_count, appmain.CONTACT_RATE_LIMIT_MAX_ATTEMPTS)

    def test_different_clients_have_independent_limits(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email"), \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            for _ in range(appmain.CONTACT_RATE_LIMIT_MAX_ATTEMPTS):
                self.submit(client_host="198.51.100.6")

            other_client_response = self.submit(client_host="198.51.100.7")

        self.assertNotIn(appmain.CONTACT_RATE_LIMIT_MESSAGE, other_client_response.body.decode())


class MailFailureTests(ContactFormTestCase):
    def test_send_failure_shows_error_and_preserves_message(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(
                 appmain, "_send_contact_report_email", side_effect=RuntimeError("smtp down")
             ) as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            response = self.submit(message="Please keep this message.")

        body = response.body.decode()
        self.assertNotIn("your report has been received", body)
        self.assertIn("could not be sent", body)
        self.assertIn("Please keep this message.", body)
        send_email.assert_called_once()

    def test_no_destination_configured_shows_error_not_success(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "", "SMTP_FROM": ""}):
            response = self.submit()

        body = response.body.decode()
        self.assertNotIn("your report has been received", body)
        self.assertIn("could not be sent", body)
        send_email.assert_not_called()


class AccountIdentityTests(ContactFormTestCase):
    def test_anonymous_submission_reports_anonymous(self):
        with mock.patch.object(appmain, "auth_current_user", return_value=None), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            self.submit()

        sent_body = send_email.call_args[0][2]
        self.assertIn("Account: anonymous", sent_body)
        self.assertNotIn("user_id", sent_body)

    def test_authenticated_submission_reports_account_id_and_email(self):
        user = {"id": 42, "username": "alice", "role": "standard"}
        account = {"id": 42, "username": "alice", "email": "alice@example.com", "role": "standard"}
        with mock.patch.object(appmain, "auth_current_user", return_value=user), \
             mock.patch("agents.auth_agent.get_user_by_id_for_admin", return_value=account), \
             mock.patch.object(appmain, "_send_contact_report_email") as send_email, \
             mock.patch.dict(os.environ, {"CONTACT_REPORT_EMAIL": "admin@leadmeleads.com"}):
            self.submit()

        sent_body = send_email.call_args[0][2]
        self.assertIn("Account: authenticated", sent_body)
        self.assertIn("user_id=42", sent_body)
        self.assertIn("email=alice@example.com", sent_body)
        # No password, cookie, session, or CSRF material should ever appear.
        self.assertNotIn("password", sent_body.lower())
        self.assertNotIn("cookie", sent_body.lower())
        self.assertNotIn("csrf", sent_body.lower())
        self.assertNotIn("session", sent_body.lower())


if __name__ == "__main__":
    unittest.main()
