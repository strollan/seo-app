"""Single-site SEO report for ACT II contacts (report format v2; v1 checks and thresholds unchanged).

Reuses the URL Compare page scan (app.main.fetch_page_data) and the thresholds Compare already applies
(title 30-65, meta 70-160, one H1, thin content < 100 / light < 300 words, 5+ internal links,
image alt coverage 80% / 50%). Deliberately NOT used: numeric scores, keyword gap, DataForSEO volume,
the OpenAI insight call, competitor search and multi-page crawling.

Every finding is a deterministic check of the fetched page with the observed value as evidence.
A blocked or failed fetch is an incomplete audit with no findings (placeholder text is never audited).

v2 adds (all from the same single fetch): H1/H2 text, indexability (robots meta, X-Robots-Tag, HTTP status),
images missing alt text, an internal-link sample, keyword-placement opportunities built ONLY from phrases that
literally occur on the page (no search volumes, rankings or traffic), a priority per recommendation and an
explicit list of checks this report cannot verify.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ENGINE = "leadmeleads-site-audit"
VERSION = 2
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


HIGH_IMPACT = {"title", "h1", "indexability", "meta_description", "content_depth"}


def priority(check_id, result):
    """Fixed, deterministic priority for a recommendation: high | medium | low ("" for pass/info)."""
    if result == FAIL:
        return "high" if check_id in HIGH_IMPACT else "medium"
    if result == WARN:
        return "medium" if check_id in HIGH_IMPACT | {"canonical", "redirect"} else "low"
    return ""


def finding(check_id, check, result, evidence, recommendation=""):
    return {"id": check_id, "check": check, "result": result, "evidence": _clip(evidence, 400),
            "recommendation": _clip(recommendation, 400), "priority": priority(check_id, result)}


# Checks a single-page fetch cannot verify. Always shown so nothing is implied that was not measured.
NOT_VERIFIED = [
    {"check": "Search rankings", "reason": "Not checked: no search-engine ranking data is requested."},
    {"check": "Search volume and traffic", "reason": "Not checked: no keyword volume or analytics data is requested."},
    {"check": "Other pages of the site", "reason": "Not checked: only this one page is fetched; no crawl."},
    {"check": "robots.txt and XML sitemap", "reason": "Not checked: those files are not fetched."},
    {"check": "Google Business Profile and reviews", "reason": "Not checked: no listing data is requested."},
    {"check": "Backlinks", "reason": "Not checked: no link data is requested."},
    {"check": "Page speed and Core Web Vitals", "reason": "Not checked: no performance test is run."},
    {"check": "Content added by JavaScript", "reason": "Not checked: the raw HTML is read; scripts are not run."},
]

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further get had has have having he her here hers him his
how i if in into is it its just let me more most my no nor not now of off on once only or other our ours out over own same
she should so some such than that the their theirs them then there these they this those through to too under until up us
very was we were what when where which while who whom why will with you your yours yourself click here read learn more home
menu skip content copyright rights reserved privacy policy terms cookie cookies call today contact page site website www com
http https view see all new best great free welcome info email phone us""".split())
WORD = re.compile(r"[a-z0-9][a-z0-9'&-]*")


def _norm(text):
    return " ".join(WORD.findall(str(text or "").lower()))


def _count(phrase, text_norm):
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text_norm))


def keyword_opportunities(page, evidence, limit=6):
    """Phrases repeated in the page's own text/H2s that the title and H1 do not use. Page evidence only:
    every phrase returned occurs verbatim (case-insensitive) on the page. No volumes or rankings."""
    body = _norm(page.get("visible_text") or "")
    h2s = [_norm(h) for h in evidence.get("h2_texts", [])]
    title, meta = _norm(page.get("title")), _norm(page.get("meta_description") if page.get("has_meta") else "")
    h1 = _norm(" ".join(evidence.get("h1_texts") or [page.get("h1") or ""]))
    if not body:
        return []
    candidates = {}
    for source in [body] + h2s:
        words = source.split()
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                if gram[0] in STOP or gram[-1] in STOP or any(w.isdigit() or len(w) < 3 for w in gram):
                    continue
                if sum(w in STOP for w in gram) > 1:
                    continue
                candidates.setdefault(" ".join(gram), None)
    ranked = []
    for phrase in candidates:
        mentions = _count(phrase, body)
        in_h2 = next((h for h in evidence.get("h2_texts", []) if _count(phrase, _norm(h))), "")
        if mentions < 3 and not (in_h2 and mentions >= 2):
            continue
        if _count(phrase, title) or _count(phrase, h1):
            continue
        ranked.append((bool(in_h2), mentions, len(phrase.split()), phrase, in_h2))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    out = []
    for in_h2_flag, mentions, _, phrase, h2_text in ranked:
        if any(phrase in o["phrase"] or o["phrase"] in phrase for o in out):
            continue
        where = f"{mentions} mention{'s' if mentions != 1 else ''} in the page text"
        if h2_text:
            where += f"; used in H2 “{_clip(h2_text, 120)}”"
        out.append({"phrase": phrase, "body_mentions": mentions, "in_h2": in_h2_flag, "in_meta": bool(_count(phrase, meta)),
                    "evidence": _clip(where + "; not in the title tag or H1.", 300),
                    "suggestion": "If this is a core service, consider using it in the title tag or H1."})
        if len(out) >= limit:
            break
    return out


def build_audit(page: dict, requested_url: str, scanned_at: str | None = None) -> dict:
    """Findings for one fetched page. `page` is fetch_page_data() output; nothing is fetched here."""
    scanned_at = scanned_at or datetime.now(timezone.utc).isoformat()
    final_url = str((page or {}).get("final_url") or "")
    base = {"engine": ENGINE, "version": VERSION, "requested_url": requested_url, "final_url": final_url,
            "scanned_at": scanned_at, "findings": [], "summary": {PASS: 0, WARN: 0, FAIL: 0, INFO: 0},
            "evidence": {}, "keyword_opportunities": [], "not_verified": NOT_VERIFIED}
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

    ev = page.get("audit_evidence") if isinstance(page.get("audit_evidence"), dict) else {}
    h1_count, h1 = _int(page, "h1_count"), str(page.get("h1") or "").strip()
    if h1_count == 0:
        found.append(finding("h1", "H1 heading", FAIL, "No H1 heading found.", "Add one H1 heading that states the main service and location."))
    elif h1_count > 1:
        found.append(finding("h1", "H1 heading", WARN, f"{h1_count} H1 headings; first: “{h1}”", "Use a single H1 heading for the page's main topic."))
    else:
        found.append(finding("h1", "H1 heading", PASS, f"“{h1}”"))

    h2 = _int(page, "h2_count")
    h2_list = [t for t in ev.get("h2_texts", []) if t]
    h2_note = (": " + "; ".join(f"“{t}”" for t in h2_list[:6]) + (" …" if len(h2_list) > 6 else "")) if h2_list else ""
    found.append(finding("h2", "Subheadings (H2)", PASS if h2 else WARN, f"{h2} H2 subheading{'s' if h2 != 1 else ''}{h2_note}",
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
        missing = ev.get("images_missing_alt") or []
        missing_note = ("; missing on: " + ", ".join(missing[:4]) + (" …" if len(missing) > 4 else "")) if missing and result != PASS else ""
        found.append(finding("image_alt", "Image alt text", result, f"{min(alts, images)} of {images} images have alt text ({round(ratio * 100)}%){missing_note}",
                             "" if result == PASS else "Add descriptive alt text to the remaining images."))

    canonical = str(page.get("canonical") or "").strip()
    if not canonical or canonical == "None":
        found.append(finding("canonical", "Canonical tag", WARN, "No canonical tag found.", "Add a canonical tag pointing to this page's preferred URL."))
    elif _host(canonical) and _host(canonical) != _host(final_url or requested_url):
        found.append(finding("canonical", "Canonical tag", WARN, f"Canonical points to another host: {canonical}", "Point the canonical tag at this website unless the duplicate is intentional."))
    else:
        found.append(finding("canonical", "Canonical tag", PASS, canonical))

    if ev:
        robots, header = str(ev.get("robots_meta") or ""), str(ev.get("x_robots_tag") or "")
        status = ev.get("http_status") or 0
        blocked = []
        if "noindex" in robots.lower():
            blocked.append(f"robots meta “{robots}”")
        if "noindex" in header.lower():
            blocked.append(f"X-Robots-Tag header “{header}”")
        if blocked:
            found.append(finding("indexability", "Indexability", FAIL, f"HTTP {status}; noindex set by " + " and ".join(blocked),
                                 "Remove the noindex directive if this page should appear in search results."))
        else:
            found.append(finding("indexability", "Indexability", PASS,
                                 f"HTTP {status}; no noindex in the robots meta tag ({robots or 'none'}) or X-Robots-Tag header ({header or 'none'})"))
    else:
        found.append(finding("indexability", "Indexability", INFO, "Not verified: robots directives were not captured for this scan."))

    summary = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
    for item in found:
        summary[item["result"]] += 1
    evidence = {
        "title": _clip(title, 300), "meta_description": _clip(meta if page.get("has_meta") else "", 400),
        "h1": [_clip(t, 200) for t in (ev.get("h1_texts") or ([h1] if h1 else []))][:5],
        "h2": [_clip(t, 200) for t in h2_list][:15], "word_count": words, "internal_link_count": links,
        "internal_links": [{"href": _clip(l.get("href"), 300), "text": _clip(l.get("text"), 120)}
                           for l in (ev.get("internal_links") or []) if isinstance(l, dict)][:12],
        "image_count": images, "images_with_alt": min(alts, images),
        "images_missing_alt": [_clip(x, 300) for x in (ev.get("images_missing_alt") or [])][:10],
        "canonical": "" if canonical in {"", "None"} else _clip(canonical, 300),
        "robots_meta": _clip(ev.get("robots_meta"), 200), "x_robots_tag": _clip(ev.get("x_robots_tag"), 200),
        "http_status": int(ev.get("http_status") or 0), "lang": _clip(ev.get("lang"), 20),
    }
    return base | {"status": "complete", "findings": found, "summary": summary, "evidence": evidence,
                   "keyword_opportunities": keyword_opportunities(page, ev)}


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
    from agents.business_website import is_google_host
    if is_google_host(url):
        # A Google redirect/Maps page is not the business website; auditing it
        # would report on Google, not the prospect.
        return 422, {"ok": False, "error": "The website address is a Google link, not the business website."}
    if not limiter.admit(connector):
        return 429, {"ok": False, "error": "Audit limit reached. Try again later."}
    audit = build_audit(fetch(url), url)
    limiter.store(connector, body["request_id"], audit)
    return 200, {"ok": True, "request_id": body["request_id"], "audit": audit, "cached": False}
