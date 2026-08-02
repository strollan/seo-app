"""
Regression tests for bare-domain normalization on the Compare Tool's
POST /analyze route (app.main.analyze).

Bug: "leadmeleads.com" (no scheme) was rejected by validate_public_url()
with "Only http:// and https:// URLs are supported." even though
app.main.sanitize_url() -- already used by fetch_page_data for the actual
crawl -- would have added https:// before fetching. The pre-fetch
validation loop ran on the raw, un-sanitized url_1/url_2 instead of the
sanitized value, so a bare domain failed validation before ever reaching
the (already-scheme-tolerant) fetch step. The fix sanitizes url_1/url_2
once, up front, and uses that same normalized value for both the
security check and the fetch, so they can't disagree with each other.

socket.getaddrinfo is mocked (or validate_public_url is no-op'd) wherever
a test doesn't specifically need real SSRF-check logic, matching
scripts/test_url_safety.py and scripts/test_analyze_rate_limit.py -- no
real DNS or network access anywhere in this file.
"""

import socket
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent


def addrinfo_for(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


PUBLIC_IP = "93.184.216.34"


class CapturedFetchError(Exception):
    """Raised by a patched fetch_page_data to prove a request reached the
    fetch checkpoint, carrying the exact URL string it was called with so
    tests can assert on the post-normalization value directly."""

    def __init__(self, url):
        super().__init__(f"reached fetch_page_data: {url!r}")
        self.url = url


class SingleCallCapture:
    """Captures the URL passed to the first fetch_page_data call (always
    url_1's fetch) and raises immediately, so nothing downstream runs."""

    def __call__(self, url):
        raise CapturedFetchError(url)


class TwoCallCapture:
    """Lets the first fetch_page_data call (url_1's site fetch) succeed
    with a harmless placeholder, then captures + raises on the second call
    (the competitor fetch, using url_2's normalized value) -- safe because
    when url_2 is non-empty, app.main.analyze() does nothing with the
    first call's return value before making the second call."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if len(self.calls) == 1:
            return {}
        raise CapturedFetchError(url)


class CompareUrlNormalizationTestCase(unittest.TestCase):
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

    def submit(self, url_1, url_2):
        return self.client.post("/analyze", data={"url_1": url_1, "url_2": url_2})


class NormalizationReachesFetchTests(CompareUrlNormalizationTestCase):
    """validate_public_url is no-op'd here -- these tests are purely about
    what sanitize_url() turns the input into and whether that normalized
    value is what actually reaches fetch_page_data, not about the SSRF
    checks themselves (covered separately below and in test_url_safety.py)."""

    def setUp(self):
        super().setUp()
        validate_patch = mock.patch.object(appmain, "validate_public_url", lambda url: None)
        validate_patch.start()
        self.addCleanup(validate_patch.stop)

    def test_bare_domain_gets_https_prepended(self):
        with mock.patch.object(appmain, "fetch_page_data", SingleCallCapture()):
            with self.assertRaises(CapturedFetchError) as ctx:
                self.submit("leadmeleads.com", "https://example.org")
        self.assertEqual(ctx.exception.url, "https://leadmeleads.com")

    def test_www_bare_domain_gets_normalized(self):
        with mock.patch.object(appmain, "fetch_page_data", SingleCallCapture()):
            with self.assertRaises(CapturedFetchError) as ctx:
                self.submit("www.leadmeleads.com", "https://example.org")
        self.assertEqual(ctx.exception.url, "https://www.leadmeleads.com")

    def test_explicit_https_remains_unchanged(self):
        with mock.patch.object(appmain, "fetch_page_data", SingleCallCapture()):
            with self.assertRaises(CapturedFetchError) as ctx:
                self.submit("https://leadmeleads.com", "https://example.org")
        self.assertEqual(ctx.exception.url, "https://leadmeleads.com")

    def test_explicit_http_remains_valid_and_unchanged(self):
        with mock.patch.object(appmain, "fetch_page_data", SingleCallCapture()):
            with self.assertRaises(CapturedFetchError) as ctx:
                self.submit("http://leadmeleads.com", "https://example.org")
        self.assertEqual(ctx.exception.url, "http://leadmeleads.com")

    def test_surrounding_whitespace_is_trimmed(self):
        with mock.patch.object(appmain, "fetch_page_data", SingleCallCapture()):
            with self.assertRaises(CapturedFetchError) as ctx:
                self.submit("   leadmeleads.com   ", "https://example.org")
        self.assertEqual(ctx.exception.url, "https://leadmeleads.com")

    def test_both_compare_fields_receive_identical_normalization(self):
        capture = TwoCallCapture()
        with mock.patch.object(appmain, "fetch_page_data", capture):
            with self.assertRaises(CapturedFetchError):
                self.submit("  www.leadmeleads.com  ", "local-leads.ai")
        self.assertEqual(capture.calls[0], "https://www.leadmeleads.com")
        self.assertEqual(capture.calls[1], "https://local-leads.ai")


class UnsupportedSchemeStillRejectedTests(CompareUrlNormalizationTestCase):
    """Real, unpatched validate_public_url -- normalization must never
    touch an input that already declares a scheme, so these still fail
    the scheme allowlist exactly as before."""

    def test_unsupported_schemes_are_rejected_on_your_site_field(self):
        for scheme_url in (
            "file:///etc/passwd",
            "ftp://evil.example.com/",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "gopher://evil.example.com/",
        ):
            with self.subTest(scheme_url=scheme_url):
                resp = self.submit(scheme_url, "https://example.org")
                self.assertEqual(resp.status_code, 400)
                self.assertIn("Your Site URL is invalid.", resp.text)
                self.assertIn(
                    "Only http:// and https:// URLs are supported.", resp.text
                )

    def test_unsupported_scheme_on_competitor_field_with_public_your_site(self):
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            return_value=addrinfo_for(PUBLIC_IP),
        ):
            resp = self.submit("https://example.org", "javascript:alert(1)")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Competitor URL is invalid.", resp.text)
        self.assertIn("Only http:// and https:// URLs are supported.", resp.text)


class PrivateAndLocalTargetsStillRejectedTests(CompareUrlNormalizationTestCase):
    """localhost / private-IP / link-local (cloud metadata) targets must
    still be rejected after normalization -- and rejected for being
    unsafe, never because a scheme was merely omitted."""

    def test_localhost_hostname_is_rejected(self):
        resp = self.submit("localhost", "https://example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Your Site URL is invalid.", resp.text)
        self.assertNotIn("Only http:// and https:// URLs are supported.", resp.text)

    def test_loopback_ip_literal_is_rejected(self):
        resp = self.submit("127.0.0.1", "https://example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Your Site URL is invalid.", resp.text)
        self.assertNotIn("Only http:// and https:// URLs are supported.", resp.text)

    def test_private_ipv4_range_is_rejected(self):
        resp = self.submit("192.168.1.1", "https://example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Your Site URL is invalid.", resp.text)
        self.assertNotIn("Only http:// and https:// URLs are supported.", resp.text)

    def test_cloud_metadata_link_local_address_is_rejected(self):
        # 169.254.169.254 is the AWS/GCP/Azure cloud-metadata endpoint --
        # the canonical SSRF target -- still rejected once normalized to
        # https://169.254.169.254.
        resp = self.submit("169.254.169.254", "https://example.org")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Your Site URL is invalid.", resp.text)
        self.assertNotIn("Only http:// and https:// URLs are supported.", resp.text)

    def test_bare_private_hostname_on_competitor_field_is_rejected(self):
        # url_1 must resolve publicly so the loop actually reaches url_2.
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            side_effect=lambda host, *a, **k: (
                addrinfo_for(PUBLIC_IP) if "example.org" in host else addrinfo_for("10.1.2.3")
            ),
        ):
            resp = self.submit("https://example.org", "internal.example.test")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Competitor URL is invalid.", resp.text)
        self.assertNotIn("Only http:// and https:// URLs are supported.", resp.text)


class ErrorPreservesEnteredValuesTests(CompareUrlNormalizationTestCase):
    def test_back_to_compare_link_preserves_both_entered_values_as_typed(self):
        resp = self.submit("ftp://evil.example.com/", "  example.org  ")
        self.assertEqual(resp.status_code, 400)

        href_match = None
        for line in resp.text.splitlines():
            if "Back to Compare" in line:
                href_match = line
        self.assertIsNotNone(href_match, "no 'Back to Compare' link found in error response")

        start = href_match.index("href='") + len("href='")
        end = href_match.index("'", start)
        link = href_match[start:end]

        query = parse_qs(urlsplit(link).query)
        self.assertEqual(query.get("url_1"), ["ftp://evil.example.com/"])
        self.assertEqual(query.get("url_2"), ["example.org"])


class DnsRebindingProtectionStillExecutesAfterNormalizationTests(unittest.TestCase):
    """Normalization only changes what string reaches validate_public_url();
    it must not let a bare-domain input skip the real SSRF check or the
    DNS-rebinding pin that the actual crawl (agents.crawl_agent.crawl_get,
    invoked via app.main.fetch_page_data) applies to every fetch. This runs
    the real, unmocked sanitize_url -> validate_public_url -> pinned_dns
    chain end to end for a bare-domain input -- only the HTTP request and
    DNS resolution are faked, matching scripts/test_url_safety.py."""

    def test_normalized_bare_domain_fetch_engages_real_ssrf_check_and_dns_pin(self):
        from agents import crawl_agent

        crawl_agent._CRAWL_CACHE.clear()
        self.addCleanup(crawl_agent._CRAWL_CACHE.clear)

        normalized = appmain.sanitize_url("leadmeleads.com")
        self.assertEqual(normalized, "https://leadmeleads.com")

        fake_response = mock.Mock(is_redirect=False, status_code=200, url=normalized, headers={})
        fake_response.iter_content = lambda chunk_size=1: [
            b"<html><head><title>t</title></head></html>"
        ]

        pin_calls = []
        real_pinned_dns = crawl_agent.pinned_dns

        def spying_pinned_dns(hostname, ips):
            pin_calls.append((hostname, tuple(ips)))
            return real_pinned_dns(hostname, ips)

        with mock.patch(
            "agents.url_safety.socket.getaddrinfo", return_value=addrinfo_for(PUBLIC_IP)
        ), mock.patch(
            "agents.crawl_agent.pinned_dns", side_effect=spying_pinned_dns
        ), mock.patch(
            "agents.crawl_agent.requests.get", return_value=fake_response
        ) as mock_get:
            appmain.fetch_page_data(normalized)

        # Proves the bare-domain-turned-https URL was not silently skipped
        # by crawl_get's non-http(s) short-circuit -- a real request fired.
        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        self.assertTrue(called_url.startswith("https://leadmeleads.com"))

        # Proves the DNS-rebinding pin was engaged for exactly the
        # validated hostname/IP pair -- not skipped, not a different host.
        self.assertEqual(len(pin_calls), 1)
        hostname, ips = pin_calls[0]
        self.assertEqual(hostname, "leadmeleads.com")
        self.assertEqual(ips, (PUBLIC_IP,))


if __name__ == "__main__":
    unittest.main()
