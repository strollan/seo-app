import time
import os
from contextlib import nullcontext
from urllib.parse import urljoin, urlparse

import requests
import urllib3

from agents.url_safety import (
    validate_public_url,
    UnsafeURLError,
    DEFAULT_MAX_REDIRECTS,
    pinned_dns,
)

# Charset detection for bodies we streamed ourselves. Both of these ship as a
# transitive dependency of `requests` (charset_normalizer on requests >= 2.26,
# chardet on older installs); neither is added to requirements by this module,
# and both are optional here -- _detect_encoding falls back to utf-8.
try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except Exception:
    _cn_from_bytes = None

try:
    import chardet as _chardet
except Exception:
    _chardet = None


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def crawl_log(message):
    if os.environ.get("LEAD_BOT_QUIET") != "1":
        print(message, flush=True)


CACHE_TTL_SECONDS = 60 * 60 * 6
_CRAWL_CACHE = {}
_BLOCKED_HTML = "<html><head><title>Blocked by site</title></head><body></body></html>"

# Hard ceiling on how many bytes a single crawl may pull into memory.
# Without this, a public-but-hostile URL (or an honest-but-enormous file)
# can exhaust server memory even though every SSRF check passed -- the
# address being public says nothing about the response being small.
# Overridable for ops tuning; clamped to a sane range.
try:
    MAX_RESPONSE_BYTES = int(os.environ.get("CRAWL_MAX_RESPONSE_BYTES") or 3 * 1024 * 1024)
except Exception:
    MAX_RESPONSE_BYTES = 3 * 1024 * 1024

MAX_RESPONSE_BYTES = max(64 * 1024, min(MAX_RESPONSE_BYTES, 25 * 1024 * 1024))

# requests/urllib3's `timeout=(connect, read)` read-timeout is a per-socket-
# operation timeout, not a total-request deadline: it resets on every chunk
# that actually arrives. A server that trickles a byte or two just often
# enough to beat the read timeout on each call can hold a connection (and
# the crawling thread/process behind it) open far longer than the (3, 6)
# timeout passed to crawl_get() suggests. This is a real cap on total wall-
# clock time spent reading one response body, checked between chunks in
# _read_capped_text() below, so a slow-trickle response can never block
# indefinitely regardless of how it paces its bytes.
try:
    CRAWL_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("CRAWL_TOTAL_TIMEOUT_SECONDS") or 20)
except Exception:
    CRAWL_TOTAL_TIMEOUT_SECONDS = 20.0


def _close_quietly(response):
    try:
        response.close()
    except Exception:
        pass


def _detect_encoding(body):
    """Guess an encoding from raw bytes we already hold.

    Deliberately does NOT use response.apparent_encoding: that property reads
    response.content, which raises
    ``RuntimeError: The content for this response was already consumed`` once
    the body has been drained through iter_content() (which is exactly what
    _read_capped_text does). Detection therefore runs on the bytes we buffered
    ourselves, never on the response object.

    Uses whichever detector `requests` already pulls in (charset_normalizer on
    modern requests, chardet on older installs). No new dependency.
    """
    if not body:
        return "utf-8"

    sample = body[:64 * 1024]

    if _cn_from_bytes is not None:
        try:
            best = _cn_from_bytes(sample).best()
            if best is not None and best.encoding:
                return best.encoding
        except Exception:
            pass

    if _chardet is not None:
        try:
            guess = _chardet.detect(sample) or {}
            if guess.get("encoding"):
                return guess["encoding"]
        except Exception:
            pass

    return "utf-8"


def _read_capped_text(response, limit=None):
    """Read at most `limit` bytes from `response`, then decode.

    Streams so an oversized body is abandoned mid-transfer instead of being
    fully buffered first. Returns whatever was read (truncated), so a huge
    page still yields usable <head> markup rather than nothing.
    """
    limit = MAX_RESPONSE_BYTES if limit is None else limit

    declared = response.headers.get("Content-Length") if response.headers else None
    if declared:
        try:
            if int(declared) > limit:
                crawl_log(f"CRAWL SIZE LIMIT (declared {declared} > {limit})")
        except (TypeError, ValueError):
            pass

    chunks = []
    total = 0
    deadline = time.monotonic() + CRAWL_TOTAL_TIMEOUT_SECONDS

    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                crawl_log(f"CRAWL SIZE LIMIT REACHED: truncated at {total} bytes")
                break
            if time.monotonic() >= deadline:
                crawl_log(
                    f"CRAWL TOTAL TIMEOUT: exceeded {CRAWL_TOTAL_TIMEOUT_SECONDS}s reading body"
                )
                break
    except Exception:
        # Fall back to whatever was already buffered rather than failing the
        # whole crawl on a mid-stream read error.
        pass
    finally:
        try:
            response.close()
        except Exception:
            pass

    body = b"".join(chunks)[:limit]

    # Prefer the charset the server actually declared in Content-Type. If it
    # declared none, sniff the bytes we buffered -- see _detect_encoding for
    # why response.apparent_encoding must not be touched here.
    try:
        encoding = response.encoding
    except Exception:
        encoding = None

    if not encoding:
        encoding = _detect_encoding(body)

    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, TypeError, ValueError):
        return body.decode("utf-8", errors="replace")


class CachedResponse:
    def __init__(self, url, status_code, text, headers=None):
        self.url = url
        self.status_code = status_code
        self.text = text or ""
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}")


def _pin_context(validated, url):
    """Pin one hop's connection to the IPs validate_public_url actually checked.

    Without this, requests/urllib3 re-resolve the hostname when they open the
    socket, so a hostile DNS server can answer "public" for the safety check and
    "127.0.0.1" (or the cloud metadata address) a few milliseconds later for the
    real connection. The Host header and TLS SNI still use the hostname.

    Degrades to a no-op when no validated addresses are available (e.g. a test
    that stubs out validate_public_url), leaving previous behaviour unchanged.
    """
    ips = tuple(getattr(validated, "ips", ()) or ())
    if not ips:
        return nullcontext()

    hostname = str(validated).strip() if isinstance(validated, str) else ""
    if not hostname:
        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            hostname = ""

    if not hostname:
        return nullcontext()

    return pinned_dns(hostname, ips)


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
        validated = validate_public_url(raw_crawl_url)
    except UnsafeURLError as exc:
        return _blocked_response(raw_crawl_url, str(exc))

    crawl_log(f"CRAWL CACHE MISS: {cache_key}")

    current_url = raw_crawl_url
    current_validated = validated
    redirects_followed = 0

    try:
        while True:
            # The connection is pinned to the addresses that were just
            # validated, for this hop only, so the name cannot be re-resolved
            # to a private address between the check and the connect.
            with _pin_context(current_validated, current_url):
                response = requests.get(
                    current_url,
                    timeout=timeout,
                    headers=headers,
                    allow_redirects=False,
                    verify=verify,
                    stream=True,
                )

            if not (allow_redirects and response.is_redirect):
                break

            next_url = response.headers.get("Location")
            if not next_url:
                break

            # stream=True keeps the redirect hop's connection open; release
            # it before issuing the next request so a redirect chain cannot
            # pin one socket per hop.
            _close_quietly(response)

            if redirects_followed >= DEFAULT_MAX_REDIRECTS:
                return _blocked_response(url, "too many redirects")

            next_url = urljoin(current_url, next_url)

            try:
                next_validated = validate_public_url(next_url)
            except UnsafeURLError as exc:
                return _blocked_response(next_url, str(exc))

            current_url = next_url
            current_validated = next_validated
            redirects_followed += 1

        payload = {
            "url": response.url,
            "status_code": response.status_code,
            # Size-capped read (see _read_capped_text). Previously this was
            # response.text, which buffers the entire body with no ceiling.
            "text": _read_capped_text(response) or "",
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
