CREATE TABLE IF NOT EXISTS session_messages (
  id                TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
  role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content           TEXT NOT NULL,
  token_prompt      INT,
  token_completion  INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS session_messages_session_id_idx ON session_messages(session_id);
