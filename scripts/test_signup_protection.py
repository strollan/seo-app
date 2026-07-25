"""
Regression tests for V1 registration protection on /signup and
/create-account (app.main.signup_post / create_account_post).

Covers: case-insensitive duplicate username/email rejection with a single
generic message, conservative email format validation, the hidden honeypot
field, in-process signup rate limiting, and successful legitimate signup.

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file for every
test, so the real data/app_auth.db is never opened or written to. The
in-process signup rate-limit counters are cleared before each test so tests
don't bleed into each other.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# agents/seo_agent.py raises RuntimeError at import time if OPENAI_API_KEY
# isn't set, and app.main imports it transitively. This suite never talks to
# OpenAI, so a placeholder key (not a real secret) is enough to let
# `import app.main` succeed in environments without a configured .env.
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent


VALID_PASSWORD = "correct-horse-battery-staple"


class SignupProtectionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        appmain._signup_rate_limit_attempts.clear()
        self.addCleanup(appmain._signup_rate_limit_attempts.clear)

        self.client = TestClient(appmain.app)

    def signup(self, username, email, password=VALID_PASSWORD, confirm=None, website=""):
        return self.client.post(
            "/signup",
            data={
                "username": username,
                "email": email,
                "password": password,
                "confirm_password": confirm if confirm is not None else password,
                "website": website,
            },
            follow_redirects=False,
        )


class SuccessfulSignupTests(SignupProtectionTestCase):
    def test_successful_signup_redirects_to_login_and_creates_user(self):
        resp = self.signup("newuser", "newuser@example.com")

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

        user = auth_agent.get_user_by_username("newuser")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "standard")
        self.assertEqual(user["email"], "newuser@example.com")

    def test_username_and_email_are_normalized_before_storage(self):
        resp = self.signup("  MixedCase  ", "  Mixed.Case@Example.COM  ")

        self.assertEqual(resp.status_code, 303)

        user = auth_agent.get_user_by_username("mixedcase")
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "mixedcase")
        self.assertEqual(user["email"], "mixed.case@example.com")


class DuplicateAccountTests(SignupProtectionTestCase):
    def setUp(self):
        super().setUp()
        auth_agent.create_user("alice", VALID_PASSWORD, role="standard", email="alice@example.com")

    def test_duplicate_username_case_insensitive_is_rejected(self):
        resp = self.signup("ALICE", "someone-else@example.com")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, resp.text)
        self.assertIsNone(auth_agent.get_user_by_email("someone-else@example.com"))

    def test_duplicate_email_case_insensitive_is_rejected(self):
        resp = self.signup("someone-else", "ALICE@EXAMPLE.COM")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, resp.text)
        self.assertIsNone(auth_agent.get_user_by_username("someone-else"))

    def test_duplicate_message_does_not_reveal_which_field_collided(self):
        username_collision = self.signup("ALICE", "unique1@example.com")
        email_collision = self.signup("unique2", "ALICE@EXAMPLE.COM")

        # Same generic message either way -- an attacker probing for taken
        # usernames/emails can't tell which one already existed.
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, username_collision.text)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, email_collision.text)

    def test_duplicate_username_rejected_via_create_account_route(self):
        resp = self.client.post(
            "/create-account",
            data={
                "username": "ALICE",
                "email": "someone-else@example.com",
                "password": VALID_PASSWORD,
                "confirm_password": VALID_PASSWORD,
                "website": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, resp.text)

    def test_legacy_mixed_case_username_is_still_protected(self):
        # Simulate a pre-existing row stored with mixed case (e.g. created
        # before normalization was enforced everywhere) rather than through
        # create_user(), which already lowercases.
        with auth_agent.managed_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at, email) "
                "VALUES (?, ?, 'standard', 1, ?, ?)",
                ("LegacyUser", auth_agent.hash_password(VALID_PASSWORD), auth_agent.iso(auth_agent.utc_now()), None),
            )
            conn.commit()

        resp = self.signup("legacyuser", "brandnew@example.com")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, resp.text)

    def test_inactive_account_email_is_still_protected(self):
        with auth_agent.managed_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at, email) "
                "VALUES (?, ?, 'standard', 0, ?, ?)",
                ("deactivated", auth_agent.hash_password(VALID_PASSWORD), auth_agent.iso(auth_agent.utc_now()), "disabled@example.com"),
            )
            conn.commit()

        resp = self.signup("brandnewuser", "DISABLED@EXAMPLE.COM")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(appmain.SIGNUP_DUPLICATE_MESSAGE, resp.text)
        self.assertIsNone(auth_agent.get_user_by_username("brandnewuser"))

    def test_concurrent_duplicate_email_insert_is_rejected_at_db_layer(self):
        # Bypass the app-level email_exists() pre-check to simulate two
        # requests racing past it concurrently -- the DB-level unique index
        # on LOWER(email) is what must stop the second insert.
        auth_agent.create_user("first", VALID_PASSWORD, role="standard", email="race@example.com")

        with self.assertRaises(Exception):
            auth_agent.create_user("second", VALID_PASSWORD, role="standard", email="RACE@EXAMPLE.COM")

        with auth_agent.managed_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE LOWER(email) = ?", ("race@example.com",)
            ).fetchone()[0]
        self.assertEqual(count, 1)


class MalformedEmailTests(SignupProtectionTestCase):
    def test_malformed_emails_are_rejected(self):
        malformed_emails = [
            "not-an-email",
            "foo@bar",
            "foo@.com",
            "@example.com",
            "foo bar@example.com",
            "foo@exa mple.com",
            "foo@@example.com",
            ".user@example.com",
            "user.@example.com",
            "user..name@example.com",
            "..@example.com",
        ]

        for index, email in enumerate(malformed_emails):
            with self.subTest(email=email):
                # Each malformed-address case is independent. Clear only
                # in-memory signup throttling state so earlier cases do not
                # mask email-validation results with a rate-limit response.
                for _name, _value in vars(appmain).items():
                    _lower = _name.lower()
                    if (
                        "signup" in _lower
                        and any(word in _lower for word in ("attempt", "rate", "limit"))
                        and hasattr(_value, "clear")
                    ):
                        _value.clear()
                resp = self.signup(f"user{index}", email)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("Use a valid email address.", resp.text)
                self.assertIsNone(auth_agent.get_user_by_username(f"user{index}"))


class OverlengthInputTests(SignupProtectionTestCase):
    def test_overlength_username_is_rejected_not_truncated(self):
        long_username = "a" * (appmain.SIGNUP_MAX_USERNAME_LENGTH + 1)

        resp = self.signup(long_username, "overlength@example.com")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("too long", resp.text)
        self.assertIsNone(auth_agent.get_user_by_username(long_username[:150]))

    def test_overlength_email_is_rejected_not_truncated(self):
        long_local_part = "a" * 250
        long_email = f"{long_local_part}@example.com"
        self.assertGreater(len(long_email), appmain.SIGNUP_MAX_EMAIL_LENGTH)

        resp = self.signup("overlengthemailuser", long_email)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("too long", resp.text)
        self.assertIsNone(auth_agent.get_user_by_username("overlengthemailuser"))


class HoneypotTests(SignupProtectionTestCase):
    def test_honeypot_submission_looks_successful_but_creates_no_account(self):
        resp = self.signup("botuser", "bot@example.com", website="http://spam.example")

        # Fake success: don't tip the bot off that it was caught.
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")
        self.assertIsNone(auth_agent.get_user_by_username("botuser"))
        self.assertIsNone(auth_agent.get_user_by_email("bot@example.com"))


class SignupRateLimitTests(SignupProtectionTestCase):
    def test_repeated_attempts_are_rate_limited(self):
        for index in range(appmain.SIGNUP_RATE_LIMIT_MAX_ATTEMPTS):
            resp = self.signup(f"rluser{index}", "not-an-email")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Use a valid email address.", resp.text)

        blocked = self.signup("rluser-over-limit", "not-an-email")
        self.assertEqual(blocked.status_code, 200)
        self.assertIn("Too many signup attempts", blocked.text)

        # The limiter blocked it before any account-creation logic ran.
        self.assertIsNone(auth_agent.get_user_by_username("rluser-over-limit"))


if __name__ == "__main__":
    unittest.main()
