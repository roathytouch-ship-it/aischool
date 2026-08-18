"""
AI School — OpenAI Speech-to-Text client (student mic → text).

Env:
  OPENAI_API_KEY      required (same key as TTS)
  OPENAI_STT_MODEL    default gpt-4o-mini-transcribe (fast/cheap; whisper-1 slower)
  OPENAI_STT_BASE     default https://api.openai.com/v1
  OPENAI_STT_TIMEOUT_SEC  default 45 (short clips; raise if needed)

Accepts raw audio bytes + filename hint (e.g. audio.webm, audio.wav).
Returns transcript text or None if not configured / on failure.
Browser STT remains free fallback when this returns None.

Live Talk (realtime speech-to-speech) stays on hold — this is STT only.
"""

from __future__ import annotations

import os
import uuid
import urllib.error
import urllib.request
from typing import Optional, Tuple
import time
import random


def _sleep_backoff(attempt: int) -> None:
    base = 0.4 * (2 ** attempt)
    delay = min(base, 4.0) * (0.7 + 0.6 * random.random())
    time.sleep(delay)


def stt_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _multipart(
    fields: dict,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> Tuple[bytes, str]:
    boundary = "----AischoolBoundary" + uuid.uuid4().hex
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(str(value).encode("utf-8") + b"\r\n")
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    lines.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    lines.append(file_bytes)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe(
    audio_bytes: bytes,
    *,
    filename: str = "audio.webm",
    content_type: str = "audio/webm",
    language: Optional[str] = None,
) -> Optional[str]:
    """
    Transcribe student speech to text via OpenAI /audio/transcriptions.
    language: optional ISO code e.g. 'en', 'km', 'fr' — omit for auto.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[stt_client] no OPENAI_API_KEY — skip server STT")
        return None

    if not audio_bytes or len(audio_bytes) < 100:
        print("[stt_client] audio too small")
        return None
    # Soft guard against huge uploads (~25MB OpenAI limit; we stay lower)
    if len(audio_bytes) > 12 * 1024 * 1024:
        print("[stt_client] audio too large")
        return None

    base = os.environ.get("OPENAI_STT_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe").strip()
    timeout = int(os.environ.get("OPENAI_STT_TIMEOUT_SEC", "45"))

    fields = {"model": model}
    if language:
        fields["language"] = language

    body, content_type_header = _multipart(
        fields, "file", filename or "audio.webm", audio_bytes, content_type or "application/octet-stream"
    )

    print(f"[stt_client] calling {base}/audio/transcriptions model={model} bytes={len(audio_bytes)}")

    max_attempts = int(os.environ.get("OPENAI_STT_RETRIES", "3"))
    raw = ""
    last_err = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            base + "/audio/transcriptions",
            data=body,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": content_type_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8")
            if raw:
                break
            last_err = "empty body"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[stt_client] HTTP {e.code}: {err_body}")
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_attempts - 1:
                return None
            _sleep_backoff(attempt)
            continue
        except Exception as e:
            print(f"[stt_client] error: {e}")
            last_err = str(e)
            if attempt >= max_attempts - 1:
                return None
            _sleep_backoff(attempt)
            continue
        if attempt < max_attempts - 1:
            _sleep_backoff(attempt)

    if not raw:
        print(f"[stt_client] failed after retries: {last_err}")
        return None

    text_out = ""
    try:
        import json

        payload = json.loads(raw)
        text_out = (payload.get("text") or "").strip()
    except Exception:
        text_out = raw.strip()

    if not text_out:
        print("[stt_client] empty transcript")
        return None
    # Soft cap for study input
    if len(text_out) > 1000:
        text_out = text_out[:1000]
    print(f"[stt_client] ok, {len(text_out)} chars")
    return text_out
