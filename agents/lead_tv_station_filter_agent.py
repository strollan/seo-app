"""
LeadBot TV Station Filter

Filters local TV/news station domains from LeadBot results.

Goal:
- Stop whack-a-mole blocking of sites like fox4kc.com.
- Avoid nuking normal businesses just because they start with K, W, or Fox.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List


MEDIA_WORDS = {
    "news",
    "tv",
    "television",
    "channel",
    "weather",
    "forecast",
    "radar",
    "breaking",
    "local news",
    "abc",
    "cbs",
    "nbc",
    "fox",
    "cw",
    "pbs",
    "broadcast",
    "broadcasting",
    "station",
}


KNOWN_TV_ROOTS = {
    # Common K/W local TV/news domains. Add more here if needed.
    "kctv5",
    "kshb",
    "kmov",
    "ksdk",
    "kmbc",
    "koco",
    "kfor",
    "kark",
    "ktla",
    "ktvu",
    "kusa",
    "kxan",
    "kvue",
    "khou",
    "kprc",
    "kdfw",
    "wabc",
    "wnbc",
    "wcbs",
    "wxyz",
    "wdiv",
    "wcvb",
    "wtae",
    "wpxi",
    "wfla",
    "wesh",
    "wjla",
    "wusa9",
    "wral",
    "wsbtv",
    "wbtv",
    "wbaltv",
    "wkrn",
    "wsmv",
    "wbir",
    "wtvr",
    "whnt",
    "wect",
    "wwltv",
}


SAFE_FALSE_POSITIVE_ROOTS = {
    # Do not block these just because they start with k/w/fox.
    "foxroofing",
    "foxplumbing",
    "foxauto",
    "foxdental",
    "foxlaw",
    "foxconstruction",
    "foxrealty",
    "walmart",
    "walgreens",
    "wawa",
    "wendys",
    "westernunion",
    "westernpest",
    "kfc",
    "kia",
    "kpmg",
    "kwikset",
    "kwiktrip",
}


def normalize_domain(value: Any) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.strip(" .,:;()[]{}<>\"'")
    return value


def domain_root(domain: str) -> str:
    domain = normalize_domain(domain)
    if not domain:
        return ""

    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2]

    return parts[0]


def row_domain(row: Dict[str, Any]) -> str:
    for key in (
        "domain",
        "Domain",
        "website",
        "Website",
        "url",
        "URL",
        "site",
        "Site",
        "homepage",
        "Homepage",
    ):
        value = row.get(key)
        if value:
            return normalize_domain(value)
    return ""


def row_text(row: Dict[str, Any]) -> str:
    parts = []
    for key in (
        "name",
        "Name",
        "business_name",
        "Business Name",
        "title",
        "Title",
        "description",
        "Description",
        "snippet",
        "Snippet",
        "category",
        "Category",
        "source",
        "Source",
        "reason",
        "Reason",
    ):
        value = row.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def has_media_words(text: str) -> bool:
    text = str(text or "").lower()
    return any(word in text for word in MEDIA_WORDS)


def is_fox_tv_domain(domain: str, text: str = "") -> bool:
    root = domain_root(domain)

    if not root or root in SAFE_FALSE_POSITIVE_ROOTS:
        return False

    # fox4kc.com, fox5atlanta.com, fox13news.com, fox2now.com
    if re.match(r"^fox\d", root):
        return True

    # fox13news / foxbusiness-style check; keep it media-specific.
    if root.startswith("fox") and (
        has_media_words(root)
        or has_media_words(text)
        or re.search(r"\d", root)
    ):
        return True

    return False


def is_kw_call_sign_tv_domain(domain: str, text: str = "") -> bool:
    root = domain_root(domain)

    if not root or root in SAFE_FALSE_POSITIVE_ROOTS:
        return False

    if root in KNOWN_TV_ROOTS:
        return True

    # K/W + TV in the domain is almost always a station.
    # Examples: kctv5, wbaltv, wwltv
    if root.startswith(("k", "w")) and "tv" in root:
        return True

    # K/W call-sign style with number, plus media clue.
    # Examples: wusa9, koco5-ish patterns
    if re.match(r"^[kw][a-z]{2,4}\d$", root) and has_media_words(text + " " + root):
        return True

    # Classic 4-letter broadcast calls like WABC, WXYZ, KSHB.
    # Require a media clue unless it is in known roots, to avoid false positives like WAWA/KPMG.
    if re.match(r"^[kw][a-z]{3}$", root) and has_media_words(text):
        return True

    return False


def is_tv_station_domain(domain: str, text: str = "") -> bool:
    domain = normalize_domain(domain)
    text = str(text or "").lower()

    if not domain:
        return False

    root = domain_root(domain)

    if root in SAFE_FALSE_POSITIVE_ROOTS:
        return False

    if is_fox_tv_domain(domain, text):
        return True

    if is_kw_call_sign_tv_domain(domain, text):
        return True

    return False


def is_tv_station_lead(lead: Dict[str, Any]) -> bool:
    domain = row_domain(lead)
    text = row_text(lead)
    return is_tv_station_domain(domain, text)


def filter_tv_station_leads(leads: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    removed = 0

    for lead in leads or []:
        if isinstance(lead, dict) and is_tv_station_lead(lead):
            removed += 1
            continue
        kept.append(lead)

    if removed:
        print(f"LEADBOT TV FILTER: removed {removed} TV/news station lead(s).", flush=True)

    return kept


def clean_export_csv_file(path: Path) -> int:
    path = Path(path)

    if not path.exists() or path.suffix.lower() != ".csv":
        return 0

    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not fieldnames:
        return 0

    kept = []
    removed = 0

    for row in rows:
        if is_tv_station_lead(row):
            removed += 1
            continue
        kept.append(row)

    if removed:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)

    return removed


def clean_all_exports(exports_dir: str = "exports") -> int:
    total_removed = 0
    base = Path(exports_dir)

    if not base.exists():
        return 0

    for csv_path in base.glob("leads_*.csv"):
        removed = clean_export_csv_file(csv_path)
        if removed:
            print(f"LEADBOT TV FILTER: removed {removed} from {csv_path}", flush=True)
        total_removed += removed

    return total_removed


# === LEADBOT VISIT TOURISM AND STRONGER TV FILTER START ===

try:
    KNOWN_TV_ROOTS.update({
        "wxii12",
        "wxii",
        "wfmy",
        "wlos",
        "wsoctv",
        "wcnc",
        "wbtv",
        "wnct",
        "wral",
        "wtvd",
        "abc11",
        "myfox8",
        "fox8",
    })
except Exception:
    pass


def is_visit_tourism_domain(domain: str) -> bool:
    root = domain_root(domain)
    return bool(root and root.startswith("visit"))


_leadbot_base_is_tv_station_domain = is_tv_station_domain


def is_tv_station_domain(domain: str, text: str = "") -> bool:
    domain = normalize_domain(domain)
    text = str(text or "").lower()
    root = domain_root(domain)

    if not domain or not root:
        return False

    if root in SAFE_FALSE_POSITIVE_ROOTS:
        return False

    if is_visit_tourism_domain(domain):
        return True

    if root in KNOWN_TV_ROOTS:
        return True

    if re.match(r"^[kw][a-z]{2,5}\d{1,2}$", root):
        return True

    if re.match(r"^[kw][a-z]{3}$", root):
        return True

    if re.match(r"^(my)?fox\d", root):
        return True

    return _leadbot_base_is_tv_station_domain(domain, text)


def is_tv_station_lead(lead: Dict[str, Any]) -> bool:
    domain = row_domain(lead)
    text = row_text(lead)
    return is_tv_station_domain(domain, text)

# === LEADBOT VISIT TOURISM AND STRONGER TV FILTER END ===

