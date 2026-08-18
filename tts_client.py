"""
AI School — OpenAI Text-to-Speech client (AI Voice).

Env:
  OPENAI_API_KEY     required for live TTS
  OPENAI_TTS_MODEL   default tts-1  (or tts-1-hd)
  OPENAI_TTS_VOICE   default nova  (alloy, echo, fable, onyx, nova, shimmer)
  OPENAI_TTS_BASE    default https://api.openai.com/v1
  OPENAI_TTS_TIMEOUT_SEC  default 60

Returns raw audio bytes (mp3) or None if not configured / on failure.
Browser TTS remains the free fallback when this returns None.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional
import time
import random


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter: ~0.4s, 0.8s, 1.6s... capped."""
    base = 0.4 * (2 ** attempt)
    delay = min(base, 4.0) * (0.7 + 0.6 * random.random())
    time.sleep(delay)


def tts_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def synthesize(
    text: str,
    *,
    voice: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[bytes]:
    """
    Turn teacher reply text into MP3 bytes via OpenAI /audio/speech.
    Caps input length to avoid runaway cost (teacher replies are short).
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[tts_client] no OPENAI_API_KEY — skip server TTS")
        return None

    text = (text or "").strip()
    if not text:
        return None
    # Hard cap: ~same order as max AI reply
    if len(text) > 2000:
        text = text[:1999].rstrip() + "…"

    base = os.environ.get("OPENAI_TTS_BASE", "https://api.openai.com/v1").rstrip("/")
    model = (model or os.environ.get("OPENAI_TTS_MODEL", "tts-1")).strip()
    voice = (voice or os.environ.get("OPENAI_TTS_VOICE", "nova")).strip()
    timeout = int(os.environ.get("OPENAI_TTS_TIMEOUT_SEC", "60"))

    print(f"[tts_client] calling {base}/audio/speech model={model} voice={voice} chars={len(text)}")

    body = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    data = json.dumps(body).encode("utf-8")
    max_attempts = int(os.environ.get("OPENAI_TTS_RETRIES", "3"))

    audio = None
    last_err = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            base + "/audio/speech",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                audio = res.read()
            if audio:
                break
            last_err = "empty audio"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[tts_client] HTTP {e.code}: {err_body}")
            last_err = f"HTTP {e.code}"
            # Retry 429 and 5xx only
            if e.code not in (429, 500, 502, 503, 504) or attempt >= max_attempts - 1:
                return None
            _sleep_backoff(attempt)
            continue
        except Exception as e:
            print(f"[tts_client] error: {e}")
            last_err = str(e)
            if attempt >= max_attempts - 1:
                return None
            _sleep_backoff(attempt)
            continue
        if attempt < max_attempts - 1:
            _sleep_backoff(attempt)

    if not audio:
        print(f"[tts_client] failed after retries: {last_err}")
        return None
    print(f"[tts_client] ok, {len(audio)} bytes")
    return audio


if __name__ == "__main__":
    # Quick local check: OPENAI_API_KEY=... python tts_client.py
    out = synthesize("Hello from AI School. This is a short voice test.")
    if out:
        path = "/tmp/aischool_tts_test.mp3"
        with open(path, "wb") as f:
            f.write(out)
        print("wrote", path)
    else:
        print("no audio (set OPENAI_API_KEY)")
