"""Phase 13B-E54: Guard-triggered duplicate signature retry — unit tests."""

from __future__ import annotations

import pytest

from app.services.orchestration.diagnostics.signature_guard import (
    SignatureViolation,
    build_duplicate_definition_retry_instruction,
)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _dup_violation(
    path: str = "src/cli/formatting.py",
    qualified_name: str = "format_summary",
    pre_signature: str = "(total, completed)",
    post_signature: str = "(total, completed) | (store)",
) -> SignatureViolation:
    return SignatureViolation(
        path=path,
        qualified_name=qualified_name,
        violation_type="duplicate_definition",
        pre_signature=pre_signature,
        post_signature=post_signature,
    )


def _sig_changed_violation(
    path: str = "src/cli/formatting.py",
    qualified_name: str = "format_summary",
) -> SignatureViolation:
    return SignatureViolation(
        path=path,
        qualified_name=qualified_name,
        violation_type="signature_changed",
        pre_signature="(total, completed)",
        post_signature="(store)",
    )


# ---------------------------------------------------------------------------
# test 1 — empty list returns empty string
# ---------------------------------------------------------------------------


def test_build_retry_instruction_empty_list():
    assert build_duplicate_definition_retry_instruction([]) == ""


# ---------------------------------------------------------------------------
# test 2 — only non-duplicate violations returns empty string
# ---------------------------------------------------------------------------


def test_build_retry_instruction_only_signature_changed_returns_empty():
    result = build_duplicate_definition_retry_instruction([_sig_changed_violation()])
    assert result == ""


# ---------------------------------------------------------------------------
# test 3 — single violation emits correction header
# ---------------------------------------------------------------------------


def test_build_retry_instruction_header_present():
    result = build_duplicate_definition_retry_instruction([_dup_violation()])
    assert "SIGNATURE GUARD CORRECTION" in result


# ---------------------------------------------------------------------------
# test 4 — keep line includes correct signature
# ---------------------------------------------------------------------------


def test_build_retry_instruction_keep_correct_sig():
    result = build_duplicate_definition_retry_instruction([_dup_violation()])
    assert "format_summary(total, completed)" in result
    assert "Keep" in result


# ---------------------------------------------------------------------------
# test 5 — remove line includes wrong signature
# ---------------------------------------------------------------------------


def test_build_retry_instruction_remove_wrong_sig():
    result = build_duplicate_definition_retry_instruction([_dup_violation()])
    assert "format_summary(store)" in result
    assert "Remove" in result


# ---------------------------------------------------------------------------
# test 6 — file path included in instruction
# ---------------------------------------------------------------------------


def test_build_retry_instruction_includes_file_path():
    v = _dup_violation(path="src/medium_cli/formatting.py")
    result = build_duplicate_definition_retry_instruction([v])
    assert "src/medium_cli/formatting.py" in result


# ---------------------------------------------------------------------------
# test 7 — multiple violations both addressed
# ---------------------------------------------------------------------------


def test_build_retry_instruction_multiple_violations():
    v1 = _dup_violation(
        path="a.py",
        qualified_name="fn1",
        pre_signature="(a)",
        post_signature="(a) | (b)",
    )
    v2 = _dup_violation(
        path="b.py",
        qualified_name="fn2",
        pre_signature="(x)",
        post_signature="(x) | (y)",
    )
    result = build_duplicate_definition_retry_instruction([v1, v2])
    assert "fn1" in result
    assert "fn2" in result


# ---------------------------------------------------------------------------
# test 8 — mixed violations: only dup_def processed, other filtered
# ---------------------------------------------------------------------------


def test_build_retry_instruction_mixed_filters_only_dup():
    dup = _dup_violation(qualified_name="good_fn")
    sig_changed = _sig_changed_violation(qualified_name="other_fn")
    result = build_duplicate_definition_retry_instruction([dup, sig_changed])
    assert "good_fn" in result
    assert "other_fn" not in result


# ---------------------------------------------------------------------------
# test 9 — method qualified name preserved
# ---------------------------------------------------------------------------


def test_build_retry_instruction_method_qualified_name():
    v = SignatureViolation(
        path="store.py",
        qualified_name="TaskStore.summary",
        violation_type="duplicate_definition",
        pre_signature="(self)",
        post_signature="(self) | (self, extra)",
    )
    result = build_duplicate_definition_retry_instruction([v])
    assert "TaskStore.summary" in result
    assert "TaskStore.summary(self)" in result
    assert "TaskStore.summary(self, extra)" in result


# ---------------------------------------------------------------------------
# test 10 — multiple wrong sigs all appear in remove lines
# ---------------------------------------------------------------------------


def test_build_retry_instruction_multiple_wrong_sigs():
    v = SignatureViolation(
        path="a.py",
        qualified_name="fn",
        violation_type="duplicate_definition",
        pre_signature="(a)",
        post_signature="(a) | (b) | (c)",
    )
    result = build_duplicate_definition_retry_instruction([v])
    assert "fn(b)" in result
    assert "fn(c)" in result
    assert result.count("Remove") >= 2


# ---------------------------------------------------------------------------
# test 11 — retry eligibility: all dup_def → eligible; any other → not eligible
# ---------------------------------------------------------------------------


def test_retry_eligibility_condition():
    dup1 = _dup_violation(path="a.py", qualified_name="fn1")
    dup2 = _dup_violation(path="b.py", qualified_name="fn2")
    sig_changed = _sig_changed_violation(path="c.py", qualified_name="fn3")

    def _retry_eligible(violations: list[SignatureViolation]) -> bool:
        dup_violations = [
            v for v in violations if v.violation_type == "duplicate_definition"
        ]
        return bool(dup_violations) and all(
            v.violation_type == "duplicate_definition" for v in violations
        )

    assert _retry_eligible([dup1]) is True
    assert _retry_eligible([dup1, dup2]) is True
    assert _retry_eligible([sig_changed]) is False
    assert _retry_eligible([dup1, sig_changed]) is False
    assert _retry_eligible([]) is False
