"""Gemini sentiment analysis + ElevenLabs TTS (optional) for incoming webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

import db as dbmod

logger = logging.getLogger("webhook.ai")


class PlaybackError(RuntimeError):
    """No local audio player could play the synthesized MP3."""


MAX_INGEST_CHARS = 14_000
MAX_SPOKEN_CHARS = 2_500

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


def _env_truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def build_ingest_text(
    *,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: bytes,
) -> str:
    """Flatten webhook into one string for the model (any content type)."""
    q = json.dumps(query, ensure_ascii=False, indent=2, default=str)
    h = json.dumps(headers, ensure_ascii=False, indent=2, default=str)
    body_preview = body[:MAX_INGEST_CHARS].decode("utf-8", errors="replace")
    if len(body) > MAX_INGEST_CHARS:
        body_preview += f"\n\n… [truncated, total {len(body)} bytes]"
    return "\n".join(
        [
            f"HTTP {method} {path}",
            "Query (JSON):",
            q,
            "Headers (JSON):",
            h,
            "Body (decoded as UTF-8, may be JSON/XML/form/binary-as-text):",
            body_preview,
        ]
    )


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*)\n```\s*$", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def call_gemini_sentiment(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    ingest_text: str,
) -> dict[str, Any]:
    """Ask Gemini for structured sentiment + spoken summary."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = """You analyze HTTP webhook traffic for a developer debugging tool.

Read the excerpt (method, path, query, headers, body — may be JSON, XML, HTML, plain text, form fields, or messy binary interpreted as text).

Respond with ONLY a single JSON object (no markdown fences) using exactly these keys:
- "polarity": one of "positive", "negative", "neutral", "mixed", or "unknown"
- "confidence": number from 0 to 1 (your confidence in polarity)
- "summary": 2-4 sentences describing what likely happened and emotional/sentiment tone of the payload if any (business-neutral language)
- "spoken_line": ONE short sentence, under 220 characters, natural when read aloud by text-to-speech, capturing the gist and sentiment (no URLs, no JSON syntax)

Webhook excerpt:
---
"""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt + ingest_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.25,
        },
    }
    r = await client.post(url, params={"key": api_key}, json=payload, timeout=90.0)
    r.raise_for_status()
    data = r.json()
    parts_out = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    if not parts_out:
        raise ValueError("empty Gemini response")
    raw = parts_out[0].get("text", "")
    raw = _strip_json_fence(raw)
    return json.loads(raw)


async def call_elevenlabs_tts(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    voice_id: str,
    text: str,
) -> bytes:
    t = text.strip()[:MAX_SPOKEN_CHARS]
    if not t:
        t = "Webhook received."
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    r = await client.post(
        url,
        headers={
            "xi-api-key": api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        },
        json={
            "text": t,
            "model_id": os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
        },
        timeout=120.0,
    )
    r.raise_for_status()
    return r.content


def _win_path_for_wsl(path: Path) -> str:
    """C:\\foo\\bar -> /mnt/c/foo/bar for wsl.exe."""
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[-1]
    return f"/mnt/{drive}{tail}"


def _find_mpv() -> str | None:
    mpv = shutil.which("mpv")
    if mpv:
        return mpv
    if os.name != "nt":
        return None
    for candidate in (
        os.path.expandvars(r"%ProgramFiles%\mpv\mpv.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\mpv\mpv.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _play_with_mpv(mpv: str, path: Path) -> bool:
    r = subprocess.run(
        [mpv, "--no-video", "--really-quiet", str(path)],
        timeout=180,
        check=False,
    )
    return r.returncode == 0


def _play_with_ffplay(ffplay: str, path: Path) -> bool:
    r = subprocess.run(
        [
            ffplay,
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel",
            "quiet",
            str(path),
        ],
        timeout=180,
        check=False,
    )
    return r.returncode == 0


def _play_with_wsl_mpv(path: Path) -> bool:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return False
    wsl_path = _win_path_for_wsl(path)
    r = subprocess.run(
        [wsl, "--", "mpv", "--no-video", "--really-quiet", wsl_path],
        timeout=180,
        check=False,
    )
    return r.returncode == 0


def _play_with_windows_media(path: Path) -> bool:
    """Play via .NET MediaPlayer (built into Windows; no mpv install required)."""
    path_ps = str(path.resolve()).replace("'", "''")
    script = f"""
Add-Type -AssemblyName presentationCore
$p = New-Object System.Windows.Media.MediaPlayer
$p.Open([Uri]::new((Resolve-Path -LiteralPath '{path_ps}').Path))
$p.Volume = 1.0
$p.Play()
$deadline = (Get-Date).AddSeconds(120)
while (-not $p.NaturalDuration.HasTimeSpan -and (Get-Date) -lt $deadline) {{
    Start-Sleep -Milliseconds 50
}}
while ($p.NaturalDuration.HasTimeSpan -and $p.Position -lt $p.NaturalDuration.TimeSpan) {{
    Start-Sleep -Milliseconds 100
}}
$p.Stop()
$p.Close()
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Sta", "-Command", script],
        timeout=180,
        check=False,
    )
    return r.returncode == 0


def play_mpeg_audio(data: bytes) -> None:
    """Play MP3/MPEG bytes (temp file + mpv/ffplay/WSL mpv/Windows MediaPlayer)."""
    if not data:
        return

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(data)
            f.flush()
            tmp_path = f.name
        path = Path(tmp_path)

        mpv = _find_mpv()
        if mpv and _play_with_mpv(mpv, path):
            logger.info("Played TTS audio via mpv (%s)", mpv)
            return

        ffplay = shutil.which("ffplay")
        if ffplay and _play_with_ffplay(ffplay, path):
            logger.info("Played TTS audio via ffplay")
            return

        if os.name == "nt":
            if _play_with_windows_media(path):
                logger.info("Played TTS audio via Windows MediaPlayer")
                return
            if _play_with_wsl_mpv(path):
                logger.info("Played TTS audio via WSL mpv")
                return

        raise PlaybackError(
            "No working audio player (tried mpv, ffplay, Windows MediaPlayer, WSL mpv). "
            "On Linux/WSL: sudo apt install mpv. On Windows, playback is routed through the TUI."
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def queue_audio_playback(event_queue: Any, *, mp3: bytes, event_id: int) -> None:
    """Hand off MP3 bytes to the TUI main process for playback (thread-safe queue)."""
    if not mp3:
        raise PlaybackError("ElevenLabs returned empty audio")
    try:
        event_queue.put_nowait(
            {
                "type": "play_audio",
                "mp3": mp3,
                "id": event_id,
                "ts": time.time(),
            }
        )
    except Exception as exc:
        raise PlaybackError(f"Could not queue audio for playback: {exc}") from exc


async def run_sentiment_for_event(
    *,
    db_path: Path,
    event_queue: Any,
    event_id: int,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Analyze webhook with Gemini, store JSON, notify TUI, optionally speak via ElevenLabs."""
    gemini_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
        or ""
    ).strip()
    eleven_key = (
        os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_LABS_API_KEY") or ""
    ).strip()
    voice_id = (
        os.environ.get("ELEVENLABS_VOICE_ID") or os.environ.get("ELEVEN_LABS_VOICE_ID") or ""
    ).strip()
    model = (os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    speak = _env_truthy("WEBHOOK_SPEAK_ENABLED", "1") and bool(eleven_key and voice_id)
    try:
        gemini_timeout = float(os.environ.get("GEMINI_REQUEST_TIMEOUT", "40"))
    except ValueError:
        gemini_timeout = 40.0

    ingest = build_ingest_text(method=method, path=path, query=query, headers=headers, body=body)
    result: dict[str, Any]

    def _notify_tui() -> None:
        try:
            event_queue.put_nowait({"type": "sentiment_ready", "id": event_id, "ts": time.time()})
        except Exception as exc:
            logger.warning("TUI sentiment_ready notify failed for event %s: %s", event_id, exc)

    async with httpx.AsyncClient() as client:
        # 1) Gemini sentiment analysis (independent of TTS)
        if not gemini_key:
            result = {
                "polarity": "unknown",
                "confidence": 0.0,
                "summary": "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env to enable AI sentiment.",
                "spoken_line": "Webhook captured. Add your Gemini API key for sentiment analysis.",
            }
        else:
            try:
                result = await asyncio.wait_for(
                    call_gemini_sentiment(
                        client, api_key=gemini_key, model=model, ingest_text=ingest
                    ),
                    timeout=gemini_timeout,
                )
                for k in ("polarity", "confidence", "summary", "spoken_line"):
                    result.setdefault(k, None)
            except asyncio.TimeoutError:
                logger.warning(
                    "Gemini timed out after %.1fs (event %s); set GEMINI_REQUEST_TIMEOUT to adjust.",
                    gemini_timeout,
                    event_id,
                )
                result = {
                    "polarity": "unknown",
                    "confidence": 0.0,
                    "summary": f"Gemini request exceeded {gemini_timeout:.0f} second timeout.",
                    "spoken_line": "Webhook received. Sentiment analysis timed out.",
                }
            except Exception as exc:
                logger.warning("Gemini sentiment failed: %s", exc)
                result = {
                    "polarity": "unknown",
                    "confidence": 0.0,
                    "summary": f"Gemini sentiment failed: {exc}",
                    "spoken_line": "Webhook received. Sentiment analysis failed.",
                }

        # 2) Persist + wake the TUI before TTS so the event stream updates even if playback hangs.
        try:
            payload = json.dumps(result, ensure_ascii=False)
            async with aiosqlite.connect(db_path) as wconn:
                wconn.row_factory = aiosqlite.Row
                await dbmod.update_sentiment_json(wconn, event_id, payload)
        except Exception as exc:
            logger.warning("sentiment DB write failed for event %s: %s", event_id, exc)
            try:
                fallback = {
                    "polarity": "unknown",
                    "confidence": 0.0,
                    "summary": f"Could not store sentiment JSON: {exc}",
                    "spoken_line": "Webhook received. Could not save sentiment.",
                }
                async with aiosqlite.connect(db_path) as wconn:
                    wconn.row_factory = aiosqlite.Row
                    await dbmod.update_sentiment_json(
                        wconn, event_id, json.dumps(fallback, ensure_ascii=False)
                    )
            except Exception as exc2:
                logger.warning("sentiment fallback DB write failed for event %s: %s", event_id, exc2)

        _notify_tui()

        # 3) ElevenLabs TTS → queue playback on TUI thread (Windows needs main-process audio)
        if speak:
            line = result.get("spoken_line") or result.get("summary") or "Webhook event received."
            line = str(line).strip()[:MAX_SPOKEN_CHARS]
            try:
                logger.info("event %s: synthesizing speech (%d chars)", event_id, len(line))
                mp3 = await call_elevenlabs_tts(
                    client, api_key=eleven_key, voice_id=voice_id, text=line
                )
                queue_audio_playback(event_queue, mp3=mp3, event_id=event_id)
                logger.info("event %s: queued %d bytes for speaker playback", event_id, len(mp3))
            except Exception as tts_exc:
                logger.warning("ElevenLabs TTS/playback failed for event %s: %s", event_id, tts_exc)
                result["tts_error"] = f"ElevenLabs TTS/playback failed: {tts_exc}"
                try:
                    async with aiosqlite.connect(db_path) as wconn:
                        wconn.row_factory = aiosqlite.Row
                        await dbmod.update_sentiment_json(
                            wconn, event_id, json.dumps(result, ensure_ascii=False)
                        )
                    _notify_tui()
                except Exception:
                    pass
