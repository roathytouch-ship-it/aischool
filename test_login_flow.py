"""
Login flow test — works without Postgres (memory) and with DATABASE_URL (Postgres).

Memory:
  python3 test_login_flow.py

Postgres (after migrations 000001–000003):
  export DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/aischool
  export JWT_SECRET=dev-secret-change-me-32chars-min!!
  python3 test_login_flow.py
"""

from __future__ import annotations

import os
import sys

# Ensure artifacts on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jwt_refresh import Principal, TokenService
from repositories import get_repos, hash_pin, use_postgres


def run() -> None:
    repo, store = get_repos()
    secret = os.environ.get("JWT_SECRET", "dev-secret-change-me-32chars-min!!")
    tokens = TokenService(store, secret)
    mode = "postgres" if use_postgres() else "memory"
    print(f"=== Login flow test ({mode}) ===")

    # 1) Parent + PIN child
    parent = repo.create_parent_account(
        telegram_user_id=None, display_name="Test Parent", language="en"
    )
    acc, st = repo.create_student_account(
        telegram_user_id=None,
        display_name="Sokha",
        grade=5,
        plan_tier="silver",
        parent_id=parent["id"],
    )
    repo.set_web_pin(st["id"], "4821")
    print("seeded student_id=", st["id"])

    # 2) Wrong PIN → attempts
    pin_row = repo.get_web_pin(st["id"])
    assert pin_row and pin_row["pin_hash"] == hash_pin("4821")
    repo.update_web_pin(st["id"], failed_attempts=1)
    pin_row = repo.get_web_pin(st["id"])
    assert int(pin_row["failed_attempts"]) == 1
    print("wrong-pin attempt counter OK")

    # 3) Correct PIN path → JWT
    repo.update_web_pin(st["id"], failed_attempts=0, locked_until=None)
    principal = Principal(
        account_id=acc["id"],
        role="student",
        auth_method="pin",
        student_id=st["id"],
        parent_id=parent["id"],
        display_name="Sokha",
        grade=5,
        plan_tier="silver",
    )
    pair = tokens.issue_token_pair(principal)
    claims = tokens.authenticate_access(pair.access_token)
    assert claims["sub"] == acc["id"]
    assert claims["student_id"] == st["id"]
    print("access JWT OK")

    # 4) Refresh rotation
    rotated = tokens.refresh(pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token
    try:
        tokens.refresh(pair.refresh_token)
        raise SystemExit("reuse should fail")
    except Exception as e:
        assert "reuse" in str(e).lower() or "refresh" in str(e).lower()
    print("refresh rotation OK")

    # 5) Reload student from store
    loaded = repo.get_student(st["id"])
    assert loaded and loaded["grade"] == 5
    kids = repo.list_children(parent["id"])
    assert any(k["id"] == st["id"] for k in kids)
    print("repo read-back OK")
    print(f"=== PASS ({mode}) ===")


if __name__ == "__main__":
    run()
