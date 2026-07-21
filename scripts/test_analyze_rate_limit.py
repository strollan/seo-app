"""
Regression tests for the V1 anonymous rate limiter on POST /analyze
(app.main.analyze_is_rate_limited / analyze_record_attempt /
analyze_retry_after_seconds).

Covers: the limiter primitives directly (accept/reject boundary, Retry-After
value, window expiry, separate IP buckets), plus the real /analyze route
(10 accepted then 429 with a Retry-After header, invalid URLs rejected
before consuming an attempt, logged-in users exempt).

Route-level tests patch app.main.validate_public_url to a no-op and
app.main.fetch_page_data to raise a distinctive sentinel exception. This
isolates the rate limiter's own placement/behavior (it runs after URL
validation and before the first external fetch) without depending on
network access or faking the rest of the report-generation pipeline, which
has its own separate concerns and is not what this suite is testing.

The in-process rate-limit counters are cleared before/after every test so
tests don't bleed into each other or into a real running server's state.
"""

import os
import sys
import tempfile
import time
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


class SentinelFetchError(Exception):
    """Raised by the patched fetch_page_data to prove a request reached the
    expensive-work checkpoint (i.e. was not blocked by the rate limiter)."""


def _raise_sentinel(url):
    raise SentinelFetchError("reached fetch_page_data")


class AnalyzeRateLimitPrimitiveTests(unittest.TestCase):
    """Direct tests of the limiter functions, independent of HTTP/FastAPI."""

    def setUp(self):
        appmain._analyze_rate_limit_attempts.clear()
        self.addCleanup(appmain._analyze_rate_limit_attempts.clear)

    def test_first_max_attempts_are_not_rate_limited(self):
        key = "1.2.3.4"
        for _ in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            self.assertFalse(appmain.analyze_is_rate_limited(key))
            appmain.analyze_record_attempt(key)

    def test_attempt_after_max_is_rate_limited(self):
        key = "1.2.3.4"
        for _ in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            appmain.analyze_record_attempt(key)

        self.assertTrue(appmain.analyze_is_rate_limited(key))

    def test_retry_after_is_positive_and_within_window(self):
        key = "1.2.3.4"
        for _ in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            appmain.analyze_record_attempt(key)

        retry_after = appmain.analyze_retry_after_seconds(key)
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, appmain.ANALYZE_RATE_LIMIT_WINDOW_SECONDS)

    def test_entries_expire_after_the_window(self):
        key = "1.2.3.4"
        expired_timestamp = time.time() - appmain.ANALYZE_RATE_LIMIT_WINDOW_SECONDS - 1
        appmain._analyze_rate_limit_attempts[
            appmain._analyze_rate_limit_key(key)
        ] = [expired_timestamp] * appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS

        self.assertFalse(appmain.analyze_is_rate_limited(key))

    def test_separate_client_hosts_have_separate_buckets(self):
        key_a = "1.2.3.4"
        key_b = "5.6.7.8"

        for _ in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            appmain.analyze_record_attempt(key_a)

        self.assertTrue(appmain.analyze_is_rate_limited(key_a))
        self.assertFalse(appmain.analyze_is_rate_limited(key_b))


class AnalyzeRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        appmain._analyze_rate_limit_attempts.clear()
        self.addCleanup(appmain._analyze_rate_limit_attempts.clear)

        self.client = TestClient(appmain.app)

    def submit(self, url_1="https://example.com", url_2="https://example.org"):
        return self.client.post(
            "/analyze",
            data={"url_1": url_1, "url_2": url_2},
        )


class AnonymousRouteLimitTests(AnalyzeRouteTestCase):
    def setUp(self):
        super().setUp()
        validate_patch = mock.patch.object(appmain, "validate_public_url", lambda url: None)
        validate_patch.start()
        self.addCleanup(validate_patch.stop)

        fetch_patch = mock.patch.object(appmain, "fetch_page_data", _raise_sentinel)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def test_first_ten_submissions_reach_expensive_work(self):
        for index in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            with self.subTest(attempt=index + 1):
                with self.assertRaises(SentinelFetchError):
                    self.submit()

    def test_eleventh_submission_is_rejected_with_429_and_retry_after(self):
        for _ in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS):
            with self.assertRaises(SentinelFetchError):
                self.submit()

        resp = self.submit()

        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp.headers)
        retry_after = int(resp.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, appmain.ANALYZE_RATE_LIMIT_WINDOW_SECONDS)
        self.assertIn("Too Many Requests", resp.text)


class InvalidUrlDoesNotConsumeAttemptTests(AnalyzeRouteTestCase):
    def test_invalid_url_rejected_before_consuming_a_rate_limit_attempt(self):
        # ftp:// fails app.main's real (unpatched) validate_public_url on the
        # scheme check alone -- no DNS/network needed -- so this exercises
        # the real validator, not a stub.
        for index in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS + 5):
            with self.subTest(attempt=index + 1):
                resp = self.submit(url_1="ftp://example.com", url_2="ftp://example.org")
                self.assertEqual(resp.status_code, 400)
                self.assertIn("Could not scan this URL", resp.text)

        # None of those rejected submissions should have consumed a slot.
        self.assertFalse(appmain._analyze_rate_limit_attempts)


class LoggedInUsersExemptTests(AnalyzeRouteTestCase):
    def setUp(self):
        super().setUp()
        validate_patch = mock.patch.object(appmain, "validate_public_url", lambda url: None)
        validate_patch.start()
        self.addCleanup(validate_patch.stop)

        fetch_patch = mock.patch.object(appmain, "fetch_page_data", _raise_sentinel)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

        auth_agent.create_user("rluser", VALID_PASSWORD, role="standard", email="rluser@example.com")
        login_resp = self.client.post(
            "/login",
            data={"username": "rluser", "password": VALID_PASSWORD},
            follow_redirects=False,
        )
        self.assertEqual(login_resp.status_code, 303)

    def test_logged_in_user_is_never_rate_limited(self):
        # Well past the anonymous ceiling -- a logged-in user should reach
        # the expensive-work checkpoint (never a 429) every single time.
        for index in range(appmain.ANALYZE_RATE_LIMIT_MAX_ATTEMPTS + 5):
            with self.subTest(attempt=index + 1):
                with self.assertRaises(SentinelFetchError):
                    self.submit()

        # And no anonymous bucket was touched by the logged-in traffic.
        self.assertFalse(appmain._analyze_rate_limit_attempts)


if __name__ == "__main__":
    unittest.main()
