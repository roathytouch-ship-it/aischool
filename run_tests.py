"""
AI School — automated local tests

Usage (from artifacts folder):

  # 1) Logic tests only (no API server needed; uses memory or DATABASE_URL)
  python run_tests.py

  # 2) Also hit a running API (start uvicorn first)
  python run_tests.py --api

  # 3) Windows PowerShell example:
  #    $env:DATABASE_URL = "postgresql+psycopg://aischool:aischool@127.0.0.1:5432/aischool"
  #    $env:JWT_SECRET = "dev-secret-change-me-32chars-min!!"
  #    python run_tests.py
  #    python run_tests.py --api
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    raise SystemExit(1)


def test_login_module() -> None:
    print("\n[1] Login flow (repositories + JWT)")
    from test_login_flow import run

    run()
    _ok("login module")


def test_study_module() -> None:
    print("\n[2] Study flow (start / message / end / basic lock)")
    from test_study_flow import run

    run()
    _ok("study module")


def test_api(base: str) -> None:
    print(f"\n[3] HTTP API smoke ({base})")
    base = base.rstrip("/")

    def req(method: str, path: str, body=None, token=None):
        data = None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        r = urllib.request.Request(
            base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(r, timeout=10) as res:
                raw = res.read().decode("utf-8")
                return res.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                detail = json.loads(raw)
            except Exception:
                detail = raw
            raise RuntimeError(f"HTTP {e.code} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach API at {base}. Is uvicorn running?\n  {e}"
            ) from e

    status, health = req("GET", "/health")
    if status != 200 or not health or not health.get("ok"):
        _fail(f"health bad: {health}")
    _ok(f"health storage={health.get('storage')}")

    status, auth = req(
        "POST",
        "/v1/auth/pin",
        {"student_id": "stu_demo_sokha", "pin": "4821"},
    )
    token = (auth or {}).get("access_token")
    if not token:
        _fail(f"no access_token: {auth}")
    _ok("PIN login stu_demo_sokha")

    status, sess = req(
        "POST",
        "/v1/sessions/start",
        {"subject_key": "coding", "mode": "lesson"},
        token=token,
    )
    sid = (sess or {}).get("session", {}).get("id")
    if not sid:
        # Maybe already active from a previous run — try general_math free core path on new student only
        _fail(f"start session failed: {sess}")
    _ok(f"start session {sid}")

    status, msg = req(
        "POST",
        f"/v1/sessions/{sid}/messages",
        {"content": "Hello teacher"},
        token=token,
    )
    if not (msg or {}).get("assistant"):
        _fail(f"message failed: {msg}")
    _ok("send message")

    status, end = req("POST", f"/v1/sessions/{sid}/end", {}, token=token)
    if not (end or {}).get("recap"):
        _fail(f"end failed: {end}")
    _ok("end session + recap")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI School automated tests")
    parser.add_argument(
        "--api",
        action="store_true",
        help="Also test HTTP API (uvicorn must be running)",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("API_BASE", "http://127.0.0.1:8080"),
        help="API base URL for --api",
    )
    args = parser.parse_args()

    print("AI School automated tests")
    print("DATABASE_URL set:" , "yes" if os.environ.get("DATABASE_URL") else "no (memory mode for login test)")

    test_login_module()
    test_study_module()

    if args.api:
        test_api(args.base)
    else:
        print("\n[3] HTTP API skipped (run with --api after starting uvicorn)")

    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()
