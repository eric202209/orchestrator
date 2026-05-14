"""Diagnostics capsules and failure classifiers for orchestration."""

from .debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt,
    build_debug_feedback_envelope,
    classify_debug_failure,
)
from .diff_capsule import DiffCapsule, build_diff_capsule
from .evidence_capsule import WorkspaceEvidenceCapsule, collect_workspace_evidence

__all__ = [
    "DebugFeedbackEnvelope",
    "DiffCapsule",
    "WorkspaceEvidenceCapsule",
    "build_bounded_debug_repair_prompt",
    "build_debug_feedback_envelope",
    "build_diff_capsule",
    "classify_debug_failure",
    "collect_workspace_evidence",
]
