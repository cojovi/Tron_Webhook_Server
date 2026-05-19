# Aurora Webhook Collector & Inspector

A **local development tool** that listens for HTTP traffic on any path, **persists POST webhooks** to a SQLite database, and provides both a **terminal user interface (TUI)** and a **TRON-themed web dashboard** so you can browse and manage requests in real time.

If you have ever wondered “what did Stripe / GitHub / my app actually send?” this tool catches the full request (method, path, query string, headers, raw body) for **POST** deliveries—the usual webhook shape—and lets you inspect them from the terminal or browser. Browser noise (**GET**, **HEAD**, **OPTIONS**, etc.) is accepted with `200 OK` but **not stored, shown, or spoken**.

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
| **HTTP server** | Opens a port (default **9876**) and accepts **any path** and several HTTP methods. **POST** (and non-standard **PUSH**) payloads are stored; other methods (e.g. **GET**) return success but are ignored for storage and UI. |
| **SQLite database** | Stores each tracked request: time, method, path, query parameters, headers, content type, **raw body**, optional **sentiment** JSON, and **done** state. |
| **Queue (pub/sub)** | When a POST webhook is saved, the server pushes a small message onto an in-memory queue. The TUI reads that queue so the list updates **immediately** without refreshing a browser. |
| **TUI** | A full-screen terminal app: event stream on the left, detail tabs on the right, hotkeys at the bottom. Lists **POST/PUSH only**. |
| **Web dashboard** | Browser UI at **`/ui`**: active queue, done today, mark done / re-open / **remove**, live feed count, text filter. **POST/PUSH only**. |

There is also a tiny **`/healthz`** endpoint used for health checks; it is **not** stored as a webhook event. Your integrations should POST to their own paths (for example `/webhooks/stripe`).

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


## Web UI (TRON dashboard)

In addition to the TUI, the server exposes a browser dashboard at:

```
http://127.0.0.1:9876/ui
```

What it includes:
- **LIVE FEED** — **Total events** count (POST/PUSH rows only; removed items are not counted)
- **Active Queue** — open items on any path **except** `/tasks`, `/reminders`, and `/email`
- **Tasks** — open items posted to **`/tasks`** (path match is case-insensitive)
- **Reminders** — open items posted to **`/reminders`** (e.g. `/Reminders`)
- **Email** — open items posted to **`/email`**
- **Done Today** — completed items from all lanes
- **Mark Done** on active items; **Re-open** and **Remove** on done items
- **Remove** permanently deletes the row from SQLite and drops it from the feed count
- Live polling refresh (every few seconds) and a text filter (path, method, body preview)

Completed items are auto-cleared once per day after **1:00 AM** (server local time).

### Web API (used by the dashboard)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/events?limit=300` | List stored events (POST/PUSH only) |
| `POST` | `/api/events/{id}/done` | Body `{"done": true}` or `{"done": false}` — mark complete or re-open |
| `DELETE` | `/api/events/{id}` | Permanently delete one event |

---

## POST-only webhooks (what gets tracked)

The collector is tuned for **push-style webhooks**, not browser page loads:

| HTTP method | Stored in DB? | TUI / `/ui` | Gemini / TTS |
|-------------|---------------|-------------|--------------|
| **POST** | Yes | Yes | Yes (if configured) |
| **PUSH** | Yes (non-standard; accepted if a client sends it) | Yes | Yes |
| **GET**, **HEAD**, **OPTIONS**, **PUT**, etc. | No | No | No |

Ignored requests still return **`200 OK`** with JSON like `{"ok": true, "ignored": true, "method": "GET"}` so probes and favicon fetches do not error.

Use **POST** for real tests and integrations (Stripe, GitHub, custom apps). There is no built-in `category` field—use **path** (`/reminders/dogfood`), **query** (`?source=omi`), **headers** (`X-Category: reminders`), or a **JSON body** to separate types; the TUI fuzzy search can match headers and body text.

---

## Send your first test webhook (from another terminal)

Leave the app running. Open a **second** terminal (virtual environment does not need to be active for `curl`).

**POST JSON:**

```bash
curl -s -X POST "http://127.0.0.1:9876/demo/hook" \
  -H "Content-Type: application/json" \
  -d '{"hello":"world","n":1}'
```

**Plain text body (quick test):**

```bash
curl -s -X POST "http://127.0.0.1:9876/test" \
  -H "Content-Type: text/plain" \
  -d "hello from localhost"
```

**Reminders lane** (`/reminders`):

```bash
curl -s -X POST "http://127.0.0.1:9876/reminders" \
  -H "Content-Type: text/plain" \
  -d "dont forget to get more dogfood"
```

**Tasks lane** (`/tasks`):

```bash
curl -s -X POST "http://127.0.0.1:9876/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"Ship webhook UI","due":"today"}'
```

**Email lane** (`/email`):

```bash
curl -s -X POST "http://127.0.0.1:9876/email" \
  -H "Content-Type: text/plain" \
  -d "New message from client@example.com"
```

**Active queue** (any other path, e.g. `/test` or `/demo/hook`):

**GET** (ignored — will not appear in the TUI or dashboard):

```bash
curl -s "http://127.0.0.1:9876/search?q=webhook&debug=1"
```

If your server uses a different port, replace **9876** with your `WEBHOOK_PORT` value (see below).

Back in the TUI or at **`/ui`**, new **POST** rows should appear. Select a row in the TUI (click or arrow keys) to load the **Pretty**, **Raw**, **Headers**, and **Sentiment** tabs.

### Expose localhost with ngrok (optional)

```bash
ngrok http 9876
```

Point your webhook URL at the ngrok HTTPS URL (any path). The collector still only **stores POST** payloads.

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

- Table of recent **POST/PUSH** events: **id**, **method**, **path**, **time**.
- Methods are color-coded (POST is highlighted).
- **GET** and other non-POST traffic never appear here (see [POST-only webhooks](#post-only-webhooks-what-gets-tracked)).
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
sqlite3 webhooks.db "SELECT id, method, path FROM events WHERE method IN ('POST','PUSH') ORDER BY id DESC LIMIT 5;"
```

Older **GET** rows may still exist in the file from before POST-only filtering; they are hidden in the TUI and `/ui` but visible in raw SQL until you delete them.

---

## Troubleshooting (common beginner issues)

### “Port already in use”

Something else is listening on **9876** (or whatever you chose). Either stop the other program or set another port:

```bash
export WEBHOOK_PORT=9880
python main.py
```

### ngrok shows webhooks but the dashboard does not update

Usually one of these:

1. **Stale server on port 9876** — an old `python main.py` is still running in another terminal. ngrok gets `200 OK` from that process, but your **current** TUI is not connected to it. Stop everything on 9876, then start once:

   ```bash
   wsl bash kill_port.sh
   source .venv/bin/activate
   python main.py
   ```

   You must see `Webhook server ready on 0.0.0.0:9876` before the TUI appears. If you see `address already in use`, the app now **exits** instead of opening a broken dashboard.

2. **WAL split-brain** — an old server wrote events into SQLite WAL files that Windows could not see. The app now forces **DELETE** journal mode on WSL `/mnt/c/` paths. After upgrading, run `wsl bash kill_port.sh`, then `python merge_wal.py` once if needed.

### `unable to open database file` when running from WSL (`/mnt/c/...`)

SQLite **WAL mode** does not work reliably on Windows drives mounted in WSL. The app now uses **DELETE** journal mode automatically. If you still see this error after upgrading, close the app everywhere and run once from Windows in the project folder:

```bash
python -c "import sqlite3; c=sqlite3.connect('webhooks.db'); c.execute('pragma wal_checkpoint(truncate)'); c.execute('pragma journal_mode=delete'); c.close()"
```

Then start again from WSL: `python main.py`.

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

## AI sentiment and voice (Gemini + ElevenLabs)

After each **POST/PUSH** webhook is saved, a **background task** sends a text digest of the request (method, path, query, headers, and a truncated body—any format) to **Google Gemini**. **GET** and other ignored methods never run this pipeline. The model returns JSON with **polarity**, **confidence**, **summary**, and a short **spoken_line** for text-to-speech.

Results are stored in SQLite (`sentiment_json` on each row) and shown in the TUI tab **Sentiment**. The UI is notified **as soon as** that JSON is written, **before** ElevenLabs playback starts, so the event stream and Sentiment tab stay in sync even if audio generation or playback is slow. When ElevenLabs is configured, the **spoken_line** (or summary) is synthesized and played through your speakers **one clip at a time** (a lock prevents overlapping audio if many webhooks arrive quickly). Gemini calls use a **timeout** (`GEMINI_REQUEST_TIMEOUT`, default 40 seconds) so a stuck model request does not block the rest of the pipeline indefinitely.

### Configuration

1. Copy the example env file and edit it:

   ```bash
   cp .env.example .env
   ```

2. Fill in at least **`GEMINI_API_KEY`** (from [Google AI Studio](https://aistudio.google.com/apikey)). The same value may be placed in **`GOOGLE_API_KEY`** if you prefer that name.

3. For voice playback, set **`ELEVENLABS_API_KEY`** and **`ELEVENLABS_VOICE_ID`** (from the [ElevenLabs](https://elevenlabs.io) dashboard). Aliases **`ELEVEN_LABS_API_KEY`** / **`ELEVEN_LABS_VOICE_ID`** are also read.

4. Optional environment variables:

   | Variable | Purpose |
   |----------|---------|
   | `GEMINI_MODEL` | Override model id (default `gemini-2.0-flash`). |
   | `GEMINI_REQUEST_TIMEOUT` | Seconds to wait for Gemini (default `40`); on timeout the pipeline continues with a fallback message and TTS can still run. |
   | `ELEVENLABS_MODEL_ID` | ElevenLabs voice model (default `eleven_multilingual_v2`). |
   | `WEBHOOK_SPEAK_ENABLED` | Set to `0` or `false` to disable speaker output while keeping the Sentiment tab. |

5. **Audio playback** (after ElevenLabs returns MP3 bytes) tries, in order:
   - **`mpv`** on your `PATH` (or `%ProgramFiles%\mpv\mpv.exe` on Windows)
   - **`ffplay`** (FFmpeg)
   - **Windows only:** built-in **MediaPlayer** via PowerShell (no extra install)
   - **Windows only:** **`wsl mpv`** if WSL has `mpv` installed (`sudo apt install mpv` in Ubuntu)
   On Linux/WSL: `sudo apt install mpv` is enough. If you run `python main.py` from **Windows**, playback is handled by the **TUI process** (Windows MediaPlayer or WSL `mpv`) — not from the background HTTP thread — so speakers work while the inspector is open.

`main.py` loads **`.env`** from the project directory automatically (`python-dotenv`). **Do not commit `.env`** (it is listed in `.gitignore`).

---

## Project files (map of the repository)

| File | Purpose |
|------|---------|
| `main.py` | Entry point: starts **uvicorn** in a background thread, then runs the **Textual** UI. |
| `server.py` | FastAPI application: catch-all route, `/healthz`, `/ui`, REST event API, POST-only persistence, enqueues events. |
| `webui.html` | TRON-themed browser dashboard (active/done queues, remove, live feed count). |
| `db.py` | Database schema and async helpers: insert/list/delete events, POST filter, done state, daily done purge. |
| `tui.py` | Terminal UI layout, theming, search, redelivery, POST-filtered event stream and inspector. |
| `ai_pipeline.py` | Gemini sentiment call, ElevenLabs TTS, optional local audio playback, DB update. |
| `requirements.txt` | Python dependencies and minimum versions. |
| `.env.example` | Template for API keys and toggles (copy to `.env`). |
| `push_to_github.sh` | Optional helper script to create **Tron_Webhook_Server** on GitHub and push `main` (requires `gh` and login). |

---

## Publish to GitHub (`Tron_Webhook_Server`)

This folder is a **Git** repository on the `main` branch. Creating the repo on github.com requires **your** GitHub login (the automated environment here cannot open a browser or use your password).

### Option A — one script (GitHub CLI)

1. Install [GitHub CLI](https://cli.github.com/) (`gh`) if you do not already have it. A copy may already exist at `~/.local/bin/gh` if you used this machine’s setup before.
2. Log in once:

   ```bash
   gh auth login
   ```

3. From **this project directory**, run:

   ```bash
   ./push_to_github.sh
   ```

That creates a **public** repository named **`Tron_Webhook_Server`** under your GitHub user, sets `origin`, and pushes `main`.

To use a **personal access token** instead of interactive login (for example in CI), set `GH_TOKEN` and run the same `gh repo create …` command from the script manually; do not commit the token.

### Option B — website + `git push`

1. On GitHub: **New repository** → name **`Tron_Webhook_Server`** → leave “Initialize with README” **unchecked** → Create.
2. In this folder:

   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/Tron_Webhook_Server.git
   git push -u origin main
   ```

Replace `YOUR_USERNAME` with your GitHub username.

### Commit author (optional)

If Git warns about name or email when committing, set them for this repo only:

```bash
git config user.name "Your Name"
git config user.email "your.email@example.com"
```

---

## Requirements summary (for reference)

- **Python:** 3.10+ recommended.
- **Libraries:** FastAPI, Uvicorn, Textual, aiosqlite, rapidfuzz, httpx, python-dotenv (see `requirements.txt`).
- **UI surfaces:** Terminal TUI and browser dashboard (`/ui`) both run from the same `python main.py` process.
- **Tracked methods:** **POST** and **PUSH** only for storage, display, and optional TTS.

---

## License

No license file is included in this folder by default. If you publish this project, add a `LICENSE` file that matches how you want others to use the code.
