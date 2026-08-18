CREATE TABLE IF NOT EXISTS web_pins (
  student_id      TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
  pin_hash        TEXT NOT NULL,
  failed_attempts INT NOT NULL DEFAULT 0,
  locked_until    TIMESTAMPTZ,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
