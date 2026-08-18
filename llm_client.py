"""
AI School — LLM client (OpenAI-compatible Chat Completions API).

Interim default: DeepSeek (set LLM_BASE_URL + LLM_MODEL).
xAI Grok when credit works (LLM_BASE_URL=https://api.x.ai/v1, LLM_MODEL=grok-4.5).

Env:
  LLM_API_KEY      required for live calls
  LLM_BASE_URL     default https://api.deepseek.com
  LLM_MODEL        default deepseek-v4-flash
  LLM_TIMEOUT_SEC  default 60
  LLM_MAX_OUTPUT_TOKENS  default 700
  LLM_RETRIES      default 2 (extra attempts after first failure)

If LLM_API_KEY is missing or all attempts fail, chat() returns None.
Call last_error() for a short reason string (safe for logs / soft UI).
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_last_error: Optional[str] = None


def last_error() -> Optional[str]:
    """Most recent failure reason (no secrets)."""
    return _last_error


def llm_configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY", "").strip())


def _set_error(msg: str) -> None:
    global _last_error
    _last_error = (msg or "unknown")[:300]


def _classify_http(code: int, body: str) -> str:
    b = (body or "").lower()
    if code in (401, 403):
        if "credit" in b or "spending" in b or "balance" in b:
            return "llm_quota"
        return "llm_auth"
    if code == 429:
        return "llm_rate_limit"
    if code in (500, 502, 503, 504):
        return "llm_upstream"
    if code == 400:
        return "llm_bad_request"
    return f"llm_http_{code}"


def chat(
    messages: List[Dict[str, str]],
    *,
    max_chars: int = 1200,
    temperature: float = 0.6,
) -> Optional[str]:
    """
    messages: [{role: system|user|assistant, content: str}, ...]
    Returns assistant text or None if not configured / on failure.
    """
    global _last_error
    _last_error = None

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("[llm_client] no LLM_API_KEY in this process — using stub")
        _set_error("llm_not_configured")
        return None

    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    timeout = int(os.environ.get("LLM_TIMEOUT_SEC", "60"))
    max_tokens = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "700"))
    retries = max(0, int(os.environ.get("LLM_RETRIES", "2")))

    print(f"[llm_client] calling {base}/chat/completions model={model}")

    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode("utf-8")

    attempts = 1 + retries
    for attempt in range(attempts):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                payload = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
            kind = _classify_http(e.code, err_body)
            print(f"[llm_client] HTTP {e.code} ({kind}) attempt {attempt + 1}/{attempts}: {err_body[:200]}")
            _set_error(kind)
            # Retry only transient errors
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep((0.4 * (2**attempt)) + random.uniform(0, 0.25))
                continue
            return None
        except urllib.error.URLError as e:
            print(f"[llm_client] network error attempt {attempt + 1}/{attempts}: {e}")
            _set_error("llm_network")
            if attempt < attempts - 1:
                time.sleep((0.4 * (2**attempt)) + random.uniform(0, 0.25))
                continue
            return None
        except TimeoutError as e:
            print(f"[llm_client] timeout attempt {attempt + 1}/{attempts}: {e}")
            _set_error("llm_timeout")
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except Exception as e:
            print(f"[llm_client] error attempt {attempt + 1}/{attempts}: {e}")
            _set_error("llm_error")
            if attempt < attempts - 1:
                time.sleep(0.3 * (attempt + 1))
                continue
            return None

        text_out = None
        try:
            text_out = payload["choices"][0]["message"]["content"]
        except Exception:
            try:
                msg = payload["choices"][0]["message"]
                text_out = msg.get("content") or msg.get("reasoning_content")
            except Exception:
                print("[llm_client] unexpected response shape:", str(payload)[:300])
                _set_error("llm_bad_response")
                return None

        text_out = (text_out or "").strip()
        if not text_out:
            print("[llm_client] empty content from model")
            _set_error("llm_empty")
            return None
        if max_chars and len(text_out) > max_chars:
            text_out = text_out[: max_chars - 1].rstrip() + "…"
        print(f"[llm_client] ok, {len(text_out)} chars")
        _last_error = None
        return text_out

    return None
