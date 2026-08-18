-- Phase A: identity
CREATE TABLE IF NOT EXISTS accounts (
  id                     TEXT PRIMARY KEY,
  role                   TEXT NOT NULL CHECK (role IN ('student', 'parent')),
  telegram_user_id       BIGINT UNIQUE,
  display_name           TEXT,
  language               TEXT NOT NULL DEFAULT 'en',
  referred_by_account_id TEXT REFERENCES accounts(id),
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS parents (
  account_id   TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  max_children INT NOT NULL DEFAULT 7
);

CREATE TABLE IF NOT EXISTS students (
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

CREATE INDEX IF NOT EXISTS students_parent_id_idx ON students(parent_id);
