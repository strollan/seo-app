"""
Market Agent

Detects local market signals from page data.
Built first for Long Island launch markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any
import re


@dataclass(frozen=True)
class MarketResult:
    market: str
    confidence: float
    reason: str
    county: str | None = None
    is_long_island: bool = False


LONG_ISLAND_TOWNS = {
    "northport": "Suffolk County",
    "patchogue": "Suffolk County",
    "yaphank": "Suffolk County",
    "selden": "Suffolk County",
    "smithtown": "Suffolk County",
    "huntington": "Suffolk County",
    "commack": "Suffolk County",
    "babylon": "Suffolk County",
    "islandia": "Suffolk County",
    "ronkonkoma": "Suffolk County",
    "islip": "Suffolk County",
    "bay shore": "Suffolk County",
    "riverhead": "Suffolk County",
    "southampton": "Suffolk County",
    "east hampton": "Suffolk County",

    "hicksville": "Nassau County",
    "bellmore": "Nassau County",
    "east meadow": "Nassau County",
    "mineola": "Nassau County",
    "merrick": "Nassau County",
    "levittown": "Nassau County",
    "westbury": "Nassau County",
    "garden city": "Nassau County",
    "long beach": "Nassau County",
}

LONG_ISLAND_AREA_CODES = {
    "516": "Nassau County",
    "631": "Suffolk County",
    "934": "Suffolk County",
}


def _page_blob(*pages: Dict[str, Any] | None) -> str:
    parts = []

    for page in pages:
        page = page or {}
        parts.extend([
            page.get("url", ""),
            page.get("domain", ""),
            page.get("title", ""),
            page.get("meta", ""),
            page.get("description", ""),
            page.get("h1", ""),
            page.get("text", ""),
            " ".join(page.get("headings", []) or []),
            " ".join(page.get("keywords", []) or []),
        ])

    return " ".join(str(x or "") for x in parts).lower()


def _find_area_codes(blob: str) -> list[str]:
    return re.findall(r'(?:\(|\b)(516|631|934)(?:\)|[\s.\-])', blob)


def detect_market(site: Dict[str, Any] | None, competitor: Dict[str, Any] | None = None) -> MarketResult:
    blob = _page_blob(site, competitor)

    town_hits = [town for town in LONG_ISLAND_TOWNS if town in blob]

    if town_hits:
        first_town = town_hits[0]
        county = LONG_ISLAND_TOWNS[first_town]

        # If the page says Long Island, use Long Island as the broader market.
        if "long island" in blob:
            return MarketResult(
                market="Long Island",
                confidence=0.93,
                reason=f"Matched Long Island plus town signal: {first_town}",
                county=county,
                is_long_island=True,
            )

        return MarketResult(
            market=first_town.title(),
            confidence=0.88,
            reason=f"Matched town signal: {first_town}",
            county=county,
            is_long_island=True,
        )

    if "suffolk county" in blob or "suffolk" in blob:
        return MarketResult(
            market="Suffolk County",
            confidence=0.92,
            reason="Matched Suffolk County signal.",
            county="Suffolk County",
            is_long_island=True,
        )

    if "nassau county" in blob or "nassau" in blob:
        return MarketResult(
            market="Nassau County",
            confidence=0.92,
            reason="Matched Nassau County signal.",
            county="Nassau County",
            is_long_island=True,
        )

    if "long island" in blob:
        return MarketResult(
            market="Long Island",
            confidence=0.90,
            reason="Matched Long Island signal.",
            county=None,
            is_long_island=True,
        )

    area_codes = _find_area_codes(blob)
    if area_codes:
        area_code = area_codes[0]
        county = LONG_ISLAND_AREA_CODES.get(area_code)
        return MarketResult(
            market="Long Island",
            confidence=0.82,
            reason=f"Matched Long Island area code: {area_code}",
            county=county,
            is_long_island=True,
        )

    return MarketResult(
        market="Long Island",
        confidence=0.45,
        reason="No strong market signal found; using launch-market default.",
        county=None,
        is_long_island=True,
    )


def market_result_to_dict(result: MarketResult) -> dict:
    return {
        "market": result.market,
        "confidence": result.confidence,
        "reason": result.reason,
        "county": result.county,
        "is_long_island": result.is_long_island,
    }
