"""Small SQLite cache for completed video summaries.

The legacy video_context.json is imported once on first access. Cache entries
expire after the configured TTL (seven days by default); source media and
credentials are never stored here.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from ..utils.paths import CREATOR_DIR

DB_FILE = os.path.join(CREATOR_DIR, "analysis_cache.sqlite3")
LEGACY_FILE = os.path.join(CREATOR_DIR, "video_context.json")


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    os.makedirs(CREATOR_DIR, mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(CREATOR_DIR, 0o700)
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_cache (
            platform TEXT NOT NULL,
            work_id TEXT NOT NULL,
            analysis_text TEXT NOT NULL,
            created_ts REAL NOT NULL,
            PRIMARY KEY (platform, work_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    _migrate_legacy(conn)
    if os.name == "posix":
        os.chmod(DB_FILE, 0o600)
    return conn


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    marker = conn.execute("SELECT value FROM cache_meta WHERE key='legacy_json_migrated'").fetchone()
    if marker:
        return
    try:
        with open(LEGACY_FILE, encoding="utf-8") as file:
            legacy = json.load(file)
        created = os.path.getmtime(LEGACY_FILE)
    except (OSError, ValueError):
        legacy, created = {}, time.time()
    if isinstance(legacy, dict):
        rows = []
        for key, value in legacy.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(value, str) or not value.strip():
                continue
            platform, work_id = key.split(":", 1)
            if platform and work_id:
                rows.append((platform, work_id, value.strip(), created))
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO analysis_cache(platform, work_id, analysis_text, created_ts) VALUES (?, ?, ?, ?)",
                rows,
            )
    conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('legacy_json_migrated', '1')")
    conn.commit()


def get_summary(platform: str, work_id: str, ttl_days: float = 7) -> str:
    if not platform or not work_id:
        return ""
    with _db() as conn:
        row = conn.execute(
            "SELECT analysis_text, created_ts FROM analysis_cache WHERE platform=? AND work_id=?",
            (platform, work_id),
        ).fetchone()
        if not row:
            return ""
        text, created = row
        if time.time() - float(created) > max(0, float(ttl_days)) * 86400:
            conn.execute("DELETE FROM analysis_cache WHERE platform=? AND work_id=?", (platform, work_id))
            return ""
        return str(text or "")


def put_summary(platform: str, work_id: str, analysis_text: str) -> None:
    text = str(analysis_text or "").strip()
    if not platform or not work_id or not text:
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO analysis_cache(platform, work_id, analysis_text, created_ts)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(platform, work_id) DO UPDATE SET
                 analysis_text=excluded.analysis_text, created_ts=excluded.created_ts""",
            (platform, work_id, text, time.time()),
        )
        # Opportunistic cleanup keeps the cache bounded without a scheduler.
        conn.execute("DELETE FROM analysis_cache WHERE created_ts < ?", (time.time() - 90 * 86400,))
