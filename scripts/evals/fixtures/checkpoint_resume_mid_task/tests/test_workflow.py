from pathlib import Path

from resume_task.workflow import build_status_report, load_step_one, load_step_two


ROOT = Path(__file__).resolve().parents[1]


def test_existing_step_one_artifact_is_preserved():
    assert (ROOT / "docs" / "step-one.txt").read_text(encoding="utf-8").strip() == (
        "step-one: complete"
    )
    assert load_step_one() == "step-one: complete"


def test_step_two_is_completed_after_resume():
    assert load_step_two() == "step-two: complete"


def test_status_report_includes_both_steps_in_order():
    assert build_status_report().splitlines() == [
        "step-one: complete",
        "step-two: complete",
    ]
