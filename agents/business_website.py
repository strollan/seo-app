"""Business website resolution for Lead Finder rows.

Search providers can return a Google redirect (google.com/goto?..., /url?q=...,
/aclk?...adurl=...) or a Google-hosted page (Maps, Search) instead of the
business's own website. Lead Finder keys leads by domain, so treating those as
websites made every such business share the domain "google.com": all but the
first were dropped as duplicates, contact crawls ran against Google pages, and
the domain-keyed business cache could mix different businesses together.

resolve_business_website() never returns a Google-hosted URL:
- a Google redirect whose target parameter is a real non-Google http(s) URL is
  unwrapped to that target (status "unwrapped_google_redirect");
- any other Google-hosted URL becomes "" (status "missing");
- everything else is returned unchanged (status "website").
Nothing is fetched and nothing is guessed.
"""

import re
from urllib.parse import parse_qs, urlsplit

WEBSITE = "website"
UNWRAPPED = "unwrapped_google_redirect"
MISSING = "missing"

# Query parameters Google redirect/click URLs use for the destination.
_TARGET_PARAMS = ("url", "q", "adurl", "u", "dest", "continue")

# google.com, google.co.uk, www.google.com, maps.google.com, ... (any subdomain).
_GOOGLE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*google(?:\.[a-z]{2,3}){1,2}$")
_GOOGLE_OTHER = (
    "goo.gl", "g.page", "g.co", "googleadservices.com", "googlesyndication.com",
    "googleusercontent.com", "gstatic.com", "doubleclick.net",
)

# Small-business sites hosted by Google are real websites, not redirects.
_GOOGLE_HOSTED_SITES = ("sites.google.com",)


def _host(url):
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_google_host(host_or_url):
    """True for Google search/maps/redirect hosts (not sites.google.com)."""
    value = str(host_or_url or "").strip().lower()
    host = _host(value) if "/" in value or ":" in value else value.strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or host in _GOOGLE_HOSTED_SITES:
        return False
    if _GOOGLE_HOST.match(host):
        return True
    return any(host == d or host.endswith("." + d) for d in _GOOGLE_OTHER)


def _http_url(value):
    value = str(value or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return value


SERP_DOMAIN = "serp_domain"

_HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _serp_domain_website(serp_domain):
    """https://<host>/ from the search provider's own "domain in SERP" field.

    Google now links many results through google.com/goto?url=<opaque token>,
    with no readable destination, while the SERP still shows the result's real
    domain. DataForSEO returns that as the item's "domain". It is accepted only
    when it is a plain, well-formed, non-Google hostname -- never guessed."""
    host = str(serp_domain or "").strip().lower().rstrip(".")
    if not host or not _HOSTNAME.match(host) or is_google_host(host):
        return ""
    return f"https://{host}/"


def resolve_business_website(url, serp_domain=""):
    """Return {"url": <business website or "">, "status": website|unwrapped_google_redirect|serp_domain|missing}.

    serp_domain: optional provider-reported domain of the result (DataForSEO
    organic item "domain"); used only when `url` is a Google link with no
    readable destination."""
    url = _http_url(url)
    if not url:
        return {"url": "", "status": MISSING}
    if not is_google_host(url):
        return {"url": url, "status": WEBSITE}

    # Google URL: accept only an explicit, real, non-Google destination.
    candidate = url
    for _ in range(2):  # a redirect may wrap one more redirect
        try:
            params = parse_qs(urlsplit(candidate).query, keep_blank_values=False)
        except ValueError:
            break
        target = ""
        for key in _TARGET_PARAMS:
            for value in params.get(key) or []:
                target = _http_url(value)
                if target:
                    break
            if target:
                break
        if not target:
            break
        if not is_google_host(target):
            return {"url": target, "status": UNWRAPPED}
        candidate = target
    fallback = _serp_domain_website(serp_domain)
    if fallback:
        return {"url": fallback, "status": SERP_DOMAIN}
    return {"url": "", "status": MISSING}


def display_safe_lead(lead):
    """Copy of a lead whose website/domain/contact page are never Google URLs.

    Display-only: the stored lead is not modified. A Google redirect with a real
    destination shows that destination; otherwise the website shows as missing.
    Name, phone, email and address are left exactly as they are.
    """
    if not isinstance(lead, dict):
        return lead
    out = dict(lead)
    url_keys = [k for k in ("url", "website", "link") if out.get(k)]
    raw = out.get(url_keys[0]) if url_keys else ""
    if raw and is_google_host(raw):
        resolved = resolve_business_website(raw)
        for key in url_keys:
            out[key] = resolved["url"]
        out["website_status"] = resolved["status"]
        out["domain"] = _host(resolved["url"]) if resolved["url"] else ""
    elif not raw and not out.get("domain"):
        out.setdefault("website_status", MISSING)
    if is_google_host(out.get("domain") or ""):
        out["domain"] = ""
    for key in ("contact_page_url", "contact_page"):
        if is_google_host(out.get(key) or ""):
            out[key] = ""
    return out


def no_website_dedupe_key(row):
    """Stable per-business key for a row without a verified website.

    Different businesses must never collapse into one key, so this uses the
    business name (plus phone when present), never a shared host like google.com.
    Returns "" when the row has nothing identifying.
    """
    if not isinstance(row, dict):
        return ""
    name = re.sub(r"[^a-z0-9]+", " ", str(row.get("title") or row.get("name") or "").lower()).strip()
    phone = re.sub(r"\D+", "", str(row.get("best_phone") or row.get("phone") or ""))[-10:]
    if not name and not phone:
        return ""
    return f"nosite:{name}|{phone}"
