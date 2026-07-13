"""SSRF guard for server-side URL fetches (report scraping, contact extraction).

validate_public_url() is the single choke point: it rejects anything that
isn't a plain public http(s) website before a request (or a redirect hop) is
allowed to go out. Callers should show UnsafeURLError's message directly to
users -- it never contains raw exception/internals detail.
"""

import ipaddress
import socket
from urllib.parse import urlparse

DEFAULT_MAX_REDIRECTS = 5

_LOCALHOST_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})

_GENERIC_MESSAGE = "That URL cannot be scanned."


class UnsafeURLError(ValueError):
    """Raised when a URL fails the SSRF safety check.

    The message is short and safe to show to end users as-is.
    """


def _is_public_ip(ip_str):
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False

    # IPv4-mapped / 6to4 IPv6 addresses can smuggle a private IPv4 address
    # inside an otherwise "public-looking" IPv6 address -- unwrap and recheck.
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None and not _is_public_ip(str(mapped)):
            return False

        sixtofour = getattr(addr, "sixtofour", None)
        if sixtofour is not None and not _is_public_ip(str(sixtofour)):
            return False

    return True


def validate_public_url(url):
    """Raise UnsafeURLError unless `url` is a public http(s) URL.

    Checks (in order): scheme, embedded credentials, hostname presence,
    localhost-style hostnames, then resolves the hostname and requires
    *every* returned IPv4/IPv6 address to be public (non-loopback, non-
    private, non-link-local, non-multicast, non-reserved, non-unspecified).

    Returns the lowercased hostname on success. Never raises anything other
    than UnsafeURLError -- resolution/parsing failures are folded into it so
    internals never leak to callers that surface the message to users.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("Enter a website URL to scan.")

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        raise UnsafeURLError("Enter a valid website URL.")

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http:// and https:// URLs are supported.")

    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded login credentials are not supported.")

    try:
        hostname = parsed.hostname
    except ValueError:
        hostname = None

    if not hostname:
        raise UnsafeURLError("Enter a valid website URL.")

    hostname_l = hostname.lower().rstrip(".")
    if hostname_l in _LOCALHOST_HOSTNAMES or hostname_l.endswith(".localhost"):
        raise UnsafeURLError(_GENERIC_MESSAGE)

    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        raise UnsafeURLError("That website's address could not be resolved.")

    if not infos:
        raise UnsafeURLError("That website's address could not be resolved.")

    for info in infos:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise UnsafeURLError(_GENERIC_MESSAGE)

    return hostname_l
