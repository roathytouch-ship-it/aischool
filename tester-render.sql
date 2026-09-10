-- AI School — run this ONCE on Render Postgres (not a zip).
-- Paste into Render → aischool-db → Connect / Query (or psql).
-- Then wait for the web service deploy that includes study_service.py.

ALTER TABLE students DROP CONSTRAINT IF EXISTS students_plan_tier_check;
ALTER TABLE students
  ADD CONSTRAINT students_plan_tier_check
  CHECK (plan_tier IN ('basic', 'silver', 'gold', 'tester'));

INSERT INTO accounts (id, role, telegram_user_id, display_name, language)
VALUES ('acc_demo_tester', 'student', NULL, 'Staff Tester', 'en')
ON CONFLICT (id) DO NOTHING;

INSERT INTO students (
  id, account_id, parent_id, grade, class_name, plan_tier, tier_version, avatar_emoji
) VALUES (
  'stu_demo_tester',
  'acc_demo_tester',
  NULL,
  10,
  NULL,
  'tester',
  1,
  '🛠️'
)
ON CONFLICT (id) DO UPDATE SET plan_tier = 'tester';

-- Web PIN 9010
INSERT INTO web_pins (student_id, pin_hash, failed_attempts, locked_until, updated_at)
VALUES (
  'stu_demo_tester',
  '29f00a4c523e9fd091ef34352e2e2a5c5755c462ae3b6a5bbde29d97a02ac891',
  0,
  NULL,
  now()
)
ON CONFLICT (student_id) DO UPDATE SET
  pin_hash = EXCLUDED.pin_hash,
  failed_attempts = 0,
  locked_until = NULL,
  updated_at = now();

-- Check
SELECT id, plan_tier, grade FROM students WHERE id = 'stu_demo_tester';
