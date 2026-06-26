"""
LeadBot Query Tracking Agent

Purpose:
- Keep the user-facing keyword clean.
- Track the exact search phrase/query variant that found each lead.
- Preserve the full query group for analytics.

Fields added/normalized:
- keyword: clean base keyword only
- base_keyword: clean base keyword only
- query_used: exact phrase that found the lead when available
- query_group: all phrase variants used for the run
- query_variant_number: phrase index inside query_group
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


BAD_VALUES = {
    "",
    "leadbot",
    "lead bot",
    "none",
    "null",
    "nan",
    "not found",
    "unknown",
    "-",
    "—",
}


def clean_text(value: Any) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def is_bad_value(value: Any) -> bool:
    return clean_text(value).lower() in BAD_VALUES


def split_query_group(value: Any) -> List[str]:
    text = clean_text(value)
    if not text:
        return []

    parts = [clean_text(part) for part in text.split("|")]
    return [part for part in parts if part]


def title_case_search(value: Any) -> str:
    value = clean_text(value).replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in value.split())


def strip_market_from_query(query: str, market: str) -> str:
    query = clean_text(query)
    market = clean_text(market)

    if not query:
        return ""

    if market:
        query = re.sub(re.escape(market), " ", query, flags=re.I)

        for part in market.split():
            if len(part) >= 2:
                query = re.sub(r"\b" + re.escape(part) + r"\b", " ", query, flags=re.I)

    query = re.sub(r"\bnear\b", " ", query, flags=re.I)
    query = re.sub(r"\bbest\b", " ", query, flags=re.I)
    query = re.sub(r"\bin\b", " ", query, flags=re.I)
    query = re.sub(r"\bof\b", " ", query, flags=re.I)

    query = re.sub(r"\s+", " ", query).strip(" -|,")
    return query


def derive_market_from_filename(filename: Any) -> str:
    name = Path(str(filename or "")).name
    stem = name.replace(".csv", "")

    if stem.startswith("leads_"):
        stem = stem[len("leads_"):]

    stem = re.sub(r"_enriched_\d{8}_\d{6}$", "", stem)
    stem = re.sub(r"_\d{8}_\d{6}$", "", stem)

    parts = [part for part in stem.split("_") if part]

    if parts and parts[0].lower() == "leadbot":
        parts = parts[1:]

    if len(parts) >= 3:
        return title_case_search(" ".join(parts[-3:]))

    if len(parts) >= 2:
        return title_case_search(" ".join(parts[-2:]))

    return ""


def clean_base_keyword(raw_keyword: Any, market: Any = "", industry: Any = "") -> str:
    raw_keyword = clean_text(raw_keyword)
    market = clean_text(market)
    industry = clean_text(industry)

    queries = split_query_group(raw_keyword)

    if queries:
        first_query = queries[0]
    else:
        first_query = raw_keyword

    base = strip_market_from_query(first_query, market)

    if not base and industry and not is_bad_value(industry):
        base = strip_market_from_query(industry, market)

    if not base and raw_keyword and "|" not in raw_keyword and len(raw_keyword) <= 45:
        base = strip_market_from_query(raw_keyword, market)

    return clean_text(base)


def query_variant_number(query_used: Any, query_group: Any) -> str:
    used = clean_text(query_used).lower()
    group = split_query_group(query_group)

    if not used or not group:
        return ""

    for index, query in enumerate(group, start=1):
        if clean_text(query).lower() == used:
            return str(index)

    return ""


def normalize_query_tracking(
    row: Dict[str, Any],
    *,
    selected_name: str = "",
    industry: Any = None,
    market: Any = None,
    base_keyword: Any = None,
    query_used: Any = None,
    query_group: Any = None,
) -> Dict[str, Any]:
    """
    Return a copy of row with clean query tracking fields.

    This is intentionally conservative:
    - If exact query_used is already present, preserve it.
    - If not present, avoid inventing a fake exact query.
    - keyword becomes clean base keyword for dashboard/export readability.
    """
    out = dict(row or {})

    raw_industry = clean_text(industry if industry is not None else out.get("industry", ""))
    raw_market = clean_text(market if market is not None else out.get("market", ""))
    raw_keyword = clean_text(out.get("keyword", ""))

    if is_bad_value(raw_market):
        raw_market = derive_market_from_filename(selected_name)

    cleaned_base = clean_text(base_keyword)
    if not cleaned_base:
        cleaned_base = clean_base_keyword(raw_keyword, raw_market, raw_industry)

    if not cleaned_base and raw_industry and not is_bad_value(raw_industry):
        cleaned_base = clean_base_keyword(raw_industry, raw_market, "")

    cleaned_industry = raw_industry
    if is_bad_value(cleaned_industry):
        cleaned_industry = cleaned_base

    cleaned_group = clean_text(query_group)
    if not cleaned_group:
        if "|" in raw_keyword:
            cleaned_group = raw_keyword
        else:
            cleaned_group = clean_text(out.get("query_group", ""))

    cleaned_used = clean_text(query_used)
    if not cleaned_used:
        cleaned_used = clean_text(
            out.get("query_used")
            or out.get("search_query")
            or out.get("source_query")
            or out.get("keyword_used")
            or out.get("serp_query")
            or ""
        )

    # If there is only one query in the group, exact query is safe to infer.
    group_parts = split_query_group(cleaned_group)
    if not cleaned_used and len(group_parts) == 1:
        cleaned_used = group_parts[0]

    out["industry"] = cleaned_industry or ""
    out["market"] = raw_market or out.get("market", "")
    out["keyword"] = cleaned_base or raw_keyword
    out["base_keyword"] = cleaned_base or out.get("base_keyword", "")
    out["query_group"] = cleaned_group or out.get("query_group", "")

    if cleaned_used:
        out["query_used"] = cleaned_used
    else:
        out.setdefault("query_used", "")

    out["query_variant_number"] = (
        clean_text(out.get("query_variant_number"))
        or query_variant_number(out.get("query_used", ""), out.get("query_group", ""))
    )

    return out


def normalize_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    selected_name: str = "",
    industry: Any = None,
    market: Any = None,
    base_keyword: Any = None,
    query_group: Any = None,
) -> List[Dict[str, Any]]:
    return [
        normalize_query_tracking(
            row,
            selected_name=selected_name,
            industry=industry,
            market=market,
            base_keyword=base_keyword,
            query_group=query_group,
        )
        for row in rows
    ]
