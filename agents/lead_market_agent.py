"""
LeadBot market parser.

Keeps the original market string but adds city/state fields for better
future search, exports, saved runs, filtering, and reporting.
"""

from __future__ import annotations

import re


STATE_ALIASES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}

STATE_CODES = set(STATE_ALIASES.values())


def clean_market_piece(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ,")


def normalize_state(value: str) -> str:
    raw = clean_market_piece(value)
    if not raw:
        return ""

    upper = raw.upper()
    if upper in STATE_CODES:
        return upper

    return STATE_ALIASES.get(raw.lower(), "")


def parse_market_parts(market: str) -> dict:
    raw = clean_market_piece(market)
    if not raw:
        return {"market": "", "city": "", "state": ""}

    # Best format: "El Cajon, CA"
    if "," in raw:
        parts = [clean_market_piece(p) for p in raw.split(",") if clean_market_piece(p)]
        city = parts[0] if parts else ""
        state = normalize_state(parts[-1]) if len(parts) >= 2 else ""
        return {
            "market": raw,
            "city": city,
            "state": state,
        }

    # Also support: "El Cajon CA"
    tokens = raw.split()
    if len(tokens) >= 2:
        possible_state = normalize_state(tokens[-1])
        if possible_state:
            city = clean_market_piece(" ".join(tokens[:-1]))
            return {
                "market": raw,
                "city": city,
                "state": possible_state,
            }

    # County / region / vague markets stay as market only.
    return {
        "market": raw,
        "city": "",
        "state": "",
    }


def apply_market_parts(row: dict, market: str = "", city: str = "", state: str = "") -> dict:
    if not isinstance(row, dict):
        return row

    base_market = clean_market_piece(row.get("market") or market)
    parsed = parse_market_parts(base_market)

    row.setdefault("market", parsed.get("market") or base_market)
    row.setdefault("city", clean_market_piece(row.get("city") or city or parsed.get("city")))
    row.setdefault("state", normalize_state(row.get("state") or state or parsed.get("state")))

    return row
