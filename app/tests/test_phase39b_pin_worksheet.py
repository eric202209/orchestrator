"""Phase 39B offline candidate-pin authoring (worker_pin_worksheet)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.orchestration import worker_identity
from app.services.orchestration.worker_identity import (
    EXPECTED_TASK_REGISTRATIONS,
    PIN_MATCHED,
    CandidateManifestError,
    CandidateSourceManifest,
    ProcessIdentity,
    create_worker_boot_identity,
)
from app.tasks import worker as worker_tasks
from scripts.maintenance import worker_pin_worksheet as worksheet_tool
from scripts.maintenance.worker_pin_worksheet import (
    NOT_APPROVED,
    WorksheetError,
    derive_worksheet,
    parse_launcher_context,
)

REAL_ROOT = Path(__file__).resolve().parents[2]
NODE = "celery@pin-worksheet-test"
WORKER_ARGS = [
    "-A",
    "app.celery_app",
    "worker",
    "--concurrency=4",
    "--loglevel=info",
]
# Prints what a process launched exactly like start.sh computes with the
# production default launcher, plus its own /proc identity.  Inputs come from
# the environment so they cannot alter this process's argv.
PROBE_BODY = """
import json, os
from app.services.orchestration import worker_identity as wi
manifest = wi.build_candidate_source_manifest(os.environ["PROBE_SOURCE_ROOT"])
process = wi.capture_process_identity()
print(json.dumps({"manifest": manifest.to_dict(), "process": process.to_dict()}))
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message)


@pytest.fixture(scope="module")
def candidate_repo(tmp_path_factory) -> Path:
    """A clean checkout holding copies of every default affected module."""

    root = tmp_path_factory.mktemp("pin-worksheet") / "candidate"
    root.mkdir()
    for name in worker_identity.DEFAULT_AFFECTED_MODULES:
        source = worker_identity._module_source_path(REAL_ROOT, name)
        target = root / source.relative_to(REAL_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Pin worksheet test")
    _commit_all(root, "candidate")
    return root.resolve()


@pytest.fixture(scope="module")
def launcher_script(candidate_repo: Path) -> Path:
    """A celery-like entry script outside the checkout, run via its shebang."""

    script = candidate_repo.parent / "bin" / "celery"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!{sys.executable}\n{PROBE_BODY}", encoding="utf-8")
    script.chmod(0o755)
    return script


def _context(root: Path, entry_script: Path, **overrides) -> dict:
    value = {
        "context_schema_version": 1,
        "launcher": "start.sh:start_workers",
        "interpreter": sys.executable,
        "script": str(entry_script),
        "args": list(WORKER_ARGS),
        "cwd": str(root),
        "worker_node_identity": NODE,
    }
    value.update(overrides)
    return value


def _derive(root: Path, entry_script: Path, **overrides) -> dict:
    return derive_worksheet(
        root,
        expected_head=_git(root, "rev-parse", "HEAD"),
        expected_tree=_git(root, "rev-parse", "HEAD^{tree}"),
        launcher_context=_context(root, entry_script, **overrides),
    )


@pytest.fixture(autouse=True)
def _candidate_process(monkeypatch):
    # Fixture checkouts are not where the test code is imported from; the
    # real guard is exercised separately below.
    monkeypatch.setattr(worksheet_tool, "_require_candidate_process", lambda root: None)


def _probe(root: Path, script: Path, args=WORKER_ARGS) -> dict:
    env = {**os.environ, "PYTHONPATH": str(REAL_ROOT), "PROBE_SOURCE_ROOT": str(root)}
    output = subprocess.run(
        [str(script), *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


# 1. Deterministic digest across repeated runs.
def test_repeated_runs_are_byte_identical(candidate_repo, launcher_script):
    first = _derive(candidate_repo, launcher_script)
    second = _derive(candidate_repo, launcher_script)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert len(first["OBSERVED_SOURCE"]["source_manifest_digest"]) == 64


# 2. The helper's own sys.argv never reaches the digest.
def test_helper_argv_does_not_change_digest(
    candidate_repo, launcher_script, monkeypatch
):
    digests = set()
    default_digests = set()
    for argv in (["helper.py"], ["/elsewhere/other.py", "--flag"], ["-c"]):
        monkeypatch.setattr(sys, "argv", argv)
        digests.add(
            _derive(candidate_repo, launcher_script)["OBSERVED_SOURCE"][
                "source_manifest_digest"
            ]
        )
        default_digests.add(
            worker_identity.build_candidate_source_manifest(
                candidate_repo
            ).source_manifest_digest
        )
    assert len(digests) == 1
    # Root cause of the failed Gate A: the default builder follows sys.argv.
    assert len(default_digests) == 3


# 3. Explicit worker-equivalent launcher context, checked against a process
# launched like start.sh (shebang script + args) using the default builder.
def test_matches_independent_worker_equivalent_process(candidate_repo, launcher_script):
    sheet = _derive(candidate_repo, launcher_script)
    probe = _probe(candidate_repo, launcher_script)
    assert (
        sheet["OBSERVED_SOURCE"]["source_manifest_digest"]
        == probe["manifest"]["source_manifest_digest"]
    )
    assert (
        sheet["OBSERVED_SOURCE"]["digest_launcher_identity"]
        == probe["manifest"]["launcher_identity"]
    )
    pin_launcher = sheet["pin_launcher_identity"]
    assert pin_launcher["argv"] == probe["process"]["argv"]
    assert pin_launcher["executable"] == probe["process"]["executable"]
    assert pin_launcher["cwd"] == probe["process"]["cwd"]
    # The two launcher forms differ by design; neither substitutes the other.
    assert (
        pin_launcher["argv"][1:]
        == sheet["OBSERVED_SOURCE"]["digest_launcher_identity"]["argv"]
    )


# 4. Missing launcher context fails closed.
@pytest.mark.parametrize("field", sorted(worksheet_tool._CONTEXT_FIELDS))
def test_missing_launcher_field_fails_closed(candidate_repo, launcher_script, field):
    context = _context(candidate_repo, launcher_script)
    del context[field]
    with pytest.raises(WorksheetError, match="launcher_context_missing_fields"):
        parse_launcher_context(context)


def test_missing_launcher_context_file_fails_closed(tmp_path, capsys):
    code = worksheet_tool.main(
        [
            "--source-root",
            str(tmp_path),
            "--expected-head",
            "0" * 40,
            "--expected-tree",
            "0" * 40,
            "--launcher-context",
            str(tmp_path / "absent.json"),
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["validation"] == {
        "result": "FAILED",
        "reason": "launcher_context_unreadable",
    }


# 5. Malformed launcher context fails closed.
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"context_schema_version": 2}, "unsupported_schema"),
        ({"context_schema_version": True}, "unsupported_schema"),
        ({"extra": "x"}, "unknown_fields"),
        ({"interpreter": "venv/bin/python3"}, "interpreter_not_absolute"),
        ({"cwd": "relative"}, "cwd_not_absolute"),
        ({"launcher": ""}, "launcher_invalid"),
        ({"args": "worker"}, "args_invalid"),
        ({"args": ["worker", ""]}, "args_invalid"),
        ({"args": ["worker"]}, "concurrency_invalid"),
        ({"args": ["--concurrency=4", "--concurrency=2"]}, "concurrency_invalid"),
        ({"args": ["--concurrency=4", "-c", "4"]}, "concurrency_invalid"),
        ({"args": ["--concurrency=0"]}, "concurrency_invalid"),
    ],
)
def test_malformed_launcher_context_fails_closed(
    candidate_repo, launcher_script, overrides, reason
):
    with pytest.raises(WorksheetError, match=reason):
        _derive(candidate_repo, launcher_script, **overrides)


def test_non_object_context_fails_closed():
    with pytest.raises(WorksheetError, match="launcher_context_not_object"):
        parse_launcher_context(["not", "an", "object"])


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"interpreter": "/usr/bin/other-python"}, "launcher_script_shebang_mismatch"),
        ({"script": "/nonexistent/celery"}, "launcher_script_missing"),
        ({"worker_node_identity": "bad node"}, "worker_node_identity_invalid"),
    ],
)
def test_inconsistent_launcher_context_fails_closed(
    candidate_repo, launcher_script, overrides, reason
):
    with pytest.raises(WorksheetError, match=reason):
        _derive(candidate_repo, launcher_script, **overrides)


def test_real_guard_rejects_foreign_root_and_cwd(candidate_repo, monkeypatch, tmp_path):
    monkeypatch.undo()
    with pytest.raises(WorksheetError, match="helper_code_not_from_source_root"):
        worksheet_tool._require_candidate_process(candidate_repo)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WorksheetError, match="helper_cwd_not_source_root"):
        worksheet_tool._require_candidate_process(REAL_ROOT)


# 6. Source checkout changes.
def test_source_changes_change_digest_or_fail_closed(
    candidate_repo, launcher_script, tmp_path
):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(candidate_repo), str(clone)], check=True)
    for args in (
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Pin worksheet test"),
    ):
        _git(clone, *args)
    clone = clone.resolve()
    before = _derive(clone, launcher_script)
    with (clone / "app" / "config.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# candidate change\n")
    with pytest.raises(WorksheetError, match="candidate source is dirty"):
        _derive(clone, launcher_script)
    _commit_all(clone, "change")
    after = _derive(clone, launcher_script)
    assert (
        before["OBSERVED_SOURCE"]["candidate_head"]
        != after["OBSERVED_SOURCE"]["candidate_head"]
    )
    assert (
        before["OBSERVED_SOURCE"]["source_manifest_digest"]
        != after["OBSERVED_SOURCE"]["source_manifest_digest"]
    )
    with pytest.raises(WorksheetError, match="candidate HEAD mismatch"):
        derive_worksheet(
            clone,
            expected_head=before["OBSERVED_SOURCE"]["candidate_head"],
            expected_tree=after["OBSERVED_SOURCE"]["candidate_tree"],
            launcher_context=_context(clone, launcher_script),
        )


# 7. Configuration changes.
def test_configuration_changes_change_configuration_identity(
    candidate_repo, launcher_script, monkeypatch
):
    base = _derive(candidate_repo, launcher_script)
    monkeypatch.setattr(
        worker_tasks.settings,
        "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        worker_tasks.settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS + 1,
    )
    changed = _derive(candidate_repo, launcher_script)
    assert (
        base["configuration_identity"]["configuration_fingerprint"]
        != changed["configuration_identity"]["configuration_fingerprint"]
    )
    assert base["OBSERVED_SOURCE"] == changed["OBSERVED_SOURCE"]


# 8. Launcher changes.
def test_launcher_changes(candidate_repo, launcher_script):
    base = _derive(candidate_repo, launcher_script)
    args = ["-A", "app.celery_app", "worker", "--concurrency=2", "--loglevel=info"]
    concurrency = _derive(candidate_repo, launcher_script, args=args)
    assert (
        concurrency["OBSERVED_SOURCE"]["source_manifest_digest"]
        != base["OBSERVED_SOURCE"]["source_manifest_digest"]
    )
    assert (
        concurrency["configuration_identity"]["configuration_fingerprint"]
        != base["configuration_identity"]["configuration_fingerprint"]
    )
    # The pin label and process cwd are compared at boot but are not part of
    # the source digest (the digest cwd is always the source root).
    for overrides in ({"launcher": "other-label"}, {"cwd": "/"}):
        changed = _derive(candidate_repo, launcher_script, **overrides)
        assert changed["OBSERVED_SOURCE"] == base["OBSERVED_SOURCE"]
        assert changed["pin_launcher_identity"] != base["pin_launcher_identity"]


# 9. No worker manifest, pin or process dependency.
def test_no_worker_manifest_or_pin_dependency(
    candidate_repo, launcher_script, monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("worker state must not be read")

    for name in (
        "load_candidate_source_manifest",
        "load_candidate_identity_pin",
        "resolve_worker_identity_paths",
        "capture_process_identity",
        "capture_child_identities",
        "capture_loaded_module_provenance",
        "create_worker_boot_identity",
        "verify_worker_identity",
    ):
        monkeypatch.setattr(worker_identity, name, forbidden)
    monkeypatch.setattr(
        worker_identity.WorkerBootIdentityManifest, "from_dict", forbidden
    )
    monkeypatch.setenv("WORKER_CANDIDATE_PIN_PATH", str(tmp_path / "pin.json"))
    sheet = _derive(candidate_repo, launcher_script)
    assert sheet["validation"]["result"] == "VALID"


# 10. No network or provider dependency.
def test_no_network_or_provider_dependency(
    candidate_repo, launcher_script, monkeypatch
):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    real_popen = subprocess.Popen

    class GitOnlyPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            assert list(args)[0] == "git", f"unexpected subprocess: {args!r}"
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GitOnlyPopen)
    sheet = _derive(candidate_repo, launcher_script)
    assert sheet["validation"]["result"] == "VALID"


# 11. No automatic pin installation or approval.
def test_worksheet_is_never_an_installable_approved_pin(
    candidate_repo, launcher_script, monkeypatch, tmp_path, capsys
):
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(_context(candidate_repo, launcher_script)), encoding="utf-8"
    )
    watched = (candidate_repo.parent, tmp_path)
    before = {root: sorted(str(path) for path in root.rglob("*")) for root in watched}
    code = worksheet_tool.main(
        [
            "--source-root",
            str(candidate_repo),
            "--expected-head",
            _git(candidate_repo, "rev-parse", "HEAD"),
            "--expected-tree",
            _git(candidate_repo, "rev-parse", "HEAD^{tree}"),
            "--launcher-context",
            str(context_path),
        ]
    )
    assert code == 0
    sheet = json.loads(capsys.readouterr().out)
    after = {root: sorted(str(path) for path in root.rglob("*")) for root in watched}
    assert before == after
    assert _git(candidate_repo, "status", "--porcelain") == ""
    assert sheet["approval_state"] == NOT_APPROVED
    assert sheet["OPERATOR_APPROVED_SOURCE"] is None
    assert sheet["proposed_pin_body"]["approval"] is None
    with pytest.raises(CandidateManifestError, match="candidate_pin_approval_invalid"):
        worker_identity._parse_candidate_pin(
            sheet["proposed_pin_body"], datetime.now(UTC)
        )


# 12. Existing boot identity compatibility.
def test_default_launcher_semantics_unchanged(candidate_repo):
    legacy = {
        "launcher": "celery-worker",
        "executable": str(Path(sys.executable).resolve()),
        "cwd": str(candidate_repo),
        "argv": [str(item) for item in sys.argv] or [str(sys.executable)],
    }
    assert (
        worker_identity.worker_source_launcher_identity(
            candidate_repo, executable=sys.executable, argv=sys.argv
        )
        == legacy
    )
    assert (
        worker_identity.build_candidate_source_manifest(
            candidate_repo
        ).source_manifest_digest
        == worker_identity.build_candidate_source_manifest(
            candidate_repo, launcher_identity=legacy
        ).source_manifest_digest
    )


def test_proposed_pin_matches_worker_equivalent_boot(
    candidate_repo, launcher_script, tmp_path
):
    sheet = _derive(candidate_repo, launcher_script)
    probe = _probe(candidate_repo, launcher_script)
    # The operator, not the helper, supplies the approval record.
    pin_body = {
        **sheet["proposed_pin_body"],
        "approval": {
            "operator_identity": "test-operator",
            "approval_id": "TEST-APPROVAL-1",
            "approved_at": "2026-01-01T00:00:00Z",
            "expires_at": "2999-01-01T00:00:00Z",
        },
    }
    pin = worker_identity._parse_candidate_pin(pin_body, datetime.now(UTC))
    process = ProcessIdentity(
        **{**probe["process"], "argv": tuple(probe["process"]["argv"])}
    )
    configuration = worker_tasks.worker_configuration_fingerprint(
        worker_tasks.celery_app, type("Runtime", (), {"concurrency": 4})()
    )
    _path, boot = create_worker_boot_identity(
        source_root=candidate_repo,
        identity_dir=tmp_path / "identities",
        candidate_manifest=CandidateSourceManifest.from_dict(probe["manifest"]),
        candidate_pin=pin,
        worker_node_identity=NODE,
        registered_tasks=EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=configuration,
        process_identity=process,
        child_identities=[process],
    )
    assert boot.candidate_pin_status == PIN_MATCHED
    assert not [
        reason
        for reason in boot.identity_status_reasons
        if "mismatch" in reason or "pin" in reason
    ]
