"""Provider-free D-37-13 credential-minimization regressions.

All configuration and credential-like values in this module are synthetic.
The tests never invoke OpenClaw, a provider, a gateway, or a production
database.  The shared test fixture keeps managed bindings below ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from app.services.orchestration.execution import binding_reconciliation as recon
from app.services.orchestration.execution import executor_workspace_binding as ewb
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext


LOCAL_TOKEN = "ollama-local"
EXTERNAL_TOKEN = "d37-synthetic-external-oauth-token-not-real"


def _persistent_config(tmp_path: Path) -> tuple[Path, bytes]:
    state = tmp_path / "persistent-openclaw"
    main_agent = state / "agents" / "main" / "agent"
    main_agent.mkdir(parents=True)
    (main_agent / "auth-profiles.json").write_text(
        json.dumps(
            {
                "openai-codex": {"accessToken": EXTERNAL_TOKEN},
                "anthropic": {"refreshToken": "d37-anthropic-refresh-not-real"},
            }
        ),
        encoding="utf-8",
    )
    config = state / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": LOCAL_TOKEN,
                            "models": [
                                {
                                    "id": "qwen-local",
                                    "name": "synthetic local qwen",
                                    "reasoning": True,
                                },
                                {"id": "unused-local"},
                            ],
                        },
                        "openai-codex": {
                            "baseUrl": "https://synthetic-external.invalid/v1",
                            "api": "openai-completions",
                            "apiKey": EXTERNAL_TOKEN,
                            "models": [{"id": "external-model"}],
                        },
                    }
                },
                "agents": {
                    "defaults": {
                        "workspace": str(state / "workspace"),
                        "model": {
                            "primary": "openai-codex/external-model",
                            "fallbacks": ["openai/qwen-local"],
                        },
                    },
                    "list": [
                        {
                            "id": "main",
                            "workspace": str(state / "workspace"),
                            "agentDir": str(main_agent),
                        }
                    ],
                },
                "channels": {"synthetic": {"token": EXTERNAL_TOKEN}},
                "plugins": {"synthetic": {"secret": EXTERNAL_TOKEN}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    return config, config.read_bytes()


def _context(tmp_path: Path, name: str = "probe") -> RuntimeExecutorContext:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    runtime_root = tmp_path / "runtime-root"
    runtime = runtime_root / "tasks" / name
    runtime.mkdir(parents=True, exist_ok=True)
    return RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime,
        project_workspace=project,
        project_id=169,
        task_execution_id=3713,
        runtime_root=runtime_root,
        sandbox=object(),
    )


def _bind(tmp_path: Path, config: Path, name: str = "probe"):
    return ewb.bind_openclaw_workspace(
        _context(tmp_path, name),
        real_config_path=config,
        model_ref=ewb.SUPPORTED_LOCAL_MODEL_REF,
    )


def _bound_config(binding) -> dict:
    return json.loads(binding.config_path.read_text(encoding="utf-8"))


def test_local_projection_is_minimal_and_preserves_required_route(tmp_path):
    config, before = _persistent_config(tmp_path)
    host_auth = config.parent / "agents" / "main" / "agent" / "auth-profiles.json"
    host_auth_before = host_auth.read_bytes()
    binding = _bind(tmp_path, config)
    try:
        projected = _bound_config(binding)
        provider = projected["models"]["providers"]["openai"]
        agent = projected["agents"]["list"][0]
        assert set(projected) == {"models", "agents", "session"}
        assert set(projected["models"]["providers"]) == {"openai"}
        assert provider["baseUrl"] == "http://synthetic-ai-gateway:8000/v1"
        assert provider["api"] == "openai-completions"
        assert provider["apiKey"] == LOCAL_TOKEN
        assert [model["id"] for model in provider["models"]] == ["qwen-local"]
        assert agent["id"] == ewb.EPHEMERAL_AGENT_ID
        assert agent["model"] == {
            "primary": ewb.SUPPORTED_LOCAL_MODEL_REF,
            "fallbacks": [],
        }
        assert Path(agent["workspace"]) == _context(tmp_path).runtime_workspace
        auth_path = binding._tmp_dir / "agent" / "auth-profiles.json"
        assert stat.S_IMODE(os.lstat(auth_path).st_mode) == 0o600
        assert json.loads(auth_path.read_text(encoding="utf-8")) == {
            "version": 1,
            "profiles": {
                "openai:default": {
                    "type": "api_key",
                    "provider": "openai",
                    "key": LOCAL_TOKEN,
                }
            },
        }
        assert EXTERNAL_TOKEN not in auth_path.read_text(encoding="utf-8")
        assert host_auth.read_bytes() == host_auth_before
        assert not list(
            (_context(tmp_path).project_workspace).rglob("auth-profiles.json")
        )
        assert config.read_bytes() == before
    finally:
        binding.release()


def test_external_profiles_keys_and_environment_are_excluded(tmp_path):
    config, _ = _persistent_config(tmp_path)
    binding = _bind(tmp_path, config)
    try:
        payload = binding.config_path.read_text(encoding="utf-8")
        assert EXTERNAL_TOKEN not in payload
        assert "openai-codex" not in payload
        assert "channels" not in payload
        assert "plugins" not in payload
        auth_profile = json.loads(
            (binding._tmp_dir / "agent" / "auth-profiles.json").read_text()
        )
        assert set(auth_profile["profiles"]) == {"openai:default"}
        assert auth_profile["profiles"]["openai:default"]["key"] == LOCAL_TOKEN
        assert EXTERNAL_TOKEN not in json.dumps(auth_profile)
        env = ewb.restricted_openclaw_environment(
            {
                "PATH": "/synthetic/bin",
                "OPENAI_API_KEY": EXTERNAL_TOKEN,
                "OPENCLAW_GATEWAY_TOKEN": EXTERNAL_TOKEN,
                "GITHUB_TOKEN": EXTERNAL_TOKEN,
                "LANG": "C.UTF-8",
            }
        )
        assert env == {"PATH": "/synthetic/bin", "LANG": "C.UTF-8"}
    finally:
        binding.release()


def test_binding_ownership_paths_and_permissions_are_private(tmp_path):
    config, _ = _persistent_config(tmp_path)
    binding = _bind(tmp_path, config)
    try:
        root = binding._tmp_dir
        projected = _bound_config(binding)
        agent = projected["agents"]["list"][0]
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(binding.config_path).st_mode) == 0o600
        assert (
            stat.S_IMODE(os.lstat(root / "agent" / "auth-profiles.json").st_mode)
            == 0o600
        )
        assert stat.S_IMODE(os.lstat(root / ewb.BINDING_METADATA_NAME).st_mode) == 0o600
        for name in ("agent", "home", "state", "tmp"):
            assert stat.S_IMODE(os.lstat(root / name).st_mode) == 0o700
        assert Path(agent["agentDir"]).parent == root
        assert Path(projected["session"]["store"]).parent == root / "state"
        metadata = json.loads(
            (root / ewb.BINDING_METADATA_NAME).read_text(encoding="utf-8")
        )
        assert metadata["task_execution_id"] == 3713
        assert metadata["artifact_type"] == ewb.BINDING_ARTIFACT_TYPE
        assert binding.environment["OPENCLAW_CONFIG_PATH"] == str(binding.config_path)
        assert Path(binding.environment["HOME"]).parent == root
        assert Path(binding.environment["TMPDIR"]).parent == root
    finally:
        binding.release()


def test_release_and_partial_failure_remove_owned_artifacts(tmp_path, monkeypatch):
    config, _ = _persistent_config(tmp_path)
    binding = _bind(tmp_path, config)
    root = binding._tmp_dir
    auth_profile = root / "agent" / "auth-profiles.json"
    assert auth_profile.exists()
    binding.release()
    assert not root.exists()

    written_profiles = []
    write_private_file = ewb._write_private_file

    def record_private_file(path: Path, text: str) -> None:
        if path.name == "auth-profiles.json":
            written_profiles.append(path)
        write_private_file(path, text)

    monkeypatch.setattr(ewb, "_write_private_file", record_private_file)
    write_text = Path.write_text
    binding_root = Path(os.environ[ewb.BINDING_ROOT_ENV])

    def fail_config_write(path, *args, **kwargs):
        if path.name == "openclaw.json" and path.parent.parent == binding_root:
            raise OSError("synthetic config write failure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_config_write)

    with pytest.raises(OSError, match="synthetic config write failure"):
        _bind(tmp_path, config, "partial")
    assert written_profiles
    assert all(not path.exists() for path in written_profiles)
    assert not binding_root.exists() or list(binding_root.iterdir()) == []


@pytest.mark.parametrize(
    ("model_ref", "message"),
    [
        ("openai-codex/external-model", "Unsupported OpenClaw runtime route"),
        ("", "Explicit OpenClaw runtime model is required"),
    ],
)
def test_unsupported_route_fails_closed_before_artifact_creation(
    tmp_path, model_ref, message
):
    config, _ = _persistent_config(tmp_path)
    with pytest.raises(ewb.ExecutorWorkspaceBindingError, match=message):
        ewb.bind_openclaw_workspace(
            _context(tmp_path), real_config_path=config, model_ref=model_ref
        )
    root = Path(os.environ[ewb.BINDING_ROOT_ENV])
    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["models"]["providers"]["openai"].update(
            {"unknownSecretField": "synthetic"}
        ),
        lambda payload: payload.update({"agents": "unknown-schema"}),
        lambda payload: payload["models"].update({"providers": []}),
    ],
)
def test_unknown_configuration_schema_fails_closed(tmp_path, mutate):
    config, _ = _persistent_config(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    mutate(payload)
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ewb.ExecutorWorkspaceBindingError):
        ewb.bind_openclaw_workspace(
            _context(tmp_path),
            real_config_path=config,
            model_ref=ewb.SUPPORTED_LOCAL_MODEL_REF,
        )
    root = Path(os.environ[ewb.BINDING_ROOT_ENV])
    assert not root.exists() or list(root.iterdir()) == []


def test_symlink_binding_root_is_rejected_without_redirecting_artifacts(
    tmp_path, monkeypatch
):
    target = tmp_path / "real-root"
    target.mkdir()
    link = tmp_path / "binding-root-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv(ewb.BINDING_ROOT_ENV, str(link))
    config, _ = _persistent_config(tmp_path)
    with pytest.raises(ewb.ExecutorWorkspaceBindingError, match="real directory"):
        ewb.bind_openclaw_workspace(
            _context(tmp_path),
            real_config_path=config,
            model_ref=ewb.SUPPORTED_LOCAL_MODEL_REF,
        )
    assert list(target.iterdir()) == []


def test_reconciliation_classifies_projected_auth_profile_as_credential_bearing(
    tmp_path, caplog
):
    config, _ = _persistent_config(tmp_path)
    binding = _bind(tmp_path, config)
    try:
        with caplog.at_level(logging.INFO):
            report = recon.reconcile_binding_artifacts(binding._tmp_dir.parent)
        artifact = next(
            item
            for item in report["artifacts"]
            if item["path"] == str(binding._tmp_dir)
        )
        assert artifact["classification"] == recon.ACTIVE
        assert artifact["credential_bearing"] is True
        assert EXTERNAL_TOKEN not in caplog.text
    finally:
        binding.release()


def test_repeated_bindings_are_deterministic_and_do_not_accumulate(tmp_path):
    config, before = _persistent_config(tmp_path)
    projections = []
    for index in range(3):
        binding = _bind(tmp_path, config, f"probe-{index}")
        try:
            projections.append(_bound_config(binding))
        finally:
            binding.release()
        assert list(Path(os.environ[ewb.BINDING_ROOT_ENV]).iterdir()) == []
    assert (
        projections[0]["models"] == projections[1]["models"] == projections[2]["models"]
    )
    assert config.read_bytes() == before


def test_historical_inventory_is_metadata_only_and_never_applies_cleanup(tmp_path):
    legacy = tmp_path / (ewb.BINDING_DIR_PREFIX + "historical")
    (legacy / "agent").mkdir(parents=True)
    (legacy / "agent" / "auth-profiles.json").write_text(
        EXTERNAL_TOKEN, encoding="utf-8"
    )
    os.chmod(legacy, 0o700)
    inventory = recon.inventory_legacy_tmp_bindings(tmp_path)
    assert inventory["summary"]["total"] == 1
    assert inventory["summary"]["credential_bearing"] == 1
    assert inventory["artifacts"][0]["classification"] == recon.LEGACY_UNMANAGED
    assert inventory["artifacts"][0]["details"]["has_ownership_metadata"] is False
    assert legacy.exists()
