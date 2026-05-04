"""Tests for CheckpointContext and CheckpointData typed schema."""

from __future__ import annotations

import pytest

from app.services.orchestration.persistence import CheckpointContext, CheckpointData


class TestCheckpointContext:
    def test_from_empty_dict(self):
        ctx = CheckpointContext.from_dict({})
        assert ctx.human_guidance == ""
        assert ctx.task_id is None

    def test_from_dict_picks_known_fields(self):
        ctx = CheckpointContext.from_dict(
            {"task_id": 42, "task_description": "do thing", "human_guidance": "use X"}
        )
        assert ctx.task_id == 42
        assert ctx.task_description == "do thing"
        assert ctx.human_guidance == "use X"

    def test_to_dict_roundtrip(self):
        original = {
            "task_id": 1,
            "task_description": "foo",
            "human_guidance": "bar",
            "project_name": None,
            "project_context": None,
            "task_subfolder": None,
            "workspace_path_override": None,
        }
        ctx = CheckpointContext.from_dict(original)
        assert ctx.to_dict() == original

    def test_human_guidance_mutation(self):
        ctx = CheckpointContext.from_dict({"human_guidance": "first"})
        ctx.human_guidance = "first\nsecond"
        assert ctx.human_guidance == "first\nsecond"

    def test_from_none_dict_uses_defaults(self):
        ctx = CheckpointContext.from_dict({})
        assert ctx.human_guidance == ""
        assert ctx.project_name is None


class TestCheckpointData:
    def test_from_empty_dict(self):
        data = CheckpointData.from_dict({})
        assert data.session_id == 0
        assert data.checkpoint_name == "autosave_latest"
        assert data.step_results == []
        assert data.current_step_index is None

    def test_from_full_dict(self):
        raw = {
            "session_id": 5,
            "checkpoint_name": "autosave_latest",
            "context": {"task_id": 10, "human_guidance": "proceed carefully"},
            "orchestration_state": {"status": "executing", "plan": []},
            "current_step_index": 2,
            "step_results": [{"step": 1}],
            "created_at": "2026-05-02T00:00:00Z",
        }
        data = CheckpointData.from_dict(raw)
        assert data.session_id == 5
        assert data.context.task_id == 10
        assert data.context.human_guidance == "proceed carefully"
        assert data.current_step_index == 2
        assert len(data.step_results) == 1
        assert data.orchestration_state["status"] == "executing"

    def test_context_is_typed(self):
        data = CheckpointData.from_dict({"context": {"human_guidance": "hint"}})
        assert isinstance(data.context, CheckpointContext)
        assert data.context.human_guidance == "hint"

    def test_none_orchestration_state_defaults_to_empty_dict(self):
        data = CheckpointData.from_dict({"orchestration_state": None})
        assert data.orchestration_state == {}

    def test_inject_guidance_pattern(self):
        raw = {
            "session_id": 1,
            "checkpoint_name": "autosave_latest",
            "context": {"human_guidance": "first hint"},
            "orchestration_state": {},
            "step_results": [],
        }
        data = CheckpointData.from_dict(raw)
        entry = "[Operator approval #7]: proceed"
        data.context.human_guidance = (
            (data.context.human_guidance + "\n" + entry).strip()
            if data.context.human_guidance
            else entry
        )
        assert (
            data.context.human_guidance == "first hint\n[Operator approval #7]: proceed"
        )
        saved_ctx = data.context.to_dict()
        assert saved_ctx["human_guidance"] == data.context.human_guidance

    def test_inject_guidance_empty_initial(self):
        data = CheckpointData.from_dict({"context": {}})
        entry = "[Operator guidance #1]: do X"
        data.context.human_guidance = (
            (data.context.human_guidance + "\n" + entry).strip()
            if data.context.human_guidance
            else entry
        )
        assert data.context.human_guidance == entry
