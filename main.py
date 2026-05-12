#!/usr/bin/env python3
"""Run the webhook collector HTTP server (background thread) and the Textual TUI."""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from server import create_app
from tui import InspectorApp


def main() -> None:
    root = Path(__file__).resolve().parent
    db_path = Path(os.environ.get("WEBHOOK_DB", str(root / "webhooks.db"))).resolve()
    port = int(os.environ.get("WEBHOOK_PORT", "9876"))
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=5000)
    app = create_app(db_path, q)
    started = time.time()
    threading.Thread(
        target=lambda: uvicorn.run(app, host=host, port=port, log_level="warning"),
        daemon=True,
        name="uvicorn-webhook",
    ).start()
    time.sleep(0.35)
    InspectorApp(
        db_path=db_path,
        event_queue=q,
        listen_port=port,
        server_started=started,
    ).run()


if __name__ == "__main__":
    main()
