"""FastAPI catch-all webhook ingestion server."""

from __future__ import annotations

import queue
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

import db as dbmod

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def create_app(
    db_path: Path,
    event_queue: queue.Queue[dict[str, Any]],
) -> FastAPI:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(dbmod.SCHEMA)
        await conn.commit()
        app.state._conn = conn
        app.state.started_at = time.time()
        yield
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
            pass

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        started = float(getattr(app.state, "started_at", time.time()))
        return {"ok": True, "uptime_s": round(time.time() - started, 3)}

    @app.api_route("/{full_path:path}", methods=ALL_METHODS)
    async def catch_all(request: Request, full_path: str) -> Response:
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

        if method == "HEAD":
            return Response(status_code=200)
        return JSONResponse(
            status_code=200,
            content={"ok": True, "id": eid, "received_at": time.time()},
        )

    return app
