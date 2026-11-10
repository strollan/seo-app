"""Per-lead Block / Delete for Lead Finder cards (Live Scan and Saved Results).

A lead is identified by a "lead key":
- Google Maps rows: ``place_id:<id>`` or ``cid:<id>`` (agents/lead_row_identity.py),
  so No Website businesses and each location of a chain are separate leads.
- Organic rows: ``domain:<normalized domain>`` (one row per website).

Delete is a per-user, per-export soft delete: the CSV is never rewritten, the
card is hidden on every render, and Undo removes the record.

Block keeps the business out of future discovery:
- Organic lead -> the existing personal domain blocklist.
- Maps lead -> a personal *listing* block on its Google ID only. A Maps lead's
  website is never blocked as a domain, so other locations of a chain (which
  share that website) and unrelated businesses are not affected.

Neither action touches the ACT II CRM, the CRM import queue or its receipts.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

DB_PATH = Path("data") / "leadbot_lead_actions.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leadbot_deleted_leads ("
                " owner_key TEXT NOT NULL, export_file TEXT NOT NULL, lead_key TEXT NOT NULL,"
                " title TEXT DEFAULT '', deleted_at TEXT DEFAULT '',"
                " PRIMARY KEY (owner_key, export_file, lead_key))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leadbot_user_blocked_listings ("
                " owner_key TEXT NOT NULL, listing_key TEXT NOT NULL,"
                " title TEXT DEFAULT '', blocked_at TEXT DEFAULT '',"
                " PRIMARY KEY (owner_key, listing_key))"
            )
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- lead keys

def clean_lead_key(value: Any) -> str:
    """Validated lead key, or "" if it is not a place/cid/domain key."""
    from agents.lead_row_identity import clean_listing_key
    from agents.leadbot_block_gate import is_valid_block_domain, normalize_domain

    raw = str(value or "").strip()
    if raw.startswith("domain:"):
        domain = normalize_domain(raw[len("domain:"):])
        return ("domain:" + domain) if is_valid_block_domain(domain) else ""
    key = clean_listing_key(raw)
    if key and len(key) <= 300:
        return key
    return ""


def row_lead_key(row: Dict[str, Any]) -> str:
    """Lead key for an export row ("" when the row has no stable identity)."""
    from agents.lead_row_identity import row_listing_key
    from agents.leadbot_block_gate import is_valid_block_domain, normalize_domain

    if isinstance(row, dict) and "website_status" in row:
        # Google Maps row: only the listing ID identifies it, never the website.
        return row_listing_key(row)
    domain = normalize_domain((row or {}).get("domain") or "")
    return ("domain:" + domain) if is_valid_block_domain(domain) else ""


def is_listing_key(lead_key: str) -> bool:
    return lead_key.startswith("place_id:") or lead_key.startswith("cid:")


# ------------------------------------------------------------------ delete

def delete_lead(owner_key: str, export_file: str, lead_key: str, title: str = "") -> bool:
    owner_key, key = str(owner_key or "").strip().lower(), clean_lead_key(lead_key)
    if not owner_key or not export_file or not key:
        return False
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO leadbot_deleted_leads VALUES (?, ?, ?, ?, ?)",
            (owner_key, export_file, key, str(title or "")[:200], _now()),
        )
    return True


def restore_lead(owner_key: str, export_file: str, lead_key: str) -> bool:
    owner_key, key = str(owner_key or "").strip().lower(), clean_lead_key(lead_key)
    if not owner_key or not export_file or not key:
        return False
    with _db() as conn:
        conn.execute(
            "DELETE FROM leadbot_deleted_leads WHERE owner_key=? AND export_file=? AND lead_key=?",
            (owner_key, export_file, key),
        )
    return True


def deleted_lead_keys(owner_key: str, export_file: str) -> Set[str]:
    owner_key = str(owner_key or "").strip().lower()
    if not owner_key or not export_file:
        return set()
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT lead_key FROM leadbot_deleted_leads WHERE owner_key=? AND export_file=?",
                (owner_key, export_file),
            ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


# ------------------------------------------------------------------- block

def block_listing(owner_key: str, listing_key: str, title: str = "") -> bool:
    owner_key, key = str(owner_key or "").strip().lower(), clean_lead_key(listing_key)
    if not owner_key or not is_listing_key(key):
        return False
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO leadbot_user_blocked_listings VALUES (?, ?, ?, ?)",
            (owner_key, key, str(title or "")[:200], _now()),
        )
    return True


def unblock_listing(owner_key: str, listing_key: str) -> bool:
    owner_key, key = str(owner_key or "").strip().lower(), clean_lead_key(listing_key)
    if not owner_key or not is_listing_key(key):
        return False
    with _db() as conn:
        conn.execute(
            "DELETE FROM leadbot_user_blocked_listings WHERE owner_key=? AND listing_key=?",
            (owner_key, key),
        )
    return True


def blocked_listing_keys(owner_key: str) -> Set[str]:
    owner_key = str(owner_key or "").strip().lower()
    if not owner_key:
        return set()
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT listing_key FROM leadbot_user_blocked_listings WHERE owner_key=?",
                (owner_key,),
            ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def block_lead(owner_key: str, lead_key: str, title: str = "") -> bool:
    key = clean_lead_key(lead_key)
    if is_listing_key(key):
        return block_listing(owner_key, key, title)
    if key.startswith("domain:"):
        from agents.lead_blocked_domain_db_agent import add_user_blocked_domain
        return bool(owner_key) and add_user_blocked_domain(owner_key, key[len("domain:"):])
    return False


def unblock_lead(owner_key: str, lead_key: str) -> bool:
    key = clean_lead_key(lead_key)
    if is_listing_key(key):
        return unblock_listing(owner_key, key)
    if key.startswith("domain:"):
        from agents.lead_blocked_domain_db_agent import remove_user_blocked_domain
        if not owner_key:
            return False
        remove_user_blocked_domain(owner_key, key[len("domain:"):])
        return True
    return False


# ----------------------------------------------------------------- filters

def row_is_user_blocked(row: Dict[str, Any], blocked_domains: Iterable[str], blocked_listings: Set[str]) -> bool:
    """True if this export row is blocked for the user (listing ID or own domain)."""
    from agents.leadbot_block_gate import domain_matches_blocked

    if isinstance(row, dict) and "website_status" in row:
        return bool(blocked_listings) and row_lead_key(row) in blocked_listings
    key = row_lead_key(row)
    if not key:
        return False
    domain = key[len("domain:"):]
    return any(domain_matches_blocked(domain, b) for b in (blocked_domains or ()) if b)


def visible_rows(rows: List[Dict[str, Any]], owner_key: str, export_file: str) -> List[Dict[str, Any]]:
    """Rows minus the user's deleted leads (this export) and personally blocked leads."""
    owner_key = str(owner_key or "").strip().lower()
    if not owner_key or not rows:
        return list(rows or [])
    from agents.leadbot_block_gate import load_user_blocked_domains

    deleted = deleted_lead_keys(owner_key, export_file)
    blocked_domains = load_user_blocked_domains(owner_key)
    blocked_listings = blocked_listing_keys(owner_key)
    out = []
    for row in rows:
        key = row_lead_key(row) if isinstance(row, dict) else ""
        if key and key in deleted:
            continue
        if isinstance(row, dict) and row_is_user_blocked(row, blocked_domains, blocked_listings):
            continue
        out.append(row)
    return out
