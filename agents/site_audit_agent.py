"""Single-page SEO audit for ACT II contacts (v1).

Reuses the URL Compare page scan (app.main.fetch_page_data) and the thresholds Compare already applies
(title 30-65, meta 70-160, one H1, thin content < 100 / light < 300 words, 5+ internal links,
image alt coverage 80% / 50%). Deliberately NOT used: numeric scores, keyword gap, DataForSEO volume,
the OpenAI insight call, competitor search and multi-page crawling.

Every finding is a deterministic check of the fetched page with the observed value as evidence.
A blocked or failed fetch is an incomplete audit with no findings (placeholder text is never audited).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

ENGINE = "leadmeleads-site-audit"
VERSION = 1
REQUEST_ID = re.compile(r"[a-f0-9]{32}")
PLACEHOLDERS = {"error", "no meta", "no meta description", "blocked by site", "unable to fetch"}
PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"


def _clip(value, limit=300):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(page, key):
    try:
        return max(int(page.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _host(url):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def finding(check_id, check, result, evidence, recommendation=""):
    return {"id": check_id, "check": check, "result": result, "evidence": _clip(evidence, 400),
            "recommendation": _clip(recommendation, 400)}


def build_audit(page: dict, requested_url: str, scanned_at: str | None = None) -> dict:
    """Findings for one fetched page. `page` is fetch_page_data() output; nothing is fetched here."""
    scanned_at = scanned_at or datetime.now(UTC).isoformat()
    final_url = str((page or {}).get("final_url") or "")
    base = {"engine": ENGINE, "version": VERSION, "requested_url": requested_url, "final_url": final_url,
            "scanned_at": scanned_at, "findings": [], "summary": {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}}
    if not isinstance(page, dict) or page.get("error"):
        status = str((page or {}).get("title_status") or "")
        reason = "The website blocked the scan (HTTP 403)." if status == "Blocked" else "The page could not be fetched."
        return base | {"status": "incomplete", "incomplete_reason": reason}

    found = []
    if final_url and final_url.rstrip("/") != requested_url.rstrip("/"):
        same = _host(final_url) == _host(requested_url)
        found.append(finding("redirect", "Redirect", INFO if same else WARN,
                             f"{requested_url} redirected to {final_url}",
                             "" if same else "The address redirects to a different website; confirm this is the client's current site."))

    title = str(page.get("title") or "").strip()
    if not title or title.lower() in PLACEHOLDERS:
        found.append(finding("title", "Title tag", FAIL, "No title tag found.", "Add a 30–65 character title tag that names the main service and location."))
    else:
        n = len(title)
        ok = 30 <= n <= 65
        found.append(finding("title", "Title tag", PASS if ok else WARN, f"“{title}” ({n} characters)",
                             "" if ok else ("Expand the title tag to 30–65 characters." if n < 30 else "Shorten the title tag to 30–65 characters so it is not cut off in search results.")))

    meta = str(page.get("meta_description") or "").strip()
    if not page.get("has_meta") or not meta or meta.lower() in PLACEHOLDERS:
        found.append(finding("meta_description", "Meta description", FAIL, "No meta description found.",
                             "Add a 70–160 character meta description summarizing the service, location and a call to action."))
    else:
        n = len(meta)
        ok = 70 <= n <= 160
        found.append(finding("meta_description", "Meta description", PASS if ok else WARN, f"“{meta}” ({n} characters)",
                             "" if ok else ("Expand the meta description to 70–160 characters." if n < 70 else "Shorten the meta description to 70–160 characters.")))

    h1_count, h1 = _int(page, "h1_count"), str(page.get("h1") or "").strip()
    if h1_count == 0:
        found.append(finding("h1", "H1 heading", FAIL, "No H1 heading found.", "Add one H1 heading that states the main service and location."))
    elif h1_count > 1:
        found.append(finding("h1", "H1 heading", WARN, f"{h1_count} H1 headings; first: “{h1}”", "Use a single H1 heading for the page's main topic."))
    else:
        found.append(finding("h1", "H1 heading", PASS, f"“{h1}”"))

    h2 = _int(page, "h2_count")
    found.append(finding("h2", "Subheadings (H2)", PASS if h2 else WARN, f"{h2} H2 subheading{'s' if h2 != 1 else ''}",
                         "" if h2 else "Add H2 subheadings that break the page into services, areas served and FAQs."))

    words = _int(page, "word_count")
    if words < 100:
        found.append(finding("content_depth", "Content depth", FAIL, f"{words} words of visible text", "The page is thin; add substantive service, area and trust content (300+ words)."))
    elif words < 300:
        found.append(finding("content_depth", "Content depth", WARN, f"{words} words of visible text", "Expand the page past 300 words with specific service and location detail."))
    else:
        found.append(finding("content_depth", "Content depth", PASS, f"{words} words of visible text"))

    links = _int(page, "internal_link_count")
    found.append(finding("internal_links", "Internal links", PASS if links >= 5 else WARN, f"{links} internal link{'s' if links != 1 else ''}",
                         "" if links >= 5 else "Link to at least 5 relevant internal pages (services, service areas, contact)."))

    images, alts = _int(page, "image_count"), _int(page, "alt_count")
    if images == 0:
        found.append(finding("image_alt", "Image alt text", INFO, "No images found on the page."))
    else:
        ratio = min(alts, images) / images
        result = PASS if ratio >= 0.8 else WARN if ratio >= 0.5 else FAIL
        found.append(finding("image_alt", "Image alt text", result, f"{min(alts, images)} of {images} images have alt text ({round(ratio * 100)}%)",
                             "" if result == PASS else "Add descriptive alt text to the remaining images."))

    canonical = str(page.get("canonical") or "").strip()
    if not canonical or canonical == "None":
        found.append(finding("canonical", "Canonical tag", WARN, "No canonical tag found.", "Add a canonical tag pointing to this page's preferred URL."))
    elif _host(canonical) and _host(canonical) != _host(final_url or requested_url):
        found.append(finding("canonical", "Canonical tag", WARN, f"Canonical points to another host: {canonical}", "Point the canonical tag at this website unless the duplicate is intentional."))
    else:
        found.append(finding("canonical", "Canonical tag", PASS, canonical))

    summary = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
    for item in found:
        summary[item["result"]] += 1
    return base | {"status": "complete", "findings": found, "summary": summary}


class AuditLimiter:
    """In-process per-connector limits plus a short idempotency cache keyed by request id (no rescans on retry)."""

    def __init__(self, per_hour=10, per_day=30, cache_seconds=86400, clock=time.time):
        self.per_hour, self.per_day, self.cache_seconds, self.clock = per_hour, per_day, cache_seconds, clock
        self.lock = threading.Lock()
        self.calls: dict[str, list[float]] = {}
        self.results: dict[tuple[str, str], tuple[float, dict]] = {}

    def cached(self, connector_id, request_id):
        with self.lock:
            hit = self.results.get((connector_id, request_id))
            return hit[1] if hit and self.clock() - hit[0] < self.cache_seconds else None

    def admit(self, connector_id):
        now = self.clock()
        with self.lock:
            recent = [t for t in self.calls.get(connector_id, []) if now - t < 86400]
            if len(recent) >= self.per_day or sum(now - t < 3600 for t in recent) >= self.per_hour:
                self.calls[connector_id] = recent
                return False
            recent.append(now)
            self.calls[connector_id] = recent
            return True

    def store(self, connector_id, request_id, audit):
        with self.lock:
            now = self.clock()
            self.results = {k: v for k, v in self.results.items() if now - v[0] < self.cache_seconds}
            self.results[(connector_id, request_id)] = (now, audit)


LIMITER = AuditLimiter()


def run_site_audit(binding, body, *, fetch, validate, sanitize, limiter=LIMITER):
    """Connector-authenticated audit of ONE page. Returns (status_code, json_body). Free: one page fetch."""
    if not isinstance(body, dict) or set(body) != {"request_id", "url"} or not isinstance(body["request_id"], str) \
            or not REQUEST_ID.fullmatch(body["request_id"]) or not isinstance(body["url"], str) or not 0 < len(body["url"]) <= 2000:
        return 422, {"ok": False, "error": "Invalid audit request."}
    connector = binding["connector_id"]
    previous = limiter.cached(connector, body["request_id"])
    if previous is not None:
        return 200, {"ok": True, "request_id": body["request_id"], "audit": previous, "cached": True}
    url = sanitize(body["url"].strip())
    try:
        validate(url)
    except Exception:
        return 422, {"ok": False, "error": "The website address is not a public http(s) URL."}
    if urlsplit(url).scheme not in {"http", "https"}:
        return 422, {"ok": False, "error": "The website address is not a public http(s) URL."}
    if not limiter.admit(connector):
        return 429, {"ok": False, "error": "Audit limit reached. Try again later."}
    audit = build_audit(fetch(url), url)
    limiter.store(connector, body["request_id"], audit)
    return 200, {"ok": True, "request_id": body["request_id"], "audit": audit, "cached": False}
