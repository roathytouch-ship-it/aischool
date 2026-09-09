"""
AI School — LLM client (OpenAI-compatible Chat Completions API).

Interim default: DeepSeek (set LLM_BASE_URL + LLM_MODEL).
xAI Grok when credit works (LLM_BASE_URL=https://api.x.ai/v1, LLM_MODEL=grok-4.5).

Env:
  LLM_API_KEY      required for live calls
  LLM_BASE_URL     default https://api.deepseek.com
  LLM_MODEL        default deepseek-v4-flash (most subjects)
  LLM_MODEL_PRO    default deepseek-v4-pro — used for Special Math only
                   set empty to force Flash for Special Math too
  LLM_PRO_SUBJECTS default special_math  (comma list). Do not split by
                   geometry-vs-algebra mid-lesson.
  LLM_TIMEOUT_SEC  default 60
  LLM_MAX_OUTPUT_TOKENS  default 700 (non-thinking); thinking uses 1800 unless set
  LLM_RETRIES      default 2 (extra attempts after first failure)
  LLM_MAX_INFLIGHT default 20  (global concurrent LLM calls; extra get llm_busy)
  LLM_THINKING     auto|enabled|disabled  (default auto)
                   auto = thinking ON for Special Math, Exam Prep, Coding, AI & Robot
                   thinking OFF for English, languages, Bee, Skills, General Math, etc.

If LLM_API_KEY is missing or all attempts fail, chat() returns None.
Call last_error() for a short reason string (safe for logs / soft UI).
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_last_error: Optional[str] = None
_last_error_lock = threading.Lock()

# Global in-flight cap (process-local). Extra callers fail fast with llm_busy.
def _max_inflight() -> int:
    try:
        n = int(os.environ.get("LLM_MAX_INFLIGHT", "20"))
    except ValueError:
        n = 20
    return max(1, min(n, 200))


_inflight_sem = threading.BoundedSemaphore(_max_inflight())
_inflight_count = 0
_inflight_lock = threading.Lock()


def last_error() -> Optional[str]:
    """Most recent failure reason (no secrets)."""
    with _last_error_lock:
        return _last_error


def llm_configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY", "").strip())


def inflight_stats() -> Dict[str, int]:
    """For ops / health (approximate)."""
    with _inflight_lock:
        return {"inflight": _inflight_count, "max": _max_inflight()}


def _set_error(msg: str) -> None:
    global _last_error
    with _last_error_lock:
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


# Subjects where chain-of-thought is worth the extra tokens / latency.
_THINKING_SUBJECTS = frozenset(
    {
        "special_math",
        "exam_preparation",
        "exam_prep",
        "coding",
        "ai_and_robot",
        "ai_robot",
    }
)


_CLIP_ABBREV = (
    "mr", "mrs", "ms", "miss", "dr", "prof", "st", "no", "vs",
    "a.m", "p.m", "am", "pm",
    "e.g", "i.e", "etc", "u.s", "u.k", "jr", "sr",
)


def _is_clip_abbrev(chunk: str, dot_index: int) -> bool:
    """Do not treat Mr. / Dr. / Q4. as the end of a sentence."""
    before = chunk[:dot_index].rstrip()
    if not before:
        return False
    word = before.split()[-1] if before.split() else before
    low = word.lower().rstrip(".")
    if low in _CLIP_ABBREV:
        return True
    if len(low) <= 3 and low.startswith("q") and low[1:].isdigit():
        return True
    return False


def strip_continue_cue(text: str) -> str:
    """Remove hidden continue markers so history does not teach a cut-off loop."""
    import re

    t = text or ""
    t = re.sub(r"\s*\[\[CONTINUE\]\]\s*", "\n", t, flags=re.I)
    t = re.sub(r"\s*Reply continue for the next part\.?\s*", "\n", t, flags=re.I)
    return t.strip()


def _clip_at_sentence(text: str, max_chars: int) -> str:
    """Trim at the last full sentence and invite continue — never mid-word."""
    cue = "\n\n[[CONTINUE]]"
    t = (text or "").strip()
    if not t or max_chars <= 0 or len(t) <= max_chars:
        return t
    room = max_chars - len(cue)
    if room < 80:
        room = max(60, max_chars - 8)
        cue = ""
    chunk = t[:room]
    last = -1
    for i, ch in enumerate(chunk):
        if ch in ".!?。！？":
            if ch in ".。" and _is_clip_abbrev(chunk, i):
                continue
            nxt = chunk[i + 1] if i + 1 < len(chunk) else " "
            if nxt.isspace() or nxt in "\"')]}":
                last = i
    if last >= int(room * 0.35):
        out = chunk[: last + 1].strip()
    else:
        sp = chunk.rfind(" ")
        out = (chunk[:sp] if sp >= 40 else chunk).rstrip(" ,;:")
        if out and out[-1] not in ".!?。！？":
            out += "."
    if cue and "[[continue]]" not in out.lower() and "continue for the next part" not in out.lower():
        return out + cue
    return out


def _pro_subjects() -> set:
    raw = (os.environ.get("LLM_PRO_SUBJECTS") or "special_math").strip().lower()
    return {p.strip() for p in raw.split(",") if p.strip()}


def resolve_model(subject_key: Optional[str] = None) -> str:
    """Flash for most subjects. Pro for Special Math (whole subject, not geometry-only)."""
    flash = (os.environ.get("LLM_MODEL") or "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    pro = (os.environ.get("LLM_MODEL_PRO") or "deepseek-v4-pro").strip()
    key = (subject_key or "").strip().lower()
    if pro and key in _pro_subjects():
        return pro
    return flash


def thinking_enabled_for(subject_key: Optional[str] = None) -> bool:
    """auto = on for hard subjects only; enabled/disabled force all calls."""
    mode = (os.environ.get("LLM_THINKING") or "auto").strip().lower()
    if mode in ("disabled", "off", "0", "false", "none"):
        return False
    if mode in ("enabled", "on", "1", "true", "thinking"):
        return True
    key = (subject_key or "").strip().lower()
    return key in _THINKING_SUBJECTS


def chat(
    messages: List[Dict[str, str]],
    *,
    max_chars: int = 1200,
    temperature: float = 0.6,
    subject_key: Optional[str] = None,
    thinking_override: Optional[bool] = None,
    **_extra,
) -> Optional[str]:
    """
    messages: [{role: system|user|assistant, content: str}, ...]
    Returns assistant text or None if not configured / on failure / busy.
    """
    global _inflight_count
    _set_error("")  # clear; empty means success path may reset to None below

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("[llm_client] no LLM_API_KEY in this process — using stub")
        _set_error("llm_not_configured")
        return None

    # Fail fast if too many concurrent LLM calls (do not wait in a long queue)
    acquired = _inflight_sem.acquire(blocking=False)
    if not acquired:
        print("[llm_client] busy — max in-flight reached")
        _set_error("llm_busy")
        return None

    with _inflight_lock:
        _inflight_count += 1

    try:
        return _chat_unlocked(
            messages,
            max_chars=max_chars,
            temperature=temperature,
            api_key=api_key,
            subject_key=subject_key,
            thinking_override=thinking_override,
        )
    finally:
        with _inflight_lock:
            _inflight_count = max(0, _inflight_count - 1)
        _inflight_sem.release()


def _chat_unlocked(
    messages: List[Dict[str, str]],
    *,
    max_chars: int,
    temperature: float,
    api_key: str,
    subject_key: Optional[str] = None,
    thinking_override: Optional[bool] = None,
) -> Optional[str]:
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = resolve_model(subject_key)
    flash_model = (os.environ.get("LLM_MODEL") or "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    timeout = int(os.environ.get("LLM_TIMEOUT_SEC", "25"))
    retries = max(0, int(os.environ.get("LLM_RETRIES", "0")))
    if thinking_override is None:
        use_think = thinking_enabled_for(subject_key)
    else:
        use_think = bool(thinking_override)
    if use_think:
        max_tokens = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "1800"))
    else:
        max_tokens = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "700"))

    print(
        f"[llm_client] calling {base}/chat/completions model={model} "
        f"thinking={'on' if use_think else 'off'} subject={subject_key or '-'}"
    )

    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "thinking": {"type": "enabled" if use_think else "disabled"},
    }
    if use_think:
        body["reasoning_effort"] = (
            os.environ.get("LLM_REASONING_EFFORT") or "high"
        ).strip().lower()
    else:
        body["temperature"] = temperature
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
            if (
                e.code in (400, 404)
                and model != flash_model
                and "model" in err_body.lower()
            ):
                print("[llm_client] Pro model unavailable — falling back to", flash_model)
                model = flash_model
                body["model"] = model
                data = json.dumps(body).encode("utf-8")
                continue
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
            text_out = _clip_at_sentence(text_out, max_chars)
        print(f"[llm_client] ok, {len(text_out)} chars")
        _set_error("")  # success
        with _last_error_lock:
            global _last_error
            _last_error = None
        return text_out

    return None
