"""SQLite persistence for webhook events."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from rapidfuzz import fuzz

logger = logging.getLogger("webhook.db")

# WAL mode breaks SQLite on WSL paths under /mnt/c (drvfs). Use DELETE everywhere.
SCHEMA = """
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query_json TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    content_type TEXT,
    body BLOB NOT NULL,
    client_host TEXT,
    response_status INTEGER NOT NULL DEFAULT 200,
    sentiment_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at DESC);
"""


def is_wsl_drvfs_path(path: Path) -> bool:
    """True when the DB lives on a Windows drive mounted in WSL (/mnt/c/...)."""
    try:
        return sys.platform == "linux" and path.resolve().as_posix().startswith("/mnt/")
    except OSError:
        return False


def _windows_path_for_drvfs(path: Path) -> str | None:
    """/mnt/c/Users/foo -> C:\\Users\\foo for host-side Python on WSL."""
    try:
        parts = path.resolve().as_posix().split("/")
    except OSError:
        return None
    if len(parts) < 3 or parts[0] != "" or parts[1] != "mnt" or len(parts[2]) != 1:
        return None
    drive = parts[2].upper()
    return drive + ":\\" + "\\".join(parts[3:])


def _checkpoint_via_windows_host(db_path: Path) -> bool:
    """Run WAL checkpoint + DELETE journal using Windows Python (WSL interop)."""
    win_db = _windows_path_for_drvfs(db_path)
    if not win_db:
        return False
    script = (
        "import sqlite3; "
        f"c=sqlite3.connect({win_db!r}); "
        "c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); "
        "c.execute('PRAGMA journal_mode=DELETE'); "
        "c.close()"
    )
    for launcher in (
        ["cmd.exe", "/c", "python", "-c", script],
        ["cmd.exe", "/c", "py", "-3", "-c", script],
        ["powershell.exe", "-NoProfile", "-Command", script],
    ):
        try:
            subprocess.run(launcher, check=True, capture_output=True, timeout=60)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
            logger.debug("host checkpoint via %s failed: %s", launcher[0], exc)
    return False


def apply_sync_sqlite_pragmas(conn: sqlite3.Connection, db_path: Path) -> None:
    """Journal mode safe for Windows, native Linux, and WSL /mnt/c."""
    if is_wsl_drvfs_path(db_path):
        conn.execute("PRAGMA journal_mode=DELETE")
    else:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row and str(row[0]).lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        else:
            conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")


async def apply_async_sqlite_pragmas(conn: aiosqlite.Connection, db_path: Path) -> None:
    if is_wsl_drvfs_path(db_path):
        await conn.execute("PRAGMA journal_mode=DELETE")
    else:
        cur = await conn.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        if row and str(row[0]).lower() == "wal":
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await conn.execute("PRAGMA journal_mode=DELETE")
        else:
            await conn.execute("PRAGMA journal_mode=DELETE")
    await conn.execute("PRAGMA busy_timeout=5000")


def _remove_wal_sidecars(db_path: Path) -> None:
    """Drop -wal/-shm files so Windows and WSL see the same webhooks.db."""
    for suffix in ("-wal", "-shm"):
        side = db_path.parent / f"{db_path.name}{suffix}"
        if side.exists():
            try:
                side.unlink()
                logger.info("Removed SQLite sidecar %s", side)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", side, exc)


def prepare_database_file(db_path: Path) -> None:
    """Ensure the DB file can be opened from this OS (fixes WAL on WSL /mnt/c)."""
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        return

    def _try_open() -> bool:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            apply_sync_sqlite_pragmas(conn, db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if is_wsl_drvfs_path(db_path) and mode and str(mode[0]).lower() != "delete":
                raise sqlite3.OperationalError(f"expected DELETE journal on drvfs, got {mode[0]}")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            conn.commit()
            _remove_wal_sidecars(db_path)
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    if _try_open():
        return

    if is_wsl_drvfs_path(db_path) and _checkpoint_via_windows_host(db_path):
        if _try_open():
            logger.info("Converted database journal mode via Windows host: %s", db_path)
            return

    raise RuntimeError(
        f"Cannot open SQLite database at {db_path}. "
        "If you use WSL with the project on /mnt/c/, close the app on Windows and run:\n"
        "  python -c \"import sqlite3; c=sqlite3.connect('webhooks.db'); "
        "c.execute('pragma wal_checkpoint(truncate)'); "
        "c.execute('pragma journal_mode=delete'); c.close()\""
    )


async def migrate_events_schema(conn: aiosqlite.Connection) -> None:
    """Add columns introduced after first release (safe for existing databases)."""
    cur = await conn.execute("PRAGMA table_info(events)")
    rows = await cur.fetchall()
    names = {row[1] for row in rows}
    if "sentiment_json" not in names:
        await conn.execute("ALTER TABLE events ADD COLUMN sentiment_json TEXT")
        await conn.commit()


async def update_sentiment_json(conn: aiosqlite.Connection, event_id: int, payload: str) -> None:
    await conn.execute(
        "UPDATE events SET sentiment_json = ? WHERE id = ?",
        (payload, event_id),
    )
    await conn.commit()


@asynccontextmanager
async def connect(db_path: Path):
    prepare_database_file(db_path)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await apply_async_sqlite_pragmas(conn, db_path)
        await conn.executescript(SCHEMA)
        await conn.commit()
        yield conn
    finally:
        await conn.close()


async def insert_event(
    conn: aiosqlite.Connection,
    *,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    content_type: str | None,
    body: bytes,
    client_host: str | None,
    response_status: int = 200,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO events (
            received_at, method, path, query_json, headers_json,
            content_type, body, client_host, response_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(),
            method,
            path,
            json.dumps(query, ensure_ascii=False, default=str),
            json.dumps(headers, ensure_ascii=False, default=str),
            content_type or "",
            body,
            client_host or "",
            response_status,
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def get_event(conn: aiosqlite.Connection, event_id: int) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return dict(row)


async def list_events(
    conn: aiosqlite.Connection,
    *,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, received_at, method, path, content_type,
               LENGTH(body) AS body_len, client_host
        FROM events
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_all_events(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("DELETE FROM events")
    await conn.commit()
    return cur.rowcount or 0


async def fuzzy_search_event_ids(
    conn: aiosqlite.Connection,
    *,
    needle: str,
    limit: int = 200,
    score_cutoff: int = 52,
    scan_limit: int = 4000,
) -> list[int]:
    """Fuzzy-match recent events against headers + body + path (rapidfuzz)."""
    if not needle.strip():
        return []
    cur = await conn.execute(
        """
        SELECT id, method, path, headers_json, body FROM events
        ORDER BY id DESC LIMIT ?
        """,
        (scan_limit,),
    )
    rows = await cur.fetchall()
    scored: list[tuple[int, int]] = []
    for r in rows:
        hid = int(r["id"])
        try:
            body = r["body"] or b""
            btxt = body.decode("utf-8", errors="replace")
        except Exception:
            btxt = ""
        hay = " ".join(
            [
                str(r["method"] or ""),
                str(r["path"] or ""),
                str(r["headers_json"] or ""),
                btxt,
            ]
        )
        score = int(fuzz.token_set_ratio(needle, hay))
        if score >= score_cutoff:
            scored.append((score, hid))
    scored.sort(reverse=True)
    return [hid for _, hid in scored[:limit]]
