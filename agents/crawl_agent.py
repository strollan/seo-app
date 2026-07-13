import time
import os
from urllib.parse import urljoin

import requests
import urllib3

from agents.url_safety import validate_public_url, UnsafeURLError, DEFAULT_MAX_REDIRECTS


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def crawl_log(message):
    if os.environ.get("LEAD_BOT_QUIET") != "1":
        print(message, flush=True)


CACHE_TTL_SECONDS = 60 * 60 * 6
_CRAWL_CACHE = {}
_BLOCKED_HTML = "<html><head><title>Blocked by site</title></head><body></body></html>"


class CachedResponse:
    def __init__(self, url, status_code, text, headers=None):
        self.url = url
        self.status_code = status_code
        self.text = text or ""
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}")


def _blocked_response(url, reason):
    crawl_log(f"CRAWL BLOCKED ({reason}): {url}")
    return CachedResponse(
        url=url,
        status_code=597,
        text=_BLOCKED_HTML,
        headers={},
    )


def crawl_get(url, headers=None, timeout=(3, 6), allow_redirects=True, verify=False):
    """
    Backend crawl helper for report pages.

    Purpose:
    - prevent long hangs
    - cache repeated crawls for 6 hours
    - return a response-like object compatible with existing fetch_page_data()
    - block SSRF: refuse to fetch (or follow a redirect to) anything that
      isn't a public http(s) host -- see agents/url_safety.py
    """

    cache_key = url.strip()

    now = time.time()
    cached = _CRAWL_CACHE.get(cache_key)

    if cached:
        created, payload = cached
        if now - created < CACHE_TTL_SECONDS:
            crawl_log(f"CRAWL CACHE HIT: {cache_key}")
            return CachedResponse(
                url=payload["url"],
                status_code=payload["status_code"],
                text=payload["text"],
                headers=payload.get("headers", {}),
            )

    # === LEADBOT NON-HTTP CRAWL SKIP START ===
    raw_crawl_url = str(url or '').strip()
    raw_crawl_url_l = raw_crawl_url.lower()
    if (
        not raw_crawl_url
        or raw_crawl_url_l.startswith(('mailto:', 'tel:', 'javascript:', '#'))
        or not raw_crawl_url_l.startswith(('http://', 'https://'))
    ):
        crawl_log(f"CRAWL SKIP NON-HTTP: {raw_crawl_url}")
        return CachedResponse(
            url=raw_crawl_url,
            status_code=0,
            text="",
            headers={},
        )
    # === LEADBOT NON-HTTP CRAWL SKIP END ===

    try:
        validate_public_url(raw_crawl_url)
    except UnsafeURLError as exc:
        return _blocked_response(raw_crawl_url, str(exc))

    crawl_log(f"CRAWL CACHE MISS: {cache_key}")

    current_url = raw_crawl_url
    redirects_followed = 0

    try:
        while True:
            response = requests.get(
                current_url,
                timeout=timeout,
                headers=headers,
                allow_redirects=False,
                verify=verify,
            )

            if not (allow_redirects and response.is_redirect):
                break

            next_url = response.headers.get("Location")
            if not next_url:
                break

            if redirects_followed >= DEFAULT_MAX_REDIRECTS:
                return _blocked_response(url, "too many redirects")

            next_url = urljoin(current_url, next_url)

            try:
                validate_public_url(next_url)
            except UnsafeURLError as exc:
                return _blocked_response(next_url, str(exc))

            current_url = next_url
            redirects_followed += 1

        payload = {
            "url": response.url,
            "status_code": response.status_code,
            "text": response.text or "",
            "headers": dict(response.headers or {}),
        }

        _CRAWL_CACHE[cache_key] = (now, payload)

        return CachedResponse(
            url=payload["url"],
            status_code=payload["status_code"],
            text=payload["text"],
            headers=payload["headers"],
        )

    except requests.exceptions.Timeout:
        crawl_log(f"CRAWL TIMEOUT: {cache_key}")
        return CachedResponse(
            url=url,
            status_code=599,
            text=_BLOCKED_HTML,
            headers={},
        )

    except requests.exceptions.RequestException as e:
        crawl_log(f"CRAWL ERROR: {cache_key} :: {e}")
        return CachedResponse(
            url=url,
            status_code=598,
            text=_BLOCKED_HTML,
            headers={},
        )
