"""
LeadBot canonical block gate.

This is the one source all LeadBot code should use for manual/client blocked domains.

Rules:
- UI-added blocks must work everywhere.
- Text-file blocks are still imported for backward compatibility.
- Existing static/default blocks are allowed as seed/fallback data.
- Domain matching is normalized: opentable.com blocks www.opentable.com and restaurants.opentable.com.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import json
import re


BLOCK_TEXT_FILES = (
    Path("data/leadbot_blocked_domains.txt"),
    Path("data/leadbot_blocked_domains_extracted.txt"),
    Path("data/leadbot_blocklist.txt"),
    Path("exports/leadbot_blocked_domains.txt"),
)

BLOCK_JSON_FILES = (
    Path("data/leadbot_fast_blocklist.json"),
    Path("data/leadbot_blocklist_global.json"),
)



# === LEADBOT BLOCKED WORD PARTS START ===
BLOCKED_WORD_PARTS = {
    "travel",
    "restaurants",
}


def contains_blocked_word_part(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False

    return any(part in raw for part in BLOCKED_WORD_PARTS if part)
# === LEADBOT BLOCKED WORD PARTS END ===

DEFAULT_HARD_BLOCKS = {
    "opentable.com",
    "www.opentable.com",
}


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""

    if "://" not in raw:
        raw = "http://" + raw

    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"^www\d*\.", "", host)
    host = host.strip(".").strip()

    return host


def domain_matches_blocked(domain: str, blocked: str) -> bool:
    d = normalize_domain(domain)
    b = normalize_domain(blocked)

    if not d or not b:
        return False

    return d == b or d.endswith("." + b)


def _load_text_blocks() -> set[str]:
    out: set[str] = set()

    for path in BLOCK_TEXT_FILES:
        try:
            if not path.exists():
                continue

            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                value = line.strip()
                if not value or value.startswith("#"):
                    continue

                domain = normalize_domain(value)
                if domain:
                    out.add(domain)

        except Exception:
            continue

    return out


def _json_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _json_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _json_values(item)


def _load_json_blocks() -> set[str]:
    out: set[str] = set()

    for path in BLOCK_JSON_FILES:
        try:
            if not path.exists():
                continue

            data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "[]")
            for value in _json_values(data):
                domain = normalize_domain(value)
                if domain:
                    out.add(domain)

        except Exception:
            continue

    user_block_dir = Path("data/user_blocklists")
    try:
        if user_block_dir.exists():
            for path in user_block_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "[]")
                    for value in _json_values(data):
                        domain = normalize_domain(value)
                        if domain:
                            out.add(domain)
                except Exception:
                    continue
    except Exception:
        pass

    return out


def _load_db_blocks() -> set[str]:
    out: set[str] = set()

    try:
        from agents.lead_blocked_domain_db_agent import list_blocked_domains

        for value in list_blocked_domains(active_only=True):
            domain = normalize_domain(value)
            if domain:
                out.add(domain)

    except Exception:
        pass

    return out


def load_main_blocked_domains() -> set[str]:
    blocked = set()

    for value in DEFAULT_HARD_BLOCKS:
        domain = normalize_domain(value)
        if domain:
            blocked.add(domain)

    blocked |= _load_text_blocks()
    blocked |= _load_json_blocks()
    blocked |= _load_db_blocks()

    return blocked


def add_main_blocked_domain(value: Any, source: str = "manual") -> str:
    domain = normalize_domain(value)
    if not domain:
        return ""

    # Write to DB if available.
    try:
        from agents.lead_blocked_domain_db_agent import add_blocked_domain

        add_blocked_domain(domain, source=source)
    except Exception:
        pass

    # Also write to main text file for portability/backward compatibility.
    path = Path("data/leadbot_blocked_domains.txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_text_blocks()
        if domain not in existing:
            with path.open("a", encoding="utf-8") as f:
                f.write(domain + "\\n")
    except Exception:
        pass

    return domain


def remove_main_blocked_domain(value: Any) -> str:
    domain = normalize_domain(value)
    if not domain:
        return ""

    try:
        from agents.lead_blocked_domain_db_agent import remove_blocked_domain

        remove_blocked_domain(domain)
    except Exception:
        pass

    # Remove from text files too.
    for path in BLOCK_TEXT_FILES:
        try:
            if not path.exists():
                continue

            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            kept = []
            changed = False

            for line in lines:
                if normalize_domain(line) == domain:
                    changed = True
                    continue
                kept.append(line)

            if changed:
                path.write_text("\\n".join(kept).strip() + "\\n", encoding="utf-8")

        except Exception:
            continue

    return domain



# === LEADBOT GEO ONLY DOMAIN BLOCK START ===
def _geo_compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _domain_root_and_tld(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return "", ""

    if "://" not in raw:
        raw = "http://" + raw

    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"^www\d*\.", "", host)
    host = host.strip(".")

    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return "", ""

    return parts[-2], parts[-1]


def _market_geo_roots(value: Any) -> set[str]:
    raw = str(value or "").strip().lower()
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"[^a-z0-9\s]+", " ", raw)
    raw = " ".join(raw.split())

    if not raw:
        return set()

    state_words = {
        "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
        "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
        "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
        "minnesota","mississippi","missouri","montana","nebraska","nevada",
        "new hampshire","new jersey","new mexico","new york","north carolina",
        "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
        "south carolina","south dakota","tennessee","texas","utah","vermont",
        "virginia","washington","west virginia","wisconsin","wyoming"
    }

    state_abbrs = {
        "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
        "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
        "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
        "va","wa","wv","wi","wy","dc"
    }

    cleaned = raw
    for state in sorted(state_words, key=len, reverse=True):
        cleaned = re.sub(r"\b" + re.escape(state) + r"\b", " ", cleaned)

    tokens = [t for t in cleaned.split() if t not in state_abbrs]
    geo = " ".join(tokens).strip()

    roots = set()
    if geo:
        compact = _geo_compact(geo)
        if len(compact) >= 5:
            roots.add(compact)

    return roots


def is_geo_only_domain_for_market(domain_or_url: Any, market: Any = "") -> bool:
    """
    Blocks bare geo portals:
      baltimore.org for Baltimore MD
      santabarbara.com/org for Santa Barbara CA
      longisland.com/org for Long Island NY

    Keeps domains with business modifiers:
      baltimorebakery.com
      cakesofbaltimore.com
      santabarbaracakes.com
    """
    root, tld = _domain_root_and_tld(domain_or_url)
    if not root:
        return False

    root_compact = _geo_compact(root)
    geo_roots = _market_geo_roots(market)

    if not geo_roots:
        return False

    if root_compact not in geo_roots:
        return False

    # Geo-only domains are junk regardless, but .org/.com/.net are the big offenders.
    return tld in {"com", "org", "net", "info", "city", "co"}
# === LEADBOT GEO ONLY DOMAIN BLOCK END ===


def is_main_blocked_domain(value: Any, blocked: set[str] | None = None) -> bool:
    if contains_blocked_word_part(value):
        return True

    domain = normalize_domain(value)
    if not domain:
        return False

    blocked_domains = blocked if blocked is not None else load_main_blocked_domains()

    for bad in blocked_domains:
        if domain_matches_blocked(domain, bad):
            return True

    return False


def lead_is_main_blocked(lead: Any, blocked: set[str] | None = None) -> bool:
    blocked_domains = blocked if blocked is not None else load_main_blocked_domains()

    if isinstance(lead, dict):
        values = [
            lead.get("domain"),
            lead.get("url"),
            lead.get("website"),
            lead.get("contact_page_url"),
            lead.get("final_url"),
        ]
    else:
        values = [lead]

    lead_market = ""
    if isinstance(lead, dict):
        lead_market = lead.get("market") or lead.get("location") or lead.get("city") or lead.get("region") or ""

    for value in values:
        if contains_blocked_word_part(value):
            return True

        if lead_market and is_geo_only_domain_for_market(value, lead_market):
            return True

        if is_main_blocked_domain(value, blocked_domains):
            return True

    # Also check common text fields, not only URLs/domains.
    if isinstance(lead, dict):
        text_values = [
            lead.get("title"),
            lead.get("business"),
            lead.get("page_title"),
            lead.get("meta_title"),
            lead.get("site_title"),
            lead.get("meta_description"),
            lead.get("meta_desc"),
            lead.get("reason"),
        ]

        for value in text_values:
            if contains_blocked_word_part(value):
                return True

    return False
