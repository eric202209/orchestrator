"""F0-R2A: Worker source identity for Git checkouts and git-archive releases."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.orchestration import worker_identity
from app.services.orchestration.worker_identity import (
    DEFAULT_AFFECTED_MODULES,
    EXPECTED_TASK_REGISTRATIONS,
    IDENTITY_MISMATCH,
    IDENTITY_UNAVAILABLE,
    IDENTITY_VERIFIED,
    RELEASE_IDENTITY_FILE_NAME,
    CandidateManifestError,
    ProcessIdentity,
    build_candidate_source_manifest,
    create_worker_boot_identity,
    verify_candidate_source_manifest,
)

F0R1_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verification/f0r1.sh"
LAUNCHER = {
    "launcher": "r2a-test",
    "executable": "/venv/bin/python3",
    "cwd": "/srv",
    "argv": ["/venv/bin/python3", "-m", "celery", "worker"],
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "R2A test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "candidate")
    return root


def _minimal_repo(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "worker.py").write_text("WORKER_VALUE = 'r2a'\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "run.sh").chmod(0o755)
    (root / "app" / "alias.py").symlink_to("worker.py")
    return _commit_repo(root)


def _full_app_repo(root: Path) -> Path:
    shutil.copytree(
        Path.cwd() / "app",
        root / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return _commit_repo(root)


def _stage_release(repo: Path, release: Path) -> Path:
    """Stage exactly as f0r1.sh does: git archive plus its identity writer."""

    release.mkdir(parents=True)
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(
        ["tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", str(release)],
        input=archive,
        check=True,
    )
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$0" plan >/dev/null && write_release_identity "$@"',
            str(F0R1_SCRIPT),
            str(repo),
            commit,
            tree,
            str(release),
        ],
        check=True,
    )
    return release


def _rewrite_identity(release: Path, **changes) -> None:
    path = release / RELEASE_IDENTITY_FILE_NAME
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def protected(monkeypatch):
    # Tests run as root, which can write anything; the real check is exercised
    # in test_writable_release_is_refused.
    monkeypatch.setattr(worker_identity, "_writable_by_process", lambda path: False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _minimal_repo(tmp_path / "repo")


@pytest.fixture
def release(repo: Path, tmp_path: Path, protected) -> Path:
    return _stage_release(repo, tmp_path / "release")


def _manifest(root: Path):
    return build_candidate_source_manifest(
        root, affected_modules=("app.worker",), launcher_identity=LAUNCHER
    )


def test_git_checkout_identity_is_unchanged(repo):
    manifest = _manifest(repo)
    assert manifest.candidate_head == _git(repo, "rev-parse", "HEAD")
    assert manifest.candidate_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert manifest.source_root == str(repo.resolve())
    (repo / "app" / "worker.py").write_text("DRIFT = 1\n", encoding="utf-8")
    with pytest.raises(CandidateManifestError, match="dirty"):
        _manifest(repo)


def test_release_without_git_has_the_checkout_identity(repo, release):
    assert not (release / ".git").exists()
    manifest = _manifest(release)
    assert manifest.candidate_head == _git(repo, "rev-parse", "HEAD")
    assert manifest.candidate_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert manifest.source_root == str(release.resolve())
    result = verify_candidate_source_manifest(
        manifest,
        expected_head=manifest.candidate_head,
        expected_tree=manifest.candidate_tree,
        expected_source_root=str(release),
    )
    assert result.module_identity_status == worker_identity.MODULE_IDENTITY_VALID
    assert result.current_tree == manifest.candidate_tree


def test_release_ignores_only_dependency_payload_and_bytecode(release):
    expected = _manifest(release)
    (release / "venv" / "lib").mkdir(parents=True)
    (release / "venv" / "lib" / "dep.py").write_text("X = 1\n", encoding="utf-8")
    (release / "app" / "__pycache__").mkdir()
    (release / "app" / "__pycache__" / "worker.cpython-312.pyc").write_bytes(b"pyc")
    assert _manifest(release).candidate_tree == expected.candidate_tree

    (release / "app" / "venv").mkdir()
    (release / "app" / "venv" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(CandidateManifestError, match="release_tree_mismatch"):
        _manifest(release)


def test_release_without_identity_is_unavailable(release):
    manifest = _manifest(release)
    (release / RELEASE_IDENTITY_FILE_NAME).unlink()
    with pytest.raises(CandidateManifestError, match="neither a Git checkout"):
        _manifest(release)
    result = verify_candidate_source_manifest(manifest)
    assert result.classification == IDENTITY_UNAVAILABLE


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p.write_text("{not json", encoding="utf-8"), "unreadable"),
        (lambda p: p.write_text("[]", encoding="utf-8"), "malformed"),
        (lambda p: _rewrite_identity(p.parent, extra="x"), "malformed"),
        (lambda p: _rewrite_identity(p.parent, schema_version=2), "malformed"),
        (lambda p: _rewrite_identity(p.parent, schema_version=True), "malformed"),
        (lambda p: _rewrite_identity(p.parent, kind="checkout"), "malformed"),
        (lambda p: _rewrite_identity(p.parent, candidate_head="ABC"), "malformed"),
        (lambda p: _rewrite_identity(p.parent, commit_object="***"), "malformed"),
        (
            lambda p: _rewrite_identity(
                p.parent, commit_object=base64.b64encode(b"tree x\n").decode()
            ),
            "does not match its recorded HEAD",
        ),
        (
            lambda p: _rewrite_identity(p.parent, candidate_tree="0" * 40),
            "does not name its recorded tree",
        ),
    ],
)
def test_malformed_release_identity_is_refused(release, mutate, message):
    mutate(release / RELEASE_IDENTITY_FILE_NAME)
    with pytest.raises(CandidateManifestError, match=message):
        _manifest(release)


def test_symlinked_release_identity_is_refused(release, tmp_path):
    identity = release / RELEASE_IDENTITY_FILE_NAME
    elsewhere = tmp_path / "identity.json"
    identity.rename(elsewhere)
    identity.symlink_to(elsewhere)
    with pytest.raises(CandidateManifestError, match="not a regular file"):
        _manifest(release)


def test_release_and_git_together_are_ambiguous(release):
    (release / ".git").mkdir()
    with pytest.raises(CandidateManifestError, match="both a Git checkout"):
        _manifest(release)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: (r / "app" / "worker.py").write_text("X = 2\n", encoding="utf-8"),
        lambda r: (r / "app" / "new.py").write_text("X = 3\n", encoding="utf-8"),
        lambda r: (r / "bin" / "run.sh").chmod(0o644),
        lambda r: (r / "bin" / "run.sh").unlink(),
        lambda r: os.replace(r / "app" / "alias.py", r / "app" / "gone.py"),
    ],
)
def test_release_content_mismatch_is_detected(release, mutate):
    manifest = _manifest(release)
    mutate(release)
    with pytest.raises(CandidateManifestError, match="release_tree_mismatch"):
        _manifest(release)
    result = verify_candidate_source_manifest(manifest)
    assert result.classification == IDENTITY_MISMATCH
    assert "dirty_source" in result.reasons
    assert "candidate_tree_mismatch" in result.reasons


def test_release_path_mismatch_is_detected(release, tmp_path):
    manifest = _manifest(release)
    other = tmp_path / "other-release"
    shutil.copytree(release, other, symlinks=True)
    result = verify_candidate_source_manifest(manifest, expected_source_root=str(other))
    assert result.classification == IDENTITY_MISMATCH
    assert "expected_source_root_mismatch" in result.reasons
    assert _manifest(other).source_root == str(other.resolve())


def test_release_identity_is_cwd_independent(release, repo, monkeypatch):
    expected = _manifest(release)
    monkeypatch.chdir(repo)
    assert _manifest(release) == expected


def test_no_fallback_to_parent_or_env_repository(repo, release, tmp_path, monkeypatch):
    # Inherited GIT_DIR does not redirect a checkout to another repository.
    expected_head = _git(repo, "rev-parse", "HEAD")
    other = _minimal_repo(tmp_path / "other")
    (other / "app" / "worker.py").write_text("OTHER = 1\n", encoding="utf-8")
    _git(other, "commit", "-qam", "other")
    with monkeypatch.context() as patch:
        patch.setenv("GIT_DIR", str(other / ".git"))
        assert _manifest(repo).candidate_head == expected_head
    # A release copy inside an unrelated repository, without its identity,
    # must not take that repository's identity.
    nested = repo / "nested-release"
    shutil.copytree(release, nested, symlinks=True)
    (nested / RELEASE_IDENTITY_FILE_NAME).unlink()
    with pytest.raises(CandidateManifestError, match="neither a Git checkout"):
        _manifest(nested)
    # An empty .git does not let Git walk up to the parent repository.
    (nested / ".git").mkdir()
    with pytest.raises(CandidateManifestError, match="top-level"):
        _manifest(nested)


def test_writable_release_is_refused(repo, tmp_path, monkeypatch):
    release = _stage_release(repo, tmp_path / "release")
    monkeypatch.setattr(
        worker_identity,
        "_writable_by_process",
        lambda path: Path(path) == release.resolve() / "app" / "worker.py",
    )
    with pytest.raises(CandidateManifestError, match="writable by this process"):
        _manifest(release)
    monkeypatch.setattr(
        worker_identity,
        "_writable_by_process",
        lambda path: Path(path).name == RELEASE_IDENTITY_FILE_NAME,
    )
    with pytest.raises(CandidateManifestError, match="identity is writable"):
        _manifest(release)


def test_stager_refuses_a_tree_that_already_has_the_identity_file(repo, tmp_path):
    (repo / RELEASE_IDENTITY_FILE_NAME).write_text("{}", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "tracked identity")
    with pytest.raises(subprocess.CalledProcessError):
        _stage_release(repo, tmp_path / "release")


def test_protected_release_satisfies_worker_boot_identity(tmp_path, protected):
    """R6-IDENTITY contract: one boot record bound to the release root."""

    repo = _full_app_repo(tmp_path / "repo")
    release = _stage_release(repo, tmp_path / "release")
    assert not (release / ".git").exists()
    process = ProcessIdentity(
        pid=501,
        linux_boot_id="boot-r2a",
        start_ticks="5001",
        executable="/venv/bin/python3",
        cwd="/srv/cwd",
        argv=("/venv/bin/python3", "-m", "celery", "worker"),
        ppid=1,
        pgid=501,
        sid=501,
    )
    fingerprint = (
        "r2a",
        {
            "schema_version": worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
            "projection_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        },
    )
    path, boot = create_worker_boot_identity(
        source_root=release,
        identity_dir=tmp_path / "runtime" / "worker-identities",
        registered_tasks=EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=fingerprint,
        process_identity=process,
        child_identities=(),
        boot_timestamp=datetime(2026, 10, 15, tzinfo=UTC),
    )
    assert path.parent == tmp_path / "runtime" / "worker-identities"
    assert boot.candidate_source_root == str(release.resolve())
    assert boot.candidate_head == _git(repo, "rev-parse", "HEAD")
    assert boot.candidate_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    # Without an external pin the record is never self-approved.
    assert boot.identity_status == worker_identity.IDENTITY_INCONCLUSIVE
    manifest = build_candidate_source_manifest(release)
    assert manifest.affected_modules == DEFAULT_AFFECTED_MODULES
    assert verify_candidate_source_manifest(manifest).classification == (
        IDENTITY_VERIFIED
    )
