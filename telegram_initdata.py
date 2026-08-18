"""
AI School — Telegram WebApp initData validation (reference implementation).

Official algorithm:
  https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

1. Parse initData as application/x-www-form-urlencoded
2. Remove `hash`; sort remaining key=value by key; join with \\n
3. secret_key = HMAC_SHA256(key=b\"WebAppData\", msg=bot_token)
4. calculated  = HMAC_SHA256(key=secret_key, msg=data_check_string).hex()
5. Compare to provided hash (constant-time)
6. Reject if auth_date is too old (default max age 24h; tighter in production OK)

Wire into POST /auth/telegram after this returns a validated TelegramUser.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qsl


# Default: accept initData up to 24 hours old (Telegram docs allow checking auth_date).
# For login, 1 hour is often enough; use 86400 for slower clients / clock skew.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class TelegramInitDataError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass
class TelegramUser:
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    language_code: Optional[str] = None
    is_premium: Optional[bool] = None
    photo_url: Optional[str] = None

    def display_name(self) -> str:
        parts = [self.first_name]
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts).strip() or (self.username or f"tg_{self.id}")


@dataclass
class ValidatedInitData:
    user: TelegramUser
    auth_date: int
    query_id: Optional[str] = None
    chat_instance: Optional[str] = None
    chat_type: Optional[str] = None
    start_param: Optional[str] = None
    raw: Mapping[str, str] = None  # type: ignore

    def age_seconds(self, now: Optional[int] = None) -> int:
        now = int(time.time()) if now is None else now
        return max(0, now - self.auth_date)


def _parse_init_data(init_data: str) -> Dict[str, str]:
    if not init_data or not isinstance(init_data, str):
        raise TelegramInitDataError("invalid_init_data", "init_data is required")
    # parse_qsl keeps blank values; does not require leading ?
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise TelegramInitDataError("invalid_init_data", "init_data is empty or malformed")
    return {k: v for k, v in pairs}


def _data_check_string(fields: Dict[str, str]) -> str:
    # All fields except hash, sorted by key
    items = sorted((k, v) for k, v in fields.items() if k != "hash")
    return "\n".join(f"{k}={v}" for k, v in items)


def _secret_key(bot_token: str) -> bytes:
    # secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def _calculate_hash(bot_token: str, data_check_string: str) -> str:
    secret = _secret_key(bot_token)
    return hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_user(user_json: str) -> TelegramUser:
    try:
        data = json.loads(user_json)
    except json.JSONDecodeError as e:
        raise TelegramInitDataError("invalid_init_data", "user field is not valid JSON") from e
    if not isinstance(data, dict) or "id" not in data:
        raise TelegramInitDataError("invalid_init_data", "user.id missing")
    try:
        uid = int(data["id"])
    except (TypeError, ValueError) as e:
        raise TelegramInitDataError("invalid_init_data", "user.id must be an integer") from e
    first = data.get("first_name") or ""
    return TelegramUser(
        id=uid,
        first_name=str(first),
        last_name=data.get("last_name"),
        username=data.get("username"),
        language_code=data.get("language_code"),
        is_premium=data.get("is_premium"),
        photo_url=data.get("photo_url"),
    )


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: Optional[int] = None,
) -> ValidatedInitData:
    """
    Validate Telegram.WebApp.initData and return structured user + meta.

    Raises TelegramInitDataError with code invalid_init_data (map to HTTP 401).
    """
    if not bot_token:
        raise TelegramInitDataError("invalid_init_data", "Server bot token not configured")

    fields = _parse_init_data(init_data)
    received_hash = fields.get("hash")
    if not received_hash:
        raise TelegramInitDataError("invalid_init_data", "hash missing from init_data")

    check_string = _data_check_string(fields)
    calculated = _calculate_hash(bot_token, check_string)

    if not hmac.compare_digest(calculated, received_hash):
        raise TelegramInitDataError("invalid_init_data", "Telegram initData failed verification")

    auth_raw = fields.get("auth_date")
    if not auth_raw:
        raise TelegramInitDataError("invalid_init_data", "auth_date missing")
    try:
        auth_date = int(auth_raw)
    except ValueError as e:
        raise TelegramInitDataError("invalid_init_data", "auth_date must be an integer") from e

    now_ts = int(time.time()) if now is None else int(now)
    if auth_date > now_ts + 60:
        # allow 60s clock skew into the future
        raise TelegramInitDataError("invalid_init_data", "auth_date is in the future")
    if max_age_seconds is not None and (now_ts - auth_date) > max_age_seconds:
        raise TelegramInitDataError(
            "invalid_init_data",
            f"initData is too old (max age {max_age_seconds}s)",
        )

    user_raw = fields.get("user")
    if not user_raw:
        # Mini App login for AI School always expects a user object
        raise TelegramInitDataError("invalid_init_data", "user missing from init_data")

    user = _parse_user(user_raw)

    return ValidatedInitData(
        user=user,
        auth_date=auth_date,
        query_id=fields.get("query_id"),
        chat_instance=fields.get("chat_instance"),
        chat_type=fields.get("chat_type"),
        start_param=fields.get("start_param"),
        raw=fields,
    )


def validate_init_data_or_error_dict(
    init_data: str,
    bot_token: str,
    **kwargs: Any,
) -> tuple[Optional[ValidatedInitData], Optional[Dict[str, str]]]:
    """Helper for HTTP handlers: (result, None) or (None, error_body)."""
    try:
        return validate_init_data(init_data, bot_token, **kwargs), None
    except TelegramInitDataError as e:
        return None, e.to_dict()


# ---------------------------------------------------------------------------
# Self-check with a known vector (synthetic)
# ---------------------------------------------------------------------------

def _self_check() -> None:
    """
    Build a synthetic initData with a fake bot token and verify round-trip.
    """
    bot_token = "123456:ABC-DEF_test_token_for_hmac"
    user = {
        "id": 42,
        "first_name": "Emma",
        "username": "emma_demo",
        "language_code": "en",
    }
    auth_date = int(time.time()) - 10
    # Build fields without hash
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = _data_check_string(fields)
    fields["hash"] = _calculate_hash(bot_token, check)
    # Encode as query string
    from urllib.parse import urlencode

    init_data = urlencode(fields)
    result = validate_init_data(init_data, bot_token, max_age_seconds=3600)
    assert result.user.id == 42
    assert result.user.first_name == "Emma"
    assert result.user.username == "emma_demo"

    # Tamper
    try:
        validate_init_data(init_data + "x", bot_token)
        raise SystemExit("tamper should fail")
    except TelegramInitDataError as e:
        assert e.code == "invalid_init_data"

    # Expired
    old_fields = dict(fields)
    old_fields["auth_date"] = str(int(time.time()) - 100_000)
    old_check = _data_check_string({k: v for k, v in old_fields.items() if k != "hash"})
    old_fields["hash"] = _calculate_hash(bot_token, old_check)
    try:
        validate_init_data(urlencode(old_fields), bot_token, max_age_seconds=60)
        raise SystemExit("expired should fail")
    except TelegramInitDataError:
        pass

    print("telegram initData validation OK")


if __name__ == "__main__":
    _self_check()
