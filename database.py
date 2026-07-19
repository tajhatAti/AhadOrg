"""
database.py — Dual-dialect database layer (SQLite + PostgreSQL).

Why this exists
---------------
The app was born on SQLite. To make data *permanently* persistent we now also
support PostgreSQL (e.g. Supabase free tier). Set the env var below to choose:

    DATABASE_URL=postgresql://user:pass@host:5432/dbname   -> PostgreSQL
    (unset / sqlite) + DB_PATH=.../database.db              -> SQLite  (default)

Both code paths expose the *same* Python API so app.py never has to know which
engine it is talking to:

    from database import get_db_connection, init_db, IntegrityError

    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (1,)).fetchone()
    print(row["username"])          # dict-style access works on both engines
    conn.execute("INSERT INTO ... VALUES (?, ?)", (a, b))
    new_id = cursor.lastrowid       # works on PG too (via RETURNING id)
    conn.commit(); conn.close()

Dialect differences handled transparently:
    * placeholder style          ?  ->  %s
    * lastrowid on INSERT        implemented via "... RETURNING id" on PG
    * AUTOINCREMENT              ->  SERIAL
    * case-insensitive columns   COLLATE NOCASE  ->  CITEXT
    * upsert                     INSERT OR REPLACE  ->  INSERT ... ON CONFLICT
    * connection lifecycle       PG uses a thread-safe connection pool
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("ahad-co-db")

# ---------------------------------------------------------------------------
# Dialect selection
# ---------------------------------------------------------------------------
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if _DATABASE_URL.startswith("postgres://") or _DATABASE_URL.startswith("postgresql://"):
    DIALECT = "postgres"
else:
    DIALECT = "sqlite"

# SQLite on-disk path (only used when DIALECT == "sqlite")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "database.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# SSL mode for PostgreSQL connections (Supabase & most managed Postgres need SSL).
# Users can force a value with PG_SSLMODE; we default to "prefer" so it works on
# managed (SSL) hosts as well as a local no-SSL dev server.
PG_SSLMODE = os.getenv("PG_SSLMODE", "prefer")

# ---------------------------------------------------------------------------
# DATABASE_URL sanity check
# ---------------------------------------------------------------------------
# A malformed DATABASE_URL makes psycopg2 fail deep inside libpq with a cryptic
# DNS-style error ("could not translate host name \"<garbage>\" to address").
# The most common cause: the password contains raw special characters ('#',
# '@', spaces) that were not percent-encoded, so the URL parser glues password
# fragments onto the hostname. Catch that HERE, at startup, and fail with an
# actionable message instead of a 50-line stack trace.
def _validate_database_url(url: str) -> None:
    from urllib.parse import urlparse

    problems: list[str] = []

    # 1) Accidental whitespace/newlines from copy-pasting into dashboards.
    if any(ch.isspace() for ch in url):
        problems.append("contains whitespace (accidental space/newline while copy-pasting?)")

    # 2) A raw '#' truncates the URL at the fragment marker and silently eats
    #    the rest of the password. In a valid URL it only appears as %23.
    if "#" in url:
        problems.append("raw '#' found in the URL — encode it as %23 (password character)")

    # 3) More than one '@' means the password holds an unencoded '@'.
    if url.split("://", 1)[-1].count("@") > 1:
        problems.append("more than one '@' found — encode '@' inside the password as %40")

    # 4) Structural checks: scheme, hostname, port, password.
    try:
        parsed = urlparse(url)
        host = parsed.hostname            # may raise ValueError on bad port
        _ = parsed.port
        if not host:
            problems.append("no hostname found (expected e.g. 'xyz.pooler.supabase.com')")
        if parsed.password is None:
            problems.append("no password found (expected form: postgresql://user:PASSWORD@host:5432/db)")
    except ValueError as exc:
        problems.append(f"cannot be parsed ({exc})")

    if problems:
        bullet_list = "\n".join(f"    * {p}" for p in problems)
        raise RuntimeError(
            "\n\n"
            "==============================================================\n"
            "  DATABASE_URL looks malformed — refusing to start.\n"
            "--------------------------------------------------------------\n"
            f"{bullet_list}\n\n"
            "  Fix: copy the exact connection string from your provider\n"
            "  (Supabase -> Project Settings -> Database -> Session pooler)\n"
            "  and percent-encode special characters in the PASSWORD:\n"
            "      #  ->  %23        @  ->  %40        space  ->  %20\n"
            "  (পাসওয়ার্ডে #, @ বা স্পেস থাকলে অবশ্যই encode করতে হবে।)\n"
            "  Example:\n"
            "      postgresql://postgres.REF:ENCODED_PASSWORD@HOST:5432/postgres\n"
            "==============================================================\n"
        )

if DIALECT == "postgres":
    _validate_database_url(_DATABASE_URL)

logger.info("Database dialect: %s", DIALECT)

# ---------------------------------------------------------------------------
# Lazy driver import + exception aliases (so app.py imports a single name)
# ---------------------------------------------------------------------------
_psycopg2 = None
_pool: object | None = None
_pool_lock = threading.Lock()


def _load_psycopg2():
    """Import psycopg2 lazily so the SQLite-only path never requires it."""
    global _psycopg2
    if _psycopg2 is None:
        import psycopg2  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        from psycopg2 import pool as _pg_pool  # type: ignore
        from psycopg2 import errors as _pg_errors  # type: ignore
        _psycopg2 = {
            "connect": psycopg2.connect,
            "RealDictCursor": RealDictCursor,
            "pool": _pg_pool,
            "errors": _pg_errors,
            "IntegrityError": psycopg2.IntegrityError,
            "OperationalError": psycopg2.OperationalError,
        }
    return _psycopg2


# Exception aliases re-exported for app.py
if DIALECT == "postgres":
    _drv = _load_psycopg2()
    IntegrityError = _drv["IntegrityError"]
    OperationalError = _drv["OperationalError"]
else:
    IntegrityError = sqlite3.IntegrityError
    OperationalError = sqlite3.OperationalError


# ---------------------------------------------------------------------------
# SQL translation helpers
# ---------------------------------------------------------------------------
def _translate_sql(sql: str) -> str:
    """Translate a qmark-style (? placeholders) statement to the active dialect.

    PostgreSQL's psycopg2 driver uses %s placeholders. Literal '%' is not used
    anywhere in the app's SQL (no LIKE with %), so a straight swap is safe.
    """
    if DIALECT == "postgres":
        return sql.replace("?", "%s")
    return sql


def _translate_ddl(ddl: str) -> str:
    """Translate SQLite-specific CREATE TABLE syntax to PostgreSQL."""
    if DIALECT != "postgres":
        return ddl
    # Case-insensitive uniqueness mirrors SQLite's "COLLATE NOCASE".
    ddl = ddl.replace("TEXT NOT NULL UNIQUE COLLATE NOCASE", "CITEXT NOT NULL UNIQUE")
    # Auto-increment integer PK -> SERIAL.
    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    return ddl


# ---------------------------------------------------------------------------
# Cursor / Connection wrappers — present one API for both engines
# ---------------------------------------------------------------------------
class _Cursor:
    """Wraps a sqlite3 or psycopg2 cursor.

    Notable behaviour for PostgreSQL:
      * qmark (?) placeholders are rewritten to %s.
      * For INSERT statements we append " RETURNING id" and read the new id so
        that `cursor.lastrowid` behaves like SQLite's.
    """

    def __init__(self, cur):
        self._cur = cur
        self._returning_id = None

    def execute(self, sql, params=()):
        sql_t = _translate_sql(sql)
        if DIALECT == "postgres":
            head = sql_t.lstrip()[:6].upper()
            if head == "INSERT":
                # Append RETURNING id (strip a trailing semicolon if present).
                body = sql_t.rstrip().rstrip(";")
                self._cur.execute(body + " RETURNING id", tuple(params))
                row = self._cur.fetchone()
                self._returning_id = row["id"] if row else None
                return self
        try:
            self._cur.execute(sql_t, tuple(params))
        except Exception as exc:
            # Every query in the app passes through HERE — log failures so a
            # leftover SQLite-only construct (or any DB error) is visible in
            # logs immediately instead of hiding behind a 500. Params are
            # intentionally NOT logged: they can carry code secrets.
            logger.error(
                "DB query failed [%s]: %s | SQL: %s",
                DIALECT, type(exc).__name__, " ".join(sql_t.split())[:500],
                exc_info=False,
            )
            raise
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(_translate_sql(sql), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        if DIALECT == "postgres":
            return self._returning_id
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()


class _Connection:
    """Wraps a sqlite3 or psycopg2 connection with the unified cursor API."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = _Cursor(self._conn.cursor())
        cur.execute(sql, params)
        return cur

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if DIALECT == "postgres":
            try:
                self._conn.rollback()  # clear any open txn before returning to pool
            except Exception:
                pass
            _return_to_pool(self._conn)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


# ---------------------------------------------------------------------------
# PostgreSQL connection pool
# ---------------------------------------------------------------------------
#
# WHY THE EXTRA MACHINERY BELOW (read: the "site hangs after a while" fix):
#   Render's free instances sleep after ~15 min idle, and Supabase's pooler —
#   plus every NAT between them — silently drops idle TCP sessions. A pooled
#   connection can therefore be dead WITHOUT psycopg2 knowing, and the first
#   query on it hangs for minutes (default TCP timeouts are ~2 HOURS).
#   Three defences, layered:
#     1. TCP keepalives + connect_timeout on every new connection, so a dead
#        socket is detected in ~80s instead of ~2h, and connects cap at 10s.
#     2. After a long idle gap the instance almost surely slept: rebuild the
#        whole pool BEFORE handing anything out — fresh connects are ~200ms.
#     3. Probe every checkout with SELECT 1; discard-and-retry dead conns.
_POOL_IDLE_RESET_S = 90.0   # more silence than this => do not trust the pool
_last_db_activity = 0.0     # monotonic timestamp of the last healthy checkout


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            drv = _load_psycopg2()
            SimpleConnectionPool = drv["pool"].SimpleConnectionPool
            # sslmode can also live inside DATABASE_URL; only inject a default
            # when the user hasn't already specified one.
            dsn = _DATABASE_URL
            connect_kwargs = {
                # libpq knobs (psycopg2 passes these straight through)
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            }
            if "application_name" not in dsn:
                connect_kwargs["application_name"] = "ahad-co"
            if "sslmode" not in _DATABASE_URL and PG_SSLMODE:
                connect_kwargs["sslmode"] = PG_SSLMODE
            logger.info("Creating PostgreSQL connection pool (maxconn=8)")
            _pool = SimpleConnectionPool(
                minconn=1,
                maxconn=8,
                dsn=dsn,
                cursor_factory=drv["RealDictCursor"],
                **connect_kwargs,
            )
    return _pool


def _reset_pool():
    """Kill every pooled connection; the next _get_pool() builds a fresh pool."""
    global _pool
    with _pool_lock:
        old, _pool = _pool, None
    if old is not None:
        try:
            old.closeall()
        except Exception:
            pass


def _checkout_pg():
    """Take a LIVE connection out of the pool (see defences at the top)."""
    global _last_db_activity
    now = time.monotonic()
    if _last_db_activity and (now - _last_db_activity) > _POOL_IDLE_RESET_S:
        # Defence 2 — we almost certainly slept; don't even bother probing
        # connections that were frozen alongside the process.
        logger.info("PostgreSQL pool idle %.0fs — rebuilding (post-sleep safety)",
                    now - _last_db_activity)
        _reset_pool()
    pool = _get_pool()
    for _ in range(3):
        raw = pool.getconn()
        try:
            # Defence 3 — cheap liveness probe (round trip is ~ms, a dead
            # conn costs a hang if we skip this)
            cur = raw.cursor()
            cur.execute("SELECT 1")
            cur.close()
            _last_db_activity = time.monotonic()
            return raw
        except Exception as exc:
            logger.warning("Discarding dead pooled PostgreSQL connection: %s",
                           type(exc).__name__)
            try:
                pool.putconn(raw, close=True)   # close & drop from the pool
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass
    # Everything we were offered was dead — nuke the pool and take a fresh one.
    _reset_pool()
    raw = _get_pool().getconn()
    _last_db_activity = time.monotonic()
    return raw


def _return_to_pool(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        # If returning fails (e.g. pool closed) make sure we don't leak.
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public: get a connection
# ---------------------------------------------------------------------------
def get_db_connection() -> _Connection:
    if DIALECT == "postgres":
        return _Connection(_checkout_pg())

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return _Connection(conn)


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------
_SCHEMA_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password TEXT NOT NULL,
        otp TEXT,
        otp_created_at TEXT,
        is_verified INTEGER NOT NULL DEFAULT 0,
        reset_otp TEXT,
        reset_otp_created_at TEXT,
        reset_verified INTEGER NOT NULL DEFAULT 0,
        password_changed_at TEXT,
        otp_attempts INTEGER NOT NULL DEFAULT 0,
        reset_otp_attempts INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'user',
        phone TEXT,
        custom_code TEXT,
        links TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        device_info TEXT,
        ip_address TEXT,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_2fa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        secret TEXT,
        is_enabled INTEGER NOT NULL DEFAULT 0,
        backup_codes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ip_address TEXT,
        device_info TEXT,
        location TEXT,
        success INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        theme TEXT DEFAULT 'dark',
        language TEXT DEFAULT 'en',
        timezone TEXT DEFAULT 'UTC',
        notifications_enabled INTEGER DEFAULT 1,
        email_notifications INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        language TEXT NOT NULL,
        code TEXT NOT NULL,
        runner_job_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        ip_address TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Admin panel: every destructive action lands here (who did what, when).
    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        details TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    -- Public "Report abuse" inbox for live URLs / published pages.
    CREATE TABLE IF NOT EXISTS abuse_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        reason TEXT,
        ip TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snippets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        language TEXT,
        content TEXT NOT NULL,
        share_token TEXT UNIQUE,
        is_public INTEGER DEFAULT 0,
        views INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
]


def _column_exists(conn: _Connection, table: str, column: str) -> bool:
    """True if `column` exists on `table` (works on both engines)."""
    if DIALECT == "postgres":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return bool(row)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    """Create all tables if missing. Safe to run on every startup."""
    conn = get_db_connection()
    try:
        if DIALECT == "postgres":
            # CITEXT gives us case-insensitive username/email (== SQLite NOCASE)
            conn.execute("CREATE EXTENSION IF NOT EXISTS citext")

        for ddl in _SCHEMA_TABLES:
            conn.execute(_translate_ddl(ddl))


        # ------------------------------------------------------------------
        # MIGRATION 001 (developer-first pivot): the vault product is gone.
        # Drop every table that backed Vault/Cards/IDs/Contacts/WiFi/Servers/
        # Seeds/Notes/Bookmarks/Tasks (+ never-used api_keys/notifications).
        # Idempotent — safe on every boot. Kept tables (users, sessions,
        # user_2fa, jobs, snippets, activity_log, admin_audit_log, abuse_reports,
        # login_history, user_preferences) only reference users, never these.
        _DROPPED_VAULT_TABLES = (
            "wifi_shares", "user_wifi",  # child first (FK parent second)
            "vault_entries", "user_notes", "user_bookmarks", "user_categories",
            "user_cards", "user_tasks", "user_identities", "user_contacts",
            "user_servers", "user_recovery", "api_keys", "notifications",
        )
        for _t in _DROPPED_VAULT_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {_t}")

        # Legacy-DB migration: ensure the `role` column exists on users.
        if not _column_exists(conn, "users", "role"):
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

        # Wrong-OTP-attempt counters (server-side OTP rate limiting).
        if not _column_exists(conn, "users", "otp_attempts"):
            conn.execute("ALTER TABLE users ADD COLUMN otp_attempts INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "reset_otp_attempts"):
            conn.execute("ALTER TABLE users ADD COLUMN reset_otp_attempts INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "password_changed_at"):
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")

        # Pivot: admin flag, suspension flag, terms-of-use acceptance stamp.
        if not _column_exists(conn, "users", "is_admin"):
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "is_suspended"):
            conn.execute("ALTER TABLE users ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "agreed_terms_at"):
            conn.execute("ALTER TABLE users ADD COLUMN agreed_terms_at TEXT")

        if not _column_exists(conn, "jobs", "env_vars"):
            conn.execute("ALTER TABLE jobs ADD COLUMN env_vars TEXT")

        conn.commit()
    finally:
        conn.close()

    if DIALECT == "postgres":
        logger.info("PostgreSQL schema initialized (pool ready)")
    else:
        logger.info("SQLite database initialized at: %s", DB_PATH)
