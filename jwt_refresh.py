"""
AI School — JWT access + opaque refresh token logic (reference implementation).

Matches:
  - artifacts/openapi-auth.yaml  (POST /auth/refresh, logout)
  - artifacts/jwt-refresh-logic.md

Dependencies (when wiring to a real app):
  pip install PyJWT

This module is framework-agnostic. Persist sessions via SessionStore protocol.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ACCESS_TTL_SECONDS = 30 * 60          # 30 minutes
REFRESH_TTL_SECONDS = 14 * 24 * 3600  # 14 days
REFRESH_BYTES = 32
JWT_ISSUER = "ai-school"
JWT_TYP_ACCESS = "access"


class AuthError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 401):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Session model + store protocol
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    id: str
    account_id: str
    family_id: str
    token_hash: str
    auth_method: str          # telegram | pin
    role: str                 # student | parent
    student_id: Optional[str]
    parent_id: Optional[str]
    expires_at: int           # unix ts
    revoked_at: Optional[int] = None
    replaced_by: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


class SessionStore(Protocol):
    def save(self, session: SessionRecord) -> None: ...
    def get_by_token_hash(self, token_hash: str) -> Optional[SessionRecord]: ...
    def get_by_id(self, session_id: str) -> Optional[SessionRecord]: ...
    def update(self, session: SessionRecord) -> None: ...
    def revoke_family(self, family_id: str, at: int) -> int: ...


class InMemorySessionStore:
    """Dev/test store. Replace with Postgres in production."""

    def __init__(self) -> None:
        self._by_id: Dict[str, SessionRecord] = {}
        self._by_hash: Dict[str, str] = {}

    def save(self, session: SessionRecord) -> None:
        self._by_id[session.id] = session
        self._by_hash[session.token_hash] = session.id

    def get_by_token_hash(self, token_hash: str) -> Optional[SessionRecord]:
        sid = self._by_hash.get(token_hash)
        return self._by_id.get(sid) if sid else None

    def get_by_id(self, session_id: str) -> Optional[SessionRecord]:
        return self._by_id.get(session_id)

    def update(self, session: SessionRecord) -> None:
        self._by_id[session.id] = session
        self._by_hash[session.token_hash] = session.id

    def revoke_family(self, family_id: str, at: int) -> int:
        n = 0
        for s in self._by_id.values():
            if s.family_id == family_id and s.revoked_at is None:
                s.revoked_at = at
                n += 1
        return n


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_BYTES)


def _b64url_encode(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    import base64
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def sign_access_jwt(claims: Dict[str, Any], secret: str, alg: str = "HS256") -> str:
    """
    Minimal HS256 JWT. Prefer PyJWT in production:
        import jwt
        return jwt.encode(claims, secret, algorithm="HS256")
    """
    import json

    if alg != "HS256":
        raise ValueError("Reference implementation only supports HS256")

    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def verify_access_jwt(token: str, secret: str, alg: str = "HS256") -> Dict[str, Any]:
    import json

    try:
        h_b64, p_b64, s_b64 = token.split(".")
    except ValueError as e:
        raise AuthError("unauthorized", "Malformed access token") from e

    signing_input = f"{h_b64}.{p_b64}".encode()
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual = _b64url_decode(s_b64)
    if not hmac.compare_digest(expected, actual):
        raise AuthError("unauthorized", "Invalid access token signature")

    try:
        claims = json.loads(_b64url_decode(p_b64))
    except Exception as e:
        raise AuthError("unauthorized", "Invalid access token payload") from e

    now = int(time.time())
    if claims.get("exp", 0) < now:
        raise AuthError("unauthorized", "Access token expired")
    if claims.get("iss") != JWT_ISSUER:
        raise AuthError("unauthorized", "Invalid token issuer")
    if claims.get("typ") != JWT_TYP_ACCESS:
        raise AuthError("unauthorized", "Not an access token")
    return claims


# ---------------------------------------------------------------------------
# Token service
# ---------------------------------------------------------------------------

@dataclass
class Principal:
    account_id: str
    role: str
    auth_method: str
    student_id: Optional[str] = None
    parent_id: Optional[str] = None
    display_name: Optional[str] = None
    grade: Optional[int] = None
    plan_tier: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "role": self.role,
            "student_id": self.student_id,
            "parent_id": self.parent_id,
            "telegram_user_id": None,
            "auth_method": self.auth_method,
            "grade": self.grade,
            "plan_tier": self.plan_tier,
            "display_name": self.display_name,
        }


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int = ACCESS_TTL_SECONDS
    token_type: str = "Bearer"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
        }


class TokenService:
    def __init__(
        self,
        store: SessionStore,
        jwt_secret: str,
        access_ttl: int = ACCESS_TTL_SECONDS,
        refresh_ttl: int = REFRESH_TTL_SECONDS,
    ) -> None:
        if not jwt_secret or len(jwt_secret) < 32:
            raise ValueError("jwt_secret must be at least 32 characters")
        self.store = store
        self.jwt_secret = jwt_secret
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    def issue_token_pair(self, principal: Principal) -> TokenPair:
        """Call after successful Telegram or PIN login."""
        now = int(time.time())
        session_id = new_id("sess")
        family_id = new_id("fam")
        refresh_raw = generate_refresh_token()

        session = SessionRecord(
            id=session_id,
            account_id=principal.account_id,
            family_id=family_id,
            token_hash=hash_refresh_token(refresh_raw),
            auth_method=principal.auth_method,
            role=principal.role,
            student_id=principal.student_id,
            parent_id=principal.parent_id,
            expires_at=now + self.refresh_ttl,
        )
        self.store.save(session)

        access = self._make_access(principal, session_id, now)
        return TokenPair(access_token=access, refresh_token=refresh_raw, expires_in=self.access_ttl)

    def refresh(self, refresh_token: str) -> TokenPair:
        """
        POST /auth/refresh body: { "refresh_token": "..." }
        Rotates refresh token; reuses of old token revoke the whole family.
        """
        if not refresh_token or not isinstance(refresh_token, str):
            raise AuthError("invalid_refresh_token", "Refresh token required")

        now = int(time.time())
        token_hash = hash_refresh_token(refresh_token)
        row = self.store.get_by_token_hash(token_hash)

        if row is None:
            raise AuthError("invalid_refresh_token", "Unknown refresh token")

        if row.revoked_at is not None:
            # Already used after rotation → possible theft
            self.store.revoke_family(row.family_id, now)
            raise AuthError(
                "refresh_token_reuse",
                "Refresh token already used; all sessions in this family were revoked",
            )

        if row.expires_at < now:
            raise AuthError("refresh_token_expired", "Refresh token expired; sign in again")

        # Rotate
        new_sid = new_id("sess")
        new_raw = generate_refresh_token()

        row.revoked_at = now
        row.replaced_by = new_sid
        self.store.update(row)

        new_session = SessionRecord(
            id=new_sid,
            account_id=row.account_id,
            family_id=row.family_id,
            token_hash=hash_refresh_token(new_raw),
            auth_method=row.auth_method,
            role=row.role,
            student_id=row.student_id,
            parent_id=row.parent_id,
            expires_at=now + self.refresh_ttl,
        )
        self.store.save(new_session)

        principal = Principal(
            account_id=row.account_id,
            role=row.role,
            auth_method=row.auth_method,
            student_id=row.student_id,
            parent_id=row.parent_id,
        )
        access = self._make_access(principal, new_sid, now)
        return TokenPair(access_token=access, refresh_token=new_raw, expires_in=self.access_ttl)

    def logout_by_access_token(self, access_token: str) -> None:
        """POST /auth/logout with Bearer access token — revoke session."""
        claims = verify_access_jwt(access_token, self.jwt_secret)
        sid = claims.get("sid")
        if not sid:
            return
        row = self.store.get_by_id(sid)
        if row and row.revoked_at is None:
            row.revoked_at = int(time.time())
            self.store.update(row)

    def logout_by_refresh_token(self, refresh_token: str) -> None:
        token_hash = hash_refresh_token(refresh_token)
        row = self.store.get_by_token_hash(token_hash)
        if row and row.revoked_at is None:
            row.revoked_at = int(time.time())
            self.store.update(row)

    def authenticate_access(self, access_token: str) -> Dict[str, Any]:
        """Middleware helper: verify access JWT and return claims."""
        return verify_access_jwt(access_token, self.jwt_secret)

    def _make_access(self, principal: Principal, session_id: str, now: int) -> str:
        claims = {
            "sub": principal.account_id,
            "role": principal.role,
            "student_id": principal.student_id,
            "parent_id": principal.parent_id,
            "auth_method": principal.auth_method,
            "sid": session_id,
            "iat": now,
            "exp": now + self.access_ttl,
            "iss": JWT_ISSUER,
            "typ": JWT_TYP_ACCESS,
        }
        return sign_access_jwt(claims, self.jwt_secret)


# ---------------------------------------------------------------------------
# Example usage / self-check
# ---------------------------------------------------------------------------

def _demo() -> None:
    secret = os.environ.get("JWT_SECRET", "dev-secret-change-me-32chars-min!!")
    store = InMemorySessionStore()
    svc = TokenService(store, secret)

    principal = Principal(
        account_id="acc_demo",
        role="student",
        auth_method="pin",
        student_id="stu_sokha_5a1",
        parent_id="acc_parent",
        display_name="Sokha",
        grade=5,
        plan_tier="silver",
    )

    pair = svc.issue_token_pair(principal)
    print("issued expires_in=", pair.expires_in)

    claims = svc.authenticate_access(pair.access_token)
    assert claims["sub"] == "acc_demo"
    assert claims["sid"].startswith("sess_")

    rotated = svc.refresh(pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token

    try:
        svc.refresh(pair.refresh_token)
        raise SystemExit("reuse should fail")
    except AuthError as e:
        assert e.code == "refresh_token_reuse"

    print("jwt refresh logic OK")


if __name__ == "__main__":
    _demo()
