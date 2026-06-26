"""
Competitor Quality Agent

Evaluates whether a competitor page is strong enough to use for SEO comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any, List


@dataclass(frozen=True)
class CompetitorQualityResult:
    status: str
    score: int
    issues: List[str] = field(default_factory=list)
    recommendation: str = ""


BAD_COMPETITOR_DOMAINS = [
    "yelp.com",
    "angi.com",
    "angieslist.com",
    "homeadvisor.com",
    "bbb.org",
    "thumbtack.com",
    "instagram.com",
    "facebook.com",
    "porch.com",
]


def _as_int(value, default=0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


def evaluate_competitor_quality(
    competitor: Dict[str, Any] | None,
    client_industry: str | None = None,
    competitor_industry: str | None = None,
    client_market: str | None = None,
    competitor_market: str | None = None,
) -> CompetitorQualityResult:
    competitor = competitor or {}

    url = str(competitor.get("url") or competitor.get("domain") or "").lower()
    title = str(competitor.get("title") or "").strip()
    meta = str(competitor.get("meta") or competitor.get("meta_description") or "").strip()
    h1 = str(competitor.get("h1") or "").strip()

    word_count = _as_int(competitor.get("word_count"), 0)
    h1_count = _as_int(competitor.get("h1_count"), 0)
    h2_count = _as_int(competitor.get("h2_count"), 0)
    internal_links = _as_int(competitor.get("internal_link_count"), 0)

    issues = []
    score = 100

    if any(domain in url for domain in BAD_COMPETITOR_DOMAINS):
        issues.append("Competitor appears to be a directory, marketplace, social profile, or citation site.")
        score -= 45

    if "blocked by site" in title.lower() or "blocked by site" in meta.lower():
        issues.append("Competitor page appears blocked or unreadable.")
        score -= 50

    if word_count <= 100:
        issues.append("Competitor page has very little crawlable text.")
        score -= 30
    elif word_count < 300:
        issues.append("Competitor page is thin and may not be ideal for keyword comparison.")
        score -= 15

    if not meta or meta.lower() in {"no meta description", "blocked by site"}:
        issues.append("Competitor is missing a usable meta description.")
        score -= 10

    if not h1 or h1.lower() in {"none", "blocked by site"} or h1_count == 0:
        issues.append("Competitor is missing a clear H1.")
        score -= 10

    if h2_count == 0:
        issues.append("Competitor has no detected H2 structure.")
        score -= 5

    if internal_links < 3:
        issues.append("Competitor has very few internal links.")
        score -= 5

    if client_industry and competitor_industry and client_industry != competitor_industry:
        issues.append(f"Competitor industry appears different: client={client_industry}, competitor={competitor_industry}.")
        score -= 35

    if client_market and competitor_market:
        client_market_l = client_market.lower()
        competitor_market_l = competitor_market.lower()

        same_long_island = (
            client_market_l in {"long island", "suffolk county", "nassau county"}
            and competitor_market_l in {"long island", "suffolk county", "nassau county"}
        )

        if client_market_l != competitor_market_l and not same_long_island:
            issues.append(f"Competitor market may differ: client={client_market}, competitor={competitor_market}.")
            score -= 15

    score = max(0, min(100, score))

    if score >= 80:
        status = "strong"
        recommendation = "Competitor is strong enough for normal comparison."
    elif score >= 55:
        status = "usable_with_caution"
        recommendation = "Competitor can be used, but recommendations should be treated with caution."
    else:
        status = "weak"
        recommendation = "Use a stronger direct competitor service/location page before making final keyword decisions."

    return CompetitorQualityResult(
        status=status,
        score=score,
        issues=issues,
        recommendation=recommendation,
    )


def competitor_quality_to_dict(result: CompetitorQualityResult) -> dict:
    return {
        "status": result.status,
        "score": result.score,
        "issues": result.issues,
        "recommendation": result.recommendation,
    }
