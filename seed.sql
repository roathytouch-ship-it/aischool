-- AI School — demo seed (parent + child + PIN + one Subject Pass)
-- Run AFTER migrations 000001–000009.
-- PIN plain text for demo: 4821  (hash matches repositories.hash_pin sketch)
--
-- SHA-256 hex of "aischool-pin:4821":
--   python3 -c "import hashlib; print(hashlib.sha256(b'aischool-pin:4821').hexdigest())"

BEGIN;

-- Parent account
INSERT INTO accounts (id, role, telegram_user_id, display_name, language)
VALUES ('acc_demo_parent', 'parent', NULL, 'Demo Parent', 'en')
ON CONFLICT (id) DO NOTHING;

INSERT INTO parents (account_id, max_children)
VALUES ('acc_demo_parent', 7)
ON CONFLICT (account_id) DO NOTHING;

-- Child account (no Telegram — PIN only)
INSERT INTO accounts (id, role, telegram_user_id, display_name, language)
VALUES ('acc_demo_sokha', 'student', NULL, 'Sokha', 'en')
ON CONFLICT (id) DO NOTHING;

INSERT INTO students (
  id, account_id, parent_id, grade, class_name, plan_tier, tier_version, avatar_emoji
) VALUES (
  'stu_demo_sokha',
  'acc_demo_sokha',
  'acc_demo_parent',
  5,
  NULL,
  'basic',
  1,
  '🌟'
)
ON CONFLICT (id) DO NOTHING;

-- Web PIN 4821 — hash = SHA-256("aischool-pin:4821") same as repositories.hash_pin
INSERT INTO web_pins (student_id, pin_hash, failed_attempts, locked_until, updated_at)
VALUES (
  'stu_demo_sokha',
  'ff1eae14000d68d4adf713b6e4be6ec5a39dc8eebabac1d390c2cb20d55cbfdc',
  0,
  NULL,
  now()
)
ON CONFLICT (student_id) DO UPDATE SET
  pin_hash = EXCLUDED.pin_hash,
  failed_attempts = 0,
  locked_until = NULL,
  updated_at = now();

-- Subject Pass: Coding for current calendar month (Phnom Penh “today” approximated by CURRENT_DATE;
-- app still uses Asia/Phnom_Penh for pools)
INSERT INTO subject_passes (
  id, student_id, subject_key, period_start, period_end, status, created_at
) VALUES (
  'pass_demo_coding',
  'stu_demo_sokha',
  'coding',
  date_trunc('month', CURRENT_DATE)::date,
  (date_trunc('month', CURRENT_DATE) + interval '1 month')::date,
  'active',
  now()
)
ON CONFLICT (id) DO NOTHING;

-- Optional: zero usage row for today
INSERT INTO usage_daily (student_id, usage_date, sessions_used)
VALUES (
  'stu_demo_sokha',
  CURRENT_DATE,
  0
)
ON CONFLICT (student_id, usage_date) DO NOTHING;

COMMIT;

-- Quick checks:
-- SELECT * FROM students WHERE id = 'stu_demo_sokha';
-- SELECT * FROM web_pins WHERE student_id = 'stu_demo_sokha';
-- SELECT * FROM subject_passes WHERE student_id = 'stu_demo_sokha';
--
-- API test:
--   POST /v1/auth/pin  { "student_id": "stu_demo_sokha", "pin": "4821" }
--   POST /v1/sessions/start  { "subject_key": "coding", "mode": "lesson" }
