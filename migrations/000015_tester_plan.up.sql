-- Test-only plan_tier: tester (Gold++ staff account)
-- Not a sold product. Unlocks all subjects. Cap is 10 lessons / subject / day (enforced in study_service).

ALTER TABLE students DROP CONSTRAINT IF EXISTS students_plan_tier_check;
ALTER TABLE students
  ADD CONSTRAINT students_plan_tier_check
  CHECK (plan_tier IN ('basic', 'silver', 'gold', 'tester'));
