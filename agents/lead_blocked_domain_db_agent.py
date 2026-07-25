from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DB_PATH = Path("data") / "leadbot_blocked_domains.sqlite"
TEXT_FILE = Path("data") / "leadbot_blocked_domains.txt"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_domain(value: str) -> str:
    value = str(value or "").strip().lower()

    if not value:
        return ""

    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
        host = parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        host = value

    host = host.lower().replace("www.", "").strip().strip("/")
    host = host.split("?")[0].split("#")[0].strip()

    bad = {"", "http:", "https:", "none", "null", "nan", "undefined"}
    if host in bad:
        return ""

    return host


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


@contextmanager
def managed_connection():
    """Preserve transaction handling while ensuring the connection closes."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leadbot_blocked_domains (
            domain TEXT PRIMARY KEY,
            source TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            first_seen_at TEXT DEFAULT '',
            last_seen_at TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leadbot_blocked_domains_active ON leadbot_blocked_domains(is_active)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leadbot_user_blocked_domains (
            owner_key TEXT NOT NULL,
            domain TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (owner_key, domain)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leadbot_user_blocks_active "
        "ON leadbot_user_blocked_domains(owner_key, is_active)"
    )
    conn.commit()


def add_blocked_domain(domain: str, source: str = "manual") -> bool:
    clean = clean_domain(domain)

    if not clean:
        return False

    current = now_iso()

    with managed_connection() as conn:
        conn.execute(
            """
            INSERT INTO leadbot_blocked_domains (
                domain, source, is_active, first_seen_at, last_seen_at
            )
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                source = COALESCE(NULLIF(excluded.source, ''), source),
                is_active = 1,
                last_seen_at = excluded.last_seen_at
            """,
            (clean, source, current, current),
        )
        conn.commit()

    sync_text_file_from_db()
    return True


def add_blocked_domains(domains, source: str = "manual") -> int:
    count = 0
    for domain in domains or []:
        if add_blocked_domain(domain, source=source):
            count += 1
    return count


def remove_blocked_domain(domain: str) -> bool:
    clean = clean_domain(domain)

    if not clean:
        return False

    with managed_connection() as conn:
        conn.execute(
            """
            UPDATE leadbot_blocked_domains
            SET is_active = 0,
                last_seen_at = ?
            WHERE domain = ?
            """,
            (now_iso(), clean),
        )
        conn.commit()

    sync_text_file_from_db()
    return True


def list_blocked_domains(active_only: bool = True) -> list[str]:
    with managed_connection() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT domain FROM leadbot_blocked_domains WHERE is_active = 1 ORDER BY domain"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT domain FROM leadbot_blocked_domains ORDER BY domain"
            ).fetchall()

    return [str(row["domain"] or "").strip() for row in rows if str(row["domain"] or "").strip()]


def sync_text_file_from_db() -> int:
    domains = list_blocked_domains(active_only=True)
    TEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEXT_FILE.write_text("\n".join(domains) + ("\n" if domains else ""), encoding="utf-8")
    return len(domains)


def import_blocked_domains_from_files() -> int:
    candidates = set()

    search_roots = [
        Path("data"),
        Path("exports"),
        Path("backups"),
    ]

    file_names = {
        "leadbot_blocked_domains.txt",
        "leadbot_blocked_domains_extracted.txt",
    }

    for root in search_roots:
        if not root.exists():
            continue

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            if path.name not in file_names:
                continue

            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue

            for line in lines:
                clean = clean_domain(line)
                if clean:
                    candidates.add(clean)

    imported = add_blocked_domains(sorted(candidates), source="historical_import")
    sync_text_file_from_db()
    return imported


def is_blocked_domain(domain: str) -> bool:
    clean = clean_domain(domain)

    if not clean:
        return False

    with managed_connection() as conn:
        row = conn.execute(
            "SELECT is_active FROM leadbot_blocked_domains WHERE domain = ?",
            (clean,),
        ).fetchone()

    return bool(row and int(row["is_active"] or 0) == 1)


def clean_owner_key(value: str) -> str:
    return str(value or "").strip().lower()[:320]


def add_user_blocked_domain(owner_key: str, domain: str) -> bool:
    owner = clean_owner_key(owner_key)
    clean = clean_domain(domain)
    if not owner or not clean:
        return False
    current = now_iso()
    with managed_connection() as conn:
        conn.execute(
            """
            INSERT INTO leadbot_user_blocked_domains
                (owner_key, domain, is_active, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(owner_key, domain) DO UPDATE SET
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (owner, clean, current, current),
        )
        conn.commit()
    return True


def remove_user_blocked_domain(owner_key: str, domain: str) -> bool:
    owner = clean_owner_key(owner_key)
    clean = clean_domain(domain)
    if not owner or not clean:
        return False
    with managed_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE leadbot_user_blocked_domains
            SET is_active = 0, updated_at = ?
            WHERE owner_key = ? AND domain = ? AND is_active = 1
            """,
            (now_iso(), owner, clean),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_user_blocked_domains(owner_key: str, active_only: bool = True) -> list[str]:
    owner = clean_owner_key(owner_key)
    if not owner:
        return []
    with managed_connection() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT domain FROM leadbot_user_blocked_domains
                WHERE owner_key = ? AND is_active = 1
                ORDER BY domain
                """,
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT domain FROM leadbot_user_blocked_domains
                WHERE owner_key = ?
                ORDER BY domain
                """,
                (owner,),
            ).fetchall()
    return [str(row["domain"]) for row in rows]
