"""SSRF guard for server-side URL fetches (report scraping, contact extraction).

validate_public_url() is the single choke point: it rejects anything that
isn't a plain public http(s) website before a request (or a redirect hop) is
allowed to go out. Callers should show UnsafeURLError's message directly to
users -- it never contains raw exception/internals detail.
"""

import ipaddress
import socket
import threading
from contextlib import contextmanager
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

    # The flags above are not stable across Python versions. Python 3.13
    # reclassified RFC 6598 shared address space (100.64.0.0/10, carrier-grade
    # NAT -- also used inside some cloud/container networks) so that
    # is_private, is_reserved AND is_multicast are all False for it, which
    # meant http://100.64.0.1/ passed every check above on 3.13+.
    # is_global is derived from the IANA special-purpose registries and is
    # False for every non-globally-routable range, so it closes that gap and
    # any future reclassification like it. Verified True for ordinary public
    # IPv4 and IPv6 (93.184.216.34, 8.8.8.8, 172.32.0.1,
    # 2606:4700:4700::1111), so it does not over-block real sites.
    if not addr.is_global:
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


class ValidatedHost(str):
    """The validated hostname, carrying the exact IPs that were checked.

    Subclasses `str` so every existing caller keeps seeing validate_public_url()
    return the lowercased hostname (comparisons, logging and f-strings are
    unchanged). The extra `ips` attribute is what closes the DNS-rebinding
    window: a caller can pin the real connection to the addresses that were
    actually validated instead of letting the HTTP client re-resolve the name
    and receive a different (private) answer the second time.
    """

    def __new__(cls, hostname, ips=()):
        obj = super().__new__(cls, hostname)
        obj.ips = tuple(ips)
        return obj


# --- DNS pinning -----------------------------------------------------------
# validate_public_url() resolves the hostname and checks the addresses, but
# requests/urllib3 then resolve the same name again when the socket is opened.
# Between those two lookups an attacker-controlled DNS server can swap a public
# answer for 127.0.0.1 / 169.254.169.254 (DNS rebinding), and the same window
# exists on every redirect hop.
#
# pinned_dns() closes it by making socket.getaddrinfo() answer from the
# already-validated address list for one specific hostname, for the duration of
# one request, on the calling thread only. The URL, Host header and TLS SNI are
# untouched, so certificate validation and virtual hosting still work.

_REAL_GETADDRINFO = socket.getaddrinfo
_PIN_STATE = threading.local()
_PIN_INSTALL_LOCK = threading.Lock()

_EAI_NONAME = getattr(socket, "EAI_NONAME", -2)


def _pin_key(hostname):
    if isinstance(hostname, (bytes, bytearray)):
        try:
            hostname = hostname.decode("idna")
        except Exception:
            try:
                hostname = hostname.decode("ascii", "ignore")
            except Exception:
                return ""
    key = str(hostname or "").strip().lower().rstrip(".")
    if key.startswith("[") and key.endswith("]"):
        key = key[1:-1]
    return key


def _addrinfo_for(addresses, port, family, socktype, proto):
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        port_num = 0

    results = []
    for ip in addresses:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue

        af = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC) and family != af:
            continue

        sockaddr = (ip, port_num, 0, 0) if af == socket.AF_INET6 else (ip, port_num)
        results.append((
            af,
            socktype or socket.SOCK_STREAM,
            proto or socket.IPPROTO_TCP,
            "",
            sockaddr,
        ))

    if not results:
        # Fail closed. Falling through to a fresh lookup for a pinned host is
        # exactly the rebinding hole this exists to prevent.
        raise socket.gaierror(_EAI_NONAME, "pinned address unavailable")

    return results


def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    pins = getattr(_PIN_STATE, "pins", None)
    if pins:
        addresses = pins.get(_pin_key(host))
        if addresses:
            return _addrinfo_for(addresses, port, family, type, proto)
    return _REAL_GETADDRINFO(host, port, family, type, proto, flags)


def _install_pin_hook():
    """Install the pass-through hook once (self-healing if something else
    replaced socket.getaddrinfo in the meantime, e.g. a test's mock.patch)."""
    global _REAL_GETADDRINFO
    with _PIN_INSTALL_LOCK:
        current = socket.getaddrinfo
        if current is not _pinned_getaddrinfo:
            _REAL_GETADDRINFO = current
            socket.getaddrinfo = _pinned_getaddrinfo


@contextmanager
def pinned_dns(hostname, ips):
    """Resolve `hostname` to `ips` only, on this thread, inside the block.

    Yields True when a pin is active, False when there was nothing to pin (no
    hostname or no validated addresses) -- in which case behaviour is unchanged.
    Every other hostname keeps resolving normally.
    """
    key = _pin_key(hostname)
    addresses = tuple(str(ip).strip() for ip in (ips or ()) if str(ip).strip())

    if not key or not addresses:
        yield False
        return

    _install_pin_hook()

    pins = getattr(_PIN_STATE, "pins", None)
    if pins is None:
        pins = {}
        _PIN_STATE.pins = pins

    had_previous = key in pins
    previous = pins.get(key)
    pins[key] = addresses

    try:
        yield True
    finally:
        if had_previous:
            pins[key] = previous
        else:
            pins.pop(key, None)


def validate_public_url(url):
    """Raise UnsafeURLError unless `url` is a public http(s) URL.

    Checks (in order): scheme, embedded credentials, hostname presence,
    localhost-style hostnames, then resolves the hostname and requires
    *every* returned IPv4/IPv6 address to be public (non-loopback, non-
    private, non-link-local, non-multicast, non-reserved, non-unspecified).

    Returns the lowercased hostname on success, as a ValidatedHost -- a `str`
    that also carries `.ips`, the exact addresses that were validated, so the
    caller can pin the connection to them (see pinned_dns) and close the DNS-
    rebinding window between this check and the actual socket connect. Never
    raises anything other than UnsafeURLError -- resolution/parsing failures
    are folded into it so internals never leak to callers that surface the
    message to users.
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

    validated_ips = []
    for info in infos:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise UnsafeURLError(_GENERIC_MESSAGE)
        if ip_str not in validated_ips:
            validated_ips.append(ip_str)

    return ValidatedHost(hostname_l, validated_ips)
