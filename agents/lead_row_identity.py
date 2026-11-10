"""Which export rows does a per-lead action (delete / save details / address) target?

Organic exports have one row per website, so the existing routes match rows by
domain. Google Maps exports (agents/lead_maps_discovery_agent.py) do not:
several "No Website" businesses all have an empty domain, and the locations of
a chain share one website. On a Maps export, a domain must therefore never
select rows. A per-lead action has to name the listing's stable Google ID
(place_id, else CID), and exactly that listing is affected.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def is_maps_export(fieldnames: Optional[Iterable[str]]) -> bool:
    names = set(fieldnames or [])
    return "website_status" in names and ("place_id" in names or "cid" in names)


def row_listing_key(row: Dict[str, Any]) -> str:
    place_id = str(row.get("place_id") or "").strip()
    if place_id:
        return "place_id:" + place_id
    cid = str(row.get("cid") or "").strip()
    if cid:
        return "cid:" + cid
    return ""


def clean_listing_key(value: Any) -> str:
    value = str(value or "").strip()
    kind, _, ident = value.partition(":")
    if kind not in {"place_id", "cid"} or not ident.strip():
        return ""
    return f"{kind}:{ident.strip()}"


def maps_rows_for_key(rows: List[Dict[str, Any]], listing_key: Any) -> List[int]:
    """Indexes of the Maps rows with this listing key ([] if the key is empty/invalid)."""
    key = clean_listing_key(listing_key)
    if not key:
        return []
    return [i for i, row in enumerate(rows) if row_listing_key(row) == key]
