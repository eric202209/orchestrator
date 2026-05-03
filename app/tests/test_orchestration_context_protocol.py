"""Tests for OrchestrationContext Protocol and DebugPromptInputs dataclass."""
from __future__ import annotations

import pytest

from app.services.orchestration.context_assembly import DebugPromptInputs, OrchestrationContext
from app.services.orchestration.types import OrchestrationRunContext


class TestOrchestrationContextProtocol:
    def test_orchestration_run_context_satisfies_protocol(self):
        """OrchestrationRunContext instance must satisfy OrchestrationContext at runtime."""
        from unittest.mock import MagicMock

        ctx = OrchestrationRunContext(
            db=MagicMock(), session=MagicMock(), project=MagicMock(),
            task=MagicMock(), session_task_link=MagicMock(),
            session_id=1, task_id=1, prompt="x", timeout_seconds=60,
            execution_profile="balanced", validation_profile="standard",
            runs_in_canonical_baseline=False, orchestration_state=MagicMock(),
            runtime_service=MagicMock(), task_service=MagicMock(),
            logger=MagicMock(), emit_live=MagicMock(), error_handler=MagicMock(),
        )
        assert isinstance(ctx, OrchestrationContext)

    def test_protocol_fields(self):
        """Protocol requires the 5 fields context_assembly uses."""
        required = {"orchestration_state", "db", "execution_profile", "prompt", "workflow_profile"}
        proto_members = set(OrchestrationContext.__protocol_attrs__)
        assert required <= proto_members

    def test_workflow_profile_default_on_run_context(self):
        """workflow_profile defaults to 'default' on OrchestrationRunContext."""
        import types as stdlib_types
        from unittest.mock import MagicMock

        ctx = OrchestrationRunContext(
            db=MagicMock(),
            session=MagicMock(),
            project=MagicMock(),
            task=MagicMock(),
            session_task_link=MagicMock(),
            session_id=1,
            task_id=1,
            prompt="do thing",
            timeout_seconds=60,
            execution_profile="balanced",
            validation_profile="standard",
            runs_in_canonical_baseline=False,
            orchestration_state=MagicMock(),
            runtime_service=MagicMock(),
            task_service=MagicMock(),
            logger=MagicMock(),
            emit_live=MagicMock(),
            error_handler=MagicMock(),
        )
        assert ctx.workflow_profile == "default"

    def test_workflow_profile_assignable(self):
        """workflow_profile can be set to a non-default value."""
        from unittest.mock import MagicMock

        ctx = OrchestrationRunContext(
            db=MagicMock(), session=MagicMock(), project=MagicMock(),
            task=MagicMock(), session_task_link=MagicMock(),
            session_id=1, task_id=1, prompt="x", timeout_seconds=60,
            execution_profile="fast", validation_profile="standard",
            runs_in_canonical_baseline=False, orchestration_state=MagicMock(),
            runtime_service=MagicMock(), task_service=MagicMock(),
            logger=MagicMock(), emit_live=MagicMock(), error_handler=MagicMock(),
        )
        ctx.workflow_profile = "greenfield"
        assert ctx.workflow_profile == "greenfield"


class TestDebugPromptInputs:
    def test_required_fields(self):
        d = DebugPromptInputs(
            step_description="run tests",
            error_message="exit code 1",
            command_output="FAILED",
            verification_output="",
            attempt_number=1,
            max_attempts=3,
        )
        assert d.step_description == "run tests"
        assert d.error_message == "exit code 1"
        assert d.attempt_number == 1
        assert d.max_attempts == 3

    def test_defaults(self):
        d = DebugPromptInputs(
            step_description="x", error_message="y", command_output="z",
            verification_output="", attempt_number=1, max_attempts=2,
        )
        assert d.compact is False
        assert d.failure_envelope is None

    def test_compact_flag(self):
        d = DebugPromptInputs(
            step_description="x", error_message="y", command_output="z",
            verification_output="", attempt_number=2, max_attempts=3,
            compact=True,
        )
        assert d.compact is True

    def test_failure_envelope(self):
        sentinel = object()
        d = DebugPromptInputs(
            step_description="x", error_message="y", command_output="z",
            verification_output="", attempt_number=1, max_attempts=2,
            failure_envelope=sentinel,
        )
        assert d.failure_envelope is sentinel
