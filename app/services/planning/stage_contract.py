"""Narrow, planning-owned contracts consumed by the stage engine.

This module contains stage identity, policy, and result values plus the
definition protocol used by planning stages.  Lifecycle persistence,
fencing, retry, recovery, and completion remain implemented by the existing
orchestration stage engine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class StageContext(Protocol):
    """Domain-facing view of the execution context supplied by the engine."""

    configuration: Mapping[str, Any]
    input_manifest: Any
    grounding_result: Any
    planning_brief: Any
    predecessor_checkpoints: Mapping[str, Any]
    session: Any


@dataclass(frozen=True)
class StageExecutionPolicy:
    """Execution controls that do not contain provider-specific behavior."""

    retryable: bool = True
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True)
class StageValidationPolicy:
    """Validation persistence policy shared by all stage types."""

    persist_rejected_output: bool = True


@dataclass(frozen=True)
class StageAcceptancePolicy:
    """Acceptance controls shared by all stage types."""

    require_explicit_acceptance: bool = True


@dataclass(frozen=True)
class StageValidation:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class StageAcceptance:
    accepted: bool
    reason: str | None = None


StageExecute = Callable[[StageContext], Any]
StageValidate = Callable[[Any, StageContext], Any]
StageAccept = Callable[[Any, StageContext], Any]


class StageDefinition:
    """Definition and behavior contract for one reusable stage."""

    def __init__(
        self,
        identifier: str,
        *,
        version: int = 1,
        prerequisites: Iterable[str] = (),
        execution_policy: StageExecutionPolicy | None = None,
        validation_policy: StageValidationPolicy | None = None,
        acceptance_policy: StageAcceptancePolicy | None = None,
        execute: StageExecute | None = None,
        validate: StageValidate | None = None,
        accept: StageAccept | None = None,
    ) -> None:
        normalized_identifier = str(identifier or "").strip()
        if not normalized_identifier:
            raise ValueError("stage identifier is required")
        if version < 1:
            raise ValueError("stage version must be positive")
        self.identifier = normalized_identifier
        self.version = int(version)
        self.prerequisites = tuple(
            dict.fromkeys(
                str(prerequisite).strip()
                for prerequisite in prerequisites
                if str(prerequisite).strip()
            )
        )
        self.execution_policy = execution_policy or StageExecutionPolicy()
        self.validation_policy = validation_policy or StageValidationPolicy()
        self.acceptance_policy = acceptance_policy or StageAcceptancePolicy()
        self._execute = execute
        self._validate = validate
        self._accept = accept

    def execute(self, context: StageContext) -> Any:
        if self._execute is None:
            raise NotImplementedError(
                f"stage {self.identifier!r} must implement execute()"
            )
        return self._execute(context)

    def validate(self, output: Any, context: StageContext) -> Any:
        if self._validate is None:
            return StageValidation(valid=True)
        return self._validate(output, context)

    def accept(self, output: Any, context: StageContext) -> Any:
        if self._accept is None:
            return StageAcceptance(accepted=True)
        return self._accept(output, context)


__all__ = [
    "StageAcceptance",
    "StageAcceptancePolicy",
    "StageAccept",
    "StageContext",
    "StageDefinition",
    "StageExecutionPolicy",
    "StageExecute",
    "StageValidation",
    "StageValidationPolicy",
    "StageValidate",
]
