-- At most one active or paused study block per student (product: one block at a time)
CREATE UNIQUE INDEX IF NOT EXISTS study_sessions_one_active_per_student
  ON study_sessions (student_id)
  WHERE status IN ('active', 'paused');
