import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


AUTH_DB = Path("data/app_auth.db")
AUTH_DB.parent.mkdir(parents=True, exist_ok=True)

PBKDF2_ITERATIONS = 310_000
SESSION_DAYS = 7


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(AUTH_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'standard')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )

        conn.commit()

        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "csrf_token_hash" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN csrf_token_hash TEXT")
            conn.commit()


def normalize_username(username):
    return str(username or "").strip().lower()


def hash_password(password):
    if not password or len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )

    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored):
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False

        iterations = int(iterations)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )

        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_user(username, password, role="standard"):
    init_auth_db()

    username = normalize_username(username)
    role = role if role in {"admin", "standard"} else "standard"

    if not username:
        raise ValueError("Username is required.")

    password_hash = hash_password(password)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (username, password_hash, role, iso(utc_now())),
        )
        conn.commit()

    return username


def get_user_by_username(username):
    init_auth_db()
    username = normalize_username(username)

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,),
        ).fetchone()

    return dict(row) if row else None


def authenticate_user(username, password):
    user = get_user_by_username(username)

    if not user:
        return None

    if not verify_password(password, user["password_hash"]):
        return None

    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
    }


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user):
    init_auth_db()

    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_token(raw_token)
    now = utc_now()
    expires = now + timedelta(days=SESSION_DAYS)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user["id"], token_hash, iso(now), iso(expires)),
        )
        conn.commit()

    return raw_token


def get_user_from_token(token):
    init_auth_db()

    if not token:
        return None

    token_hash = hash_token(token)

    with connect() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.username, users.role, sessions.expires_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND users.is_active = 1
            """,
            (token_hash,),
        ).fetchone()

    if not row:
        return None

    expires_at = datetime.fromisoformat(row["expires_at"])

    if expires_at < utc_now():
        delete_session(token)
        return None

    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
    }


def delete_session(token):
    if not token:
        return

    token_hash = hash_token(token)

    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()


def _active_session_row(conn, session_token_hash, columns):
    return conn.execute(
        f"""
        SELECT {columns}
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
          AND users.is_active = 1
        """,
        (session_token_hash,),
    ).fetchone()


def issue_csrf_token(raw_session_token):
    """Generate a fresh CSRF token for the exact active session identified by
    raw_session_token, storing only its hash, and return the raw value once.
    Returns None if the session is missing, inactive, or expired."""
    init_auth_db()

    if not raw_session_token:
        return None

    session_token_hash = hash_token(raw_session_token)

    with connect() as conn:
        row = _active_session_row(conn, session_token_hash, "sessions.id, sessions.expires_at")
        if not row:
            return None

        if datetime.fromisoformat(row["expires_at"]) < utc_now():
            return None

        raw_csrf_token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE sessions SET csrf_token_hash = ? WHERE id = ?",
            (hash_token(raw_csrf_token), row["id"]),
        )
        conn.commit()

    return raw_csrf_token


def verify_csrf_token(raw_session_token, submitted_csrf_token):
    """Validate a submitted CSRF token against the exact active session
    identified by raw_session_token. Fails closed on any missing/expired/
    mismatched input."""
    init_auth_db()

    if not raw_session_token or not submitted_csrf_token:
        return False

    session_token_hash = hash_token(raw_session_token)

    with connect() as conn:
        row = _active_session_row(
            conn, session_token_hash, "sessions.csrf_token_hash, sessions.expires_at"
        )

    if not row or not row["csrf_token_hash"]:
        return False

    if datetime.fromisoformat(row["expires_at"]) < utc_now():
        return False

    submitted_hash = hash_token(submitted_csrf_token)
    return secrets.compare_digest(row["csrf_token_hash"], submitted_hash)


def is_admin(user):
    return bool(user and user.get("role") == "admin")


def cookie_secure_enabled():
    return os.environ.get("APP_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}


# === LOGIN RATE LIMIT DB HELPERS START ===
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 10 * 60


def _ensure_login_attempts_table():
    init_auth_db()
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_key_time ON login_attempts(key, attempted_at)"
        )
        conn.commit()


def login_rate_key(client_host, username):
    clean_host = str(client_host or "").strip()[:80]
    clean_user = normalize_username(username)[:254]
    return f"{clean_host}|{clean_user}"


def login_is_limited(client_host, username):
    import time

    _ensure_login_attempts_table()

    key = login_rate_key(client_host, username)
    cutoff = int(time.time()) - LOGIN_WINDOW_SECONDS

    with connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key = ? AND attempted_at >= ?",
            (key, cutoff),
        ).fetchone()[0]
        conn.commit()

    return count >= LOGIN_MAX_ATTEMPTS


def login_record_failure(client_host, username):
    import time

    _ensure_login_attempts_table()

    key = login_rate_key(client_host, username)
    now = int(time.time())
    cutoff = now - LOGIN_WINDOW_SECONDS

    with connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        conn.execute(
            "INSERT INTO login_attempts (key, attempted_at) VALUES (?, ?)",
            (key, now),
        )
        conn.commit()


def login_clear_failures(client_host, username):
    _ensure_login_attempts_table()

    key = login_rate_key(client_host, username)

    with connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE key = ?", (key,))
        conn.commit()
# === LOGIN RATE LIMIT DB HELPERS END ===


def user_exists(username):
    return get_user_by_username(username) is not None
