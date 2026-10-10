"""Phase 37 Pre-B F2A: strict-controls guard and retained-evidence decoupling.

Provider-free. Every OpenClaw config and state directory is a temp copy under
an isolated HOME; the operator's persistent installation is never touched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services.agents.openclaw_service import (
    OpenClawProviderControlError,
    OpenClawSessionService,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    EPHEMERAL_AGENT_ID,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.orchestration.planning.repair_prompts import (
    effective_repair_prompt_max_chars,
)
from app.tests import test_phase32n1_repair_budget_authority as n1

# Committed bytes of the Phase 32N-1 retained shape fixture. It must never be
# rewritten to follow current source (Phase 37 Pre-B F2A).
RETAINED_SHAPES_SHA256 = (
    "a22b1b084067ac722a1d1cbd66e35c2e3151ec87bb40f1a380eb2cfae4081aed"
)
HISTORICAL_SOURCE_COMMIT = "b260a7499a80a5865e81e02a556a56e9cc08d3d1"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _persistent_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    state = home / ".openclaw"
    (state / "agents" / "main" / "agent").mkdir(parents=True)
    (state / "agents" / "main" / "sessions").mkdir(parents=True)
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "config-audit.jsonl").write_text("", encoding="utf-8")
    (state / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {"models": [{"id": "qwen-local", "reasoning": True}]}
                    }
                },
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "default": True,
                            "workspace": str(state / "workspace"),
                            "agentDir": str(state / "agents" / "main" / "agent"),
                        },
                        {
                            "id": "planning",
                            "workspace": str(state / "planning-ws"),
                            "model": "openai/qwen-local",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "OPENCLAW_CONFIG_PATH",
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_RUNNER_AGENT_ID",
        "ORCHESTRATOR_OPENCLAW_PROTOCOL_V2_PLANNING_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)
    return state


def _snapshot(state: Path) -> dict:
    config = state / "openclaw.json"
    return {
        "sha256": _sha256_bytes(config.read_bytes()),
        "agents_list": json.loads(config.read_text(encoding="utf-8"))["agents"]["list"],
        "agent_tree": sorted(
            str(p.relative_to(state / "agents")) for p in (state / "agents").rglob("*")
        ),
        "config_audit": (state / "logs" / "config-audit.jsonl").read_bytes(),
    }


def _service() -> OpenClawSessionService:
    service = object.__new__(OpenClawSessionService)
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._workspace_binding = None
    service._strict_planning_config_dir = None
    service._openclaw_config_path_override = None
    service.execution_cwd_override = None
    service._last_selected_openclaw_agent_id = None
    return service


def _runtime_context(tmp_path: Path) -> RuntimeExecutorContext:
    project = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "runtime"
    runtime_workspace = runtime_root / "tasks" / "1" / "1"
    project.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    return RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project,
        project_id=1,
        task_execution_id=1,
        runtime_root=runtime_root,
        sandbox=object(),
    )


# A: no binding ---------------------------------------------------------------


def test_a_no_binding_fails_closed_without_persistent_writes(tmp_path, monkeypatch):
    state = _persistent_home(tmp_path, monkeypatch)
    persistent = state / "openclaw.json"
    before = _snapshot(state)
    service = _service()

    with pytest.raises(OpenClawProviderControlError, match="ephemeral"):
        service._configure_strict_provider_controls("planning")

    # An env path or an override that is not the active binding's copy is
    # refused as well.
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(persistent))
    with pytest.raises(OpenClawProviderControlError, match="ephemeral"):
        service._configure_strict_provider_controls("planning")
    service._openclaw_config_path_override = persistent
    with pytest.raises(OpenClawProviderControlError, match="ephemeral"):
        service._configure_strict_provider_controls("planning")

    assert _snapshot(state) == before


# B: runtime binding ----------------------------------------------------------


def test_b_runtime_binding_writes_only_the_temp_config(tmp_path, monkeypatch):
    state = _persistent_home(tmp_path, monkeypatch)
    before = _snapshot(state)
    service = _service()

    service.bind_runtime_workspace(_runtime_context(tmp_path))
    binding = service._workspace_binding
    try:
        assert binding.agent_id == EPHEMERAL_AGENT_ID
        assert state not in binding.config_path.parents
        controls = service._configure_strict_provider_controls(binding.agent_id)
        assert controls["temperature"] == 0
        bound = json.loads(binding.config_path.read_text(encoding="utf-8"))
        selected = next(
            a for a in bound["agents"]["list"] if a["id"] == EPHEMERAL_AGENT_ID
        )
        assert selected["params"]["temperature"] == 0
        model = bound["models"]["providers"]["openai"]["models"][0]
        assert model["compat"]["thinkingFormat"] == "qwen-chat-template"
        assert _snapshot(state) == before
        # A binding exists, but the override no longer names its copy.
        service._openclaw_config_path_override = state / "openclaw.json"
        with pytest.raises(OpenClawProviderControlError, match="ephemeral"):
            service._configure_strict_provider_controls(binding.agent_id)
        service._openclaw_config_path_override = binding.config_path
    finally:
        service.release_runtime_workspace_binding()

    assert not binding.config_path.exists()
    assert _snapshot(state) == before


# C: dedicated planning binding -----------------------------------------------


def test_c_dedicated_planning_binding_writes_only_its_temp_config(
    tmp_path, monkeypatch
):
    state = _persistent_home(tmp_path, monkeypatch)
    before = _snapshot(state)
    service = _service()
    runtime_workspace = _runtime_context(tmp_path).runtime_workspace

    service._bind_dedicated_strict_planning_agent(runtime_workspace, "planning")
    config_dir = Path(service._strict_planning_config_dir.name)
    try:
        assert service._last_selected_openclaw_agent_id == "planning"
        assert service._openclaw_config_path_override == config_dir / "openclaw.json"
        service._configure_strict_provider_controls("planning")
        bound = json.loads((config_dir / "openclaw.json").read_text(encoding="utf-8"))
        selected = next(a for a in bound["agents"]["list"] if a["id"] == "planning")
        assert selected["workspace"] == str(runtime_workspace)
        assert selected["params"]["temperature"] == 0
        assert _snapshot(state) == before
    finally:
        service._release_dedicated_strict_planning_agent()

    assert not config_dir.exists()
    assert service._openclaw_config_path_override is None
    assert _snapshot(state) == before


# D: historical fixture --------------------------------------------------------


def test_d_historical_fixture_is_unchanged_and_matches_retained_excerpts():
    raw = n1.RETAINED_SHAPES_FIXTURE.read_bytes()
    assert _sha256_bytes(raw) == RETAINED_SHAPES_SHA256
    shapes = json.loads(raw)
    excerpts = json.loads(n1.RETAINED_EXCERPTS_FIXTURE.read_text(encoding="utf-8"))[
        "excerpts"
    ]
    materialized = [
        entry
        for shape in shapes.values()
        for entry in shape["source_materialization"]["files"]
        if entry["status"] == "existing_file_with_materialized_source"
    ]
    assert len(materialized) == 7
    assert {e["content_hash"] for e in materialized} == set(excerpts)
    for entry in materialized:
        retained = excerpts[entry["content_hash"]]
        assert retained["relative_path"] == entry["relative_path"]
        assert len(retained["text"]) == entry["included_prompt_length"]
        # The openclaw_service.py offset/size fields were re-based to later
        # source revisions (last at f991620); the runtime-authentic values
        # are the content hash and length above, and the excerpt's own
        # original_spans/full_source_bytes from the first fixture revision.
        if entry["relative_path"] != "app/services/agents/openclaw_service.py":
            assert retained["full_source_bytes"] == entry["full_source_bytes"]
    for content_hash, retained in excerpts.items():
        assert n1._sha256(retained["text"]) == content_hash
        assert retained["source_commit"] == HISTORICAL_SOURCE_COMMIT


def _git_blob(blob: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(n1.REPOSITORY_ROOT), "cat-file", "blob", blob],
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def test_d_retained_excerpts_rederive_from_the_historical_source_commit():
    excerpts = json.loads(n1.RETAINED_EXCERPTS_FIXTURE.read_text(encoding="utf-8"))[
        "excerpts"
    ]
    for content_hash, retained in excerpts.items():
        source = _git_blob(retained["source_blob"])
        if source is None:
            pytest.skip("historical source blob unavailable (shallow clone)")
        assert len(source) == retained["full_source_bytes"]
        spans = [tuple(span) for span in retained["original_spans"]]
        assert n1._compose_excerpt(source, spans) == retained["text"]


# E: source insertion resilience ----------------------------------------------


def test_e_reconstruction_does_not_read_current_source(tmp_path, monkeypatch):
    # Point the module's source root at a copy whose openclaw_service.py has an
    # unrelated insertion at the top; the reconstruction must not care.
    root = tmp_path / "repo"
    target = root / "app/services/agents/openclaw_service.py"
    target.parent.mkdir(parents=True)
    current = (
        n1.REPOSITORY_ROOT / "app/services/agents/openclaw_service.py"
    ).read_bytes()
    target.write_bytes(b"# unrelated insertion\n" * 50 + current)
    monkeypatch.setattr(n1, "REPOSITORY_ROOT", root)

    shapes = json.loads(n1.RETAINED_SHAPES_FIXTURE.read_text(encoding="utf-8"))
    for name in ("attempt7", "attempt9"):
        materialization = n1.build_retained_materialization(
            shapes[name]["source_materialization"]
        )
        hashes = {f.content_hash for f in materialization.files if f.content}
        assert hashes


# F: behavioural regression detection -----------------------------------------


def test_f_tampered_excerpt_is_rejected(tmp_path, monkeypatch):
    excerpts = json.loads(n1.RETAINED_EXCERPTS_FIXTURE.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(excerpts)
    first = next(iter(tampered["excerpts"].values()))
    first["text"] = first["text"].replace("def ", "def  ", 1)
    path = tmp_path / "excerpts.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    monkeypatch.setattr(n1, "RETAINED_EXCERPTS_FIXTURE", path)

    shapes = json.loads(n1.RETAINED_SHAPES_FIXTURE.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError, match="not byte-identical"):
        for name in ("attempt7", "attempt9"):
            n1.build_retained_materialization(shapes[name]["source_materialization"])


def test_f_retained_envelopes_still_detect_a_budget_regression(monkeypatch):
    # Reverting the Phase 32N-1 budget authority to the old fixed floor must
    # make the retained envelopes overflow again, so the 32N-1 fit
    # assertions keep their detection power.
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    for name in ("attempt7", "attempt9"):
        prompt = n1._envelope(n1._retained_shape(name))
        assert len(prompt) > effective_repair_prompt_max_chars()
