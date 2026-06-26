from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from agents.lead_market_agent import parse_market_parts


DB_PATH = Path("data") / "leadbot_detail_index.sqlite"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_domain(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""

    if "://" not in value and "/" not in value:
        raw = value
    else:
        parsed = urlparse(value if "://" in value else "https://" + value)
        raw = parsed.netloc or parsed.path.split("/")[0]

    raw = raw.replace("www.", "").strip().strip("/")
    return raw


def first_value(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value and value.lower() not in {"not found", "none", "null", "nan", "n/a", "unknown"}:
            return value
    return ""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_detail_index (
            domain TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            website TEXT DEFAULT '',
            best_phone TEXT DEFAULT '',
            emails TEXT DEFAULT '',
            contact_page_url TEXT DEFAULT '',
            address TEXT DEFAULT '',
            industry TEXT DEFAULT '',
            market TEXT DEFAULT '',
            keyword TEXT DEFAULT '',
            contact_confidence INTEGER DEFAULT 0,
            outreach_status TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            first_seen_at TEXT DEFAULT '',
            last_seen_at TEXT DEFAULT '',
            last_enriched_at TEXT DEFAULT ''
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_detail_market ON lead_detail_index(market)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_detail_industry ON lead_detail_index(industry)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_detail_phone ON lead_detail_index(best_phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_detail_email ON lead_detail_index(emails)")
    conn.commit()


def row_to_index_payload(row: dict, source_file: str = "") -> dict:
    website = first_value(row, "website", "url", "link", "homepage")
    domain = normalize_domain(first_value(row, "domain") or website)

    emails = row.get("emails") or row.get("email") or row.get("email_1") or ""
    if isinstance(emails, list):
        emails = ", ".join(str(e).strip() for e in emails if str(e).strip())
    emails = str(emails or "").strip()

    try:
        from agents.lead_email_cleaner_agent import clean_lead_emails
        emails = clean_lead_emails(emails, lead_domain=domain, website=website)
    except Exception:
        pass

    try:
        from agents.lead_email_cleaner_agent import clean_lead_emails
        emails = clean_lead_emails(emails, lead_domain=domain, website=website)
    except Exception:
        pass

    return {
        "domain": domain,
        "title": first_value(row, "title", "business", "business_name", "name"),
        "website": website,
        "best_phone": first_value(row, "best_phone", "phone", "phones", "phone_number", "telephone", "tel"),
        "emails": emails,
        "contact_page_url": first_value(row, "contact_page_url", "contact_page"),
        "address": first_value(
            row,
            "address",
            "full_address",
            "business_address",
            "formatted_address",
            "street_address",
            "place_address",
            "location",
        ),
        "industry": first_value(row, "industry", "service"),
        "market": first_value(row, "market", "city", "region", "location_market"),
        "keyword": first_value(row, "keyword", "query", "service_keyword"),
        "contact_confidence": (
            __import__("agents.lead_confidence_agent", fromlist=["calculate_contact_confidence"])
            .calculate_contact_confidence(row)
        ),
        "outreach_status": first_value(row, "outreach_status"),
        "source_file": source_file,
    }


def upsert_lead(row: dict, source_file: str = "") -> bool:
    payload = row_to_index_payload(row, source_file=source_file)

    if not payload["domain"]:
        return False

    current = now_iso()
    enriched = bool(payload["best_phone"] or payload["emails"] or payload["contact_page_url"] or payload["address"])

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO lead_detail_index (
                domain, title, website, best_phone, emails, contact_page_url, address,
                industry, market, keyword, contact_confidence, outreach_status,
                source_file, first_seen_at, last_seen_at, last_enriched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                title = COALESCE(NULLIF(excluded.title, ''), title),
                website = COALESCE(NULLIF(excluded.website, ''), website),
                best_phone = COALESCE(NULLIF(excluded.best_phone, ''), best_phone),
                emails = COALESCE(NULLIF(excluded.emails, ''), emails),
                contact_page_url = COALESCE(NULLIF(excluded.contact_page_url, ''), contact_page_url),
                address = COALESCE(NULLIF(excluded.address, ''), address),
                industry = COALESCE(NULLIF(excluded.industry, ''), industry),
                market = COALESCE(NULLIF(excluded.market, ''), market),
                keyword = COALESCE(NULLIF(excluded.keyword, ''), keyword),
                contact_confidence = MAX(contact_confidence, excluded.contact_confidence),
                outreach_status = COALESCE(NULLIF(excluded.outreach_status, ''), outreach_status),
                source_file = COALESCE(NULLIF(excluded.source_file, ''), source_file),
                last_seen_at = excluded.last_seen_at,
                last_enriched_at = CASE
                    WHEN excluded.last_enriched_at != '' THEN excluded.last_enriched_at
                    ELSE last_enriched_at
                END
            """,
            (
                payload["domain"],
                payload["title"],
                payload["website"],
                payload["best_phone"],
                payload["emails"],
                payload["contact_page_url"],
                payload["address"],
                payload["industry"],
                payload["market"],
                payload["keyword"],
                payload["contact_confidence"],
                payload["outreach_status"],
                payload["source_file"],
                current,
                current,
                current if enriched else "",
            ),
        )
        conn.commit()

    return True


def apply_index_to_lead(lead: dict) -> tuple[dict, bool]:
    domain = normalize_domain(first_value(lead, "domain") or first_value(lead, "url", "website", "link"))
    if not domain:
        return lead, False

    with connect() as conn:
        row = conn.execute("SELECT * FROM lead_detail_index WHERE domain = ?", (domain,)).fetchone()

    if not row:
        return lead, False

    changed = False

    def fill(target_key: str, cached_key: str) -> None:
        nonlocal changed
        current = first_value(lead, target_key)
        cached = str(row[cached_key] or "").strip()
        if cached and not current:
            lead[target_key] = cached
            changed = True

    fill("title", "title")
    fill("url", "website")
    fill("website", "website")
    fill("best_phone", "best_phone")
    fill("phone", "best_phone")
    fill("emails", "emails")
    fill("email", "emails")
    fill("contact_page_url", "contact_page_url")
    fill("contact_page", "contact_page_url")
    fill("address", "address")
    fill("market", "market")
    fill("industry", "industry")
    fill("keyword", "keyword")

    cached_conf = int(row["contact_confidence"] or 0)
    current_conf = int(float(first_value(lead, "contact_confidence") or 0))
    if cached_conf > current_conf:
        lead["contact_confidence"] = cached_conf
        changed = True

    if row["outreach_status"] and not first_value(lead, "outreach_status"):
        lead["outreach_status"] = row["outreach_status"]
        changed = True

    return lead, changed


def sync_csv_to_index(csv_path, source_file: str = "") -> int:
    path = Path(csv_path) if csv_path else None
    if not path or not path.exists():
        return 0

    count = 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if upsert_lead(row, source_file=source_file or path.name):
                count += 1

    return count
