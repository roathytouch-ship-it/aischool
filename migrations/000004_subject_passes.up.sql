CREATE TABLE IF NOT EXISTS subject_passes (
  id           TEXT PRIMARY KEY,
  student_id   TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  subject_key  TEXT NOT NULL,
  period_start DATE NOT NULL,
  period_end   DATE NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('active', 'canceled', 'expired')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS subject_passes_student_idx ON subject_passes(student_id);
