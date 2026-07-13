"""
Regression tests for the SSRF guard (agents/url_safety.py) and its use in
agents/crawl_agent.crawl_get, which is the network choke point behind the
anonymous POST /analyze route (app.main.analyze) and lead contact-page
scraping (agents/contact_extraction_agent.py).

socket.getaddrinfo is mocked wherever a test needs a *hostname* (rather than
a bare IP literal) to resolve to a specific address, so these tests never
depend on real DNS or network access.
"""

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.url_safety import validate_public_url, UnsafeURLError
from agents.crawl_agent import crawl_get, _CRAWL_CACHE


def addrinfo_for(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


class LoopbackAndPrivateAddressTests(unittest.TestCase):
    def test_ipv4_loopback_literal_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http://127.0.0.1/admin")

    def test_localhost_hostname_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http://localhost:8000/")

    def test_private_ipv4_ranges_are_rejected(self):
        for host in ("10.0.0.5", "172.16.0.1", "192.168.1.1"):
            with self.subTest(host=host):
                with self.assertRaises(UnsafeURLError):
                    validate_public_url(f"http://{host}/")

    def test_link_local_metadata_address_is_rejected(self):
        # 169.254.169.254 is the AWS/GCP/Azure cloud-metadata endpoint --
        # the canonical SSRF target.
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_literal_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http://[::1]/")

    def test_hostname_resolving_to_private_ip_is_rejected(self):
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            return_value=addrinfo_for("10.1.2.3"),
        ):
            with self.assertRaises(UnsafeURLError):
                validate_public_url("http://internal.example.test/")


class PublicUrlTests(unittest.TestCase):
    def test_public_https_url_is_allowed(self):
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            return_value=addrinfo_for("93.184.216.34"),
        ):
            hostname = validate_public_url("https://example.com/page")
            self.assertEqual(hostname, "example.com")

    def test_public_url_with_multiple_resolved_ips_all_checked(self):
        # One public, one private -- every returned address must be public.
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            return_value=addrinfo_for("93.184.216.34") + addrinfo_for("10.0.0.9"),
        ):
            with self.assertRaises(UnsafeURLError):
                validate_public_url("https://mixed.example.test/")


class SchemeAndFormatTests(unittest.TestCase):
    def test_non_http_scheme_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("file:///etc/passwd")

    def test_ftp_scheme_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("ftp://example.com/")

    def test_embedded_credentials_are_rejected(self):
        with mock.patch(
            "agents.url_safety.socket.getaddrinfo",
            return_value=addrinfo_for("93.184.216.34"),
        ):
            with self.assertRaises(UnsafeURLError):
                validate_public_url("http://user:pass@example.com/")

    def test_missing_hostname_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http:///path-only")

    def test_blank_url_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("")

    def test_error_message_never_leaks_internals(self):
        # Even an unresolvable/garbage hostname must produce the short,
        # generic message -- never a raw socket/DNS exception string.
        try:
            validate_public_url("http://this-host-does-not-exist.invalid/")
        except UnsafeURLError as exc:
            self.assertNotIn("gaierror", str(exc))
            self.assertNotIn("Errno", str(exc))
        else:
            self.fail("Expected UnsafeURLError")


class CrawlGetRedirectSafetyTests(unittest.TestCase):
    def setUp(self):
        _CRAWL_CACHE.clear()
        self.addCleanup(_CRAWL_CACHE.clear)

    def test_redirect_to_private_address_is_blocked(self):
        initial = mock.Mock(is_redirect=True, headers={"Location": "http://169.254.169.254/secret"})

        with mock.patch(
            "agents.crawl_agent.validate_public_url",
            side_effect=[None, UnsafeURLError("That URL cannot be scanned.")],
        ), mock.patch("agents.crawl_agent.requests.get", return_value=initial) as mock_get:
            result = crawl_get("http://public-site.example.test/")

        # Only the first hop is ever actually requested -- the redirect
        # target is validated and rejected before a second request fires.
        mock_get.assert_called_once()
        self.assertNotEqual(result.status_code, 200)
        self.assertNotIn("secret", result.text)

    def test_too_many_redirects_is_blocked(self):
        bouncing = mock.Mock(is_redirect=True, headers={"Location": "http://public-site.example.test/next"})

        with mock.patch(
            "agents.crawl_agent.validate_public_url", return_value="public-site.example.test"
        ), mock.patch("agents.crawl_agent.requests.get", return_value=bouncing) as mock_get:
            result = crawl_get("http://public-site.example.test/")

        self.assertEqual(mock_get.call_count, 6)  # DEFAULT_MAX_REDIRECTS (5) + 1, not unbounded
        self.assertNotEqual(result.status_code, 200)

    def test_initial_url_failing_validation_never_makes_a_request(self):
        with mock.patch("agents.crawl_agent.requests.get") as mock_get:
            result = crawl_get("http://127.0.0.1/admin")

        mock_get.assert_not_called()
        self.assertNotEqual(result.status_code, 200)


if __name__ == "__main__":
    unittest.main()
