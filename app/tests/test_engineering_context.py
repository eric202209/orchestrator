"""Phase 27G tests for the single project-log-authorization slice."""

from __future__ import annotations

import json
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.models import PlanningSession, Project
from app.services.engineering_context import EngineeringContextService
from app.services.engineering_context.service import RegistrationError
from app.services.planning.planning_session_service import PlanningSessionService


REMOTE = "git@github.com:henrycode03/orchestrator.git"
SCOPE = (
    "app/api/v1/endpoints/project_logs.py",
    "app/dependencies.py",
    "app/services/auth/authorization.py",
    "app/tests/test_api_security_regressions.py",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    for relative_path in SCOPE:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative_path}\nvalue = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Phase 27G Tests")
    _git(root, "remote", "add", "origin", REMOTE)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    registry = tmp_path / "registrations.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "registrations": [
                    {
                        "repository_identity": REMOTE,
                        "subsystem_id": "project-log-authorization",
                        "subsystem_version": 1,
                        "scope": list(SCOPE),
                        "triggers": [
                            "project-log-authorization",
                            "apply the established per-user Project access predicate",
                            "project-log SSE",
                        ],
                        "status": "live",
                        "provenance": {"authority": "operator", "source": "test"},
                        "created_at": "2026-07-17T00:00:00+00:00",
                        "retired_at": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, registry


def _service(root: Path, registry: Path) -> EngineeringContextService:
    return EngineeringContextService(registry_path=registry)


def _write_structural_fixture(root: Path) -> None:
    (root / SCOPE[0]).write_text(
        """from app.services.auth.authorization import get_project_for_user
from fastapi import APIRouter

router = APIRouter()

@router.get(\"/logs\")
def get_project_logs(project_id):
    return get_project_for_user(project_id)

@router.websocket(\"/logs/ws\")
async def websocket_project_logs():
    return None
""",
        encoding="utf-8",
    )
    (root / SCOPE[1]).write_text(
        """from app.auth import verify_token

def get_current_user():
    return verify_token(\"token\")
""",
        encoding="utf-8",
    )
    (root / SCOPE[2]).write_text(
        """def project_access_filter(db, user):
    return user

def get_project_for_user(db, project_id, user):
    return project_access_filter(db, user)
""",
        encoding="utf-8",
    )
    (root / SCOPE[3]).write_text(
        """from app.services.auth.authorization import project_access_filter

def test_project_logs_authorization():
    return project_access_filter
""",
        encoding="utf-8",
    )


def test_registration_loads_and_identity_includes_version(tmp_path):
    root, registry = _make_repo(tmp_path)
    registration = _service(root, registry).load_registrations()[0]
    assert registration.scope == SCOPE
    assert registration.identity.endswith(":project-log-authorization:1")
    assert registration.is_live


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload["registrations"][0].pop("scope"),
        lambda payload: payload["registrations"][0].update({"scope": ["../escape.py"]}),
        lambda payload: payload["registrations"][0].update({"subsystem_version": 0}),
    ],
)
def test_invalid_registration_is_rejected(tmp_path, change):
    root, registry = _make_repo(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    change(payload)
    registry.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistrationError):
        _service(root, registry).load_registrations()


def test_duplicate_live_identity_and_retired_registration_are_rejected_or_skipped(
    tmp_path,
):
    root, registry = _make_repo(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["registrations"].append(payload["registrations"][0].copy())
    registry.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistrationError, match="duplicate_live_registration"):
        _service(root, registry).load_registrations()

    payload["registrations"].pop()
    payload["registrations"][0]["status"] = "retired"
    payload["registrations"][0]["retired_at"] = "2026-07-17T01:00:00+00:00"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    result = _service(root, registry).select(
        root, task_title="project-log-authorization"
    )
    assert result.context is None
    assert result.reason == "retired_registration"


def test_generation_is_raw_deterministic_and_selection_is_fresh(tmp_path):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    first = service.generate_and_verify(root)
    second = service.generate_and_verify(root)
    assert first["status"] == second["status"] == "published"
    assert first["object_id"] == second["object_id"]
    assert first["commit_fingerprint"] == second["commit_fingerprint"]

    selected = service.select(
        root,
        task_title="Authorization enforcement",
        task_text="project-log-authorization GET summary SSE WebSocket",
    )
    assert selected.supplied
    assert selected.reason == "fresh_published_object"
    assert selected.matched_trigger == "project-log-authorization"
    assert selected.context is not None
    assert set(selected.context.scope) == set(SCOPE)
    assert selected.context.total_source_bytes > 0

    unrelated = service.select(root, task_title="Improve dashboard colors")
    assert unrelated.context is None
    assert unrelated.reason == "no_subsystem_match"


def test_structural_information_is_deterministic_versioned_and_additive(tmp_path):
    root, registry = _make_repo(tmp_path)
    _write_structural_fixture(root)
    service = _service(root, registry)

    first = service.generate_and_verify(root)
    assert first["structural_information_status"] == "published"
    structural_paths = list(
        (root / ".agent/engineering-context").glob("*.structural.json")
    )
    assert len(structural_paths) == 1
    first_sidecar = structural_paths[0].read_bytes()
    second = service.generate_and_verify(root)
    assert second["structural_information_status"] == "published"
    assert structural_paths[0].read_bytes() == first_sidecar

    selected = service.select(root, task_title="project-log-authorization")
    assert selected.context is not None
    assert selected.structural_information is not None
    assert selected.diagnostics["structural_information_reason"] == (
        "fresh_structural_information"
    )
    facts = selected.structural_information.facts
    assert {
        (item["entry_point"]["symbol"], item["method"], item["path"])
        for item in facts["routing_relationships"]
    } == {
        ("get_project_logs", "GET", "/logs"),
        ("websocket_project_logs", "WEBSOCKET", "/logs/ws"),
    }
    assert any(
        item["from_file"] == SCOPE[3] and item["to_file"] == SCOPE[2]
        for item in facts["dependency_relationships"]
    )
    assert any(
        item["kind"] == "call" and item["reference"] == "project_access_filter"
        for item in facts["authentication_boundary_references"]
    )
    assert facts["test_ownership"] == [
        {
            "test_file": SCOPE[3],
            "test_symbol": "test_project_logs_authorization",
            "line": 3,
        }
    ]

    prompt = service.render_prompt_block(selected)
    assert prompt is not None
    assert prompt.index("RAW SOURCE CONTENT BEGIN") < prompt.index(
        "STRUCTURAL INFORMATION JSON BEGIN"
    )
    structural_json = prompt.split("STRUCTURAL INFORMATION JSON BEGIN\n", 1)[1].split(
        "\nSTRUCTURAL INFORMATION JSON END", 1
    )[0]
    rendered = json.loads(structural_json)
    assert rendered["schema_version"] == 1
    assert rendered["algorithm_version"] == 1
    assert "summary" not in rendered


def test_structural_staleness_does_not_discard_fresh_raw_context(tmp_path):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    result = service.generate_and_verify(root)
    assert result["structural_information_status"] == "published"
    sidecar_path = next((root / ".agent/engineering-context").glob("*.structural.json"))
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["source_fingerprint"] = "sha256:" + "0" * 64
    unsigned = dict(payload)
    unsigned.pop("content_hash")
    payload["content_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    selected = service.select(root, task_title="project-log-authorization")
    assert selected.context is not None
    assert selected.reason == "fresh_published_object"
    assert selected.structural_information is None
    assert selected.diagnostics["structural_information_reason"] == (
        "stale_structural_information"
    )


def test_old_raw_context_without_structural_sidecar_remains_selectable(tmp_path):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    result = service.generate_and_verify(root)
    sidecar = next((root / ".agent/engineering-context").glob("*.structural.json"))
    sidecar.unlink()

    selected = service.select(root, task_title="project-log-authorization")
    assert result["status"] == "published"
    assert selected.context is not None
    assert selected.structural_information is None
    assert selected.diagnostics["structural_information_reason"] == (
        "missing_structural_information"
    )


def test_scoped_change_makes_object_stale_and_restoring_bytes_is_fresh(tmp_path):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    assert service.generate_and_verify(root)["status"] == "published"
    target = root / SCOPE[0]
    original = target.read_bytes()
    target.write_bytes(original + b"\nchanged = True\n")

    stale = service.select(root, task_title="project-log-authorization")
    assert stale.context is None
    assert stale.reason == "stale_object"

    target.write_bytes(original)
    fresh = service.select(root, task_title="project-log-authorization")
    assert fresh.supplied
    assert fresh.reason == "fresh_published_object"


def test_missing_file_and_failed_replacement_leave_no_selectable_partial_object(
    tmp_path,
):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    original = service.generate_and_verify(root)
    assert original["status"] == "published"
    missing = root / SCOPE[1]
    saved = missing.read_bytes()
    missing.unlink()

    selection = service.select(root, task_title="project-log-authorization")
    assert selection.context is None
    assert selection.reason == "missing_scoped_file"

    replacement = service.generate_and_verify(root)
    assert replacement["status"] == "failed"
    assert "missing_scoped_file" in replacement["reason"]
    published = list((root / ".agent/engineering-context").glob("*.published.json"))
    assert len(published) == 1
    missing.write_bytes(saved)


def test_verification_rejects_rehash_mismatch_and_previous_object_survives(
    tmp_path, monkeypatch
):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    original = service.generate_and_verify(root)
    target = root / SCOPE[2]
    original_bytes = target.read_bytes()
    snapshot = service._snapshot
    calls = 0

    def mutate_after_generation_snapshot(repository_root, registration):
        nonlocal calls
        result = snapshot(repository_root, registration)
        calls += 1
        if calls == 1:
            target.write_bytes(original_bytes + b"changed = True\n")
        return result

    monkeypatch.setattr(service, "_snapshot", mutate_after_generation_snapshot)
    failed = service.generate_and_verify(root)
    assert failed["status"] == "failed"
    assert "mismatch" in failed["reason"]
    target.write_bytes(original_bytes)
    # A failed replacement cannot overwrite the previous immutable object.
    published = list((root / ".agent/engineering-context").glob("*.published.json"))
    assert len(published) == 1
    assert any(
        json.loads(path.read_text(encoding="utf-8"))["object_id"]
        == original["object_id"]
        for path in published
    )


def test_ambiguous_match_wrong_repository_and_wrong_version_fall_back(tmp_path):
    root, registry = _make_repo(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    second = payload["registrations"][0].copy()
    second["subsystem_id"] = "other-live-subsystem"
    second["triggers"] = ["project-log-authorization"]
    payload["registrations"].append(second)
    registry.write_text(json.dumps(payload), encoding="utf-8")
    service = _service(root, registry)
    ambiguous = service.select(root, task_title="project-log-authorization")
    assert ambiguous.context is None
    assert ambiguous.reason == "ambiguous_registration_match"

    payload["registrations"].pop()
    registry.write_text(json.dumps(payload), encoding="utf-8")
    wrong_version = service.select(
        root, task_title="project-log-authorization", subsystem_version=2
    )
    assert wrong_version.context is None
    assert wrong_version.reason == "subsystem_version_mismatch"

    other_root, _ = _make_repo(tmp_path / "other")
    _git(other_root, "remote", "set-url", "origin", "git@github.com:other/repo.git")
    wrong_repo = service.select(other_root, task_title="project-log-authorization")
    assert wrong_repo.context is None
    assert wrong_repo.reason == "repository_identity_mismatch"


def test_concurrent_generation_publishes_one_complete_object(tmp_path):
    root, registry = _make_repo(tmp_path)
    service = _service(root, registry)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: service.generate_and_verify(root), range(4))
        )
    assert all(result["status"] == "published" for result in results)
    assert len({result["object_id"] for result in results}) == 1
    assert (
        len(list((root / ".agent/engineering-context").glob("*.published.json"))) == 1
    )


def test_planning_prompt_adds_fresh_context_without_generation_or_contract_change(
    tmp_path, db_session, monkeypatch
):
    root, registry = _make_repo(tmp_path)
    context_service = _service(root, registry)
    assert context_service.generate_and_verify(root)["status"] == "published"
    project = Project(name="fixture", workspace_path=str(root))
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="project-log-authorization",
        prompt="Apply authorization enforcement to project-log GET, summary, SSE, and WebSocket transports.",
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.flush()
    service = PlanningSessionService(
        db_session, engineering_context_service=context_service
    )
    generation_called = False

    def fail_generation(*args, **kwargs):
        nonlocal generation_called
        generation_called = True
        raise AssertionError("Planning must not invoke Generation")

    monkeypatch.setattr(context_service, "generate_and_verify", fail_generation)
    monkeypatch.setattr(
        service,
        "_render_adapted_prompt",
        lambda **kwargs: json.dumps(kwargs, sort_keys=True),
    )
    prompt = service._build_synthesis_prompt(session, project)
    assert not generation_called
    assert "project-log-authorization" in prompt
    assert "app/api/v1/endpoints/project_logs.py" in prompt
    assert "RAW SOURCE CONTENT BEGIN" in prompt
    assert "get_project_for_user" not in prompt  # fixture source is intentionally small
    assert "Return JSON only with exactly these keys" in prompt


def test_planning_fallback_keeps_baseline_prompt_shape_and_does_not_mutate_state(
    tmp_path, db_session, monkeypatch
):
    root, registry = _make_repo(tmp_path)
    context_service = _service(root, registry)
    project = Project(name="fixture", workspace_path=str(root))
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="unrelated task",
        prompt="Improve dashboard colors without changing authorization.",
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.flush()
    service = PlanningSessionService(
        db_session, engineering_context_service=context_service
    )
    monkeypatch.setattr(
        service,
        "_render_adapted_prompt",
        lambda **kwargs: json.dumps(kwargs, sort_keys=True),
    )
    before = (
        set((root / ".agent").glob("**/*")) if (root / ".agent").exists() else set()
    )
    prompt = service._build_synthesis_prompt(session, project)
    after = set((root / ".agent").glob("**/*")) if (root / ".agent").exists() else set()
    assert "Engineering Context" not in prompt
    assert "Return JSON only with exactly these keys" in prompt
    assert before == after
