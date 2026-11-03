"""Same-website detection for URL Compare (no network, no new dependencies).

Two URLs are the same website when their *site identities* match:

* hosts are compared case-insensitively, IDNA-normalised, without a trailing
  dot or a leading ``www.`` label;
* subdomains of one registrable domain are one site
  (``shop.example.com`` == ``example.com``);
* the registrable domain is the label before the public suffix.  No public
  suffix library is installed, so a conservative embedded subset is used:
  common multi-label country suffixes (``co.uk``, ``com.au`` …), generic
  second-level patterns under two-letter country codes (``com.sg``,
  ``co.za`` …), and shared hosting platforms whose subdomains belong to
  different owners (``github.io``, ``wixsite.com`` …);
* a few platforms host unrelated businesses under *paths* of one host
  (``sites.google.com/view/<name>``, ``facebook.com/<page>``); there the
  identity also includes those path segments.

When in doubt the rules keep sites apart: they never merge two hosts that do
not share a registrable domain.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# Multi-label public suffixes seen for small businesses.
MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "sch.uk",
    "com.au", "net.au", "org.au", "id.au", "asn.au", "co.nz", "net.nz", "org.nz",
    "co.za", "co.in", "net.in", "org.in", "co.jp", "ne.jp", "or.jp", "co.kr", "or.kr",
    "com.br", "net.br", "com.mx", "com.ar", "com.co", "com.sg", "com.my", "com.ph",
    "com.hk", "com.tw", "com.tr", "com.cn", "com.pk", "com.ng", "co.il", "co.id",
    "gc.ca", "qc.ca", "on.ca", "bc.ca", "ab.ca",
})
# Generic second-level labels under a two-letter country code (e.g. com.sg).
GENERIC_SECOND_LEVEL = frozenset({"com", "net", "org", "co", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil", "nom", "ltd"})
# Hosting platforms where each subdomain is a separate customer's site.
SHARED_HOST_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "blogspot.com", "wordpress.com", "wixsite.com", "weebly.com",
    "squarespace.com", "godaddysites.com", "myshopify.com", "business.site", "web.app",
    "firebaseapp.com", "appspot.com", "herokuapp.com", "netlify.app", "vercel.app",
    "pages.dev", "azurewebsites.net", "cloudfront.net", "webflow.io", "carrd.co",
    "site123.me", "jimdosite.com", "strikingly.com", "mystrikingly.com", "square.site",
    "ueniweb.com", "yolasite.com", "wix.com",
})
# Hosts that serve unrelated businesses under path prefixes: host -> path segments that identify one site.
PATH_SCOPED_HOSTS = {
    "sites.google.com": 2,   # /view/<name> or /site/<name>
    "facebook.com": 1, "m.facebook.com": 1, "instagram.com": 1, "linkedin.com": 2,
    "yelp.com": 2, "linktr.ee": 1,
}


def normalize_host(url: str) -> str | None:
    """Lower-case IDNA host without port, trailing dot or leading ``www.``; None if absent."""
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    while host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    return host or None


def registrable_domain(host: str) -> str:
    """The registrable domain for ``host`` under the embedded suffix rules."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    for suffix in SHARED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            depth = suffix.count(".") + 2  # one label to the left of the platform suffix
            return ".".join(labels[-depth:]) if len(labels) >= depth else host
    last_two = ".".join(labels[-2:])
    if last_two in MULTI_LABEL_SUFFIXES or (len(labels[-1]) == 2 and labels[-2] in GENERIC_SECOND_LEVEL):
        return ".".join(labels[-3:])
    return last_two


def site_identity(url: str) -> str | None:
    """A comparable identity for the website ``url`` belongs to, or None if it has no host."""
    host = normalize_host(url)
    if not host:
        return None
    base = registrable_domain(host)
    segments_needed = PATH_SCOPED_HOSTS.get(host, PATH_SCOPED_HOSTS.get(base))
    if segments_needed:
        text = str(url).strip()
        path = urlsplit(text if "://" in text else "http://" + text).path
        segments = [s.lower() for s in path.split("/") if s][:segments_needed]
        return base + ("/" + "/".join(segments) if segments else "")
    return base


def same_website(urls_a, urls_b) -> bool:
    """True when any URL for site A and any URL for site B share a site identity.

    Each argument is one URL or an iterable of URLs (requested and final/redirected).
    """
    def identities(urls):
        values = [urls] if isinstance(urls, str) else list(urls or [])
        return {identity for identity in map(site_identity, values) if identity}
    return bool(identities(urls_a) & identities(urls_b))


SAME_WEBSITE_MESSAGE = "Your Site and Competitor are the same website. Enter a different competitor."


def rules_for_browser() -> dict:
    """The same rules, for the compare form's client-side check."""
    return {
        "multi": sorted(MULTI_LABEL_SUFFIXES), "generic": sorted(GENERIC_SECOND_LEVEL),
        "shared": sorted(SHARED_HOST_SUFFIXES), "paths": PATH_SCOPED_HOSTS,
    }
