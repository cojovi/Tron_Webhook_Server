"""FastAPI catch-all webhook ingestion server."""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

import db as dbmod
from ai_pipeline import run_sentiment_for_event

logger = logging.getLogger("webhook.server")

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def create_app(
    db_path: Path,
    event_queue: queue.Queue[dict[str, Any]],
) -> FastAPI:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        dbmod.prepare_database_file(db_path)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await dbmod.apply_async_sqlite_pragmas(conn, db_path)
        cur = await conn.execute("PRAGMA journal_mode")
        mode_row = await cur.fetchone()
        if dbmod.is_wsl_drvfs_path(db_path) and mode_row and str(mode_row[0]).lower() != "delete":
            await conn.close()
            raise RuntimeError(
                f"Refusing to run with journal_mode={mode_row[0]} on WSL /mnt/c "
                "(use DELETE so Windows and WSL share webhooks.db). Stop other servers and restart."
            )
        await conn.executescript(dbmod.SCHEMA)
        await conn.commit()
        await dbmod.migrate_events_schema(conn)
        app.state._conn = conn
        app.state.started_at = time.time()
        app.state._sentiment_tasks: set[asyncio.Task[None]] = set()
        yield
        pending = list(getattr(app.state, "_sentiment_tasks", set()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await conn.close()
        app.state._conn = None

    app = FastAPI(title="Universal Webhook Collector", version="1.0.0", lifespan=lifespan)
    app.state.db_path = db_path
    app.state.event_queue = event_queue

    def _conn() -> aiosqlite.Connection:
        c = getattr(app.state, "_conn", None)
        if c is None:
            raise RuntimeError("Database not initialized")
        return c

    async def _enqueue(event_id: int, method: str, path: str, preview: str) -> None:
        payload = {
            "type": "new_event",
            "id": event_id,
            "method": method,
            "path": path,
            "preview": preview[:200],
            "ts": time.time(),
        }
        try:
            event_queue.put_nowait(payload)
        except queue.Full:
            logger.warning(
                "TUI event queue is full (%s); dropping new_event id=%s — "
                "increase queue size or slow webhook burst; UI will still catch up via DB poll.",
                getattr(event_queue, "maxsize", "?"),
                event_id,
            )



    async def _clear_done_daily_if_needed() -> None:
        now = time.time()
        last = float(getattr(app.state, "_last_done_clear", 0.0))
        lt = time.localtime(last) if last else None
        nt = time.localtime(now)
        should = (nt.tm_hour >= 1) and (last == 0.0 or lt is None or (lt.tm_year, lt.tm_yday) != (nt.tm_year, nt.tm_yday))
        if not should:
            return
        cutoff = time.mktime((nt.tm_year, nt.tm_mon, nt.tm_mday, 1, 0, 0, nt.tm_wday, nt.tm_yday, nt.tm_isdst))
        await dbmod.clear_done_before(_conn(), cutoff)
        app.state._last_done_clear = now

    @app.get("/ui")
    async def web_ui() -> Response:
        await _clear_done_daily_if_needed()
        return FileResponse(Path(__file__).with_name("webui.html"))

    @app.get("/api/events")
    async def api_events(limit: int = 300, offset: int = 0) -> dict[str, Any]:
        await _clear_done_daily_if_needed()
        rows = await dbmod.list_events(_conn(), limit=max(1, min(limit, 1000)), offset=max(0, offset))
        return {"events": rows}

    @app.post("/api/events/{event_id}/done")
    async def api_event_done(event_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        done = bool(payload.get("done"))
        ev = await dbmod.get_event(_conn(), event_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="event not found")
        await dbmod.set_event_done(_conn(), event_id, done)
        return {"ok": True, "id": event_id, "is_done": done}

    @app.delete("/api/events/{event_id}")
    async def api_delete_event(event_id: int) -> dict[str, Any]:
        deleted = await dbmod.delete_event(_conn(), event_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="event not found")
        return {"ok": True, "id": event_id, "deleted": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        started = float(getattr(app.state, "started_at", time.time()))
        return {"ok": True, "uptime_s": round(time.time() - started, 3)}

    @app.api_route("/{full_path:path}", methods=ALL_METHODS)
    async def catch_all(request: Request, full_path: str) -> Response:
        await _clear_done_daily_if_needed()
        method = request.method.upper()
        path = "/" + full_path if full_path else "/"

        grouped: dict[str, list[str]] = defaultdict(list)
        for k, v in request.query_params.multi_items():
            grouped[k].append(v)
        query: dict[str, Any] = {}
        for k, vals in grouped.items():
            query[k] = vals[0] if len(vals) == 1 else vals

        headers = {str(k): str(v) for k, v in request.headers.items()}
        ct = headers.get("content-type") or headers.get("Content-Type")

        body = await request.body()
        client = request.client.host if request.client else None

        if not dbmod.is_push_webhook_method(method):
            if method == "HEAD":
                return Response(status_code=200)
            return JSONResponse(
                status_code=200,
                content={"ok": True, "ignored": True, "method": method},
            )

        eid = await dbmod.insert_event(
            _conn(),
            method=method,
            path=path,
            query=query,
            headers=headers,
            content_type=ct,
            body=body,
            client_host=client,
        )

        preview = body[:512].decode("utf-8", errors="replace")
        await _enqueue(eid, method, path, preview)

        task = asyncio.create_task(
            run_sentiment_for_event(
                db_path=app.state.db_path,
                event_queue=event_queue,
                event_id=eid,
                method=method,
                path=path,
                query=query,
                headers=headers,
                body=body,
            ),
            name=f"sentiment-{eid}",
        )
        tasks: set[asyncio.Task[None]] = app.state._sentiment_tasks
        tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("sentiment task for event %s failed: %s", eid, exc)

        task.add_done_callback(_done)

        if method == "HEAD":
            return Response(status_code=200)
        return JSONResponse(
            status_code=200,
            content={"ok": True, "id": eid, "received_at": time.time()},
        )

    return app
