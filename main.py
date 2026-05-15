#!/usr/bin/env python3
"""Run the webhook collector HTTP server (background thread) and the Textual TUI."""

from __future__ import annotations

import logging
import os
import queue
import socket
import sys
import threading
from threading import Lock
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import uvicorn

import db as dbmod
from server import create_app
from tui import InspectorApp


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_healthz(port: int, timeout: float = 10.0) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.1)
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    load_dotenv()
    db_path = Path(os.environ.get("WEBHOOK_DB", str(root / "webhooks.db"))).resolve()
    dbmod.prepare_database_file(db_path)
    port = int(os.environ.get("WEBHOOK_PORT", "9876"))
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20_000)
    speech_lock = Lock()
    if _port_in_use(port):
        print(
            f"ERROR: port {port} is already in use.\n"
            "Another webhook server is running (often a stale `python main.py`).\n"
            "Stop it first, then restart:\n"
            "  wsl bash kill_port.sh\n"
            "  # or: kill the other terminal running main.py\n",
            file=sys.stderr,
        )
        sys.exit(1)

    app = create_app(db_path, q)
    started = time.time()
    threading.Thread(
        target=lambda: uvicorn.run(app, host=host, port=port, log_level="warning"),
        daemon=True,
        name="uvicorn-webhook",
    ).start()
    if not _wait_for_healthz(port):
        print(
            f"ERROR: HTTP server did not start on port {port}.\n"
            "Check logs above; the dashboard cannot receive webhooks without it.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    logging.getLogger("webhook").info(
        "Webhook server ready on %s:%s (db=%s)", host, port, db_path
    )
    InspectorApp(
        db_path=db_path,
        event_queue=q,
        listen_port=port,
        server_started=started,
        speech_lock=speech_lock,
    ).run()


if __name__ == "__main__":
    main()
