"""
AI School — Postgres connection pooling (SQLAlchemy 2.x)

Usage:
  from db import get_engine, get_connection, session_scope

Env:
  DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/aischool
  DB_POOL_SIZE=5
  DB_MAX_OVERFLOW=5
  DB_POOL_RECYCLE=1800

If DATABASE_URL is unset, get_engine() returns None (app can use in-memory).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator, Optional

_engine = None


def database_url() -> Optional[str]:
    url = os.environ.get("DATABASE_URL", "").strip()
    return url or None


def get_engine():
    """
    Singleton engine with connection pool.
    Created once per process — safe for FastAPI workers.
    """
    global _engine
    if _engine is not None:
        return _engine

    url = database_url()
    if not url:
        return None

    try:
        from sqlalchemy import create_engine
    except ImportError as e:
        raise RuntimeError(
            "SQLAlchemy is required for Postgres. pip install 'sqlalchemy>=2' 'psycopg[binary]'"
        ) from e

    # Normalize classic postgres:// → postgresql+psycopg://
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+asyncpg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
    max_overflow = int(os.environ.get("DB_MAX_OVERFLOW", "5"))
    pool_recycle = int(os.environ.get("DB_POOL_RECYCLE", "1800"))

    _engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # drop stale connections
        pool_recycle=pool_recycle,
        pool_timeout=30,
        future=True,
    )
    return _engine


def pool_status() -> dict:
    eng = get_engine()
    if eng is None:
        return {"enabled": False, "reason": "DATABASE_URL not set"}
    pool = eng.pool
    return {
        "enabled": True,
        "pool_size": getattr(pool, "size", lambda: None)(),
        "checked_in": getattr(pool, "checkedin", lambda: None)(),
        "checked_out": getattr(pool, "checkedout", lambda: None)(),
        "overflow": getattr(pool, "overflow", lambda: None)(),
    }


@contextmanager
def get_connection():
    """
    Borrow one connection from the pool; always return it.
    Usage:
      with get_connection() as conn:
          conn.execute(...)
    """
    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL not configured")
    conn = eng.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()  # returns connection to the pool (does not close TCP if pooled)


@contextmanager
def session_scope() -> Generator:
    """ORM Session scope if you use declarative models later."""
    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL not configured")
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Call on app shutdown to close pooled connections cleanly."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
