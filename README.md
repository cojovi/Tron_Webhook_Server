# Aurora Webhook Collector & Inspector

A **local development tool** that listens for HTTP traffic on any path, **saves everything** to a SQLite database, and shows a **terminal user interface (TUI)** so you can browse requests in real time—without polling a web dashboard.

If you have ever wondered “what did Stripe / GitHub / my app actually send?” this tool catches the full request (method, path, query string, headers, raw body) and lets you inspect it from the terminal.

---

## What you need first (beginner checklist)

1. **Python 3.10 or newer** installed. Check in a terminal:

   ```bash
   python3 --version
   ```

   You should see something like `Python 3.12.x`. If the command fails, install Python from your operating system’s package manager or from [python.org](https://www.python.org/downloads/).

2. **A terminal** that supports modern TUIs (most terminals on Linux, macOS, and Windows Terminal on Windows work well).

3. **Optional but nice:** a font that supports icons (many default fonts already show the emoji-style icons used in the UI). [Nerd Fonts](https://www.nerdfonts.com/) are a popular upgrade if glyphs look wrong.

---

## What this project does (in plain language)

| Piece | What it does |
|--------|----------------|
| **HTTP server** | Opens a port (default **9876**) and accepts **any path** and several HTTP methods. There is no “wrong URL”—everything is recorded. |
| **SQLite database** | Stores each request: time, method, path, query parameters, headers, content type, and the **raw body** (JSON, XML, form data, plain text, etc.). |
| **Queue (pub/sub)** | When a request arrives, the server pushes a small message onto an in-memory queue. The TUI reads that queue so the list updates **immediately** without refreshing a browser. |
| **TUI** | A full-screen terminal app: list of events on the left, detail tabs on the right, hotkeys at the bottom. |

There is also a tiny **`/healthz`** endpoint used for health checks; it is **not** stored as a normal “webhook event” in the same way as arbitrary paths—your integrations should use their own paths (for example `/webhooks/stripe`).

---

## Installation (recommended: virtual environment)

Using a **virtual environment** keeps these libraries separate from your system Python. On many Linux distributions, installing packages system-wide is blocked (this is normal and good for safety).

### Step 1: Open a terminal in this folder

```bash
cd /path/to/webhook
```

Use the real path where you cloned or copied this project (for example `~/dev/webhook`).

### Step 2: Create the virtual environment

```bash
python3 -m venv .venv
```

This creates a folder named `.venv` next to the code.

### Step 3: Activate the virtual environment

**Linux / macOS:**

```bash
source .venv/bin/activate
```

**Windows (Command Prompt):**

```cmd
.venv\Scripts\activate.bat
```

**Windows (PowerShell):**

```powershell
.venv\Scripts\Activate.ps1
```

After activation, your prompt often shows `(.venv)`.

### Step 4: Install dependencies

```bash
pip install -r requirements.txt
```

Wait until it finishes without errors.

---

## How to run the app

With the virtual environment **activated** and your shell **inside the project folder**:

```bash
python main.py
```

You should see the **Aurora Webhook Inspector** TUI fill the terminal, and the server is already listening in the background.

### Stop the app

Press **`q`** (quit) inside the TUI, or use **Ctrl+C** if your terminal focuses the process that way.

---

## Send your first test webhook (from another terminal)

Leave the app running. Open a **second** terminal (virtual environment does not need to be active for `curl`).

**POST JSON:**

```bash
curl -s -X POST "http://127.0.0.1:9876/demo/hook" \
  -H "Content-Type: application/json" \
  -d '{"hello":"world","n":1}'
```

**GET with query parameters:**

```bash
curl -s "http://127.0.0.1:9876/search?q=webhook&debug=1"
```

**Raw XML:**

```bash
curl -s -X PUT "http://127.0.0.1:9876/xml" \
  -H "Content-Type: application/xml" \
  -d '<root><item id="1"/></root>'
```

If your server uses a different port, replace **9876** with your `WEBHOOK_PORT` value (see below).

Back in the TUI, new rows should appear in the **Event Stream** on the left. Select a row (click or arrow keys) to load the **Pretty**, **Raw**, and **Headers** tabs.

---

## Environment variables (all optional)

These are read when you start `main.py`. You can set them in the shell before running, or in a tool-specific environment panel.

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEBHOOK_PORT` | `9876` | TCP port the HTTP server listens on. |
| `WEBHOOK_HOST` | `0.0.0.0` | Bind address. `0.0.0.0` means “all network interfaces” (reachable from other machines on your LAN if your firewall allows). Use `127.0.0.1` to listen **only on your own computer** (slightly safer for local experiments). |
| `WEBHOOK_DB` | `webhooks.db` in the project folder | Path to the SQLite database file. |
| `WEBHOOK_REDELIVER_BASE` | `http://127.0.0.1:8080` | Base URL used when you press **`r`** to **redeliver** a captured request (see below). |

**Examples:**

```bash
export WEBHOOK_PORT=9000
export WEBHOOK_HOST=127.0.0.1
python main.py
```

```bash
WEBHOOK_DB=/tmp/my-hooks.db WEBHOOK_PORT=7777 python main.py
```

---

## Using the TUI (screen tour)

### Top bar

- **Status** and a short **highlight** when a new event arrives.
- **Uptime** since the app started.
- **TPS sparkline** (rough “traffic per recent time buckets”) so you can see bursts at a glance.
- **Port** the server is using.

### Left: Event Stream

- Table of recent events: **id**, **method**, **path**, **time**.
- Methods are color-coded (for example POST vs GET).
- **Search box:** type to **fuzzy-filter** by payload text, headers, path, or method. Clear the box to show everything again.

### Right: Inspector tabs

- **Pretty Payload:** If the body looks like **JSON**, you get a **collapsible tree** plus syntax-friendly display. Otherwise you get highlighted text (for example XML-like content when it starts with `<`).
- **Raw Body:** Exactly what was received, decoded as UTF-8 (replacement characters may appear for invalid bytes).
- **Request Headers:** JSON showing **`query`** and **`headers`** together so you do not miss URL parameters.

### Bottom bar

- Reminder of important keys (search, clear, redelivery, quit).

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| **`s`** | Focus the fuzzy search box. |
| **`x`** | **Clear** all stored events from the database (you will be asked **y** / **n** to confirm). |
| **`r`** | **Redeliver** the currently selected event: repeats the same HTTP method, path (appended to the base URL), body, and most headers to **`WEBHOOK_REDELIVER_BASE`**. Handy when your real app runs on localhost and you want to replay one payload. |
| **`Tab`** | Cycle the inspector tabs (Pretty → Raw → Headers). |
| **`q`** | Quit the application. |

**Redelivery details:** Some hop-by-hop headers are stripped (`Host`, `Content-Length`, `Connection`, etc.) so the new request is well-formed. If nothing is selected, you will get a warning notification.

---

## Where is my data?

By default, SQLite writes **`webhooks.db`** in the same directory as `main.py`. You can open it with any SQLite client, for example:

```bash
sqlite3 webhooks.db "SELECT id, method, path FROM events ORDER BY id DESC LIMIT 5;"
```

---

## Troubleshooting (common beginner issues)

### “Port already in use”

Something else is listening on **9876** (or whatever you chose). Either stop the other program or set another port:

```bash
export WEBHOOK_PORT=9880
python main.py
```

### `pip install` fails with “externally managed environment”

Your OS is protecting system Python. Always use a **virtual environment** (the steps above). Do not use `--break-system-packages` unless you know why you need it.

### The TUI looks cramped or misaligned

- Maximize the terminal window.
- Try a larger font or a terminal with better Unicode support.

### I bound to `0.0.0.0` and worry about security

`0.0.0.0` means other devices on your network **might** reach the collector if your firewall allows it. For purely local debugging, use:

```bash
export WEBHOOK_HOST=127.0.0.1
python main.py
```

This tool is meant for **development and debugging**, not as a hardened internet-facing service.

### `curl` cannot connect

- Confirm the app is running.
- Confirm you use the same port as shown in the TUI header.
- If you use `127.0.0.1` vs `localhost`, both should work on most systems; if one fails, try the other.

---

## Project files (map of the repository)

| File | Purpose |
|------|---------|
| `main.py` | Entry point: starts **uvicorn** in a background thread, then runs the **Textual** UI. |
| `server.py` | FastAPI application: catch-all route, `/healthz`, writes to SQLite, enqueues events. |
| `db.py` | Database schema and async helpers used by the server. |
| `tui.py` | Terminal UI layout, theming, search, redelivery, and SQLite reads for the table and inspector. |
| `requirements.txt` | Python dependencies and minimum versions. |

---

## Requirements summary (for reference)

- **Python:** 3.10+ recommended.
- **Libraries:** FastAPI, Uvicorn, Textual, aiosqlite, rapidfuzz, httpx (see `requirements.txt`).

---

## License

No license file is included in this folder by default. If you publish this project, add a `LICENSE` file that matches how you want others to use the code.
