"""
AI School — study session service (start / message / end).

Enforces:
  - one active block per student
  - plan session caps (basic 1 / silver 3 / gold 4)
  - duration by plan (25 / 45 / 45) or review 15 / reflect 10
  - subject unlock: silver/gold all; basic free cores + passes

Memory + Postgres via repositories patterns.
LLM call is stubbed (echo) — replace with real provider later.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import llm_client
from prompts import (
    format_teacher_track_card,
    parse_recap_llm_output,
    parse_recap_structured_json,
    recap_system_prompt,
    recap_user_payload,
    teacher_system_prompt,
)

PHNOM_PENH = ZoneInfo("Asia/Phnom_Penh")


def _season_note_safe() -> str:
    try:
        from season_calendar import season_note_for_today

        return season_note_for_today() or ""
    except Exception:
        return ""


_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|tg://|mailto:|file://|"
    r"(?:^|[\s<(])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:[/:\s]|$))",
    re.I,
)


def message_has_link(text: str) -> bool:
    return bool(_LINK_RE.search(text or ""))


_CHOICE_ANSWER_RE = re.compile(
    r"^\s*(?:q\s*\d+\s*[:.)-]?\s*)?"
    r"(?:[a-e]|true|false|t|f|ng|not given)"
    r"(?:\s*[,;/]\s*(?:q\s*\d+\s*[:.)-]?\s*)?"
    r"(?:[a-e]|true|false|t|f|ng|not given))*"
    r"\s*[.]?\s*$",
    re.I,
)


def _is_choice_answer(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 48:
        return False
    return bool(_CHOICE_ANSWER_RE.match(t))


def _expand_choice_answer(text: str) -> str:
    """Keep DB as typed; send the model a full sentence so a lone letter is not 'cut off'."""
    t = (text or "").strip()
    if not _is_choice_answer(t):
        return t
    return f"My answer is {t}."


def _looks_cut_off_mcq(text: str) -> bool:
    """True when a paper item started A) but never reached B)."""
    t = llm_client.strip_continue_cue(text or "")
    if not t:
        return False
    if re.search(r"\bA\)\s*$", t):
        return True
    if re.search(r"\bA\)", t) and not re.search(r"\bB\)", t):
        return True
    return False


def _last_assistant_content(history: List[Any], skip_id: Optional[str] = None) -> str:
    for m in reversed(history or []):
        if skip_id and getattr(m, "id", None) == skip_id:
            continue
        if getattr(m, "role", "") == "assistant":
            return getattr(m, "content", None) or ""
    return ""


def _wait_for_options_reply() -> str:
    return (
        "I got your answer, but I should not mark it yet.\n\n"
        "The last question only showed **A)** — B, C, and D were still missing. "
        "Marking now would not be fair.\n\n"
        "Tap **Continue** so I can send the rest of the choices. "
        "Then send your letter again."
        "\n\n[[CONTINUE]]"
    )


def _choice_glitch_first(student_text: str) -> str:
    """Replace a cut-off reply. No LLM retry. Do not ask them to send the same letter again."""
    shown = (student_text or "").strip() or "your answer"
    return (
        f"I received **{shown}**, but I could not read that check on my end.\n\n"
        f"Let's move on — tap **Practice more** for a new item, "
        f"or **Hints** if you still want a nudge on this one. "
        f"You can also type one short sentence about the question."
    )


def _choice_glitch_again(student_text: str) -> str:
    """Same letter sent after a glitch — still no fake mark, still no 'send B again'."""
    shown = (student_text or "").strip() or "your answer"
    return (
        f"I still have **{shown}**, but I could not check it.\n\n"
        f"Skip this item. Tap **Practice more** for a new question, "
        f"or **Explain** for the idea without a score."
    )


def _looks_like_cut_off_glitch(text: str) -> bool:
    t = (text or "").lower()
    needles = (
        "got cut off",
        "was cut off",
        "answer got cut",
        "message was cut",
        "reply got cut",
        "sorry the answer",
        "send that again",
        "send it again",
        "short glitch",
    )
    return any(n in t for n in needles)


FREE_CORES = {"general_math", "general_english"}
PLAN_SESSIONS = {"basic": 1, "silver": 3, "gold": 4}
PLAN_MINUTES = {"basic": 25, "silver": 45, "gold": 45}
TEACHERS = {
    "general_math": "alex",
    "general_english": "emma",
    "advanced_english": "ms_claire",
    "special_math": "dr_nova",
    "exam_preparation": "sophia",
    "coding": "codey",
    "ai_and_robot": "calliope",
    "spelling_bee": "ivy",
    "vocabulary_building": "lexsis",
    "skills_path": "sage",
    "health_science": "dr_mira",
    "french": "etoile",
    "spanish": "estrella",
    "russian": "ksenia",
    "languages": "etoile",  # refined by subject_track at prompt layer
    "scholarship_prep": "nadia",
    "scholarship": "nadia",
}


def today_phnom() -> date:
    return datetime.now(PHNOM_PENH).date()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(10)}"


class StudyError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass
class StudySession:
    id: str
    student_id: str
    subject_key: str
    subject_track: Optional[str]
    teacher_key: str
    mode: str  # lesson | review | reflect
    status: str  # active | paused | ended | abandoned
    plan_tier_snapshot: str
    started_at: float
    ended_at: Optional[float]
    duration_limit_sec: int
    seconds_remaining: int
    pauses_used: int
    extension_used: bool
    usage_date: date
    # Vocabulary Building: words/phrases introduced this block (from **bold** in teacher replies)
    session_vocab_items: List[str] = field(default_factory=list)
    # Sticky goals for this live block (1–2 short lines) — survives 6/8-turn history window
    session_goals: List[str] = field(default_factory=list)
    # Last letter/T-F-NG that hit a cut-off glitch (memory only — no extra DB column)
    choice_glitch_letter: Optional[str] = None


def build_initial_session_goals(
    *,
    subject_key: str,
    subject_track: Optional[str],
    prior_recap: Optional[str],
    mode: str = "lesson",
) -> List[str]:
    """1–2 short sticky goals for this block. Prefer last Next; else subject/track default."""
    from prompts import SUBJECT_LABELS

    goals: List[str] = []
    prior = (prior_recap or "").strip()
    if prior:
        for line in prior.splitlines():
            s = line.strip()
            low = s.lower()
            if low.startswith("next:"):
                g = s.split(":", 1)[1].strip()
                if g and len(g) >= 3:
                    goals.append(g[:120])
                    break
        # Optional second: one weak/strength hint if present and short
        if len(goals) < 2:
            for line in prior.splitlines():
                s = line.strip()
                low = s.lower()
                if low.startswith("strength:") or low.startswith("weak"):
                    g = s.split(":", 1)[1].strip() if ":" in s else ""
                    if g and len(g) >= 8:
                        goals.append(("Build on: " + g)[:120])
                        break

    label = SUBJECT_LABELS.get(subject_key, subject_key or "this subject")
    track = (subject_track or "").strip()
    sk = (subject_key or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not goals:
        if mode == "review":
            goals.append(f"Review: clarify {label}" + (f" ({track})" if track else ""))
        elif mode == "reflect":
            goals.append(f"Reflect on recent {label} work")
        elif sk in ("coding",):
            # Golden-rule style: one tangible outcome for THIS block
            if track:
                goals.append(f"Coding — finish one working piece in {track} this block")
            else:
                goals.append("Coding — one small working result before End")
        elif sk in ("ai_and_robot", "ai_robot"):
            if track:
                goals.append(f"AI & Robot — one clear step in {track} this block")
            else:
                goals.append("AI & Robot — one clear build/understand step this block")
        elif sk in ("spelling_bee", "spelling"):
            goals.append("Spelling Bee — one Round of 5 (easy→hard); retry misses if time")
        elif sk in ("exam_prep", "exam_preparation", "special_math"):
            if track:
                goals.append(f"{label} — practice or mini mock in {track}")
            else:
                goals.append(f"{label} — practice first; mini mock only if student asks")
        elif track:
            goals.append(f"{label} — focus: {track}")
        else:
            goals.append(f"{label} — practice and deepen this block")

    # Second goal only when track adds a distinct focus and we only have one generic line
    if len(goals) == 1 and track and track.lower() not in goals[0].lower():
        if mode == "lesson":
            if sk in ("coding", "ai_and_robot", "ai_robot"):
                goals.append("Keep the goal tangible — small working step, not a huge unfinished project")
            else:
                goals.append(f"Stay with {track} unless student asks a different path")

    # Dedupe + cap 2
    out: List[str] = []
    seen: set[str] = set()
    for g in goals:
        g = " ".join(str(g).split()).strip()
        if not g:
            continue
        key = g.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(g[:120])
        if len(out) >= 2:
            break
    return out


def collect_session_vocab_items(messages: List[StudyMessage], *, limit: int = 20) -> List[str]:
    """Pull target vocab items from teacher **bold** highlights this session. Order preserved, de-duped."""
    import re

    items: List[str] = []
    seen: set[str] = set()
    # **phrase** markdown; allow spaces and short hyphens inside
    pat = re.compile(r"\*\*([^*]{2,48})\*\*")
    for m in messages:
        if not m or (m.role or "") not in ("assistant", "teacher"):
            continue
        text = m.content or ""
        for raw in pat.findall(text):
            item = " ".join(str(raw).split()).strip(" .:;,!?\"'")
            if len(item) < 2 or len(item) > 48:
                continue
            key = item.lower()
            if key in seen:
                continue
            # skip pure numbers / single letters
            if item.isdigit() or (len(item) == 1 and item.isalpha()):
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def collect_session_bee_round(
    messages: List[StudyMessage], *, limit: int = 12
) -> Dict[str, List[str]]:
    """Spelling Bee light tracker: words tried + soft misses this session."""
    import re

    words: List[str] = []
    misses: List[str] = []
    seen_w: set[str] = set()
    seen_m: set[str] = set()
    bold = re.compile(r"\*\*([^*]{2,40})\*\*")
    spell_cue = re.compile(
        r"(?:spell|spelling|please spell|try)\s+[\"']?([A-Za-z][A-Za-z\-']{1,38})[\"']?",
        re.I,
    )
    miss_cue = re.compile(
        r"\b(not quite|incorrect|almost|try again|missed|wrong spelling|close,? but)\b",
        re.I,
    )
    last_word: Optional[str] = None

    def _add_word(raw: str) -> Optional[str]:
        nonlocal last_word
        item = " ".join(str(raw).split()).strip(" .:;,!?\"'")
        if len(item) < 2 or len(item) > 40:
            return None
        if not re.search(r"[A-Za-z]", item):
            return None
        key = item.lower()
        if key not in seen_w:
            seen_w.add(key)
            words.append(item)
        last_word = item
        return item

    for m in messages:
        if not m:
            continue
        text = m.content or ""
        role = (m.role or "").lower()
        if role in ("assistant", "teacher"):
            for raw in bold.findall(text):
                _add_word(raw)
            for g in spell_cue.findall(text):
                _add_word(g)
            if miss_cue.search(text) and last_word:
                mk = last_word.lower()
                if mk not in seen_m:
                    seen_m.add(mk)
                    misses.append(last_word)
        if len(words) >= limit:
            break
    return {"words": words[:limit], "misses": misses[: min(8, limit)]}


def collect_session_mock_items(
    messages: List[StudyMessage], *, limit: int = 8
) -> List[str]:
    """Exam Prep / Special Math mini-mock: light Q tags + soft result from nearby teacher lines."""
    import re

    items: List[str] = []
    seen: set[str] = set()
    q_pat = re.compile(
        r"\b(?:Q(?:uestion)?\s*(\d{1,2})|(\d{1,2})\s*[\).:])\b",
        re.I,
    )
    ok_cue = re.compile(r"\b(correct|right|yes\.|well done|nice work|exactly)\b", re.I)
    miss_cue = re.compile(
        r"\b(not quite|incorrect|almost|try again|missed|wrong|close,? but)\b", re.I
    )
    pending: Optional[str] = None

    for m in messages:
        if not m or (m.role or "") not in ("assistant", "teacher", "user"):
            continue
        text = m.content or ""
        role = (m.role or "").lower()
        if role in ("assistant", "teacher"):
            for match in q_pat.finditer(text):
                num = match.group(1) or match.group(2)
                if not num:
                    continue
                tag = f"Q{int(num)}"
                pending = tag
                if tag.lower() not in seen:
                    seen.add(tag.lower())
                    items.append(tag)
                    if len(items) >= limit:
                        return items
            if pending:
                if ok_cue.search(text) and not miss_cue.search(text):
                    soft = f"{pending} ok"
                    if soft.lower() not in seen:
                        seen.add(soft.lower())
                        items.append(soft)
                    pending = None
                elif miss_cue.search(text):
                    soft = f"{pending} miss"
                    if soft.lower() not in seen:
                        seen.add(soft.lower())
                        items.append(soft)
                    pending = None
    return items[:limit]


def collect_session_language_phrases(
    messages: List[StudyMessage], *, limit: int = 4
) -> List[str]:
    """FR/ES/RU: 2–4 target phrases this block from teacher **bold** (or short quoted lines)."""
    import re

    items: List[str] = []
    seen: set[str] = set()
    bold = re.compile(r"\*\*([^*]{2,60})\*\*")
    quoted = re.compile(r"[«\"]([^»\"]{2,60})[»\"]")

    for m in messages:
        if not m or (m.role or "") not in ("assistant", "teacher"):
            continue
        text = m.content or ""
        for raw in bold.findall(text) + quoted.findall(text):
            item = " ".join(str(raw).split()).strip(" .:;,!")
            if len(item) < 2 or len(item) > 60:
                continue
            if len(item.split()) > 12:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


@dataclass
class StudyMessage:
    id: str
    session_id: str
    role: str
    content: str
    created_at: float = field(default_factory=time.time)


class MemoryStudyStore:
    def __init__(self) -> None:
        self.sessions: Dict[str, StudySession] = {}
        self.messages: Dict[str, List[StudyMessage]] = {}
        self.recaps: Dict[str, Dict[str, Any]] = {}
        self.usage: Dict[Tuple[str, date], Dict[str, int]] = {}
        self.passes: Dict[str, List[str]] = {}  # student_id -> subject_keys

    def get_usage(self, student_id: str, d: date) -> Dict[str, int]:
        key = (student_id, d)
        if key not in self.usage:
            self.usage[key] = {
                "sessions_used": 0,
                "review_seconds_used": 0,
                "reflect_seconds_used": 0,
            }
        return self.usage[key]

    def active_session(self, student_id: str) -> Optional[StudySession]:
        for s in self.sessions.values():
            if s.student_id == student_id and s.status in ("active", "paused"):
                return s
        return None

    def list_active_sessions(self, student_id: Optional[str] = None) -> List[StudySession]:
        out = []
        for s in self.sessions.values():
            if s.status not in ("active", "paused"):
                continue
            if student_id and s.student_id != student_id:
                continue
            out.append(s)
        return out


    def save_session(self, s: StudySession) -> None:
        self.sessions[s.id] = s

    def get_session(self, session_id: str) -> Optional[StudySession]:
        return self.sessions.get(session_id)

    def add_message(self, m: StudyMessage) -> None:
        self.messages.setdefault(m.session_id, []).append(m)

    def list_messages(self, session_id: str) -> List[StudyMessage]:
        return list(self.messages.get(session_id, []))

    def save_usage(self, student_id: str, d: date, usage: Dict[str, int]) -> None:
        self.usage[(student_id, d)] = dict(usage)


    def get_latest_recap_bundle_for_track(
        self, student_id: str, subject_key: str, subject_track: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        best_sid = None
        best_t = -1.0
        want = (subject_track or "").strip()
        for s in self.sessions.values():
            if s.student_id != student_id or s.subject_key != subject_key:
                continue
            if s.status not in ("ended", "abandoned"):
                continue
            if want and (s.subject_track or "").strip() != want:
                continue
            t = float(s.ended_at or s.started_at or 0)
            if t >= best_t:
                best_t = t
                best_sid = s.id
        if not best_sid:
            return None
        rec = self.recaps.get(best_sid) or {}
        return {
            "summary_en": (rec.get("summary_en") or "").strip() or None,
            "practice_json": rec.get("practice_json") or {},
            "subject_track": want or None,
        }

    def get_latest_recap_for_subject(self, student_id: str, subject_key: str) -> Optional[str]:
        """Level B: newest ended session recap for this student+subject."""
        best_sid = None
        best_t = -1.0
        for s in self.sessions.values():
            if s.student_id != student_id or s.subject_key != subject_key:
                continue
            if s.status not in ("ended", "abandoned"):
                continue
            t = float(s.ended_at or s.started_at or 0)
            if t >= best_t:
                best_t = t
                best_sid = s.id
        if not best_sid:
            return None
        rec = self.recaps.get(best_sid) or {}
        en = (rec.get("summary_en") or "").strip()
        return en or None


    def get_progress_layer_summaries(self, student_id: str, subject_key: str) -> dict:
        """Higher-level summaries if present (memory store)."""
        out = {"semester": None, "daily": None, "session": None}
        if hasattr(self, "progress_semester"):
            # pick latest semester row for subject
            best = None
            best_key = ()
            for k, row in self.progress_semester.items():
                if not k[0] == student_id or k[1] != subject_key:
                    continue
                key = (k[2], k[3])  # year, semester
                if key >= best_key:
                    best_key = key
                    best = row
            if best and (best.get("summary_en") or "").strip():
                out["semester"] = best["summary_en"].strip()
        if hasattr(self, "progress_daily"):
            best_d = None
            best_date = ""
            for k, row in self.progress_daily.items():
                if k[0] != student_id or k[1] != subject_key:
                    continue
                d = str(k[2])
                if d >= best_date and (row.get("summary_en") or "").strip():
                    best_date = d
                    best_d = row["summary_en"].strip()
            if best_d:
                out["daily"] = best_d
        out["session"] = self.get_latest_recap_for_subject(student_id, subject_key)
        return out


    def save_recap(
        self,
        session_id: str,
        summary_en: str,
        summary_km: str = "",
        practice_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.recaps[session_id] = {
            "session_id": session_id,
            "summary_en": summary_en,
            "summary_km": summary_km,
            "practice_json": dict(practice_json or {}),
        }

    def has_pass(self, student_id: str, subject_key: str) -> bool:
        return subject_key in self.passes.get(student_id, [])

    def grant_pass(self, student_id: str, subject_key: str) -> None:
        self.passes.setdefault(student_id, [])
        if subject_key not in self.passes[student_id]:
            self.passes[student_id].append(subject_key)

    def upsert_progress_daily(
        self,
        *,
        student_id: str,
        subject_key: str,
        progress_date: date,
        minutes: int,
        summary_en: str,
        summary_km: str,
        session_id: str,
    ) -> None:
        if not hasattr(self, "progress_daily"):
            self.progress_daily = {}
        key = (student_id, subject_key, progress_date)
        row = self.progress_daily.get(key) or {
            "sessions_count": 0,
            "minutes_studied": 0,
            "summary_en": "",
            "summary_km": "",
            "last_session_id": None,
        }
        row["sessions_count"] = int(row["sessions_count"]) + 1
        row["minutes_studied"] = int(row["minutes_studied"]) + max(0, int(minutes))
        row["summary_en"] = summary_en
        row["summary_km"] = summary_km
        row["last_session_id"] = session_id
        self.progress_daily[key] = row


def _ts_to_dt(ts: Optional[float]):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _dt_to_ts(dt) -> Optional[float]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return float(dt)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class PostgresStudyStore:
    """Persists study_sessions, session_messages, session_recaps, usage_daily, subject_passes."""

    def get_usage(self, student_id: str, d: date) -> Dict[str, int]:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT sessions_used, review_seconds_used, reflect_seconds_used
                    FROM usage_daily
                    WHERE student_id = :sid AND usage_date = :d
                    """
                ),
                {"sid": student_id, "d": d},
            ).mappings().first()
            if not row:
                conn.execute(
                    text(
                        """
                        INSERT INTO usage_daily (student_id, usage_date)
                        VALUES (:sid, :d)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"sid": student_id, "d": d},
                )
                return {
                    "sessions_used": 0,
                    "review_seconds_used": 0,
                    "reflect_seconds_used": 0,
                }
            return {
                "sessions_used": int(row["sessions_used"] or 0),
                "review_seconds_used": int(row["review_seconds_used"] or 0),
                "reflect_seconds_used": int(row["reflect_seconds_used"] or 0),
            }

    def save_usage(self, student_id: str, d: date, usage: Dict[str, int]) -> None:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO usage_daily (
                      student_id, usage_date, sessions_used,
                      review_seconds_used, reflect_seconds_used
                    ) VALUES (
                      :sid, :d, :su, :rev, :ref
                    )
                    ON CONFLICT (student_id, usage_date) DO UPDATE SET
                      sessions_used = EXCLUDED.sessions_used,
                      review_seconds_used = EXCLUDED.review_seconds_used,
                      reflect_seconds_used = EXCLUDED.reflect_seconds_used
                    """
                ),
                {
                    "sid": student_id,
                    "d": d,
                    "su": usage.get("sessions_used", 0),
                    "rev": usage.get("review_seconds_used", 0),
                    "ref": usage.get("reflect_seconds_used", 0),
                },
            )

    def active_session(self, student_id: str) -> Optional[StudySession]:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM study_sessions
                    WHERE student_id = :sid AND status IN ('active', 'paused')
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ),
                {"sid": student_id},
            ).mappings().first()
            return self._row_session(row) if row else None

    def list_active_sessions(self, student_id: Optional[str] = None) -> List[StudySession]:
        import db as db_pool
        from sqlalchemy import text
        sql = """
            SELECT * FROM study_sessions
            WHERE status IN ('active', 'paused')
        """
        params = {}
        if student_id:
            sql += " AND student_id = :sid"
            params["sid"] = student_id
        with db_pool.get_connection() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        out = []
        for r in rows:
            sid = r["id"] if hasattr(r, "keys") else r[0]
            if hasattr(r, "keys"):
                sid = r["id"]
            s = self.get_session(sid)
            if s:
                out.append(s)
        return out


    def save_session(self, s: StudySession) -> None:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO study_sessions (
                      id, student_id, subject_key, subject_track, teacher_key, mode, status,
                      plan_tier_snapshot, started_at, ended_at, duration_limit_sec,
                      seconds_remaining, pauses_used, extension_used, usage_date
                    ) VALUES (
                      :id, :student_id, :subject_key, :subject_track, :teacher_key, :mode, :status,
                      :plan_tier_snapshot, :started_at, :ended_at, :duration_limit_sec,
                      :seconds_remaining, :pauses_used, :extension_used, :usage_date
                    )
                    ON CONFLICT (id) DO UPDATE SET
                      status = EXCLUDED.status,
                      ended_at = EXCLUDED.ended_at,
                      seconds_remaining = EXCLUDED.seconds_remaining,
                      pauses_used = EXCLUDED.pauses_used,
                      extension_used = EXCLUDED.extension_used
                    """
                ),
                {
                    "id": s.id,
                    "student_id": s.student_id,
                    "subject_key": s.subject_key,
                    "subject_track": s.subject_track,
                    "teacher_key": s.teacher_key,
                    "mode": s.mode,
                    "status": s.status,
                    "plan_tier_snapshot": s.plan_tier_snapshot,
                    "started_at": _ts_to_dt(s.started_at),
                    "ended_at": _ts_to_dt(s.ended_at),
                    "duration_limit_sec": s.duration_limit_sec,
                    "seconds_remaining": s.seconds_remaining,
                    "pauses_used": s.pauses_used,
                    "extension_used": s.extension_used,
                    "usage_date": s.usage_date,
                },
            )

    def get_session(self, session_id: str) -> Optional[StudySession]:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text("SELECT * FROM study_sessions WHERE id = :id"), {"id": session_id}
            ).mappings().first()
            return self._row_session(row) if row else None

    def add_message(self, m: StudyMessage) -> None:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO session_messages (id, session_id, role, content, created_at)
                    VALUES (:id, :session_id, :role, :content, :created_at)
                    """
                ),
                {
                    "id": m.id,
                    "session_id": m.session_id,
                    "role": m.role,
                    "content": m.content,
                    "created_at": _ts_to_dt(m.created_at),
                },
            )

    def list_messages(self, session_id: str) -> List[StudyMessage]:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM session_messages
                    WHERE session_id = :sid
                    ORDER BY created_at ASC
                    """
                ),
                {"sid": session_id},
            ).mappings().all()
            out: List[StudyMessage] = []
            for r in rows:
                out.append(
                    StudyMessage(
                        id=r["id"],
                        session_id=r["session_id"],
                        role=r["role"],
                        content=r["content"],
                        created_at=_dt_to_ts(r["created_at"]) or time.time(),
                    )
                )
            return out

    def save_recap(
        self,
        session_id: str,
        summary_en: str,
        summary_km: str = "",
        practice_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        import json
        import db as db_pool
        from sqlalchemy import text

        payload = json.dumps(practice_json or {})
        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO session_recaps (session_id, summary_en, summary_km, practice_json, created_at)
                    VALUES (:sid, :en, :km, CAST(:pj AS jsonb), now())
                    ON CONFLICT (session_id) DO UPDATE SET
                      summary_en = EXCLUDED.summary_en,
                      summary_km = EXCLUDED.summary_km,
                      practice_json = EXCLUDED.practice_json
                    """
                ),
                {"sid": session_id, "en": summary_en, "km": summary_km, "pj": payload},
            )

    def get_latest_recap_bundle_for_track(
        self, student_id: str, subject_key: str, subject_track: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        import db as db_pool
        from sqlalchemy import text

        want = (subject_track or "").strip()
        sql = """
            SELECT r.summary_en, r.practice_json, s.subject_track
            FROM session_recaps r
            JOIN study_sessions s ON s.id = r.session_id
            WHERE s.student_id = :sid
              AND s.subject_key = :sk
              AND s.status IN ('ended', 'abandoned')
        """
        params: Dict[str, Any] = {"sid": student_id, "sk": subject_key}
        if want:
            sql += " AND COALESCE(s.subject_track, '') = :tr"
            params["tr"] = want
        sql += """
            ORDER BY COALESCE(s.ended_at, s.started_at) DESC
            LIMIT 1
        """
        with db_pool.get_connection() as conn:
            row = conn.execute(text(sql), params).mappings().first()
        if not row:
            return None
        pj = row.get("practice_json") or {}
        if isinstance(pj, str):
            try:
                import json
                pj = json.loads(pj)
            except Exception:
                pj = {}
        return {
            "summary_en": (row.get("summary_en") or "").strip() or None,
            "practice_json": pj if isinstance(pj, dict) else {},
            "subject_track": row.get("subject_track"),
        }


    def get_latest_recap_for_subject(self, student_id: str, subject_key: str) -> Optional[str]:
        """Level B: latest recap EN for student + subject from Postgres."""
        from sqlalchemy import text
        with db_pool.get_connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT r.summary_en
                    FROM session_recaps r
                    JOIN study_sessions s ON s.id = r.session_id
                    WHERE s.student_id = :sid
                      AND s.subject_key = :sk
                      AND s.status IN ('ended', 'abandoned')
                      AND r.summary_en IS NOT NULL
                      AND length(trim(r.summary_en)) > 0
                    ORDER BY COALESCE(s.ended_at, s.started_at) DESC
                    LIMIT 1
                    """
                ),
                {"sid": student_id, "sk": subject_key},
            ).mappings().first()
        if not row:
            return None
        en = (row.get("summary_en") or "").strip()
        return en or None



    def get_progress_layer_summaries(self, student_id: str, subject_key: str) -> dict:
        """Layered: semester (if any) + latest daily + latest session recap."""
        from sqlalchemy import text
        out = {"semester": None, "daily": None, "session": None}
        with db_pool.get_connection() as conn:
            try:
                row = conn.execute(
                    text(
                        """
                        SELECT summary_en FROM progress_semester
                        WHERE student_id = :sid AND subject_key = :sk
                          AND summary_en IS NOT NULL AND length(trim(summary_en)) > 0
                        ORDER BY year DESC, semester DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": student_id, "sk": subject_key},
                ).mappings().first()
                if row and row.get("summary_en"):
                    out["semester"] = row["summary_en"].strip()
            except Exception as e:
                print(f"[study_store] semester layer skip: {e}")
            try:
                row = conn.execute(
                    text(
                        """
                        SELECT summary_en FROM progress_daily
                        WHERE student_id = :sid AND subject_key = :sk
                          AND summary_en IS NOT NULL AND length(trim(summary_en)) > 0
                        ORDER BY progress_date DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": student_id, "sk": subject_key},
                ).mappings().first()
                if row and row.get("summary_en"):
                    out["daily"] = row["summary_en"].strip()
            except Exception as e:
                print(f"[study_store] daily layer skip: {e}")
        out["session"] = self.get_latest_recap_for_subject(student_id, subject_key)
        return out


    def has_pass(self, student_id: str, subject_key: str) -> bool:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM subject_passes
                    WHERE student_id = :sid AND subject_key = :sk
                      AND status = 'active'
                      AND period_start <= CURRENT_DATE
                      AND period_end > CURRENT_DATE
                    LIMIT 1
                    """
                ),
                {"sid": student_id, "sk": subject_key},
            ).first()
            return row is not None

    def grant_pass(self, student_id: str, subject_key: str) -> None:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO subject_passes (
                      id, student_id, subject_key, period_start, period_end, status
                    ) VALUES (
                      :id, :sid, :sk, date_trunc('month', CURRENT_DATE)::date,
                      (date_trunc('month', CURRENT_DATE) + interval '1 month')::date,
                      'active'
                    )
                    """
                ),
                {"id": new_id("pass"), "sid": student_id, "sk": subject_key},
            )

    def upsert_progress_daily(
        self,
        *,
        student_id: str,
        subject_key: str,
        progress_date: date,
        minutes: int,
        summary_en: str,
        summary_km: str,
        session_id: str,
    ) -> None:
        import db as db_pool
        from sqlalchemy import text

        with db_pool.get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO progress_daily (
                      student_id, subject_key, progress_date,
                      sessions_count, minutes_studied,
                      summary_en, summary_km, last_session_id, updated_at
                    ) VALUES (
                      :sid, :sk, :d, 1, :mins, :en, :km, :ses, now()
                    )
                    ON CONFLICT (student_id, subject_key, progress_date) DO UPDATE SET
                      sessions_count = progress_daily.sessions_count + 1,
                      minutes_studied = progress_daily.minutes_studied + EXCLUDED.minutes_studied,
                      summary_en = EXCLUDED.summary_en,
                      summary_km = EXCLUDED.summary_km,
                      last_session_id = EXCLUDED.last_session_id,
                      updated_at = now()
                    """
                ),
                {
                    "sid": student_id,
                    "sk": subject_key,
                    "d": progress_date,
                    "mins": max(0, minutes),
                    "en": summary_en,
                    "km": summary_km,
                    "ses": session_id,
                },
            )

    @staticmethod
    def _row_session(row) -> StudySession:
        d = dict(row)
        ud = d["usage_date"]
        if hasattr(ud, "isoformat"):
            usage_date = ud if isinstance(ud, date) and not isinstance(ud, datetime) else date.fromisoformat(str(ud)[:10])
        else:
            usage_date = date.fromisoformat(str(ud)[:10])
        return StudySession(
            id=d["id"],
            student_id=d["student_id"],
            subject_key=d["subject_key"],
            subject_track=d.get("subject_track"),
            teacher_key=d["teacher_key"],
            mode=d["mode"],
            status=d["status"],
            plan_tier_snapshot=d.get("plan_tier_snapshot") or "basic",
            started_at=_dt_to_ts(d["started_at"]) or time.time(),
            ended_at=_dt_to_ts(d.get("ended_at")),
            duration_limit_sec=int(d["duration_limit_sec"]),
            seconds_remaining=int(d["seconds_remaining"] if d.get("seconds_remaining") is not None else d["duration_limit_sec"]),
            pauses_used=int(d.get("pauses_used") or 0),
            extension_used=bool(d.get("extension_used")),
            usage_date=usage_date,
        )


def get_study_store():
    """Postgres when DATABASE_URL set; else memory."""
    import db as db_pool

    if db_pool.get_engine() is not None:
        return PostgresStudyStore()
    return MemoryStudyStore()


class StudyService:
    def __init__(self, store):
        self.store = store


    def _term_boundary_marker(self) -> str:
        """Asia/Phnom_Penh calendar: year + semester bucket (1=Jan-Jun, 2=Jul-Dec)."""
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Phnom_Penh"))
        except Exception:
            now = datetime.utcnow()
        sem = 1 if now.month <= 6 else 2
        return f"{now.year}-S{sem}"

    def _is_first_study_day_of_term(self, student_id: str, subject_key: str) -> bool:
        """
        True if this is the first lesson day in the current term for this subject.
        Uses progress_daily dates when available; otherwise only session recaps' days.
        """
        marker = self._term_boundary_marker()
        year = int(marker.split("-S")[0])
        sem = int(marker.split("-S")[1])
        # Month range for semester
        if sem == 1:
            start_m, end_m = 1, 6
        else:
            start_m, end_m = 7, 12
        try:
            from datetime import date
            today = date.today()
            try:
                from zoneinfo import ZoneInfo
                from datetime import datetime
                today = datetime.now(ZoneInfo("Asia/Phnom_Penh")).date()
            except Exception:
                pass
        except Exception:
            from datetime import date
            today = date.today()

        # If we have progress_daily, any earlier day this term for this subject => not first
        try:
            if hasattr(self.store, "progress_daily"):
                for k, row in getattr(self.store, "progress_daily", {}).items():
                    if k[0] != student_id or k[1] != subject_key:
                        continue
                    d = k[2]
                    if hasattr(d, "year"):
                        dy, dm = d.year, d.month
                    else:
                        parts = str(d).split("-")
                        dy, dm = int(parts[0]), int(parts[1])
                    if dy == year and start_m <= dm <= end_m:
                        if d != today and str(d) < str(today):
                            return False
                return True
        except Exception:
            pass

        # Postgres path
        try:
            if hasattr(self.store, "get_connection") or True:
                import os
                if not os.environ.get("DATABASE_URL", "").strip():
                    return True  # no history → treat as first
                import db as db_pool
                from sqlalchemy import text
                with db_pool.get_connection() as conn:
                    row = conn.execute(
                        text(
                            """
                            SELECT COUNT(*) AS n FROM progress_daily
                            WHERE student_id = :sid AND subject_key = :sk
                              AND progress_date >= :start_d
                              AND progress_date < :today
                            """
                        ),
                        {
                            "sid": student_id,
                            "sk": subject_key,
                            "start_d": f"{year}-{start_m:02d}-01",
                            "today": str(today),
                        },
                    ).mappings().first()
                    n = int((row or {}).get("n") or 0)
                    return n == 0
        except Exception as e:
            print(f"[study_service] first-day check: {e}")
            return True

        return True

    def build_prior_recap_context(
        self,
        student_id: str,
        subject_key: str,
        subject_track: Optional[str] = None,
    ) -> Optional[str]:
        """
        Default = Level B (last lesson only).
        Prefer last recap + teacher card for THIS track when a track is set.
        Higher semester/year notes only on the **first study day of the new term** for this subject.
        """
        session_only = None
        teacher_card = ""
        try:
            bundle = None
            if hasattr(self.store, "get_latest_recap_bundle_for_track"):
                bundle = self.store.get_latest_recap_bundle_for_track(
                    student_id, subject_key, subject_track
                )
            if bundle:
                session_only = bundle.get("summary_en")
                teacher_card = format_teacher_track_card(
                    bundle.get("practice_json"),
                    subject_track=subject_track or bundle.get("subject_track"),
                )
            elif hasattr(self.store, "get_latest_recap_for_subject"):
                session_only = self.store.get_latest_recap_for_subject(student_id, subject_key)
        except Exception:
            session_only = None
            teacher_card = ""

        use_higher = False
        try:
            use_higher = self._is_first_study_day_of_term(student_id, subject_key)
        except Exception as e:
            print(f"[study_service] term boundary: {e}")
            use_higher = False

        parts = []
        if use_higher:
            layers = {"semester": None, "daily": None, "session": session_only}
            try:
                if hasattr(self.store, "get_progress_layer_summaries"):
                    layers = self.store.get_progress_layer_summaries(student_id, subject_key) or layers
                    if not layers.get("session"):
                        layers["session"] = session_only
            except Exception as e:
                print(f"[study_service] layer load failed: {e}")
            if layers.get("semester"):
                parts.append("SEMESTER/TERM NOTES (new term start only):\n" + str(layers["semester"])[:400])
            # Optional recent day on first day only if different
            if layers.get("daily"):
                daily = str(layers["daily"]).strip()
                sess = (layers.get("session") or "").strip()
                if daily and daily != sess:
                    parts.append("RECENT DAY NOTES:\n" + daily[:350])
            if layers.get("session"):
                parts.append("LAST LESSON:\n" + str(layers["session"])[:500])
        else:
            # Level B only
            if session_only:
                parts.append("LAST LESSON:\n" + str(session_only)[:600])
        if teacher_card:
            parts.append(teacher_card)

        if not parts:
            return None
        return "\n\n".join(parts)


    def build_weekly_review(
        self,
        student_id: str,
        *,
        which: str = "current",
    ) -> Dict[str, Any]:
        """
        Weekly review cycle snapshot for Journal / parent.
        which: "current" | "previous"
        Aggregates progress_daily for Mon–Sun Asia/Phnom_Penh.
        """
        import weekly_review as wr

        if which == "previous":
            start, end = wr.previous_week_bounds()
        else:
            start, end = wr.week_bounds()

        by_subject: Dict[str, Dict[str, Any]] = {}

        # Memory store path
        if hasattr(self.store, "progress_daily"):
            for k, row in getattr(self.store, "progress_daily", {}).items():
                if k[0] != student_id:
                    continue
                d = k[2]
                try:
                    if hasattr(d, "isoformat"):
                        dd = d
                    else:
                        from datetime import date as date_cls
                        parts = str(d).split("-")
                        dd = date_cls(int(parts[0]), int(parts[1]), int(parts[2]))
                except Exception:
                    continue
                if dd < start or dd > end:
                    continue
                sk = k[1]
                slot = by_subject.setdefault(
                    sk,
                    {"subject_key": sk, "minutes": 0, "sessions": 0, "last_summary_en": ""},
                )
                slot["minutes"] += int(row.get("minutes_studied") or 0)
                slot["sessions"] += int(row.get("sessions_count") or 0)
                if row.get("summary_en"):
                    slot["last_summary_en"] = row["summary_en"]

        # Postgres path
        try:
            import os
            if os.environ.get("DATABASE_URL", "").strip():
                import db as db_pool
                from sqlalchemy import text

                with db_pool.get_connection() as conn:
                    rows = conn.execute(
                        text(
                            """
                            SELECT subject_key,
                                   COALESCE(SUM(minutes_studied), 0) AS minutes,
                                   COALESCE(SUM(sessions_count), 0) AS sessions,
                                   (
                                     SELECT p2.summary_en FROM progress_daily p2
                                     WHERE p2.student_id = :sid
                                       AND p2.subject_key = progress_daily.subject_key
                                       AND p2.progress_date >= :start_d
                                       AND p2.progress_date <= :end_d
                                       AND p2.summary_en IS NOT NULL
                                     ORDER BY p2.progress_date DESC
                                     LIMIT 1
                                   ) AS last_summary_en
                            FROM progress_daily
                            WHERE student_id = :sid
                              AND progress_date >= :start_d
                              AND progress_date <= :end_d
                            GROUP BY subject_key
                            ORDER BY subject_key
                            """
                        ),
                        {
                            "sid": student_id,
                            "start_d": start.isoformat(),
                            "end_d": end.isoformat(),
                        },
                    ).mappings().all()
                for row in rows:
                    sk = row["subject_key"]
                    by_subject[sk] = {
                        "subject_key": sk,
                        "minutes": int(row["minutes"] or 0),
                        "sessions": int(row["sessions"] or 0),
                        "last_summary_en": (row.get("last_summary_en") or "") or "",
                    }
        except Exception as e:
            print(f"[study_service] weekly review query: {e}")

        subjects = sorted(by_subject.values(), key=lambda x: x["subject_key"])
        return wr.format_weekly_snapshot(
            student_id=student_id,
            week_start=start,
            week_end=end,
            subjects=subjects,
        )


    def is_session_stale(self, session: StudySession, *, grace_sec: int = 120, hard_hours: float = 12.0) -> bool:
        """True if active/paused past duration+grace, or older than hard_hours."""
        if session.status not in ("active", "paused"):
            return False
        now = time.time()
        started = float(session.started_at or now)
        limit = int(session.duration_limit_sec or 0)
        # paused time still counts wall clock for v1 simplicity
        if now >= started + limit + grace_sec:
            return True
        if now >= started + hard_hours * 3600:
            return True
        return False

    def close_stale_sessions(
        self,
        student_id: Optional[str] = None,
        *,
        grace_sec: int = 120,
        hard_hours: float = 12.0,
    ) -> List[Dict[str, Any]]:
        """
        Auto-End stale active/paused sessions and generate recap.
        Call on Start, optional sweep endpoint, or cron.
        """
        closed = []
        if not hasattr(self.store, "list_active_sessions"):
            # fallback single active
            if student_id:
                s = self.store.active_session(student_id)
                sessions = [s] if s else []
            else:
                sessions = []
        else:
            sessions = self.store.list_active_sessions(student_id)

        for s in sessions:
            if not self.is_session_stale(s, grace_sec=grace_sec, hard_hours=hard_hours):
                continue
            try:
                result = self.end(s.id, s.student_id)
                closed.append(
                    {
                        "session_id": s.id,
                        "student_id": s.student_id,
                        "subject_key": s.subject_key,
                        "recap": (result or {}).get("recap"),
                    }
                )
                print(f"[study_service] auto-ended stale session {s.id}")
            except StudyError as e:
                if e.code == "already_ended":
                    continue
                print(f"[study_service] stale end fail {s.id}: {e}")
            except Exception as e:
                print(f"[study_service] stale end error {s.id}: {e}")
        return closed

    def can_study(self, plan_tier: str, student_id: str, subject_key: str) -> bool:
        if plan_tier in ("silver", "gold"):
            return True
        if subject_key in FREE_CORES:
            return True
        return self.store.has_pass(student_id, subject_key)

    def start(
        self,
        *,
        student_id: str,
        plan_tier: str,
        subject_key: str,
        subject_track: Optional[str] = None,
        mode: str = "lesson",
    ) -> StudySession:
        if mode not in ("lesson", "review", "reflect"):
            raise StudyError("invalid_mode", "mode must be lesson|review|reflect")

        # Auto-end timed-out / abandoned blocks so Start is not stuck
        try:
            self.close_stale_sessions(student_id=student_id)
        except Exception as e:
            print(f"[study_service] close_stale on start: {e}")

        leftover = self.store.active_session(student_id)
        if leftover:
            print(f"[study_service] start: closing leftover {leftover.id} ({leftover.status})")
            try:
                self.end(leftover.id, leftover.student_id)
            except StudyError as e:
                if e.code != "already_ended":
                    print(f"[study_service] leftover end: {e}")
                    leftover.status = "ended"
                    leftover.ended_at = time.time()
                    self.store.save_session(leftover)
            except Exception as e:
                print(f"[study_service] leftover force-close: {e}")
                leftover.status = "ended"
                leftover.ended_at = time.time()
                self.store.save_session(leftover)
        leftover = self.store.active_session(student_id)
        if leftover:
            leftover.status = "ended"
            leftover.ended_at = leftover.ended_at or time.time()
            self.store.save_session(leftover)

        if mode == "lesson" and not self.can_study(plan_tier, student_id, subject_key):
            raise StudyError(
                "subject_locked",
                "Subject not unlocked — subscribe with Subject Pass (Basic) or upgrade plan",
                403,
            )

        d = today_phnom()
        usage = self.store.get_usage(student_id, d)

        if mode == "lesson":
            cap = PLAN_SESSIONS.get(plan_tier, 1)
            if usage["sessions_used"] >= cap:
                raise StudyError(
                    "session_cap",
                    f"No study sessions left today ({cap} on {plan_tier})",
                    403,
                )
            limit = PLAN_MINUTES.get(plan_tier, 25) * 60
        elif mode == "review":
            if plan_tier not in ("silver", "gold"):
                raise StudyError("review_not_allowed", "Review is Silver/Gold only", 403)
            if usage["review_seconds_used"] >= 15 * 60:
                raise StudyError("review_exhausted", "Review pool used up for today", 403)
            limit = 15 * 60 - usage["review_seconds_used"]
        else:  # reflect
            if plan_tier != "basic":
                raise StudyError("reflect_not_allowed", "Reflect is Basic + Pass only", 403)
            if usage["reflect_seconds_used"] >= 10 * 60:
                raise StudyError("reflect_exhausted", "Reflect pool used up for today", 403)
            limit = 10 * 60 - usage["reflect_seconds_used"]

        teacher = TEACHERS.get(subject_key, "alex")
        now = time.time()
        session = StudySession(
            id=new_id("ses"),
            student_id=student_id,
            subject_key=subject_key,
            subject_track=subject_track,
            teacher_key=teacher,
            mode=mode,
            status="active",
            plan_tier_snapshot=plan_tier,
            started_at=now,
            ended_at=None,
            duration_limit_sec=limit,
            seconds_remaining=limit,
            pauses_used=0,
            extension_used=False,
            usage_date=d,
        )
        self.store.save_session(session)

        if mode == "lesson":
            usage["sessions_used"] += 1  # consumed on Start
            self.store.save_usage(student_id, d, usage)

        # Level B+: last lesson + higher rollups (semester/daily) when they exist
        prior = None
        try:
            prior = self.build_prior_recap_context(student_id, subject_key, subject_track)
        except Exception as e:
            print(f"[study_service] prior recap load failed: {e}")
            prior = None

        # Sticky session goals (1–2) — set once at Start; injected every chat turn
        try:
            session.session_goals = build_initial_session_goals(
                subject_key=subject_key,
                subject_track=subject_track,
                prior_recap=prior,
                mode=mode,
            )
        except Exception as e:
            print(f"[study_service] session goals failed: {e}")
            session.session_goals = []
        try:
            session._prior_recap = prior  # type: ignore[attr-defined]
        except Exception:
            pass

        # Greeting message
        if prior and mode == "lesson":
            greet = (
                f"Hi! I'm your AI teacher ({teacher}). "
                f"Subject: {subject_key}"
                + (f" · {subject_track}" if subject_track else "")
                + f". You have about {limit // 60} minutes. "
                f"I saved notes from last time — we can continue from where you left off, "
                f"or review if you want. What would you like to do?"
            )
        else:
            greet = (
                f"Hi! I'm your AI teacher ({teacher}). "
                f"Subject: {subject_key}"
                + (f" · {subject_track}" if subject_track else "")
                + f". Mode: {mode}. You have about {limit // 60} minutes."
            )
        self.store.add_message(
            StudyMessage(id=new_id("msg"), session_id=session.id, role="assistant", content=greet)
        )
        # Stash on session object for message path (in-memory attribute)
        try:
            setattr(session, "_prior_recap", prior)
        except Exception:
            pass
        return session

    def add_user_message(self, session_id: str, student_id: str, content: str) -> Tuple[StudyMessage, StudyMessage]:
        session = self.store.get_session(session_id)
        if not session or session.student_id != student_id:
            raise StudyError("not_found", "Session not found", 404)
        if session.status != "active":
            raise StudyError("not_active", "Session is not active", 409)
        text = (content or "").strip()
        if not text:
            raise StudyError("empty", "Message is empty")
        if message_has_link(text):
            raise StudyError(
                "no_links",
                "Paste the question here — the teacher cannot open links.",
            )

        # Per-subject hard caps (chars). Soft UI nudge (~400) stays client-side.
        # Default 600/1200 · Exam Prep + Special Math 700/1300 · Coding + AI & Robot 1000/1400
        sk = (session.subject_key or "").strip().lower()
        if sk in ("coding", "ai_and_robot", "ai_robot"):
            student_max, ai_max = 1000, 1400
        elif sk in ("special_math", "exam_preparation", "exam_prep", "scholarship_prep", "scholarship"):
            student_max, ai_max = 700, 1300
        else:
            student_max, ai_max = 600, 1200

        if len(text) > student_max:
            raise StudyError(
                "too_long",
                f"Message too long (max {student_max} characters for this subject)",
            )

        user_msg = StudyMessage(
            id=new_id("msg"), session_id=session_id, role="user", content=text
        )
        self.store.add_message(user_msg)

        # Student grade + preferred name for teaching (from DB when available)
        grade_val = None
        preferred_name = None
        teacher_words = "school"
        try:
            from repositories import get_repos

            _ur, _sess = get_repos()
            st_row = _ur.get_student(student_id) if _ur else None
            if st_row and st_row.get("grade") is not None:
                grade_val = int(st_row["grade"])
                if grade_val < 4 or grade_val > 12:
                    grade_val = None
            if st_row:
                tw = str(st_row.get("teacher_words") or "school").strip().lower()
                if tw in ("school", "academic", "challenge"):
                    teacher_words = tw
                preferred_name = (
                    st_row.get("display_name")
                    or st_row.get("preferred_name")
                    or st_row.get("first_name")
                    or None
                )
                if preferred_name is not None:
                    preferred_name = str(preferred_name).strip() or None
                    if preferred_name and preferred_name.lower().startswith("stu_"):
                        preferred_name = None
        except Exception:
            grade_val = None
            preferred_name = None
            teacher_words = "school"

        # Build short context from this session only
        history = self.store.list_messages(session_id)
        vocab_items: List[str] = []
        bee_words: List[str] = []
        bee_misses: List[str] = []
        mock_items: List[str] = []
        lang_phrases: List[str] = []
        sk_lower = (session.subject_key or "").strip().lower().replace(" ", "_").replace("-", "_")
        if sk_lower in ("vocabulary_building", "vocabulary", "vocab"):
            vocab_items = collect_session_vocab_items(history)
            session.session_vocab_items = list(vocab_items)
        if sk_lower in ("spelling_bee", "spelling"):
            bee = collect_session_bee_round(history)
            bee_words = list(bee.get("words") or [])
            bee_misses = list(bee.get("misses") or [])
        if sk_lower in ("exam_prep", "exam_preparation", "special_math"):
            mock_items = collect_session_mock_items(history)
        if sk_lower in ("languages", "french", "spanish", "russian"):
            lang_phrases = collect_session_language_phrases(history, limit=4)

        goals = list(getattr(session, "session_goals", None) or [])
        if not goals and session.mode == "lesson":
            try:
                prior_for_goals = getattr(session, "_prior_recap", None) or self.build_prior_recap_context(
                    session.student_id, session.subject_key, session.subject_track
                )
                goals = build_initial_session_goals(
                    subject_key=session.subject_key,
                    subject_track=session.subject_track,
                    prior_recap=prior_for_goals,
                    mode=session.mode,
                )
                session.session_goals = list(goals)
            except Exception:
                goals = []

        llm_messages = [
            {
                "role": "system",
                "content": teacher_system_prompt(
                    teacher_key=session.teacher_key,
                    subject_key=session.subject_key,
                    subject_track=session.subject_track,
                    grade=grade_val,
                    plan_tier=session.plan_tier_snapshot,
                    mode=session.mode,
                    prior_recap=(
                        getattr(session, "_prior_recap", None)
                        or self.build_prior_recap_context(
                            session.student_id, session.subject_key, session.subject_track
                        )
                    ),
                    seconds_remaining=max(
                        0,
                        int(session.duration_limit_sec or 0)
                        - int(max(0, time.time() - float(session.started_at or time.time()))),
                    ),
                    duration_limit_sec=int(session.duration_limit_sec or 0),
                    season_note=_season_note_safe(),
                    teacher_words=teacher_words,
                    student_preferred_name=preferred_name,
                    session_vocab_items=vocab_items or None,
                    session_goals=goals or None,
                    session_bee_words=bee_words or None,
                    session_bee_misses=bee_misses or None,
                    session_mock_items=mock_items or None,
                    session_language_phrases=lang_phrases or None,
                ),
            }
        ]
        # Cap history by plan: Basic 6 · Silver/Gold 8 (token control)
        # Truncate stored turns to subject student_max so history stays bounded
        tier = (session.plan_tier_snapshot or "basic").lower()
        hist_n = 6 if tier == "basic" else 8
        hist_clip_user = max(student_max, 600)
        hist_clip_ai = max(ai_max, 800)
        for m in history[-hist_n:]:
            if m.role not in ("user", "assistant"):
                continue
            raw = llm_client.strip_continue_cue(m.content or "")
            if m.role == "user":
                raw = _expand_choice_answer(raw)
                piece = raw[:hist_clip_user]
            else:
                piece = raw[:hist_clip_ai]
            if piece:
                llm_messages.append({"role": m.role, "content": piece})
        choice = _is_choice_answer(text)
        last_ai = _last_assistant_content(history, skip_id=user_msg.id)
        if choice and _looks_cut_off_mcq(last_ai):
            reply_body = _wait_for_options_reply()
            session.choice_glitch_letter = None
            if len(reply_body) > ai_max:
                reply_body = llm_client._clip_at_sentence(reply_body, ai_max)
            if _looks_cut_off_mcq(reply_body) and "[[CONTINUE]]" not in reply_body.upper():
                reply_body = reply_body.rstrip() + "\n\n[[CONTINUE]]"
            ai_msg = StudyMessage(
                id=new_id("msg"), session_id=session_id, role="assistant", content=reply_body
            )
            self.store.add_message(ai_msg)
            return user_msg, ai_msg
        prev_glitch = (getattr(session, "choice_glitch_letter", None) or "").strip().lower()
        same_after_glitch = bool(
            choice and prev_glitch and text.strip().lower() == prev_glitch
        )
        if same_after_glitch:
            print("[study_service] choice-answer glitch — same letter again, no extra LLM")
            reply_body = _choice_glitch_again(text)
            session.choice_glitch_letter = None
        else:
            if choice:
                llm_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "The student sent only a letter/T-F-NG. Treat it as their choice. "
                            "Mark it now. Never say cut off / incomplete / send again. "
                            "After the mark, go to the next item. Do not make them redo this item."
                        ),
                    }
                )

            reply_body = llm_client.chat(
                llm_messages,
                max_chars=ai_max,
                subject_key=session.subject_key,
                thinking_override=False if choice else None,
            )
            if choice and (not reply_body or _looks_like_cut_off_glitch(reply_body or "")):
                print("[study_service] choice-answer glitch — no retry, move on")
                reply_body = _choice_glitch_first(text)
                session.choice_glitch_letter = text.strip()
            else:
                session.choice_glitch_letter = None
        if not reply_body:
            err = None
            try:
                err = llm_client.last_error()
            except Exception:
                err = None
            print(f"[study_service] LLM unavailable ({err}) — soft fallback reply")
            # Friendly student-facing fallback (not a raw error code)
            if err == "llm_busy":
                reply_body = (
                    "Your teacher is busy with other students right now. "
                    "Please try again in a few seconds."
                )
            elif err in ("llm_rate_limit", "llm_upstream", "llm_timeout", "llm_network"):
                reply_body = (
                    "I'm having a short connection problem. "
                    "Please send your question again in a moment. "
                    f"You wrote: “{text[:100]}”."
                )
            elif err in ("llm_quota", "llm_auth", "llm_not_configured"):
                reply_body = (
                    "The AI teacher is temporarily unavailable. "
                    "Your message was saved — try again soon, or ask a parent for help if this continues."
                )
            else:
                reply_body = (
                    "I had a short glitch reading that. "
                    "Please tap Send again — we can keep going from here."
                )
        if len(reply_body) > ai_max:
            reply_body = llm_client._clip_at_sentence(reply_body, ai_max)
        if _looks_cut_off_mcq(reply_body) and "[[CONTINUE]]" not in reply_body.upper():
            reply_body = reply_body.rstrip() + "\n\n[[CONTINUE]]"
        ai_msg = StudyMessage(
            id=new_id("msg"), session_id=session_id, role="assistant", content=reply_body
        )
        self.store.add_message(ai_msg)
        return user_msg, ai_msg

    def end(self, session_id: str, student_id: str) -> Dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session or session.student_id != student_id:
            raise StudyError("not_found", "Session not found", 404)
        if session.status in ("ended", "abandoned"):
            raise StudyError("already_ended", "Session already ended", 409)

        session.status = "ended"
        session.ended_at = time.time()
        elapsed = int(session.ended_at - session.started_at)
        session.seconds_remaining = max(0, session.duration_limit_sec - elapsed)
        self.store.save_session(session)

        usage = self.store.get_usage(student_id, session.usage_date)
        used = min(elapsed, session.duration_limit_sec)
        if session.mode == "review":
            usage["review_seconds_used"] += used
            self.store.save_usage(student_id, session.usage_date, usage)
        elif session.mode == "reflect":
            usage["reflect_seconds_used"] += used
            self.store.save_usage(student_id, session.usage_date, usage)

        # Human-friendly duration (avoid "0 min" on short test sessions)
        if used < 60:
            duration_label = f"{used} sec"
            minutes_for_progress = 1 if used >= 15 else 0  # count 1 min if they stayed ≥15s
        else:
            duration_label = f"~{used // 60} min"
            minutes_for_progress = max(1, used // 60)

        fallback_en = (
            f"Did: Session complete for {session.subject_key}"
            + (f" ({session.subject_track})" if session.subject_track else "")
            + f" ({duration_label})." + "\n"
            + "Strength: You stayed with the lesson." + "\n"
            + "Next: Open Journal or start another subject from Home when ready."
        )
        fallback_km = "មេរៀនបានបញ្ចប់។ សូមមើលសង្ខេបក្នុង Journal ឬជ្រើសមុខវិជ្ជាថ្មីនៅទំព័រដើម។"

        # Automated recap from live chat (last turns) — never blocks End
        all_msgs = self.store.list_messages(session_id)
        excerpt_parts = []
        for m in all_msgs[-8:]:
            if m.role in ("user", "assistant"):
                excerpt_parts.append(f"{m.role}: {m.content[:220]}")
        excerpt = "\n".join(excerpt_parts)[:1600] or "(no chat yet)"
        vocab_for_recap: Optional[List[str]] = None
        if (session.subject_key or "").strip().lower() in (
            "vocabulary_building",
            "vocabulary",
            "vocab",
        ):
            vocab_for_recap = collect_session_vocab_items(all_msgs)
            session.session_vocab_items = list(vocab_for_recap or [])
        summary_en = fallback_en
        summary_km = fallback_km
        recap_llm = ""
        try:
            recap_llm = llm_client.chat(
                [
                    {"role": "system", "content": recap_system_prompt()},
                    {
                        "role": "user",
                        "content": recap_user_payload(
                            subject_key=session.subject_key,
                            subject_track=session.subject_track,
                            mode=session.mode,
                            duration_label=duration_label,
                            chat_excerpt=excerpt,
                            session_vocab_items=vocab_for_recap,
                        ),
                    },
                ],
                max_chars=900,
                temperature=0.35,
            )
            summary_en, summary_km = parse_recap_llm_output(
                recap_llm or "",
                fallback_en=fallback_en,
                fallback_km=fallback_km,
            )
        except Exception:
            summary_en, summary_km = fallback_en, fallback_km

        practice_json = parse_recap_structured_json(recap_llm or "") if recap_llm else {}
        if session.subject_track and not practice_json.get("track"):
            practice_json["track"] = session.subject_track
        try:
            self.store.save_recap(session_id, summary_en, summary_km, practice_json)
        except TypeError:
            self.store.save_recap(session_id, summary_en, summary_km)

        # Learning progress: one row per subject per day (lesson mode)
        if session.mode == "lesson" and hasattr(self.store, "upsert_progress_daily"):
            self.store.upsert_progress_daily(
                student_id=student_id,
                subject_key=session.subject_key,
                progress_date=session.usage_date,
                minutes=minutes_for_progress,
                summary_en=summary_en,
                summary_km=summary_km,
                session_id=session_id,
            )

        return {
            "session": self.session_dict(session),
            "recap": {
                "summary_en": summary_en,
                "summary_km": summary_km,
            },
        }

    @staticmethod
    def session_dict(s: StudySession) -> Dict[str, Any]:
        return {
            "id": s.id,
            "student_id": s.student_id,
            "subject_key": s.subject_key,
            "subject_track": s.subject_track,
            "teacher_key": s.teacher_key,
            "mode": s.mode,
            "status": s.status,
            "plan_tier_snapshot": s.plan_tier_snapshot,
            "duration_limit_sec": s.duration_limit_sec,
            "seconds_remaining": s.seconds_remaining,
            "pauses_used": s.pauses_used,
            "usage_date": s.usage_date.isoformat(),
        }
