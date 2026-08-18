CREATE TABLE IF NOT EXISTS study_sessions (
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

CREATE INDEX IF NOT EXISTS study_sessions_student_status_idx
  ON study_sessions(student_id, status);
