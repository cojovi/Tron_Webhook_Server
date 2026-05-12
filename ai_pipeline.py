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


def play_mpeg_audio(data: bytes) -> None:
    """Play MP3/MPEG bytes via mpv (preferred) or ffplay."""
    if not data:
        return
    mpv = shutil.which("mpv")
    if mpv:
        subprocess.run(
            [mpv, "--no-video", "--really-quiet", "-"],
            input=data,
            timeout=180,
            check=False,
        )
        return
    ffplay = shutil.which("ffplay")
    if ffplay:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            try:
                f.write(data)
                f.flush()
                subprocess.run(
                    [
                        ffplay,
                        "-nodisp",
                        "-autoexit",
                        "-hide_banner",
                        "-loglevel",
                        "quiet",
                        f.name,
                    ],
                    timeout=180,
                    check=False,
                )
            finally:
                try:
                    os.unlink(f.name)
                except OSError:
                    pass
        return
    logger.warning("No mpv/ffplay found; install one to hear ElevenLabs playback.")


async def _speak_line(
    client: httpx.AsyncClient,
    *,
    eleven_key: str,
    voice_id: str,
    line: str,
    speech_lock: asyncio.Lock,
) -> None:
    mp3 = await call_elevenlabs_tts(client, api_key=eleven_key, voice_id=voice_id, text=line)
    async with speech_lock:
        await asyncio.to_thread(play_mpeg_audio, mp3)


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
    speech_lock: asyncio.Lock,
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

    ingest = build_ingest_text(method=method, path=path, query=query, headers=headers, body=body)
    result: dict[str, Any]

    if not gemini_key:
        result = {
            "polarity": "unknown",
            "confidence": 0.0,
            "summary": "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env to enable AI sentiment.",
            "spoken_line": "Webhook captured. Add your Gemini API key for sentiment analysis.",
        }
    else:
        try:
            async with httpx.AsyncClient() as client:
                result = await call_gemini_sentiment(
                    client, api_key=gemini_key, model=model, ingest_text=ingest
                )
                for k in ("polarity", "confidence", "summary", "spoken_line"):
                    result.setdefault(k, None)
                if speak:
                    line = result.get("spoken_line") or result.get("summary") or "Webhook event received."
                    line = str(line).strip()[:MAX_SPOKEN_CHARS]
                    await _speak_line(
                        client,
                        eleven_key=eleven_key,
                        voice_id=voice_id,
                        line=line,
                        speech_lock=speech_lock,
                    )
        except Exception as exc:
            logger.warning("Gemini sentiment failed: %s", exc)
            result = {
                "polarity": "unknown",
                "confidence": 0.0,
                "summary": f"AI analysis failed: {exc}",
                "spoken_line": "Webhook received. Sentiment analysis failed.",
            }
            if speak:
                try:
                    async with httpx.AsyncClient() as client:
                        await _speak_line(
                            client,
                            eleven_key=eleven_key,
                            voice_id=voice_id,
                            line=str(result["spoken_line"]),
                            speech_lock=speech_lock,
                        )
                except Exception as tts_exc:
                    logger.warning("ElevenLabs TTS failed: %s", tts_exc)

    if not gemini_key and speak:
        try:
            async with httpx.AsyncClient() as client:
                await _speak_line(
                    client,
                    eleven_key=eleven_key,
                    voice_id=voice_id,
                    line=str(result["spoken_line"]),
                    speech_lock=speech_lock,
                )
        except Exception as tts_exc:
            logger.warning("ElevenLabs TTS failed: %s", tts_exc)

    payload = json.dumps(result, ensure_ascii=False)
    async with aiosqlite.connect(db_path) as wconn:
        wconn.row_factory = aiosqlite.Row
        await dbmod.update_sentiment_json(wconn, event_id, payload)

    try:
        event_queue.put_nowait({"type": "sentiment_ready", "id": event_id, "ts": time.time()})
    except Exception:
        pass
