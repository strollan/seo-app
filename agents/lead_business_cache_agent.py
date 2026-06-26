# === LEADBOT EMAIL CLEANER IMPORT START ===
try:
    from agents.lead_email_cleaner_agent import clean_lead_emails
except Exception:
    def clean_lead_emails(value, lead_domain="", website=""):
        if isinstance(value, list):
            return ", ".join([str(v).strip() for v in value if str(v).strip()])
        return str(value or "").strip()
# === LEADBOT EMAIL CLEANER IMPORT END ===

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DB_PATH = Path("data/leadbot_businesses.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_domain(value):
    value = str(value or "").strip().lower()

    if not value:
        return ""

    if "://" in value:
        parsed = urlparse(value)
        host = parsed.netloc or parsed.path
    else:
        host = value.split("/")[0]

    host = host.strip().lower()
    host = host.replace("www.", "")

    return host


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS businesses (
                domain TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                url TEXT DEFAULT '',
                best_phone TEXT DEFAULT '',
                emails TEXT DEFAULT '',
                contact_page_url TEXT DEFAULT '',
                contact_confidence INTEGER DEFAULT 0,
                outreach_status TEXT DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_enriched_at TEXT DEFAULT '',
                times_seen INTEGER DEFAULT 1
            )
            """
        )
        conn.commit()


def get_business(domain_or_url):
    init_db()
    domain = normalize_domain(domain_or_url)

    if not domain:
        return None

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM businesses WHERE domain = ?",
            (domain,),
        ).fetchone()

    return dict(row) if row else None


def save_business_from_lead(lead, enriched=False):
    init_db()

    domain = normalize_domain(
        lead.get("domain")
        or lead.get("url")
        or lead.get("website")
        or lead.get("link")
    )

    if not domain:
        return None

    title = str(lead.get("title") or "").strip()
    url = str(lead.get("url") or lead.get("website") or lead.get("link") or "").strip()
    best_phone = str(lead.get("best_phone") or lead.get("phone") or "").strip()
    emails = lead.get("emails") or lead.get("email") or ""

    if isinstance(emails, list):
        emails = ", ".join([str(e).strip() for e in emails if str(e).strip()])
    else:
        emails = str(emails or "").strip()

    emails = clean_lead_emails(
        emails,
        lead_domain=lead.get("domain", ""),
        website=lead.get("url") or lead.get("website") or "",
    )

    emails = clean_lead_emails(
        emails,
        lead_domain=lead.get("domain", ""),
        website=lead.get("url") or lead.get("website") or "",
    )

    emails = clean_lead_emails(
        emails,
        lead_domain=lead.get("domain", ""),
        website=lead.get("url") or lead.get("website") or "",
    )

    contact_page_url = str(lead.get("contact_page_url") or lead.get("contact_page") or "").strip()
    contact_confidence = int(lead.get("contact_confidence") or 0)
    outreach_status = str(lead.get("outreach_status") or "").strip()

    existing = get_business(domain)
    current_time = now_iso()
    last_enriched_at = current_time if enriched and (best_phone or emails or contact_page_url) else ""

    with connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE businesses
                SET
                    title = COALESCE(NULLIF(?, ''), title),
                    url = COALESCE(NULLIF(?, ''), url),
                    best_phone = COALESCE(NULLIF(?, ''), best_phone),
                    emails = COALESCE(NULLIF(?, ''), emails),
                    contact_page_url = COALESCE(NULLIF(?, ''), contact_page_url),
                    contact_confidence = MAX(contact_confidence, ?),
                    outreach_status = COALESCE(NULLIF(?, ''), outreach_status),
                    last_seen_at = ?,
                    last_enriched_at = COALESCE(NULLIF(?, ''), last_enriched_at),
                    times_seen = times_seen + 1
                WHERE domain = ?
                """,
                (
                    title,
                    url,
                    best_phone,
                    emails,
                    contact_page_url,
                    contact_confidence,
                    outreach_status,
                    current_time,
                    last_enriched_at,
                    domain,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO businesses (
                    domain, title, url, best_phone, emails, contact_page_url,
                    contact_confidence, outreach_status, first_seen_at,
                    last_seen_at, last_enriched_at, times_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    domain,
                    title,
                    url,
                    best_phone,
                    emails,
                    contact_page_url,
                    contact_confidence,
                    outreach_status,
                    current_time,
                    current_time,
                    last_enriched_at,
                ),
            )

        conn.commit()

    return get_business(domain)


def apply_cached_business_to_lead(lead):
    domain = (
        lead.get("domain")
        or lead.get("url")
        or lead.get("website")
        or lead.get("link")
    )

    cached = get_business(domain)

    if not cached:
        return lead, False

    changed = False

    if cached.get("best_phone") and not str(lead.get("best_phone") or lead.get("phone") or "").strip():
        lead["best_phone"] = cached["best_phone"]
        changed = True

    if cached.get("emails") and not str(lead.get("emails") or lead.get("email") or "").strip():
        lead["emails"] = cached["emails"]
        changed = True

    if cached.get("contact_page_url") and not str(lead.get("contact_page_url") or lead.get("contact_page") or "").strip():
        lead["contact_page_url"] = cached["contact_page_url"]
        changed = True

    if changed:
        lead["contact_confidence"] = max(
            int(lead.get("contact_confidence") or 0),
            int(cached.get("contact_confidence") or 0),
        )

        if lead.get("best_phone") and lead.get("emails"):
            lead["outreach_status"] = "email_and_call_ready"
        elif lead.get("best_phone"):
            lead["outreach_status"] = "call_ready"
        elif lead.get("emails"):
            lead["outreach_status"] = "email_ready"

        lead["contact_flags"] = ["business_cache"]

    return lead, changed



# === CONTACT REFRESH CACHE FIELDS START ===
def ensure_refresh_columns():
    init_db()

    wanted = {
        "next_refresh_at": "TEXT DEFAULT ''",
        "refresh_status": "TEXT DEFAULT ''",
        "refresh_attempts": "INTEGER DEFAULT 0",
        "last_refresh_error": "TEXT DEFAULT ''",
    }

    with connect() as conn:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(businesses)").fetchall()
        }

        for name, ddl in wanted.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE businesses ADD COLUMN {name} {ddl}")

        conn.commit()


def get_refresh_candidates(limit=25, stale_days=30):
    ensure_refresh_columns()

    from datetime import datetime, timezone, timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(stale_days))).isoformat(timespec="seconds")

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM businesses
            WHERE
                (
                    COALESCE(best_phone, '') = ''
                    OR COALESCE(emails, '') = ''
                    OR COALESCE(contact_page_url, '') = ''
                    OR COALESCE(last_enriched_at, '') = ''
                    OR last_enriched_at < ?
                )
                AND COALESCE(refresh_status, '') != 'running'
            ORDER BY
                CASE
                    WHEN COALESCE(best_phone, '') = '' AND COALESCE(emails, '') = '' THEN 0
                    WHEN COALESCE(last_enriched_at, '') = '' THEN 1
                    ELSE 2
                END,
                last_seen_at DESC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()

    return [dict(row) for row in rows]


def mark_refresh_running(domain):
    ensure_refresh_columns()
    domain = normalize_domain(domain)

    if not domain:
        return

    with connect() as conn:
        conn.execute(
            """
            UPDATE businesses
            SET refresh_status = 'running',
                refresh_attempts = COALESCE(refresh_attempts, 0) + 1,
                last_refresh_error = '',
                last_seen_at = ?
            WHERE domain = ?
            """,
            (now_iso(), domain),
        )
        conn.commit()


def mark_refresh_error(domain, error):
    ensure_refresh_columns()
    domain = normalize_domain(domain)

    if not domain:
        return

    with connect() as conn:
        conn.execute(
            """
            UPDATE businesses
            SET refresh_status = 'error',
                last_refresh_error = ?,
                last_seen_at = ?
            WHERE domain = ?
            """,
            (str(error)[:500], now_iso(), domain),
        )
        conn.commit()


def mark_refresh_done(domain):
    ensure_refresh_columns()
    domain = normalize_domain(domain)

    if not domain:
        return

    with connect() as conn:
        conn.execute(
            """
            UPDATE businesses
            SET refresh_status = 'fresh',
                last_refresh_error = '',
                last_enriched_at = ?,
                last_seen_at = ?
            WHERE domain = ?
            """,
            (now_iso(), now_iso(), domain),
        )
        conn.commit()
# === CONTACT REFRESH CACHE FIELDS END ===
