"""
AI School — repositories for accounts, students, web_pins, auth_sessions.

Uses db.get_connection() when DATABASE_URL is set.
PIN hashing: hashlib.sha256 for sketch (swap to argon2 in production).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db as db_pool
from jwt_refresh import SessionRecord


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(10)}"


def use_postgres() -> bool:
    return db_pool.get_engine() is not None


def hash_pin(pin: str) -> str:
    # Sketch only — production: argon2/bcrypt
    return hashlib.sha256(f"aischool-pin:{pin}".encode()).hexdigest()


def _ts(dt) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _from_ts(ts: Optional[int]):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# In-memory fallback (when no DATABASE_URL)
# ---------------------------------------------------------------------------

class MemoryRepos:
    def __init__(self) -> None:
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.by_telegram: Dict[int, str] = {}
        self.students: Dict[str, Dict[str, Any]] = {}
        self.by_account_student: Dict[str, str] = {}
        self.web_pins: Dict[str, Dict[str, Any]] = {}

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        return self.accounts.get(account_id)

    def get_account_by_telegram(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        aid = self.by_telegram.get(telegram_user_id)
        return self.accounts.get(aid) if aid else None

    def create_parent_account(
        self, *, telegram_user_id: Optional[int], display_name: str, language: str = "en"
    ) -> Dict[str, Any]:
        account_id = _new_id("acc")
        row = {
            "id": account_id,
            "role": "parent",
            "telegram_user_id": telegram_user_id,
            "display_name": display_name,
            "language": language,
        }
        self.accounts[account_id] = row
        if telegram_user_id is not None:
            self.by_telegram[telegram_user_id] = account_id
        return row

    def create_student_account(
        self,
        *,
        telegram_user_id: Optional[int],
        display_name: str,
        grade: int,
        plan_tier: str = "basic",
        parent_id: Optional[str] = None,
        language: str = "en",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        account_id = _new_id("acc")
        student_id = _new_id("stu")
        acc = {
            "id": account_id,
            "role": "student",
            "telegram_user_id": telegram_user_id,
            "display_name": display_name,
            "language": language,
        }
        st = {
            "id": student_id,
            "account_id": account_id,
            "parent_id": parent_id,
            "grade": grade,
            "plan_tier": plan_tier,
            "tier_version": 1,
            "class_name": None,
            "avatar_emoji": None,
        }
        self.accounts[account_id] = acc
        self.students[student_id] = st
        self.by_account_student[account_id] = student_id
        if telegram_user_id is not None:
            self.by_telegram[telegram_user_id] = account_id
        return acc, st

    def get_student(self, student_id: str) -> Optional[Dict[str, Any]]:
        return self.students.get(student_id)

    def get_student_by_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        sid = self.by_account_student.get(account_id)
        return self.students.get(sid) if sid else None

    def list_children(self, parent_account_id: str) -> List[Dict[str, Any]]:
        return [s for s in self.students.values() if s.get("parent_id") == parent_account_id]

    def set_web_pin(self, student_id: str, pin: str) -> None:
        self.web_pins[student_id] = {
            "student_id": student_id,
            "pin_hash": hash_pin(pin),
            "failed_attempts": 0,
            "locked_until": None,
        }

    def get_web_pin(self, student_id: str) -> Optional[Dict[str, Any]]:
        return self.web_pins.get(student_id)

    def update_web_pin(self, student_id: str, **fields: Any) -> None:
        row = self.web_pins.get(student_id)
        if not row:
            return
        row.update(fields)


# ---------------------------------------------------------------------------
# Postgres repository
# ---------------------------------------------------------------------------

class PostgresRepos:
    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM accounts WHERE id = :id"), {"id": account_id}
            ).mappings().first()
            return dict(row) if row else None

    def get_account_by_telegram(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM accounts WHERE telegram_user_id = :tg"),
                {"tg": telegram_user_id},
            ).mappings().first()
            return dict(row) if row else None

    def create_parent_account(
        self, *, telegram_user_id: Optional[int], display_name: str, language: str = "en"
    ) -> Dict[str, Any]:
        from sqlalchemy import text

        account_id = _new_id("acc")
        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO accounts (id, role, telegram_user_id, display_name, language)
                    VALUES (:id, 'parent', :tg, :name, :lang)
                    """
                ),
                {"id": account_id, "tg": telegram_user_id, "name": display_name, "lang": language},
            )
            conn.execute(
                text("INSERT INTO parents (account_id, max_children) VALUES (:id, 7)"),
                {"id": account_id},
            )
        return self.get_account(account_id)  # type: ignore

    def create_student_account(
        self,
        *,
        telegram_user_id: Optional[int],
        display_name: str,
        grade: int,
        plan_tier: str = "basic",
        parent_id: Optional[str] = None,
        language: str = "en",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        from sqlalchemy import text

        account_id = _new_id("acc")
        student_id = _new_id("stu")
        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO accounts (id, role, telegram_user_id, display_name, language)
                    VALUES (:id, 'student', :tg, :name, :lang)
                    """
                ),
                {"id": account_id, "tg": telegram_user_id, "name": display_name, "lang": language},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO students
                      (id, account_id, parent_id, grade, plan_tier, tier_version)
                    VALUES
                      (:sid, :aid, :pid, :grade, :tier, 1)
                    """
                ),
                {
                    "sid": student_id,
                    "aid": account_id,
                    "pid": parent_id,
                    "grade": grade,
                    "tier": plan_tier,
                },
            )
        acc = self.get_account(account_id)
        st = self.get_student(student_id)
        assert acc and st
        return acc, st

    def get_student(self, student_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM students WHERE id = :id"), {"id": student_id}
            ).mappings().first()
            return dict(row) if row else None

    def get_student_by_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM students WHERE account_id = :id"), {"id": account_id}
            ).mappings().first()
            return dict(row) if row else None

    def list_children(self, parent_account_id: str) -> List[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            rows = conn.execute(
                text("SELECT * FROM students WHERE parent_id = :pid"),
                {"pid": parent_account_id},
            ).mappings().all()
            return [dict(r) for r in rows]

    def set_web_pin(self, student_id: str, pin: str) -> None:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO web_pins (student_id, pin_hash, failed_attempts, locked_until, updated_at)
                    VALUES (:sid, :hash, 0, NULL, now())
                    ON CONFLICT (student_id) DO UPDATE SET
                      pin_hash = EXCLUDED.pin_hash,
                      failed_attempts = 0,
                      locked_until = NULL,
                      updated_at = now()
                    """
                ),
                {"sid": student_id, "hash": hash_pin(pin)},
            )

    def get_web_pin(self, student_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM web_pins WHERE student_id = :id"), {"id": student_id}
            ).mappings().first()
            return dict(row) if row else None

    def update_web_pin(self, student_id: str, **fields: Any) -> None:
        from sqlalchemy import text

        allowed = {"pin_hash", "failed_attempts", "locked_until"}
        sets = []
        params: Dict[str, Any] = {"sid": student_id}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "locked_until" and isinstance(v, (int, float)):
                v = _from_ts(int(v))
            sets.append(f"{k} = :{k}")
            params[k] = v
        if not sets:
            return
        sets.append("updated_at = now()")
        sql = f"UPDATE web_pins SET {', '.join(sets)} WHERE student_id = :sid"
        with db_pool.get_connection() as conn:
            conn.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Postgres SessionStore for jwt_refresh.TokenService
# ---------------------------------------------------------------------------

class PostgresSessionStore:
    def save(self, session: SessionRecord) -> None:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO auth_sessions (
                      id, account_id, family_id, token_hash, auth_method, role,
                      student_id, parent_id, expires_at, revoked_at, replaced_by, created_at
                    ) VALUES (
                      :id, :account_id, :family_id, :token_hash, :auth_method, :role,
                      :student_id, :parent_id, :expires_at, :revoked_at, :replaced_by, :created_at
                    )
                    """
                ),
                {
                    "id": session.id,
                    "account_id": session.account_id,
                    "family_id": session.family_id,
                    "token_hash": session.token_hash,
                    "auth_method": session.auth_method,
                    "role": session.role,
                    "student_id": session.student_id,
                    "parent_id": session.parent_id,
                    "expires_at": _from_ts(session.expires_at),
                    "revoked_at": _from_ts(session.revoked_at),
                    "replaced_by": session.replaced_by,
                    "created_at": _from_ts(session.created_at),
                },
            )

    def get_by_token_hash(self, token_hash: str) -> Optional[SessionRecord]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM auth_sessions WHERE token_hash = :h"), {"h": token_hash}
            ).mappings().first()
            return self._row_to_session(row) if row else None

    def get_by_id(self, session_id: str) -> Optional[SessionRecord]:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM auth_sessions WHERE id = :id"), {"id": session_id}
            ).mappings().first()
            return self._row_to_session(row) if row else None

    def update(self, session: SessionRecord) -> None:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE auth_sessions SET
                      token_hash = :token_hash,
                      revoked_at = :revoked_at,
                      replaced_by = :replaced_by,
                      expires_at = :expires_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": session.id,
                    "token_hash": session.token_hash,
                    "revoked_at": _from_ts(session.revoked_at),
                    "replaced_by": session.replaced_by,
                    "expires_at": _from_ts(session.expires_at),
                },
            )

    def revoke_family(self, family_id: str, at: int) -> int:
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = :at
                    WHERE family_id = :fid AND revoked_at IS NULL
                    """
                ),
                {"fid": family_id, "at": _from_ts(at)},
            )
            return result.rowcount or 0

    @staticmethod
    def _row_to_session(row) -> SessionRecord:
        d = dict(row)
        return SessionRecord(
            id=d["id"],
            account_id=d["account_id"],
            family_id=d["family_id"],
            token_hash=d["token_hash"],
            auth_method=d["auth_method"],
            role=d["role"],
            student_id=d.get("student_id"),
            parent_id=d.get("parent_id"),
            expires_at=_ts(d["expires_at"]) or 0,
            revoked_at=_ts(d.get("revoked_at")),
            replaced_by=d.get("replaced_by"),
            created_at=_ts(d.get("created_at")) or int(time.time()),
        )


def get_repos():
    """Return (user_repo, session_store) depending on DATABASE_URL."""
    if use_postgres():
        return PostgresRepos(), PostgresSessionStore()
    mem = MemoryRepos()
    from jwt_refresh import InMemorySessionStore

    return mem, InMemorySessionStore()
