-- Revert tester tier. Move any tester rows to gold first so the old CHECK can apply.
UPDATE students SET plan_tier = 'gold' WHERE plan_tier = 'tester';

ALTER TABLE students DROP CONSTRAINT IF EXISTS students_plan_tier_check;
ALTER TABLE students
  ADD CONSTRAINT students_plan_tier_check
  CHECK (plan_tier IN ('basic', 'silver', 'gold'));
