from urllib.parse import urlparse


BLACKLISTED_DOMAINS = {
    "reddit.com",
    "forbes.com",
    "justia.com",
    "superlawyers.com",
    "bestlawfirms.com",
    "attorneyatlawmagazine.com",
    "avvo.com",
    "findlaw.com",
    "martindale.com",
    "expertise.com",
    "yelp.com",
}


BLACKLISTED_TITLE_PHRASES = [
    "best",
    "top",
    "near you",
    "super lawyers",
    "directory",
    "reviews",
    "ratings",
    "compare",
    "forum",
    "reddit",
]


def get_domain(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def is_blacklisted_result(result):
    title = (result.get("title") or "").lower()
    link = result.get("link") or ""
    domain = get_domain(link)

    if domain in BLACKLISTED_DOMAINS:
        return True

    for blocked_domain in BLACKLISTED_DOMAINS:
        if domain.endswith("." + blocked_domain):
            return True

    for phrase in BLACKLISTED_TITLE_PHRASES:
        if phrase in title:
            return True

    return False


def filter_business_results(results):
    return [
        result
        for result in results
        if not is_blacklisted_result(result)
    ]


# Added business-result safety filters
EXTRA_BLOCKED_DOMAINS = {
    "yelp.com",
    "angi.com",
    "angieslist.com",
    "homeadvisor.com",
    "bbb.org",
    "thumbtack.com",
    "porch.com",

    "facebook.com",
    "linkedin.com",
    "youtube.com",
    "gaf.com",
    "instagram.com",
}

EXTRA_BLOCKED_PATTERNS = {
    "/roofing-contractors/",
    "/contractors/",
    "/directory/",
    "/profile/",
    "gaf residential roofers",
    "find a contractor",
    "best roofers",
    "top roofers",
    "top 10",
    "reviews",
}

def _extra_is_blocked_result(url="", title="", snippet=""):
    combined = f"{url} {title} {snippet}".lower()
    url_l = (url or "").lower()

    if any(domain in url_l for domain in EXTRA_BLOCKED_DOMAINS):
        return True

    if any(pattern in combined for pattern in EXTRA_BLOCKED_PATTERNS):
        return True

    return False


def is_extra_blocked_business_result(result):
    url = result.get("link") or result.get("url") or ""
    title = result.get("title") or ""
    snippet = result.get("snippet") or ""
    return _extra_is_blocked_result(url, title, snippet)


# Final safety block for non-business SERP results
FINAL_BLOCKED_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "gaf.com",
    "reddit.com",
    "forbes.com",
    "justia.com",
    "superlawyers.com",
    "bestlawfirms.com",
    "attorneyatlawmagazine.com",
    "avvo.com",
    "findlaw.com",
    "yelp.com",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "bbb.org",
    "yellowpages.com",
    "mapquest.com",
}

FINAL_BLOCKED_PATTERNS = {
    "/roofing-contractors/",
    "/contractors/",
    "/directory/",
    "/profile/",
    "gaf residential roofers",
    "find a contractor",
    "best roofers",
    "top roofers",
    "top 10",
    "best of",
    "reviews",
    "facebook",
    "instagram",
}

def final_is_blocked_business_result(result):
    if not isinstance(result, dict):
        return False

    url = (
        result.get("link")
        or result.get("url")
        or result.get("website")
        or ""
    )
    title = result.get("title") or ""
    snippet = result.get("snippet") or result.get("description") or ""

    combined = f"{url} {title} {snippet}".lower()

    if any(domain in combined for domain in FINAL_BLOCKED_DOMAINS):
        return True

    if any(pattern in combined for pattern in FINAL_BLOCKED_PATTERNS):
        return True

    return False

def final_filter_business_results(results):
    if not results:
        return []
    return [
        result for result in results
        if not final_is_blocked_business_result(result)
    ]
