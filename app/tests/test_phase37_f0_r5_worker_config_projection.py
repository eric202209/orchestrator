"""F0-R5: Worker boot configuration projection against real Celery objects.

The F0-R4 rehearsal Workers (and the production Worker log) failed boot
identity with CONFIG_PROJECTION_INCOMPLETE for six fields. Two were projection
defects (Consumer sender, unset Celery task_routes); four are effective values
that no trusted source supplies at boot and must keep failing closed.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from app.celery_app import celery_app
from app.services.orchestration.worker_identity import (
    CONFIG_PROJECTION_INCOMPLETE,
    EXPECTED_TASK_REGISTRATIONS,
    IDENTITY_UNAVAILABLE,
    REQUIRED_EFFECTIVE_CONFIGURATION_KEYS,
    ConfigurationFingerprintError,
    ProcessIdentity,
    collect_worker_configuration,
    create_worker_boot_identity,
)
from app.services.orchestration import worker_identity
from app.services.session import recovery_coordinator
from app.tasks import worker as worker_tasks
from app.tests.test_phase37_preb_ops1b4b_dp3_i1_worker_identity_controller import (  # noqa: F401
    _boot,
    _controller,
    _observation,
    candidate,
)

D37_VALUES = {
    "d37.binding.provider_base_url_identity": "http://127.0.0.1:8000",
    "d37.binding.model_selection": "openai/qwen-local",
    "d37.binding.auth_schema_identity": "api-key",
}


@pytest.fixture
def consumer():
    """The real object Celery passes as the worker_ready sender."""

    controller = celery_app.WorkController(
        concurrency=3, pool_cls="solo", hostname="f0r5@test"
    )
    return controller.consumer


@pytest.fixture
def boot_kwargs(monkeypatch, consumer):
    """Run the real on_worker_ready and capture what it hands the projector."""

    captured = {}

    def capture(settings_object, celery_config, **kwargs):
        captured.update(settings_object=settings_object, celery_config=celery_config)
        captured.update(kwargs)
        return collect_worker_configuration(settings_object, celery_config, **kwargs)

    created = []
    monkeypatch.setattr(worker_tasks.settings, "OPENCLAW_CLI_PATH", "")
    monkeypatch.setattr(worker_tasks, "_collect_worker_configuration", capture)
    monkeypatch.setattr(
        worker_tasks,
        "_create_worker_boot_identity",
        lambda **kwargs: created.append(kwargs),
    )
    _stub_boot_side_effects(monkeypatch)
    worker_tasks.on_worker_ready(consumer)
    return captured, created


def _stub_boot_side_effects(monkeypatch):
    class FakeDb:
        def close(self):
            return None

    monkeypatch.setattr(worker_tasks, "get_db_session", lambda: FakeDb())
    monkeypatch.setattr(
        recovery_coordinator,
        "inspect_startup_running_executions",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        worker_tasks, "reconcile_backend_slots_on_boot", lambda db: None
    )
    monkeypatch.setattr(
        worker_tasks, "report_openclaw_binding_residue_on_boot", lambda: None
    )


def _collect(captured, *, settings_object=None, operational=None, **changes):
    kwargs = {
        "worker_runtime": captured["worker_runtime"],
        "operational_values": {
            **captured["operational_values"],
            **(operational or {}),
        },
        "task_registry": captured["task_registry"],
        **changes,
    }
    return collect_worker_configuration(
        settings_object or captured["settings_object"],
        captured["celery_config"],
        **kwargs,
    )


def _settings(captured, **overrides):
    real = captured["settings_object"]
    values = {name: getattr(real, name) for name in dir(real) if name.isupper()}
    return SimpleNamespace(**{**values, **overrides})


def _missing(exc: pytest.ExceptionInfo) -> str:
    return str(exc.value).split("effective values unavailable: ", 1)[1]


def test_worker_ready_sender_is_consumer_without_concurrency(consumer):
    assert type(consumer).__name__ == "Consumer"
    assert getattr(consumer, "concurrency", None) is None
    assert consumer.controller.concurrency == 3


def test_option_c_projection_accepts_consumer_with_unset_invocation_values(
    boot_kwargs, consumer
):
    """Option C keeps boot projection independent of invocation values."""

    captured, _created = boot_kwargs
    assert celery_app.conf.task_routes is None
    assert captured["worker_runtime"] is consumer.controller
    _fingerprint, payload = _collect(captured)
    values = payload["allowlisted_values"]
    assert values["openclaw.cli.executable"] == {"configured": False}
    assert not set(D37_VALUES).intersection(values)
    assert payload["identity_scope"] == "worker_boot"
    assert payload["invocation_identity"] == "UNVERIFIED"
    assert (
        payload["invocation_identity_reason"]
        == "trusted_invocation_projection_not_implemented"
    )


def test_boot_passes_work_controller_not_consumer(boot_kwargs, consumer):
    captured, _created = boot_kwargs
    assert captured["worker_runtime"] is consumer.controller


def test_boot_projection_reaches_manifest_creation_without_invocation_values(
    monkeypatch, consumer, tmp_path
):
    source_root = tmp_path / "source-root"
    runtime_root = tmp_path / "runtime-root"
    source_root.mkdir()
    runtime_root.mkdir()
    monkeypatch.setattr(worker_tasks.settings, "WORKER_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(worker_tasks.settings, "RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(worker_tasks.settings, "OPENCLAW_CLI_PATH", "")
    created = []
    monkeypatch.setattr(
        worker_tasks,
        "_create_worker_boot_identity",
        lambda **kwargs: created.append(kwargs),
    )
    _stub_boot_side_effects(monkeypatch)
    worker_tasks.on_worker_ready(consumer)
    assert len(created) == 1
    projection = created[0]["configuration_fingerprint"][1]
    assert projection["identity_scope"] == "worker_boot"
    assert projection["invocation_identity"] == "UNVERIFIED"


def test_projection_succeeds_when_effective_values_exist(boot_kwargs):
    captured, _created = boot_kwargs
    settings_object = _settings(captured, OPENCLAW_CLI_PATH="/usr/local/bin/openclaw")
    fingerprint, payload = _collect(
        captured, settings_object=settings_object, operational=D37_VALUES
    )
    values = payload["allowlisted_values"]
    assert len(fingerprint) == 64
    assert values["worker.concurrency"] == 3
    assert values["celery.task_routes"] == {}
    assert values["task.effective_policies"]["routing_table"] == {}
    assert values["openclaw.cli.executable"] == "/usr/local/bin/openclaw"
    assert not set(D37_VALUES).intersection(values)
    assert "OPENAI_API_KEY" in payload["secret_presence"]


def test_real_consumer_projection_creates_inconclusive_unverified_boot_manifest(
    boot_kwargs, candidate, tmp_path, monkeypatch  # noqa: F811
):
    captured, _created = boot_kwargs
    fingerprint, projection = _collect(captured)
    root, candidate_manifest = candidate
    process = ProcessIdentity(
        pid=401,
        linux_boot_id="boot-option-c",
        start_ticks="4001",
        executable="/venv/bin/python3",
        cwd=str(root),
        argv=("/venv/bin/python3", "-m", "celery", "worker"),
        ppid=1,
        pgid=401,
        sid=401,
    )
    monkeypatch.setattr(
        worker_identity, "capture_loaded_module_provenance", lambda _: ()
    )
    _path, manifest = create_worker_boot_identity(
        source_root=root,
        identity_dir=tmp_path / "identity",
        candidate_manifest=candidate_manifest,
        registered_tasks=EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=(fingerprint, projection),
        process_identity=process,
        child_identities=(),
    )
    assert manifest.identity_status == "IDENTITY_INCONCLUSIVE"
    assert manifest.identity_scope == "worker_boot"
    assert manifest.invocation_identity == "UNVERIFIED"
    assert (
        manifest.invocation_identity_reason
        == "trusted_invocation_projection_not_implemented"
    )
    values = manifest.configuration_projection["allowlisted_values"]
    assert not set(D37_VALUES).intersection(values)


def test_unset_cli_never_uses_path_discovery(boot_kwargs, monkeypatch):
    captured, _created = boot_kwargs

    def fail_which(*_args, **_kwargs):
        raise AssertionError("PATH discovery must not run during boot projection")

    monkeypatch.setattr(shutil, "which", fail_which)
    _fingerprint, projection = _collect(captured)
    assert projection["allowlisted_values"]["openclaw.cli.executable"] == {
        "configured": False
    }


def test_every_remaining_required_boot_key_fails_closed(boot_kwargs):
    captured, _created = boot_kwargs
    _fingerprint, projection = _collect(captured)
    values = projection["allowlisted_values"]
    for field in sorted(REQUIRED_EFFECTIVE_CONFIGURATION_KEYS):
        missing = dict(values)
        missing.pop(field)
        with pytest.raises(
            ConfigurationFingerprintError, match=CONFIG_PROJECTION_INCOMPLETE
        ):
            worker_identity.build_non_secret_configuration_fingerprint(
                missing,
                secret_presence=projection["secret_presence"],
            )


def test_explicit_task_routes_are_projected_not_replaced(boot_kwargs):
    captured, _created = boot_kwargs
    routes = {"app.tasks.worker.*": {"queue": "orchestration"}}
    conf = dict(captured["celery_config"], task_routes=routes)
    settings_object = _settings(captured, OPENCLAW_CLI_PATH="/usr/local/bin/openclaw")
    _fp, payload = collect_worker_configuration(
        settings_object,
        conf,
        worker_runtime=captured["worker_runtime"],
        operational_values={**captured["operational_values"], **D37_VALUES},
        task_registry=captured["task_registry"],
    )
    assert payload["allowlisted_values"]["celery.task_routes"] == routes


def test_unsupported_task_routes_type_still_fails_closed(boot_kwargs):
    captured, _created = boot_kwargs
    conf = dict(captured["celery_config"], task_routes="app.router.route_task")
    with pytest.raises(
        ConfigurationFingerprintError, match=CONFIG_PROJECTION_INCOMPLETE
    ):
        collect_worker_configuration(
            _settings(captured, OPENCLAW_CLI_PATH="/usr/local/bin/openclaw"),
            conf,
            worker_runtime=captured["worker_runtime"],
            operational_values={**captured["operational_values"], **D37_VALUES},
            task_registry=captured["task_registry"],
        )


def test_controller_never_verifies_a_worker_without_boot_manifest(
    candidate,  # noqa: F811 - pytest fixture imported above
):
    """Lifecycle gate: a Worker whose boot identity failed stays unverified."""

    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    controller, _state, adapter = _controller(
        manifest,
        boot,
        _observation(
            worker,
            beat,
            api,
            children=(),
        ),
    )
    controller.boot_manifest = None
    result = controller.verify_started()
    assert result.status == IDENTITY_UNAVAILABLE
    assert result.reasons == ("boot_or_process_identity_unavailable",)
    assert not adapter.start_calls and not adapter.stop_calls
