"""
Industry Agent

Determines the most likely business category from crawled page data.
This keeps roofing, plumbing, cesspool/septic, painting, and other industries
from leaking keywords into each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any


@dataclass(frozen=True)
class IndustryResult:
    industry: str
    confidence: float
    reason: str


INDUSTRY_SIGNALS = {
    "cesspool": [
        "cesspool",
        "septic",
        "septic tank",
        "sewer and drain",
        "sewer & drain",
        "cesspool pumping",
        "cesspool cleaning",
        "cesspool repair",
        "qualitycesspool",
        "jwcesspool",
    ],
    "roofing": [
        "roofing",
        "roofer",
        "roofers",
        "roof repair",
        "roof replacement",
        "roof inspection",
        "commercial roofing",
        "residential roofing",
        "flat roof",
        "longislandroofing",
        "liroofing",
    ],
    "plumbing": [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "water heaters",
        "pipe repair",
        "leak repair",
        "emergency plumber",
        "sewer drain",
    ],
    "painting": [
        "painting",
        "painter",
        "painters",
        "house painters",
        "interior painting",
        "exterior painting",
        "commercial painting",
        "residential painting",
        "cabinet painting",
    ],
    "seo": [
        "seo",
        "search engine optimization",
        "digital marketing",
        "technical seo",
        "local seo",
        "google ads",
        "ppc",
        "content marketing",
    ],
}


def _page_blob(page: Dict[str, Any] | None) -> str:
    page = page or {}

    parts = [
        page.get("url", ""),
        page.get("domain", ""),
        page.get("title", ""),
        page.get("meta", ""),
        page.get("description", ""),
        page.get("h1", ""),
        page.get("text", ""),
        " ".join(page.get("headings", []) or []),
        " ".join(page.get("keywords", []) or []),
    ]

    return " ".join(str(x or "") for x in parts).lower()


def detect_industry(site: Dict[str, Any] | None, competitor: Dict[str, Any] | None = None) -> IndustryResult:
    """
    Returns the most likely industry.

    Important priority:
    - Cesspool/septic wins over generic plumbing when both appear.
    - Roofing wins over plumbing when roof terms appear.
    """

    blob = (_page_blob(site) + " " + _page_blob(competitor)).lower()

    scores = {}
    matched_terms = {}

    for industry, terms in INDUSTRY_SIGNALS.items():
        matches = [term for term in terms if term in blob]
        scores[industry] = len(matches)
        matched_terms[industry] = matches

    # Strong priority guards.
    if scores.get("cesspool", 0) > 0:
        return IndustryResult(
            industry="cesspool",
            confidence=min(0.93, 0.65 + scores["cesspool"] * 0.05),
            reason="Matched cesspool/septic terms: " + ", ".join(matched_terms["cesspool"][:5]),
        )

    if scores.get("roofing", 0) > 0:
        return IndustryResult(
            industry="roofing",
            confidence=min(0.93, 0.65 + scores["roofing"] * 0.05),
            reason="Matched roofing terms: " + ", ".join(matched_terms["roofing"][:5]),
        )

    best = max(scores, key=scores.get)
    best_score = scores.get(best, 0)

    if best_score <= 0:
        return IndustryResult(
            industry="local_service",
            confidence=0.35,
            reason="No strong industry match found.",
        )

    return IndustryResult(
        industry=best,
        confidence=min(0.90, 0.55 + best_score * 0.06),
        reason=f"Matched {best} terms: " + ", ".join(matched_terms[best][:5]),
    )
