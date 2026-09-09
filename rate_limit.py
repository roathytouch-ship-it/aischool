"""
AI School — simple in-memory rate limiter.

Layers:
  1) Login: per-IP + per telegram_user_id / student_id (PIN)
  2) Study chat: per student_id (+ optional per-IP)
  3) TTS / STT: per student_id (abuse / cost control)
  4) PIN row lockout remains in repositories (5 fails → 15 min)

For multi-process / multi-host production, replace backend with Redis.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    limit: int = 0
    remaining: int = 0


class SlidingWindowLimiter:
    """
    Allow `max_hits` per `window_seconds` per key.
    Thread-safe; process-local memory only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str, max_hits: int, window_seconds: int) -> RateLimitResult:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= max_hits:
                retry = int(max(1, window_seconds - (now - q[0])))
                return RateLimitResult(
                    allowed=False,
                    retry_after_seconds=retry,
                    limit=max_hits,
                    remaining=0,
                )
            q.append(now)
            remaining = max(0, max_hits - len(q))
            return RateLimitResult(
                allowed=True,
                retry_after_seconds=0,
                limit=max_hits,
                remaining=remaining,
            )


# Shared limiter instance for the API process
limiter = SlidingWindowLimiter()

# --- Login defaults ---
LOGIN_IP_MAX = 30
LOGIN_IP_WINDOW = 60
TELEGRAM_ID_MAX = 20
TELEGRAM_ID_WINDOW = 60
PIN_ID_MAX = 15
PIN_ID_WINDOW = 60

# --- Study chat (LLM cost + abuse) ---
# ~1 message every 2s sustained, or a short burst then wait
CHAT_STUDENT_MAX = 30          # messages per student per window
CHAT_STUDENT_WINDOW = 60       # seconds
CHAT_IP_MAX = 90               # all chat from one IP (shared family / NAT)
CHAT_IP_WINDOW = 60

# --- Voice (OpenAI cost) ---
TTS_STUDENT_MAX = 40
TTS_STUDENT_WINDOW = 60
STT_STUDENT_MAX = 40
STT_STUDENT_WINDOW = 60

# --- Session start (anti-spam start/end flapping) ---
START_STUDENT_MAX = 10
START_STUDENT_WINDOW = 60


def check_login_ip(ip: str) -> RateLimitResult:
    return limiter.check(f"login:ip:{ip or 'unknown'}", LOGIN_IP_MAX, LOGIN_IP_WINDOW)


def check_telegram_user(telegram_user_id: int) -> RateLimitResult:
    return limiter.check(
        f"login:tg:{telegram_user_id}", TELEGRAM_ID_MAX, TELEGRAM_ID_WINDOW
    )


def check_pin_student(student_id: str) -> RateLimitResult:
    return limiter.check(f"login:pin:{student_id}", PIN_ID_MAX, PIN_ID_WINDOW)


def check_chat_student(student_id: str) -> RateLimitResult:
    return limiter.check(
        f"chat:stu:{student_id or 'unknown'}", CHAT_STUDENT_MAX, CHAT_STUDENT_WINDOW
    )


def check_chat_ip(ip: str) -> RateLimitResult:
    return limiter.check(
        f"chat:ip:{ip or 'unknown'}", CHAT_IP_MAX, CHAT_IP_WINDOW
    )


def check_tts_student(student_id: str) -> RateLimitResult:
    return limiter.check(
        f"tts:stu:{student_id or 'unknown'}", TTS_STUDENT_MAX, TTS_STUDENT_WINDOW
    )


def check_stt_student(student_id: str) -> RateLimitResult:
    return limiter.check(
        f"stt:stu:{student_id or 'unknown'}", STT_STUDENT_MAX, STT_STUDENT_WINDOW
    )


def check_start_student(student_id: str) -> RateLimitResult:
    return limiter.check(
        f"start:stu:{student_id or 'unknown'}", START_STUDENT_MAX, START_STUDENT_WINDOW
    )


def raise_if_limited(
    result: RateLimitResult,
    *,
    message: str = "Too many requests. Please wait and try again.",
) -> Optional[dict]:
    """Return FastAPI detail dict if limited; else None."""
    if result.allowed:
        return None
    return {
        "error": "rate_limited",
        "message": message,
        "retry_after_seconds": result.retry_after_seconds,
        "limit": result.limit,
    }
