import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


TRIAL_DB = Path("data/leadbot_trial.db")
TRIAL_DB.parent.mkdir(parents=True, exist_ok=True)

TRIAL_LIMIT = 3


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(TRIAL_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_trial_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anonymous_trials (
                visitor_id TEXT PRIMARY KEY,
                scans_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_ip TEXT DEFAULT '',
                last_user_agent TEXT DEFAULT ''
            )
            """
        )
        conn.commit()


def new_visitor_id():
    return secrets.token_urlsafe(32)


def get_or_create_trial(visitor_id="", ip="", user_agent=""):
    init_trial_db()

    visitor_id = str(visitor_id or "").strip()

    if not visitor_id:
        visitor_id = new_visitor_id()

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM anonymous_trials WHERE visitor_id = ?",
            (visitor_id,),
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT INTO anonymous_trials
                    (visitor_id, scans_used, created_at, updated_at, last_ip, last_user_agent)
                VALUES (?, 0, ?, ?, ?, ?)
                """,
                (visitor_id, now_iso(), now_iso(), ip, user_agent[:300]),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM anonymous_trials WHERE visitor_id = ?",
                (visitor_id,),
            ).fetchone()

    return dict(row)


def remaining_scans(visitor_id):
    trial = get_or_create_trial(visitor_id)
    return max(0, TRIAL_LIMIT - int(trial.get("scans_used") or 0))


def can_run_scan(visitor_id):
    trial = get_or_create_trial(visitor_id)
    return int(trial.get("scans_used") or 0) < TRIAL_LIMIT


def record_scan(visitor_id, ip="", user_agent=""):
    trial = get_or_create_trial(visitor_id, ip=ip, user_agent=user_agent)

    with connect() as conn:
        conn.execute(
            """
            UPDATE anonymous_trials
            SET scans_used = scans_used + 1,
                updated_at = ?,
                last_ip = ?,
                last_user_agent = ?
            WHERE visitor_id = ?
            """,
            (now_iso(), ip, user_agent[:300], trial["visitor_id"]),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM anonymous_trials WHERE visitor_id = ?",
            (trial["visitor_id"],),
        ).fetchone()

    return dict(row)
