"""
AI School — simple in-memory rate limiter for login endpoints.

Layers:
  1) Per-IP request budget (all login routes)
  2) Per-key budget (telegram_user_id or student_id when known)
  3) PIN row lockout remains in repositories (5 fails → 15 min)

For multi-process / multi-host production, replace backend with Redis.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


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

# Defaults (override via env in the route layer if needed)
LOGIN_IP_MAX = 30          # all login attempts from one IP
LOGIN_IP_WINDOW = 60       # per 60 seconds
TELEGRAM_ID_MAX = 20       # per telegram user id (when parsed)
TELEGRAM_ID_WINDOW = 60
PIN_ID_MAX = 15            # per student_id (extra to row lockout)
PIN_ID_WINDOW = 60


def check_login_ip(ip: str) -> RateLimitResult:
    return limiter.check(f"login:ip:{ip or 'unknown'}", LOGIN_IP_MAX, LOGIN_IP_WINDOW)


def check_telegram_user(telegram_user_id: int) -> RateLimitResult:
    return limiter.check(
        f"login:tg:{telegram_user_id}", TELEGRAM_ID_MAX, TELEGRAM_ID_WINDOW
    )


def check_pin_student(student_id: str) -> RateLimitResult:
    return limiter.check(f"login:pin:{student_id}", PIN_ID_MAX, PIN_ID_WINDOW)


def raise_if_limited(result: RateLimitResult) -> Optional[dict]:
    """Return FastAPI detail dict if limited; else None."""
    if result.allowed:
        return None
    return {
        "error": "rate_limited",
        "message": "Too many login attempts. Please wait and try again.",
        "retry_after_seconds": result.retry_after_seconds,
        "limit": result.limit,
    }
