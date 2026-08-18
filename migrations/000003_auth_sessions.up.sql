CREATE TABLE IF NOT EXISTS auth_sessions (
  id          TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  family_id   TEXT NOT NULL,
  token_hash  TEXT NOT NULL UNIQUE,
  auth_method TEXT NOT NULL CHECK (auth_method IN ('telegram', 'pin')),
  role        TEXT NOT NULL,
  student_id  TEXT,
  parent_id   TEXT,
  expires_at  TIMESTAMPTZ NOT NULL,
  revoked_at  TIMESTAMPTZ,
  replaced_by TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS auth_sessions_account_id_idx ON auth_sessions(account_id);
CREATE INDEX IF NOT EXISTS auth_sessions_family_id_idx ON auth_sessions(family_id);
