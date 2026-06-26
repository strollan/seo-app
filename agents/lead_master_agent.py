import csv
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


MASTER_PATH = Path("exports/leadbot_master.csv")

MASTER_FIELDS = [
    "added_at",
    "last_seen_at",
    "times_seen",
    "industry",
    "market",
    "query",
    "title",
    "domain",
    "url",
    "serp_page",
    "serp_position",
    "seo_opportunity_score",
    "best_phone",
    "emails",
    "contact_page_url",
    "gbp_search_url",
    "outreach_status",
    "score",
    "final_lead_score",
    "contact_confidence",
    "contact_flags",
    "reason",
]


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_domain(value=""):
    value = str(value or "").strip().lower()

    if not value:
        return ""

    if "://" not in value:
        value = "http://" + value

    try:
        parsed = urlparse(value)
        domain = parsed.netloc or parsed.path
    except Exception:
        domain = value

    domain = domain.replace("www.", "").strip("/")
    domain = re.sub(r"[^a-z0-9.-]", "", domain)

    return domain


def lead_key(lead):
    domain = clean_domain(lead.get("domain") or lead.get("url"))

    if domain:
        return domain

    return clean_domain(lead.get("title", ""))


def value_to_string(value):
    if value is None:
        return ""

    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x)

    return str(value)


def read_master_rows():
    if not MASTER_PATH.exists():
        return []

    with MASTER_PATH.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_master_rows(rows):
    MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)

    with MASTER_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_FIELDS)
        writer.writeheader()

        for row in rows:
            clean = {field: row.get(field, "") for field in MASTER_FIELDS}
            writer.writerow(clean)


def append_to_master(leads, industry="", market="", query=""):
    existing_rows = read_master_rows()
    existing_by_key = {}

    for row in existing_rows:
        key = clean_domain(row.get("domain") or row.get("url"))
        if key:
            existing_by_key[key] = row

    added = 0
    updated = 0
    now = now_stamp()

    for lead in leads or []:
        key = lead_key(lead)

        if not key:
            continue

        if key in existing_by_key:
            row = existing_by_key[key]
            row["last_seen_at"] = now

            try:
                row["times_seen"] = str(int(row.get("times_seen") or 1) + 1)
            except Exception:
                row["times_seen"] = "2"

            # Fill missing fields from newer scan, but do not wipe old useful contact data.
            for field in MASTER_FIELDS:
                if field in {"added_at", "last_seen_at", "times_seen"}:
                    continue

                new_value = value_to_string(lead.get(field, ""))
                if field == "industry" and industry:
                    new_value = industry
                elif field == "market" and market:
                    new_value = market
                elif field == "query" and query:
                    new_value = query

                if new_value and not row.get(field):
                    row[field] = new_value

            updated += 1
            continue

        row = {field: "" for field in MASTER_FIELDS}
        row["added_at"] = now
        row["last_seen_at"] = now
        row["times_seen"] = "1"
        row["industry"] = industry
        row["market"] = market
        row["query"] = query

        for field in MASTER_FIELDS:
            if field in {"added_at", "last_seen_at", "times_seen", "industry", "market", "query"}:
                continue
            row[field] = value_to_string(lead.get(field, ""))

        row["domain"] = clean_domain(row.get("domain") or row.get("url"))
        existing_rows.append(row)
        existing_by_key[key] = row
        added += 1

    write_master_rows(existing_rows)

    return {
        "path": str(MASTER_PATH),
        "total": len(existing_rows),
        "added": added,
        "updated": updated,
    }
