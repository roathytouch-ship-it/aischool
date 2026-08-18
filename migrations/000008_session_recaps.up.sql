CREATE TABLE IF NOT EXISTS session_recaps (
  session_id     TEXT PRIMARY KEY REFERENCES study_sessions(id) ON DELETE CASCADE,
  summary_en     TEXT,
  summary_km     TEXT,
  practice_json  JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
