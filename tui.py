"""Textual TUI: Aurora / cyberpunk inspector for the webhook collector."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterator

import httpx
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

CYAN = "#00f0ff"
MAGENTA = "#ff007f"
BG = "#0a0a0f"


def _sparkline(values: list[int], width: int = 28) -> str:
    if not values:
        return "▁" * width
    blocks = "▁▂▃▄▅▆▇█"
    vals = list(values)[-width:]
    if len(vals) < width:
        vals = [0] * (width - len(vals)) + vals
    m = max(max(vals), 1)
    out = []
    for v in vals:
        idx = int((v / m) * (len(blocks) - 1))
        out.append(blocks[idx])
    return "".join(out)


def _method_style(method: str) -> str:
    m = method.upper()
    if m == "POST":
        return f"bold {CYAN}"
    if m == "GET":
        return "bold #ffb020"
    if m in ("PUT", "PATCH"):
        return "bold #7cff7c"
    if m == "DELETE":
        return "bold #ff6b6b"
    return "bold #c0c0ff"


def _tree_fill(tree: Tree[Any], node: Any, label: str, value: Any) -> None:
    if isinstance(value, dict):
        branch = node.add(label, data=value, expand=True)
        for k, v in value.items():
            _tree_fill(tree, branch, str(k), v)
    elif isinstance(value, list):
        branch = node.add(f"{label}", data=value, expand=len(value) < 8)
        for i, v in enumerate(value):
            _tree_fill(tree, branch, f"[{i}]", v)
    else:
        node.add_leaf(f"{label}: {value!r}")


class ConfirmClearScreen(ModalScreen[bool]):
    """Yes/No confirmation for clearing stored events."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("Clear all stored webhook events?", id="dlg_title")
            yield Label("This cannot be undone.", id="dlg_sub")
            with Horizontal(id="dlg_btns"):
                yield Static("[bold]y[/] Yes   [bold]n[/] No", id="dlg_hint")

    def on_key(self, event) -> None:
        k = event.key.lower()
        if k == "y":
            self.dismiss(True)
        elif k in ("n", "escape"):
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    CSS = """
    ConfirmClearScreen {
        align: center middle;
    }
    #dlg {
        width: 46;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #dlg_title { text-align: center; margin-bottom: 1; }
    #dlg_sub { text-align: center; color: $text-muted; margin-bottom: 1; }
    #dlg_hint { text-align: center; width: 100%; }
    """


class InspectorApp(App[None]):
    """High-contrast terminal UI for browsing captured webhooks."""

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("s", "focus_search", "Search", show=True),
        Binding("x", "try_clear", "Clear", show=True),
        Binding("r", "redeliver", "Redeliver", show=True),
        Binding("tab", "cycle_tabs", "Tabs", show=False),
    ]

    CSS = f"""
    Screen {{
        background: {BG};
        color: #e8f6ff;
    }}
    #topbar {{
        height: 3;
        dock: top;
        background: #050508;
        border-bottom: heavy {CYAN};
        padding: 0 1;
    }}
    #brand {{
        color: {MAGENTA};
        text-style: bold;
    }}
    #status_dot {{
        color: {CYAN};
        text-style: bold;
    }}
    #status_dot.pulse {{
        color: {MAGENTA};
        text-style: bold;
    }}
    #spark {{
        color: {CYAN};
    }}
    #sidebar {{
        width: 38%;
        min-width: 28;
        border-right: round {MAGENTA};
        background: #070712;
    }}
    #stream_title {{
        padding: 0 1;
        color: {CYAN};
        text-style: bold;
        border-bottom: solid #1a1a2e;
    }}
    #search {{
        margin: 0 1 1 1;
        height: 3;
        border: tall {CYAN};
    }}
    DataTable {{
        height: 1fr;
        background: #070712;
    }}
    #main {{
        width: 1fr;
        padding: 0 1 1 1;
    }}
    TabbedContent {{
        height: 1fr;
        border: round {CYAN};
        padding: 0 1;
    }}
    TabPane {{
        padding: 0 1;
    }}
    #pretty_scroll, #raw_scroll, #hdr_scroll {{
        height: 1fr;
        border: tall #1f2a44;
        background: #050508;
    }}
    Tree {{
        background: #050508;
    }}
    #foot {{
        dock: bottom;
        height: 1;
        background: #050508;
        color: #9fb7d6;
        border-top: solid #1a1a2e;
        padding: 0 1;
    }}
    Footer {{
        background: #050508;
    }}
    """

    def __init__(
        self,
        *,
        db_path: Path,
        event_queue: Queue[dict[str, Any]],
        listen_port: int,
        server_started: float,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.event_queue = event_queue
        self.listen_port = listen_port
        self.server_started = server_started
        self._tps_buckets: deque[int] = deque([0] * 36, maxlen=36)
        self._count_this_second = 0
        self._last_bucket_ts = int(time.time())
        self._selected_id: int | None = None
        base = os.environ.get("WEBHOOK_REDELIVER_BASE", "http://127.0.0.1:8080").rstrip("/")
        self._redeliver_base = base

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("⚡ Aurora Webhook Inspector", id="brand")
            yield Static(" ● ", id="status_dot")
            yield Static("online", id="status_txt")
            yield Static("  │  ", id="sep1")
            yield Static("", id="uptime")
            yield Static("  │  🕒 TPS ", id="sep2")
            yield Static("", id="spark")
            yield Static(f"  │  📦 :{self.listen_port}", id="port_lbl")
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("📦 Event Stream", id="stream_title")
                yield Input(placeholder="Fuzzy filter (payload / headers / path)…", id="search")
                yield DataTable(id="event_table", zebra_stripes=True, cursor_type="row")
            with Vertical(id="main"):
                with TabbedContent(initial="pretty"):
                    with TabPane("Pretty Payload", id="pretty"):
                        with VerticalScroll(id="pretty_scroll"):
                            yield Tree("JSON", id="json_tree")
                            yield Static("", id="pretty_static", markup=False)
                    with TabPane("Raw Body", id="raw"):
                        with VerticalScroll(id="raw_scroll"):
                            yield Static("", id="raw_body", markup=False)
                    with TabPane("Request Headers", id="hdr"):
                        with VerticalScroll(id="hdr_scroll"):
                            yield Static("", id="hdr_body", markup=False)
        yield Static(
            "  palette: s search  x clear DB  r redeliver → "
            f"{self._redeliver_base}  q quit  │  Tab: switch inspector tabs",
            id="foot",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#event_table", DataTable)
        table.add_column("id", key="id", width=6)
        table.add_column("method", key="method", width=8)
        table.add_column("path", key="path", width=24)
        table.add_column("when", key="when", width=14)
        self.set_interval(0.05, self._drain_queue)
        self.set_interval(1.0, self._roll_tps_bucket)
        self.set_interval(0.5, self._refresh_header)
        self.refresh_table()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA journal_mode=WAL")
        cx.execute("PRAGMA busy_timeout=5000")
        try:
            yield cx
        finally:
            cx.close()

    def _roll_tps_bucket(self) -> None:
        now = int(time.time())
        if now != self._last_bucket_ts:
            self._tps_buckets.append(self._count_this_second)
            self._count_this_second = 0
            self._last_bucket_ts = now

    def _drain_queue(self) -> None:
        got = False
        while True:
            try:
                msg = self.event_queue.get_nowait()
            except Empty:
                break
            if msg.get("type") == "new_event":
                got = True
                self._count_this_second += 1
        if got:
            dot = self.query_one("#status_dot", Static)
            dot.add_class("pulse")
            self.set_timer(0.5, lambda: dot.remove_class("pulse"))
            self.refresh_table()

    def _refresh_header(self) -> None:
        up = time.time() - self.server_started
        h = int(up // 3600)
        m = int((up % 3600) // 60)
        s = int(up % 60)
        self.query_one("#uptime", Static).update(f"uptime {h:02d}:{m:02d}:{s:02d}")
        vals = list(self._tps_buckets)
        if self._count_this_second:
            vals = vals + [self._count_this_second]
        self.query_one("#spark", Static).update(_sparkline(vals))

    def refresh_table(self) -> None:
        table = self.query_one("#event_table", DataTable)
        table.clear()
        needle = self.query_one("#search", Input).value.strip()
        with self._db() as cx:
            cur = cx.execute(
                """
                SELECT id, received_at, method, path, content_type, LENGTH(body) AS blen
                FROM events ORDER BY id DESC LIMIT 800
                """
            )
            rows = cur.fetchall()
        allowed = self._fuzzy_ids_sync(rows, needle) if needle else None

        for r in rows:
            eid = int(r["id"])
            if allowed is not None and eid not in allowed:
                continue
            ts = float(r["received_at"])
            when = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            method = str(r["method"])
            path = str(r["path"])
            if len(path) > 30:
                path = path[:27] + "…"
            table.add_row(
                str(eid),
                Text(method, style=_method_style(method)),
                path,
                when,
                key=str(eid),
            )

    def _fuzzy_ids_sync(self, rows: list[Any], needle: str) -> set[int]:
        from rapidfuzz import fuzz

        scored: list[tuple[int, int]] = []
        with self._db() as cx:
            for r in rows:
                eid = int(r["id"])
                cur = cx.execute("SELECT headers_json, body FROM events WHERE id = ?", (eid,))
                hr = cur.fetchone()
                if not hr:
                    continue
                try:
                    btxt = (hr["body"] or b"").decode("utf-8", errors="replace")
                except Exception:
                    btxt = ""
                hay = f"{r['method']} {r['path']} {hr['headers_json'] or ''} {btxt}"
                score = int(fuzz.token_set_ratio(needle, hay))
                if score >= 52:
                    scored.append((score, eid))
        scored.sort(reverse=True)
        return {i for _, i in scored[:500]}

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_try_clear(self) -> None:
        self.push_screen(ConfirmClearScreen(), self._on_clear_result)

    def _on_clear_result(self, ok: bool | None) -> None:
        if not ok:
            return
        with self._db() as cx:
            cx.execute("DELETE FROM events")
        self._selected_id = None
        self._clear_inspector()
        self.refresh_table()

    def _clear_inspector(self) -> None:
        self.query_one("#raw_body", Static).update("")
        self.query_one("#hdr_body", Static).update("")
        tree = self.query_one("#json_tree", Tree)
        tree.clear()
        self.query_one("#pretty_static", Static).update("")

    @on(Input.Changed, "#search")
    def on_search_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.refresh_table()

    @on(DataTable.RowSelected, "#event_table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._apply_row_key(event.row_key)

    @on(DataTable.RowHighlighted, "#event_table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._apply_row_key(event.row_key)

    def _apply_row_key(self, row_key) -> None:
        if row_key is None:
            return
        val = getattr(row_key, "value", row_key)
        if val is None:
            return
        try:
            eid = int(str(val))
        except ValueError:
            return
        self._selected_id = eid
        self._load_inspector(eid)

    def _load_inspector(self, eid: int) -> None:
        with self._db() as cx:
            cur = cx.execute("SELECT * FROM events WHERE id = ?", (eid,))
            row = cur.fetchone()
        if row is None:
            return
        body: bytes = row["body"] or b""
        headers = json.loads(row["headers_json"] or "{}")
        query = json.loads(row["query_json"] or "{}")
        ct = (row["content_type"] or "").lower()

        raw_txt = body.decode("utf-8", errors="replace")
        self.query_one("#raw_body", Static).update(raw_txt if raw_txt else "(empty body)")

        hdr_txt = json.dumps({"query": query, "headers": headers}, indent=2, ensure_ascii=False)
        self.query_one("#hdr_body", Static).update(hdr_txt)

        tree = self.query_one("#json_tree", Tree)
        tree.clear()
        tree.root.set_label("JSON")
        ps = self.query_one("#pretty_static", Static)
        ps.update("")
        is_json = "json" in ct or self._looks_json(body)
        if is_json:
            try:
                data = json.loads(raw_txt)
            except json.JSONDecodeError:
                is_json = False
            else:
                tree.root.expand()
                if isinstance(data, dict):
                    for k, v in data.items():
                        _tree_fill(tree, tree.root, str(k), v)
                elif isinstance(data, list):
                    for i, v in enumerate(data):
                        _tree_fill(tree, tree.root, f"[{i}]", v)
                else:
                    tree.root.add_leaf(repr(data))
                ps.update("")
        if not is_json:
            tree.root.add_leaf("(not JSON — see Raw tab)")
            lexer = "xml" if raw_txt.lstrip().startswith("<") else "text"
            ps.update(Syntax(raw_txt, lexer, theme="dracula"))

    @staticmethod
    def _looks_json(b: bytes) -> bool:
        s = b.lstrip()[:1]
        return s in (b"{", b"[")

    def action_cycle_tabs(self) -> None:
        tabs = self.query_one(TabbedContent)
        order = ["pretty", "raw", "hdr"]
        try:
            cur = str(tabs.active)
        except Exception:
            cur = "pretty"
        if cur in order:
            idx = (order.index(cur) + 1) % len(order)
            tabs.active = order[idx]

    def action_redeliver(self) -> None:
        threading.Thread(target=self._redeliver_job, daemon=True).start()

    def _redeliver_job(self) -> None:
        eid = self._selected_id
        if eid is None:
            self.call_from_thread(
                self.notify,
                "Select an event first (press r after selecting).",
                severity="warning",
            )
            return
        with self._db() as cx:
            cur = cx.execute("SELECT * FROM events WHERE id = ?", (eid,))
            row = cur.fetchone()
        if row is None:
            self.call_from_thread(self.notify, "Event not found.", severity="error")
            return
        method = str(row["method"])
        path = str(row["path"])
        body: bytes = row["body"] or b""
        headers = json.loads(row["headers_json"] or "{}")
        skip = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "expect",
        }
        fwd = {k: v for k, v in headers.items() if k.lower() not in skip}
        url = f"{self._redeliver_base}{path}"
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                r = client.request(method, url, content=body, headers=fwd)
            self.call_from_thread(
                self.notify,
                f"Redelivered → {url} — HTTP {r.status_code}",
                title="Redelivery",
                severity="information",
            )
        except Exception as exc:
            self.call_from_thread(self.notify, f"Redelivery failed: {exc}", severity="error")
