"""
LeadBot Blocklist Agent

Single source of truth for blocked / junk domains.

Purpose:
- Normalize domains consistently.
- Load every known blocklist file.
- Block directory/wrapper domains like maps.apple.com and joe.coffee.
- Filter live job leads before they are written/displayed/exported.
"""

from __future__ import annotations
from agents.leadbot_block_gate import load_main_blocked_domains, add_main_blocked_domain, remove_main_blocked_domain, is_main_blocked_domain, lead_is_main_blocked

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DATA_DIR = Path("data")

BLOCKLIST_FILES = [
    DATA_DIR / "leadbot_blocked_domains.txt",
    DATA_DIR / "leadbot_blocked_domains_extracted.txt",
    DATA_DIR / "leadbot_blocklist.txt",
]

DEFAULT_BLOCKED_DOMAINS = {
    "opentable.com",
    "www.opentable.com",
    "joe.coffee",
    "maps.apple.com",
}


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip().lower().strip(" ,")

    if not raw:
        return ""

    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
    else:
        host = raw.split("/")[0].lower()

    host = host.split("@")[-1]
    host = host.split(":")[0]
    host = host.strip().strip(".")

    if host.startswith("www."):
        host = host[4:]

    if not host or "." not in host:
        return ""

    return host


def seed_default_blocklist() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    primary = BLOCKLIST_FILES[0]
    primary.touch(exist_ok=True)

    existing = load_blocked_domains()

    with primary.open("a", encoding="utf-8") as f:
        for domain in sorted(DEFAULT_BLOCKED_DOMAINS):
            clean = normalize_domain(domain)
            if clean and clean not in existing:
                f.write(clean + "\n")
                existing.add(clean)


def load_blocked_domains() -> set[str]:
    blocked: set[str] = set()

    for domain in DEFAULT_BLOCKED_DOMAINS:
        clean = normalize_domain(domain)
        if clean:
            blocked.add(clean)

    for path in BLOCKLIST_FILES:
        try:
            if not path.exists():
                continue

            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                clean = normalize_domain(line)
                if clean:
                    blocked.add(clean)
        except Exception:
            continue

    return blocked


def add_blocked_domain(value: Any) -> str:
    clean = normalize_domain(value)

    if not clean:
        return ""

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing = load_blocked_domains()

    if clean not in existing:
        for path in BLOCKLIST_FILES:
            path.touch(exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(clean + "\n")

    return clean


def domain_is_blocked(value: Any, blocked: set[str] | None = None) -> bool:
    clean = normalize_domain(value)

    if not clean:
        return False

    blocked_domains = blocked if blocked is not None else load_blocked_domains()

    for bad in blocked_domains:
        if clean == bad or clean.endswith("." + bad):
            return True

    return False


def lead_is_blocked(lead: Any, blocked: set[str] | None = None) -> bool:
    blocked_domains = blocked if blocked is not None else load_blocked_domains()

    if isinstance(lead, dict):
        candidates = [
            lead.get("domain"),
            lead.get("url"),
            lead.get("website"),
            lead.get("contact_page_url"),
        ]

        title = str(lead.get("title") or "").lower()
        reason = str(lead.get("reason") or "").lower()

        for value in candidates:
            if domain_is_blocked(value, blocked_domains):
                return True

        for bad in blocked_domains:
            label = bad.replace(".", " ")
            if bad in title or bad in reason or label in title or label in reason:
                return True

        return False

    return domain_is_blocked(lead, blocked_domains)


def filter_blocked_leads(leads: Any) -> tuple[list[Any], list[str]]:
    if not isinstance(leads, list):
        return [], []

    blocked_domains = load_blocked_domains()

    kept: list[Any] = []
    removed: list[str] = []

    for lead in leads:
        if lead_is_blocked(lead, blocked_domains):
            if isinstance(lead, dict):
                removed.append(
                    normalize_domain(lead.get("domain"))
                    or normalize_domain(lead.get("url"))
                    or str(lead.get("title") or "blocked")
                )
            else:
                removed.append(str(lead))
            continue

        kept.append(lead)

    return kept, sorted(set(x for x in removed if x))


def recount_leads(job: dict[str, Any]) -> None:
    leads = job.get("leads")

    if not isinstance(leads, list):
        return

    counts = job.setdefault("counts", {})

    if not isinstance(counts, dict):
        job["counts"] = {}
        counts = job["counts"]

    counts["found"] = len(leads)

    enriched = 0

    for lead in leads:
        if not isinstance(lead, dict):
            continue

        if (
            str(lead.get("best_phone") or "").strip()
            or str(lead.get("emails") or "").strip()
            or str(lead.get("contact_page_url") or "").strip()
        ):
            enriched += 1

    counts["enriched"] = enriched
    counts["needs_research"] = max(0, len(leads) - enriched)


def apply_blocklist_to_job(job: Any) -> Any:
    if not isinstance(job, dict):
        return job

    leads = job.get("leads")

    if not isinstance(leads, list):
        return job

    kept, removed = filter_blocked_leads(leads)

    if removed:
        job["leads"] = kept

        old_removed = job.get("blocked_domains_removed", [])
        if not isinstance(old_removed, list):
            old_removed = []

        job["blocked_domains_removed"] = sorted(set(old_removed + removed))
        recount_leads(job)

    return job


# === LEADBOT CONSOLIDATED BLOCKLIST OVERRIDES START ===
def load_blocked_domains() -> set[str]:
    return load_main_blocked_domains()

def add_blocked_domain(value):
    return add_main_blocked_domain(value)

def domain_is_blocked(value, blocked_domains=None) -> bool:
    return is_main_blocked_domain(value, blocked_domains)

def lead_is_blocked(lead, blocked_domains=None) -> bool:
    return lead_is_main_blocked(lead, blocked_domains)

def remove_blocked_domain(value):
    return remove_main_blocked_domain(value)
# === LEADBOT CONSOLIDATED BLOCKLIST OVERRIDES END ===

