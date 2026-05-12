"""SQLite persistence for webhook events."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from rapidfuzz import fuzz

SCHEMA = """
PRAGMA journal_mode=WAL;
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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
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
