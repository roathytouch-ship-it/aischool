"""
AI School — Auth API sketch (FastAPI)

Phase A+B: Telegram login, PIN login, refresh, logout, /me
Persists to Postgres when DATABASE_URL is set; otherwise in-memory.

Requires:
  pip install fastapi uvicorn pydantic
  pip install 'sqlalchemy>=2' 'psycopg[binary]'   # for Postgres

Env:
  TELEGRAM_BOT_TOKEN, JWT_SECRET, DATABASE_URL
  MASTER_ADMIN_TELEGRAM_IDS, ADMIN_API_KEY   # Master Admin auth
  DB_POOL_SIZE=5  DB_MAX_OVERFLOW=5

Run migrations first, then:
  uvicorn auth_api_sketch:app --reload --port 8080
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import db as db_pool
from jwt_refresh import AuthError, Principal, TokenService
from rate_limit import (
    check_login_ip,
    check_pin_student,
    check_telegram_user,
    raise_if_limited,
)
from repositories import get_repos, hash_pin, use_postgres
from telegram_initdata import TelegramInitDataError, validate_init_data
# Optional modules — soft import so local lesson works even if some files are missing
try:
    import admin_auth
except ImportError:
    admin_auth = None  # type: ignore
try:
    import admin_audit
except ImportError:
    admin_audit = None  # type: ignore
try:
    import payment_telegram
except ImportError:
    payment_telegram = None  # type: ignore
try:
    import referral
except ImportError:
    referral = None  # type: ignore
try:
    import tts_client
except ImportError:
    tts_client = None  # type: ignore
try:
    import stt_client
except ImportError:
    stt_client = None  # type: ignore
try:
    import live_talk_service
except ImportError:
    live_talk_service = None  # type: ignore

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me-32chars-min!!")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE_SECONDS", "86400"))

user_repo, session_store = get_repos()
tokens = TokenService(session_store, JWT_SECRET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    eng = db_pool.get_engine()
    if eng is not None:
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    yield
    db_pool.dispose_engine()


app = FastAPI(title="AI School Auth API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TelegramAuthBody(BaseModel):
    init_data: str
    role: Optional[Literal["student", "parent"]] = None
    grade: Optional[int] = Field(None, ge=4, le=12)
    plan_tier: Optional[Literal["basic", "silver", "gold"]] = "basic"
    display_name: Optional[str] = None
    referral_code: Optional[str] = None


class PinAuthBody(BaseModel):
    student_id: str
    pin: str = Field(..., min_length=4, max_length=4, pattern=r"^[0-9]{4}$")


class RefreshBody(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bearer_principal(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"error": "unauthorized", "message": "Bearer token required"})
    token = authorization.split(" ", 1)[1].strip()
    try:
        return tokens.authenticate_access(token)
    except AuthError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())



def _admin_actor(ctx: Dict[str, Any]) -> tuple:
    if ctx.get("auth") == "admin_api_key":
        return "admin_api_key", "api_key"
    return "jwt", str(ctx.get("sub") or ctx.get("account_id") or "")


def _client_meta(request: Request) -> tuple:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua

def require_master_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
) -> Dict[str, Any]:
    """Master Admin only: valid X-Admin-Key OR Bearer JWT with role master_admin."""
    if admin_auth is not None and admin_auth.admin_key_valid(x_admin_key):
        return {"auth": "admin_api_key", "role": "master_admin"}
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = tokens.authenticate_access(token)
        except AuthError as e:
            raise HTTPException(e.http_status, detail=e.to_dict())
        if admin_auth is not None and admin_auth.claims_are_master_admin(claims):
            return claims
        raise HTTPException(
            403,
            detail={"error": "forbidden", "message": "Master Admin role required"},
        )
    raise HTTPException(
        401,
        detail={
            "error": "unauthorized",
            "message": "Bearer master_admin token or X-Admin-Key required",
        },
    )


def _principal_from_account(account_id: str, auth_method: str) -> Principal:
    acc = user_repo.get_account(account_id)
    if not acc:
        raise HTTPException(401, detail={"error": "unauthorized", "message": "Account not found"})
    st = user_repo.get_student_by_account(account_id)
    parent_id = None
    if acc["role"] == "parent":
        parent_id = account_id
    elif st:
        parent_id = st.get("parent_id")
    return Principal(
        account_id=account_id,
        role=acc["role"],
        auth_method=auth_method,
        student_id=st["id"] if st else None,
        parent_id=parent_id,
        display_name=acc.get("display_name"),
        grade=st.get("grade") if st else None,
        plan_tier=st.get("plan_tier") if st else None,
    )


def _auth_success(principal: Principal, is_new: bool) -> Dict[str, Any]:
    pair = tokens.issue_token_pair(principal)
    body = pair.to_dict()
    body["principal"] = principal.to_dict()
    body["is_new_account"] = is_new
    return body


def _locked_until_ts(pin_row: Dict[str, Any]) -> float:
    lu = pin_row.get("locked_until")
    if lu is None:
        return 0.0
    if isinstance(lu, (int, float)):
        return float(lu)
    if isinstance(lu, datetime):
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        return lu.timestamp()
    return 0.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------



@app.exception_handler(Exception)
async def unhandled_api_error(request: Request, exc: Exception):
    """Last-resort handler — never leak stack traces to clients."""
    print(f"[api] unhandled {type(exc).__name__}: {exc}")
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={
            "error": "server_error",
            "message": "Something went wrong. Please try again in a moment.",
        },
    )

@app.get("/health")
def health():
    import llm_client
    return {
        "ok": True,
        "schema_phase": "A+B",
        "storage": "postgres" if use_postgres() else "memory",
        "postgres_pool": db_pool.pool_status(),
        "llm_configured": llm_client.llm_configured(),
        "llm_last_error": llm_client.last_error(),
    }


@app.get("/v1/dev/pool-check")
def pool_check():
    if not use_postgres():
        return {"ok": False, "message": "DATABASE_URL not set — using in-memory repos"}
    from sqlalchemy import text

    with db_pool.get_connection() as conn:
        row = conn.execute(text("SELECT 1 AS n")).mappings().first()
    return {"ok": True, "result": dict(row) if row else None, "pool": db_pool.pool_status()}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/v1/auth/telegram")
def auth_telegram(body: TelegramAuthBody, request: Request):
    limited = raise_if_limited(check_login_ip(_client_ip(request)))
    if limited:
        raise HTTPException(429, detail=limited, headers={"Retry-After": str(limited["retry_after_seconds"])})

    if not BOT_TOKEN:
        raise HTTPException(
            503,
            detail={
                "error": "bot_token_missing",
                "message": "Set TELEGRAM_BOT_TOKEN for Telegram login",
            },
        )
    try:
        validated = validate_init_data(
            body.init_data, BOT_TOKEN, max_age_seconds=INITDATA_MAX_AGE
        )
        tg_user = validated.user
    except TelegramInitDataError as e:
        raise HTTPException(401, detail=e.to_dict())

    limited = raise_if_limited(check_telegram_user(tg_user.id))
    if limited:
        raise HTTPException(429, detail=limited, headers={"Retry-After": str(limited["retry_after_seconds"])})

    existing = user_repo.get_account_by_telegram(tg_user.id)
    if existing:
        principal = _principal_from_account(existing["id"], "telegram")
        # Secure elevation: only allowlisted Telegram IDs become master_admin
        if admin_auth is not None and admin_auth.is_master_admin_telegram_user(tg_user.id):
            principal = Principal(
                account_id=principal.account_id,
                role="master_admin",
                auth_method="telegram",
                student_id=None,
                parent_id=None,
                display_name=principal.display_name,
            )
        return _auth_success(principal, is_new=False)

    if body.role not in ("student", "parent"):
        raise HTTPException(
            400,
            detail={"error": "role_required", "message": "role student|parent required on first signup"},
        )

    name = body.display_name or tg_user.display_name()
    lang = tg_user.language_code or "en"

    if body.role == "parent":
        acc = user_repo.create_parent_account(
            telegram_user_id=tg_user.id, display_name=name, language=lang
        )
        principal = Principal(
            account_id=acc["id"],
            role="parent",
            auth_method="telegram",
            parent_id=acc["id"],
            display_name=name,
        )
        return _auth_success(principal, is_new=True)

    if body.grade is None:
        raise HTTPException(
            400,
            detail={"error": "grade_required", "message": "grade 4-12 required for student signup"},
        )
    acc, st = user_repo.create_student_account(
        telegram_user_id=tg_user.id,
        display_name=name,
        grade=body.grade,
        plan_tier=body.plan_tier or "basic",
        language=lang,
    )
    principal = Principal(
        account_id=acc["id"],
        role="student",
        auth_method="telegram",
        student_id=st["id"],
        display_name=name,
        grade=st["grade"],
        plan_tier=st["plan_tier"],
    )
    return _auth_success(principal, is_new=True)


@app.post("/v1/auth/pin")
def auth_pin(body: PinAuthBody, request: Request):
    limited = raise_if_limited(check_login_ip(_client_ip(request)))
    if limited:
        raise HTTPException(429, detail=limited, headers={"Retry-After": str(limited["retry_after_seconds"])})

    limited = raise_if_limited(check_pin_student(body.student_id))
    if limited:
        raise HTTPException(429, detail=limited, headers={"Retry-After": str(limited["retry_after_seconds"])})

    st = user_repo.get_student(body.student_id)
    if not st:
        raise HTTPException(401, detail={"error": "invalid_pin", "message": "Incorrect student or PIN"})

    pin_row = user_repo.get_web_pin(body.student_id)
    if not pin_row:
        raise HTTPException(401, detail={"error": "invalid_pin", "message": "PIN not set for this student"})

    now = time.time()
    locked_until = _locked_until_ts(pin_row)
    if locked_until > now:
        raise HTTPException(
            423,
            detail={
                "error": "pin_locked",
                "message": "Too many failed attempts",
                "locked_until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(locked_until)),
            },
        )

    if pin_row.get("pin_hash") != hash_pin(body.pin):
        fails = int(pin_row.get("failed_attempts") or 0) + 1
        remaining = max(0, 5 - fails)
        if fails >= 5:
            user_repo.update_web_pin(
                body.student_id, failed_attempts=0, locked_until=int(now + 15 * 60)
            )
            raise HTTPException(
                423,
                detail={
                    "error": "pin_locked",
                    "message": "Too many failed attempts. Locked 15 minutes.",
                },
            )
        user_repo.update_web_pin(body.student_id, failed_attempts=fails)
        raise HTTPException(
            401,
            detail={
                "error": "invalid_pin",
                "message": "Incorrect PIN",
                "attempts_remaining": remaining,
            },
        )

    user_repo.update_web_pin(body.student_id, failed_attempts=0, locked_until=None)
    principal = _principal_from_account(st["account_id"], "pin")
    return _auth_success(principal, is_new=False)


@app.post("/v1/auth/refresh")
def auth_refresh(body: RefreshBody):
    try:
        pair = tokens.refresh(body.refresh_token)
        return pair.to_dict()
    except AuthError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())


@app.post("/v1/auth/logout", status_code=204)
def auth_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            tokens.logout_by_access_token(token)
        except AuthError:
            pass
    return None


@app.get("/v1/me")
def me(claims: Dict[str, Any] = Depends(bearer_principal)):
    account_id = claims["sub"]
    principal = _principal_from_account(account_id, claims.get("auth_method", "telegram"))
    out: Dict[str, Any] = {"principal": principal.to_dict()}
    if principal.role == "parent":
        kids = user_repo.list_children(account_id)
        children = []
        for k in kids:
            child_acc = user_repo.get_account(k["account_id"])
            children.append(
                {
                    "student_id": k["id"],
                    "display_name": (child_acc or {}).get("display_name"),
                    "grade": k["grade"],
                    "plan_tier": k["plan_tier"],
                    "has_telegram": (child_acc or {}).get("telegram_user_id") is not None,
                    "has_pin": user_repo.get_web_pin(k["id"]) is not None,
                }
            )
        out["children"] = children
    return out


@app.post("/v1/dev/seed-pin-child")
def seed_pin_child():
    """Create parent + PIN child for local tests (memory or Postgres)."""
    parent = user_repo.create_parent_account(
        telegram_user_id=None, display_name="Demo Parent", language="en"
    )
    acc, st = user_repo.create_student_account(
        telegram_user_id=None,
        display_name="Sokha",
        grade=5,
        plan_tier="silver",
        parent_id=parent["id"],
    )
    user_repo.set_web_pin(st["id"], "4821")
    return {
        "parent_account_id": parent["id"],
        "student_id": st["id"],
        "pin": "4821",
        "storage": "postgres" if use_postgres() else "memory",
        "note": "POST /v1/auth/pin with student_id + pin",
    }


# ---------------------------------------------------------------------------
# Study session routes (stub AI)
# ---------------------------------------------------------------------------

from study_service import StudyError, StudyService, get_study_store

_study_store = get_study_store()
_study = StudyService(_study_store)


class StartSessionBody(BaseModel):
    subject_key: str
    subject_track: Optional[str] = None
    mode: Literal["lesson", "review", "reflect"] = "lesson"


class SessionMessageBody(BaseModel):
    content: str


def _student_from_claims(claims: Dict[str, Any]) -> tuple[str, str]:
    """Returns (student_id, plan_tier)."""
    sid = claims.get("student_id")
    if not sid:
        raise HTTPException(403, detail={"error": "forbidden", "message": "Student token required"})
    st = user_repo.get_student(sid)
    if not st:
        raise HTTPException(404, detail={"error": "not_found", "message": "Student not found"})
    return sid, st.get("plan_tier") or "basic"


@app.post("/v1/sessions/start")
def sessions_start(body: StartSessionBody, claims: Dict[str, Any] = Depends(bearer_principal)):
    student_id, plan_tier = _student_from_claims(claims)
    try:
        session = _study.start(
            student_id=student_id,
            plan_tier=plan_tier,
            subject_key=body.subject_key,
            subject_track=body.subject_track,
            mode=body.mode,
        )
    except StudyError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())
    msgs = _study.store.list_messages(session.id)
    return {
        "session": StudyService.session_dict(session),
        "messages": [{"id": m.id, "role": m.role, "content": m.content} for m in msgs],
    }


@app.post("/v1/sessions/{session_id}/messages")
def sessions_message(
    session_id: str,
    body: SessionMessageBody,
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    student_id, _ = _student_from_claims(claims)
    try:
        user_msg, ai_msg = _study.add_user_message(session_id, student_id, body.content)
    except StudyError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())
    return {
        "user": {"id": user_msg.id, "role": user_msg.role, "content": user_msg.content},
        "assistant": {"id": ai_msg.id, "role": ai_msg.role, "content": ai_msg.content},
    }




@app.post("/v1/sessions/sweep-stale")
def sessions_sweep_stale(claims: Dict[str, Any] = Depends(bearer_principal)):
    """End this student's stale active sessions (timer expired / AFK / closed app) + recap."""
    student_id, _ = _student_from_claims(claims)
    closed = _study.close_stale_sessions(student_id=student_id)
    return {"closed": closed, "count": len(closed)}

@app.post("/v1/sessions/{session_id}/end")
def sessions_end(session_id: str, claims: Dict[str, Any] = Depends(bearer_principal)):
    student_id, _ = _student_from_claims(claims)
    try:
        return _study.end(session_id, student_id)
    except StudyError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())


@app.get("/v1/sessions/{session_id}")
def sessions_get(session_id: str, claims: Dict[str, Any] = Depends(bearer_principal)):
    student_id, _ = _student_from_claims(claims)
    session = _study.store.get_session(session_id)
    if not session or session.student_id != student_id:
        raise HTTPException(404, detail={"error": "not_found", "message": "Session not found"})
    msgs = _study.store.list_messages(session_id)
    return {
        "session": StudyService.session_dict(session),
        "messages": [{"id": m.id, "role": m.role, "content": m.content} for m in msgs],
    }


@app.get("/v1/sessions/{session_id}/transcript")
def sessions_transcript(
    session_id: str,
    claims: Dict[str, Any] = Depends(bearer_principal),
    format: str = "txt",
):
    """Export session chat (+ future Live Talk captions) as plain text."""
    student_id, _ = _student_from_claims(claims)
    session = _study.store.get_session(session_id)
    if not session or session.student_id != student_id:
        raise HTTPException(404, detail={"error": "not_found", "message": "Session not found"})
    msgs = _study.store.list_messages(session_id)
    lines = [
        "AI School — Session transcript",
        f"Session: {session_id}",
        f"Student: {student_id}",
        f"Subject: {getattr(session, 'subject_key', '')} {getattr(session, 'subject_track', '') or ''}".strip(),
        f"Status: {getattr(session, 'status', '')}",
        "",
    ]
    for m in msgs:
        role = (m.role or "unknown").lower()
        label = "Student" if role in ("user", "student") else ("Teacher" if role in ("assistant", "teacher", "ai") else role)
        content = (m.content or "").replace("\r\n", "\n").strip()
        lines.append(f"[{label}] {content}")
        lines.append("")
    text_body = "\n".join(lines).strip() + "\n"
    if format == "json":
        return {
            "session_id": session_id,
            "student_id": student_id,
            "subject_key": getattr(session, "subject_key", None),
            "text": text_body,
            "messages": [{"role": m.role, "content": m.content} for m in msgs],
        }
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=text_body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="aischool-transcript-{session_id[:12]}.txt"'
        },
    )



class TtsBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice: Optional[str] = None


@app.post("/v1/tts")
def tts_speak(body: TtsBody, claims: Dict[str, Any] = Depends(bearer_principal)):
    """OpenAI TTS — teacher text → mp3. Auth required. Falls back not applied server-side."""
    _ = claims  # any logged-in principal
    if not tts_client.tts_configured():
        raise HTTPException(
            503,
            detail={"error": "tts_unavailable", "message": "OPENAI_API_KEY not set — use browser TTS"},
        )
    audio = tts_client.synthesize(body.text, voice=body.voice)
    if not audio:
        raise HTTPException(
            502,
            detail={"error": "tts_failed", "message": "OpenAI TTS failed"},
        )
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/v1/stt")
async def stt_transcribe(
    claims: Dict[str, Any] = Depends(bearer_principal),
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """OpenAI STT — student audio → text. Auth required. Live Talk realtime still on hold."""
    _ = claims
    if not stt_client.stt_configured():
        raise HTTPException(
            503,
            detail={"error": "stt_unavailable", "message": "OPENAI_API_KEY not set — use browser STT"},
        )
    raw = await file.read()
    filename = file.filename or "audio.webm"
    ctype = file.content_type or "application/octet-stream"
    text = stt_client.transcribe(raw, filename=filename, content_type=ctype, language=language)
    if not text:
        raise HTTPException(
            502,
            detail={"error": "stt_failed", "message": "OpenAI STT failed or empty transcript"},
        )
    return {"text": text, "provider": "openai"}



# ---------------------------------------------------------------------------
# Master Admin routes (secure)
# ---------------------------------------------------------------------------

class PriceBookBody(BaseModel):
    silver: Optional[float] = Field(None, ge=0)
    gold: Optional[float] = Field(None, ge=0)
    subject_pass: Optional[float] = Field(None, ge=0)  # legacy default
    subject_pass_by_subject: Optional[Dict[str, float]] = None  # per-subject Pass prices


class SubjectVisibilityBody(BaseModel):
    hidden: bool


# In-memory price book / visibility for demo (Postgres later)
_ADMIN_PRICE_BOOK = {
    "silver": 8.0,
    "gold": 18.0,
    "subject_pass": 2.5,
    "subject_pass_by_subject": {
        "general_math": 2.5,
        "general_english": 2.5,
        "advanced_english": 3.0,
        "special_math": 3.0,
        "exam_preparation": 3.5,
        "coding": 3.0,
        "ai_and_robot": 3.5,
    },
}
_ADMIN_HIDDEN_SUBJECTS: set = set()




class LiveTalkCreditBody(BaseModel):
    minutes: int = Field(..., description="60 or 120")
    source: Optional[str] = "demo"


class LiveTalkConsumeBody(BaseModel):
    session_id: str
    seconds: int = Field(..., ge=1, le=120)


@app.get("/v1/live-talk/balance")
def live_talk_balance(claims: Dict[str, Any] = Depends(bearer_principal)):
    student_id, _ = _student_from_claims(claims)
    try:
        return live_talk_service.get_balance(student_id)
    except Exception as e:
        raise HTTPException(500, detail={"error": "live_talk_error", "message": str(e)})


@app.post("/v1/live-talk/credit")
def live_talk_credit(
    body: LiveTalkCreditBody,
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    """Credit pack (demo / admin / later payment-proof). Stacks + 90-day expiry."""
    student_id, _ = _student_from_claims(claims)
    try:
        return live_talk_service.credit_pack(
            student_id, body.minutes, source=body.source or "demo"
        )
    except live_talk_service.LiveTalkError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())


@app.post("/v1/live-talk/assert")
def live_talk_assert(
    body: LiveTalkConsumeBody,
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    """Check pack + session cap before turning Live Talk on."""
    student_id, _ = _student_from_claims(claims)
    try:
        return live_talk_service.assert_can_use_live(student_id, body.session_id)
    except live_talk_service.LiveTalkError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())


@app.post("/v1/live-talk/consume")
def live_talk_consume(
    body: LiveTalkConsumeBody,
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    """Burn pack seconds + session live_seconds_used (server enforcement)."""
    student_id, _ = _student_from_claims(claims)
    try:
        return live_talk_service.consume(student_id, body.session_id, body.seconds)
    except live_talk_service.LiveTalkError as e:
        raise HTTPException(e.http_status, detail=e.to_dict())




@app.get("/v1/admin/season-events")
def admin_get_season_events(ctx: Dict[str, Any] = Depends(require_master_admin)):
    """List calendar season/public-event windows (edit via PUT)."""
    try:
        from season_calendar import load_season_config, active_season_events

        cfg = load_season_config()
        return {
            "config": cfg,
            "active_today": active_season_events(),
        }
    except Exception as e:
        raise HTTPException(500, detail={"error": "season_load_failed", "message": str(e)})


@app.put("/v1/admin/season-events")
def admin_put_season_events(body: Dict[str, Any], ctx: Dict[str, Any] = Depends(require_master_admin)):
    """Replace season_events.json — Master Admin editable public events."""
    try:
        from season_calendar import save_season_config, load_season_config

        save_season_config(body if isinstance(body, dict) else {})
        if admin_audit:
            actor_type, actor_id = _admin_actor(ctx)
            admin_audit.log_admin_action(
                actor_type=actor_type,
                actor_id=actor_id,
                action="season_events.update",
                resource_type="config",
                resource_id="season_events",
                detail={"events": len((body or {}).get("events") or [])},
            )
        return {"ok": True, "config": load_season_config()}
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_config", "message": str(e)})
    except Exception as e:
        raise HTTPException(500, detail={"error": "season_save_failed", "message": str(e)})

@app.get("/v1/admin/me")
def admin_me(ctx: Dict[str, Any] = Depends(require_master_admin)):
    return {
        "ok": True,
        "role": "master_admin",
        "auth": ctx.get("auth") or "jwt",
        "account_id": ctx.get("sub") or ctx.get("account_id"),
    }


@app.get("/v1/admin/price-book")
def admin_get_prices(ctx: Dict[str, Any] = Depends(require_master_admin)):
    return {"prices": dict(_ADMIN_PRICE_BOOK)}


@app.patch("/v1/admin/price-book")
def admin_patch_prices(
    body: PriceBookBody,
    request: Request,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    before = dict(_ADMIN_PRICE_BOOK)
    if body.silver is not None:
        _ADMIN_PRICE_BOOK["silver"] = float(body.silver)
    if body.gold is not None:
        _ADMIN_PRICE_BOOK["gold"] = float(body.gold)
    if body.subject_pass is not None:
        _ADMIN_PRICE_BOOK["subject_pass"] = float(body.subject_pass)
    if body.subject_pass_by_subject is not None:
        cur = dict(_ADMIN_PRICE_BOOK.get("subject_pass_by_subject") or {})
        for k, v in body.subject_pass_by_subject.items():
            cur[str(k)] = float(v)
        _ADMIN_PRICE_BOOK["subject_pass_by_subject"] = cur
    actor_type, actor_id = _admin_actor(ctx)
    ip, ua = _client_meta(request)
    admin_audit and admin_audit.log_admin_action(
        action="price_book.patch",
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="price_book",
        resource_id="global",
        detail={"before": before, "after": dict(_ADMIN_PRICE_BOOK)},
        ip=ip,
        user_agent=ua,
    )
    return {"ok": True, "prices": dict(_ADMIN_PRICE_BOOK)}


@app.get("/v1/admin/subjects/visibility")
def admin_list_visibility(ctx: Dict[str, Any] = Depends(require_master_admin)):
    return {"hidden": sorted(_ADMIN_HIDDEN_SUBJECTS)}


@app.patch("/v1/admin/subjects/{subject_key}/visibility")
def admin_set_visibility(
    subject_key: str,
    body: SubjectVisibilityBody,
    request: Request,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    key = subject_key.strip()
    if body.hidden:
        _ADMIN_HIDDEN_SUBJECTS.add(key)
    else:
        _ADMIN_HIDDEN_SUBJECTS.discard(key)
    actor_type, actor_id = _admin_actor(ctx)
    ip, ua = _client_meta(request)
    admin_audit and admin_audit.log_admin_action(
        action="subject.visibility",
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="subject",
        resource_id=key,
        detail={"hidden": body.hidden},
        ip=ip,
        user_agent=ua,
    )
    return {"ok": True, "subject_key": key, "hidden": body.hidden}


@app.get("/v1/admin/students")
def admin_list_students(ctx: Dict[str, Any] = Depends(require_master_admin)):
    """Placeholder list — wire to Postgres student directory later."""
    return {
        "students": [],
        "message": "Directory endpoint ready; attach user_repo list when implementing full admin UI",
    }


@app.get("/v1/admin/audit")
def admin_audit_list(
    limit: int = 50,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    return {"events": admin_audit.list_admin_actions(limit=limit)}


# ---------------------------------------------------------------------------
# Payments — manual proof + Telegram Approve/Reject
# ---------------------------------------------------------------------------

class PaymentProofBody(BaseModel):
    buyer_type: Literal["parent", "independent_student"]
    buyer_id: str
    buyer_name: str = ""
    product: str  # silver | gold | subject_pass:Coding | credit_topup
    amount: float = Field(..., ge=0)
    currency: str = "USD"
    proof_note: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


@app.post("/v1/payments/proof")
def submit_payment_proof(
    body: PaymentProofBody,
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    """User finished screenshot flow → pending_review + Telegram buttons to Admin."""
    payment = payment_telegram.create_pending_payment(
        buyer_type=body.buyer_type,
        buyer_id=body.buyer_id,
        buyer_name=body.buyer_name or body.buyer_id,
        product=body.product,
        amount=body.amount,
        currency=body.currency,
        detail=body.detail,
        proof_note=body.proof_note,
    )
    notify = payment_telegram.notify_admins_payment_pending(payment)
    return {
        "ok": True,
        "payment": payment,
        "telegram_notify": {
            "ok": notify.get("ok"),
            "sent": notify.get("sent", 0),
            "error": notify.get("error"),
        },
        "message": "Pending Admin approval. Access unlocks after Approve.",
    }


@app.get("/v1/admin/payments/pending")
def admin_pending_payments(
    limit: int = 50,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    return {"payments": payment_telegram.list_pending(limit=limit)}


@app.post("/v1/admin/payments/{payment_id}/approve")
def admin_approve_payment(
    payment_id: str,
    request: Request,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    actor_type, actor_id = _admin_actor(ctx)
    ok, reason, row = payment_telegram.resolve_payment(
        payment_id, "approve", actor_id=actor_id, actor_type=actor_type
    )
    if not ok:
        raise HTTPException(400, detail={"error": reason, "payment": row})
    return {"ok": True, "payment": row}


@app.post("/v1/admin/payments/{payment_id}/reject")
def admin_reject_payment(
    payment_id: str,
    request: Request,
    ctx: Dict[str, Any] = Depends(require_master_admin),
):
    actor_type, actor_id = _admin_actor(ctx)
    ok, reason, row = payment_telegram.resolve_payment(
        payment_id, "reject", actor_id=actor_id, actor_type=actor_type
    )
    if not ok:
        raise HTTPException(400, detail={"error": reason, "payment": row})
    return {"ok": True, "payment": row}


@app.post("/v1/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Set webhook to https://YOUR_API/v1/telegram/webhook
    Handles callback_query from Approve / Reject inline buttons.
    """
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(400, detail={"error": "invalid_json"})

    cq = update.get("callback_query")
    if cq:
        result = payment_telegram.handle_callback_query(cq)
        return {"ok": True, "handled": "callback_query", "result": result}

    return {"ok": True, "handled": "ignored"}


@app.get("/v1/me/wallet")
def me_wallet(claims: Dict[str, Any] = Depends(bearer_principal)):
    account_id = str(claims.get("sub") or claims.get("account_id") or "")
    if not account_id:
        raise HTTPException(401, detail={"error": "unauthorized"})
    return referral.get_wallet(account_id)


@app.get("/v1/me/referral")
def me_referral(claims: Dict[str, Any] = Depends(bearer_principal)):
    account_id = str(claims.get("sub") or claims.get("account_id") or "")
    if not account_id:
        raise HTTPException(401, detail={"error": "unauthorized"})
    return referral.referrer_summary(account_id)


@app.get("/v1/students/me/weekly-review")
def student_weekly_review(
    which: str = "current",
    claims: Dict[str, Any] = Depends(bearer_principal),
):
    """Weekly review cycle snapshot (Mon–Sun Asia/Phnom_Penh). which=current|previous"""
    if claims.get("role") not in ("student", "parent", "master_admin"):
        # student token preferred; parent later can pass child id
        pass
    student_id = claims.get("student_id") or claims.get("sub")
    if not student_id:
        raise HTTPException(400, detail={"error": "no_student", "message": "Student id required"})
    if which not in ("current", "previous"):
        which = "current"
    try:
        return _study.build_weekly_review(student_id, which=which)
    except Exception as e:
        print(f"[api] weekly-review: {e}")
        raise HTTPException(500, detail={"error": "weekly_review_failed", "message": "Could not build weekly review"})

