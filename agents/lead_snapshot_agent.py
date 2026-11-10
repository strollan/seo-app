"""Compact "Lead snapshot" panel for Lead Finder cards (display only).

Reuses saved fields only: no lookups, no paid calls.
- Lead score: the saved final_lead_score (else score), out of 100.
- Google organic position: only for organic results with a numeric position;
  results page = ceil(position / 10). Google Maps and Google Places positions
  are never shown as organic rank.
- Google Maps position: shown separately, for Maps listings only.
- Website: the recorded URL as-is (never invented or substituted). "Verified"
  only when the Maps website check confirmed it; otherwise "Listed but not
  verified". Google search/redirect URLs are not business websites.

app/main.py's live page mirrors this in JS (leadSnapshotHtml); keep the two in step.
"""

from __future__ import annotations

import html
from typing import Any, Dict, Optional

NOT_ORGANIC_PAGES = {"google maps", "google places"}
NOT_ORGANIC_SOURCES = {"dataforseo_maps", "google_places", "google_maps"}


def _int(value: Any) -> Optional[int]:
    try:
        number = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return None
    return number


def is_maps_row(row: Dict[str, Any]) -> bool:
    return "website_status" in row and ("place_id" in row or "cid" in row) or \
        str(row.get("source") or "").strip().lower() == "dataforseo_maps" or \
        str(row.get("discovery_source") or "").strip().lower() == "google_maps"


def lead_score(row: Dict[str, Any]) -> Optional[int]:
    for key in ("final_lead_score", "score"):
        number = _int(row.get(key))
        if number is not None:
            return max(0, min(100, number))
    return None


def organic_position(row: Dict[str, Any]) -> Optional[int]:
    """Verified organic rank, or None when it was not measured."""
    if is_maps_row(row):
        return None
    if str(row.get("source") or "").strip().lower() in NOT_ORGANIC_SOURCES:
        return None
    if str(row.get("serp_page") or "").strip().lower() in NOT_ORGANIC_PAGES:
        return None
    position = _int(row.get("serp_position"))
    return position if position and position >= 1 else None


def results_page(position: int) -> int:
    return (position - 1) // 10 + 1


def maps_position(row: Dict[str, Any]) -> Optional[int]:
    if not is_maps_row(row):
        return None
    position = _int(row.get("maps_position"))
    return position if position and position >= 1 else None


def website(row: Dict[str, Any]) -> Dict[str, Any]:
    """{"url": recorded URL or "", "link": True if safe to link, "verified": bool}."""
    from agents.business_website import _http_url, is_google_host

    url = str(row.get("url") or row.get("website") or "").strip()
    if not url or url.lower() in {"not found", "none", "null", "nan", "missing"} or is_google_host(url):
        return {"url": "", "link": False, "verified": False}
    verified = is_maps_row(row) and str(row.get("website_verified") or "").strip().lower() == "true"
    return {"url": url, "link": bool(_http_url(url)), "verified": verified}


def snapshot_html(row: Dict[str, Any]) -> str:
    esc = html.escape
    parts = ['<aside class="lead-snapshot" aria-label="Lead snapshot">']

    score = lead_score(row)
    if score is None:
        parts.append('<div class="ls-row"><span class="ls-label">Lead score</span>'
                     '<span class="ls-value ls-muted">Not scored</span></div>')
    else:
        parts.append(
            '<div class="ls-row ls-score"><span class="ls-label">Lead score</span>'
            f'<span class="ls-value"><b>{score}</b> / 100</span>'
            f'<span class="ls-bar" role="img" aria-label="Lead score {score} out of 100">'
            f'<span style="width:{score}%"></span></span></div>'
        )

    position = organic_position(row)
    if position:
        organic = f"#{position} · Results page {results_page(position)}"
        parts.append('<div class="ls-row"><span class="ls-label">Google organic position</span>'
                     f'<span class="ls-value">{esc(organic)}</span></div>')
    else:
        parts.append('<div class="ls-row"><span class="ls-label">Google organic position</span>'
                     '<span class="ls-value ls-muted">Organic rank not checked.</span></div>')

    maps_rank = maps_position(row)
    if maps_rank:
        parts.append('<div class="ls-row"><span class="ls-label">Google Maps position</span>'
                     f'<span class="ls-value">#{maps_rank}</span></div>')

    site = website(row)
    if site["url"]:
        url_html = (
            f'<a href="{esc(site["url"], quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{esc(site["url"])}</a>' if site["link"] else esc(site["url"])
        )
        tag = ('<span class="ls-tag ls-verified">Verified</span>' if site["verified"]
               else '<span class="ls-tag ls-unverified">Listed but not verified</span>')
        parts.append('<div class="ls-row ls-website"><span class="ls-label">Website</span>'
                     f'<span class="ls-value">Yes · {url_html} {tag}</span></div>')
    else:
        parts.append('<div class="ls-row ls-website"><span class="ls-label">Website</span>'
                     '<span class="ls-value">No — No website listed</span></div>')

    parts.append("</aside>")
    return "".join(parts)
