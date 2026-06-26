from __future__ import annotations

import sqlite3
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
    conn.commit()


def add_blocked_domain(domain: str, source: str = "manual") -> bool:
    clean = clean_domain(domain)

    if not clean:
        return False

    current = now_iso()

    with connect() as conn:
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

    with connect() as conn:
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
    with connect() as conn:
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

    with connect() as conn:
        row = conn.execute(
            "SELECT is_active FROM leadbot_blocked_domains WHERE domain = ?",
            (clean,),
        ).fetchone()

    return bool(row and int(row["is_active"] or 0) == 1)
