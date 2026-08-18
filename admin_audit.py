"""
AI School — Master Admin audit log.

Writes every sensitive admin action for later review.
- Postgres table admin_audit_log when DATABASE_URL is set
- In-memory ring buffer otherwise (dev)

Do not log secrets (API keys, tokens, full PINs).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_memory: List[Dict[str, Any]] = []
_MEMORY_MAX = 500


def _use_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def log_admin_action(
    *,
    action: str,
    actor_type: str,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    detail = detail or {}
    # never persist obvious secrets
    safe = {k: v for k, v in detail.items() if k.lower() not in ("password", "pin", "token", "api_key", "authorization")}
    entry = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "detail": safe,
        "ip": ip,
        "user_agent": (user_agent or "")[:300] or None,
    }

    if _use_postgres():
        try:
            import db as db_pool
            from sqlalchemy import text

            with db_pool.get_connection() as conn:
                row = conn.execute(
                    text(
                        """
                        INSERT INTO admin_audit_log
                          (actor_type, actor_id, action, resource_type, resource_id, detail, ip, user_agent)
                        VALUES
                          (:actor_type, :actor_id, :action, :resource_type, :resource_id,
                           CAST(:detail AS jsonb), :ip, :user_agent)
                        RETURNING id, created_at
                        """
                    ),
                    {
                        "actor_type": actor_type,
                        "actor_id": actor_id,
                        "action": action,
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "detail": json.dumps(safe),
                        "ip": ip,
                        "user_agent": entry["user_agent"],
                    },
                ).mappings().first()
                if row:
                    entry["id"] = row["id"]
                    entry["created_at"] = row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"])
            return entry
        except Exception as e:
            print(f"[admin_audit] postgres write failed: {e}; falling back to memory")

    with _lock:
        entry["id"] = len(_memory) + 1
        _memory.append(entry)
        if len(_memory) > _MEMORY_MAX:
            del _memory[: len(_memory) - _MEMORY_MAX]
    return entry


def list_admin_actions(*, limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    if _use_postgres():
        try:
            import db as db_pool
            from sqlalchemy import text

            with db_pool.get_connection() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT id, created_at, actor_type, actor_id, action,
                               resource_type, resource_id, detail, ip, user_agent
                        FROM admin_audit_log
                        ORDER BY created_at DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": limit},
                ).mappings().all()
            out = []
            for r in rows:
                d = dict(r)
                if hasattr(d.get("created_at"), "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
                if isinstance(d.get("detail"), str):
                    try:
                        d["detail"] = json.loads(d["detail"])
                    except Exception:
                        pass
                out.append(d)
            return out
        except Exception as e:
            print(f"[admin_audit] postgres read failed: {e}")

    with _lock:
        return list(reversed(_memory[-limit:]))
