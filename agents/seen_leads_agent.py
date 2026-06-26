import json
from datetime import datetime
from pathlib import Path


SEEN_PATH = Path("data/seen_leads.json")


def load_seen_domains():
    if not SEEN_PATH.exists():
        return set()

    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()

    return set(data.get("domains", []))


def save_seen_domains(domains):
    SEEN_PATH.parent.mkdir(exist_ok=True)

    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "domains": sorted(set(domains)),
    }

    SEEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def mark_seen(leads):
    existing = load_seen_domains()

    for lead in leads or []:
        domain = lead.get("domain")
        if domain:
            existing.add(domain)

    save_seen_domains(existing)


def filter_unseen(leads):
    seen = load_seen_domains()

    fresh = []
    skipped = []

    for lead in leads or []:
        domain = lead.get("domain")

        if domain and domain in seen:
            skipped.append(lead)
        else:
            fresh.append(lead)

    return fresh, skipped


def reset_seen():
    save_seen_domains(set())
