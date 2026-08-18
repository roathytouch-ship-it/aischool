"""Study session flow test (in-memory service)."""

from __future__ import annotations

from study_service import MemoryStudyStore, StudyError, StudyService


def run() -> None:
    store = MemoryStudyStore()
    svc = StudyService(store)
    student_id = "stu_test"
    plan = "gold"

    # Start lesson
    s = svc.start(
        student_id=student_id,
        plan_tier=plan,
        subject_key="general_math",
        mode="lesson",
    )
    assert s.status == "active"
    assert s.duration_limit_sec == 60 * 60
    print("start lesson OK", s.id)

    # Second start blocked
    try:
        svc.start(student_id=student_id, plan_tier=plan, subject_key="coding", mode="lesson")
        raise SystemExit("should block second session")
    except StudyError as e:
        assert e.code == "session_active"
    print("one-active-block OK")

    # Message
    u, a = svc.add_user_message(s.id, student_id, "Help me with fractions")
    assert u.role == "user" and a.role == "assistant"
    print("message OK")

    # End
    out = svc.end(s.id, student_id)
    assert out["session"]["status"] == "ended"
    assert "summary_en" in out["recap"]
    print("end+recap OK")

    # Basic locked subject
    store2 = MemoryStudyStore()
    svc2 = StudyService(store2)
    try:
        svc2.start(
            student_id="stu_basic",
            plan_tier="basic",
            subject_key="coding",
            mode="lesson",
        )
        raise SystemExit("coding should be locked on basic")
    except StudyError as e:
        assert e.code == "subject_locked"
    store2.grant_pass("stu_basic", "coding")
    s2 = svc2.start(
        student_id="stu_basic",
        plan_tier="basic",
        subject_key="coding",
        mode="lesson",
    )
    assert s2.duration_limit_sec == 25 * 60
    print("basic pass unlock OK")
    print("=== STUDY PASS ===")


if __name__ == "__main__":
    run()
