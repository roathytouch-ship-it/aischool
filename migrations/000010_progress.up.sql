-- Daily per-subject progress (keep for semester)
CREATE TABLE IF NOT EXISTS progress_daily (
  student_id      TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  subject_key     TEXT NOT NULL,
  progress_date   DATE NOT NULL,
  sessions_count  INT NOT NULL DEFAULT 0,
  minutes_studied INT NOT NULL DEFAULT 0,
  summary_en      TEXT,
  summary_km      TEXT,
  last_session_id TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, subject_key, progress_date)
);

CREATE INDEX IF NOT EXISTS progress_daily_student_date_idx
  ON progress_daily (student_id, progress_date DESC);

-- Long-term semester record
CREATE TABLE IF NOT EXISTS progress_semester (
  student_id      TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  subject_key     TEXT NOT NULL,
  year            INT NOT NULL,
  semester        INT NOT NULL CHECK (semester IN (1, 2)),
  sessions_count  INT NOT NULL DEFAULT 0,
  minutes_studied INT NOT NULL DEFAULT 0,
  summary_en      TEXT,
  summary_km      TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, subject_key, year, semester)
);

CREATE INDEX IF NOT EXISTS progress_semester_student_idx
  ON progress_semester (student_id, year DESC, semester DESC);
