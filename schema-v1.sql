-- AI School v1 schema (PostgreSQL-oriented)
-- See data-model.md for rules and product constraints.
-- Timezone for business "day": Asia/Phnom_Penh (enforce in app)

CREATE TABLE accounts (
  id                    TEXT PRIMARY KEY,
  role                  TEXT NOT NULL CHECK (role IN ('student', 'parent')),
  telegram_user_id      BIGINT UNIQUE,
  display_name          TEXT,
  language              TEXT NOT NULL DEFAULT 'en',
  referred_by_account_id TEXT REFERENCES accounts(id),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE parents (
  account_id   TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  max_children INT NOT NULL DEFAULT 7
);

CREATE TABLE students (
  id            TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
  parent_id     TEXT REFERENCES accounts(id),
  grade         INT NOT NULL CHECK (grade BETWEEN 4 AND 12),
  class_name    TEXT,
  plan_tier     TEXT NOT NULL DEFAULT 'basic'
                  CHECK (plan_tier IN ('basic', 'silver', 'gold')),
  tier_version  INT NOT NULL DEFAULT 1,
  avatar_emoji  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX students_parent_id_idx ON students(parent_id);

CREATE TABLE web_pins (
  student_id       TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
  pin_hash         TEXT NOT NULL,
  failed_attempts  INT NOT NULL DEFAULT 0,
  locked_until     TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth_sessions (
  id           TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  family_id    TEXT NOT NULL,
  token_hash   TEXT NOT NULL UNIQUE,
  auth_method  TEXT NOT NULL CHECK (auth_method IN ('telegram', 'pin')),
  role         TEXT NOT NULL,
  student_id   TEXT,
  parent_id    TEXT,
  expires_at   TIMESTAMPTZ NOT NULL,
  revoked_at   TIMESTAMPTZ,
  replaced_by  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX auth_sessions_account_id_idx ON auth_sessions(account_id);
CREATE INDEX auth_sessions_family_id_idx ON auth_sessions(family_id);

CREATE TABLE subject_passes (
  id           TEXT PRIMARY KEY,
  student_id   TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  subject_key  TEXT NOT NULL,
  period_start DATE NOT NULL,
  period_end   DATE NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('active', 'canceled', 'expired')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX subject_passes_student_idx ON subject_passes(student_id);

CREATE TABLE usage_daily (
  student_id            TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  usage_date            DATE NOT NULL,
  sessions_used         INT NOT NULL DEFAULT 0,
  session_seconds_used  INT NOT NULL DEFAULT 0,
  review_seconds_used   INT NOT NULL DEFAULT 0,
  reflect_seconds_used  INT NOT NULL DEFAULT 0,
  voice_units_used      INT NOT NULL DEFAULT 0,
  live_seconds_used     INT NOT NULL DEFAULT 0,
  PRIMARY KEY (student_id, usage_date)
);

CREATE TABLE study_sessions (
  id                   TEXT PRIMARY KEY,
  student_id           TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  subject_key          TEXT NOT NULL,
  subject_track        TEXT,
  teacher_key          TEXT NOT NULL,
  mode                 TEXT NOT NULL CHECK (mode IN ('lesson', 'review', 'reflect')),
  status               TEXT NOT NULL CHECK (status IN ('active', 'paused', 'ended', 'abandoned')),
  plan_tier_snapshot   TEXT,
  started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at             TIMESTAMPTZ,
  duration_limit_sec   INT NOT NULL,
  seconds_remaining    INT,
  pauses_used          INT NOT NULL DEFAULT 0,
  extension_used       BOOLEAN NOT NULL DEFAULT FALSE,
  usage_date           DATE NOT NULL
);

CREATE INDEX study_sessions_student_status_idx ON study_sessions(student_id, status);

CREATE TABLE session_messages (
  id                TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
  role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content           TEXT NOT NULL,
  token_prompt      INT,
  token_completion  INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX session_messages_session_id_idx ON session_messages(session_id);

CREATE TABLE session_recaps (
  session_id     TEXT PRIMARY KEY REFERENCES study_sessions(id) ON DELETE CASCADE,
  summary_en     TEXT,
  summary_km     TEXT,
  practice_json  JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payment_intents (
  id                      TEXT PRIMARY KEY,
  payer_account_id        TEXT NOT NULL REFERENCES accounts(id),
  line_items              JSONB NOT NULL,
  amount_cents            INT NOT NULL,
  currency                TEXT NOT NULL DEFAULT 'USD',
  status                  TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'expired', 'failed')),
  tier_versions_snapshot  JSONB,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
