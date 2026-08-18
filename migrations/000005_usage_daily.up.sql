CREATE TABLE IF NOT EXISTS usage_daily (
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
