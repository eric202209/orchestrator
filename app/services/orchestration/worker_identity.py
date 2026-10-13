"""Worker source, process, and boot-identity evidence.

This module deliberately has no application, database, broker, provider, or
process-control imports.  It is safe to import from the Celery readiness hook
and from a read-only operator verifier.

The evidence here is intentionally layered.  A source hash and Python import
origin prove the source file that was observable at the boundary; they do not
constitute an in-memory bytecode attestation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
IDENTITY_VERIFIED = "IDENTITY_VERIFIED"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
IDENTITY_INCONCLUSIVE = "IDENTITY_INCONCLUSIVE"
IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"

DEFAULT_WORKER_IDENTITY_DIR = Path("run/worker-identities")
ROADMAP_DOC_PREFIX = "docs/roadmap/"

# This is the fixed, reviewable allowlist for the Phase 37 Worker identity
# contract.  It covers the Worker registration/readiness boundary, S1's
# orphan-sweep decision, D-37-13's binding projection/lifecycle, and the
# subprocess/boot helpers that can affect those paths.  It intentionally does
# not become a transitive import dump.
DEFAULT_AFFECTED_MODULES = (
    "app.celery_app",
    "app.tasks.worker",
    "app.tasks.maintenance",
    "app.services.session.session_lifecycle_service",
    "app.services.orchestration.execution.executor_workspace_binding",
    "app.services.agents.openclaw_service",
    "app.services.agents.subprocess_lifecycle",
    # Direct load-bearing S1/D-37-13 dependencies.  This is intentionally an
    # exact boundary rather than a recursive package dump.
    "app.services.session.orphan_ownership",
    "app.services.session.recovery_coordinator",
    "app.services.orchestration.execution.runtime_context",
    "app.services.agents.runtime_configuration",
    "app.services.orchestration.lifecycle.worker_bootstrap",
    "app.services.orchestration.lifecycle.worker_capacity",
)

EXPECTED_TASK_REGISTRATIONS = frozenset(
    {
        "app.tasks.github_tasks.process_github_issue_event",
        "app.tasks.github_tasks.process_github_pr_event",
        "app.tasks.github_tasks.process_github_push_event",
        "app.tasks.maintenance.cleanup_old_logs",
        "app.tasks.maintenance.generate_task_report",
        "app.tasks.maintenance.process_github_webhook",
        "app.tasks.maintenance.scheduled_task_execution",
        "app.tasks.maintenance.sweep_orphaned_running_sessions",
        "app.tasks.maintenance.sweep_stranded_continuation_deliveries",
        "app.tasks.planning_tasks.advance_planning_session",
        "app.tasks.worker.answer_human_intervention_query",
        "app.tasks.worker.execute_orchestration_task",
        "app.tasks.worker.receive_execution_task_dispatch",
    }
)

# Only these configuration keys may enter the fingerprint.  Secret-bearing
# settings are intentionally absent; presence indicators can be supplied by
# the caller under the separate ``secret_presence`` field.
NON_SECRET_CONFIGURATION_KEYS = frozenset(
    {
        "queue.identity",
        "worker.concurrency",
        "worker.prefetch_multiplier",
        "worker.acks_late",
        "worker.reject_on_worker_lost",
        "worker.acks_on_failure_or_timeout",
        "task.soft_time_limit_seconds",
        "task.time_limit_seconds",
        "runtime.profile",
        "runtime.workspace_enabled",
        "runtime.max_parallel_sessions",
        "route.planning_backend",
        "route.execution_backend",
        "route.agent_backend",
        "route.planning_provider",
        "route.planning_model",
        "route.execution_model",
        "route.repair_model",
        "route.inline_planning",
        "route.planning_repair_enabled",
        "route.typed_grounding_enabled",
        "route.artifact_continuation_enabled",
        "celery.task_serializer",
        "celery.accept_content",
        "celery.result_serializer",
        "celery.timezone",
        "celery.enable_utc",
        "celery.task_default_queue",
        "celery.task_default_exchange",
        "celery.task_default_routing_key",
        "celery.task_routes",
        "celery.worker_send_task_events",
        "celery.task_publish_retry",
        "celery.task_publish_retry_policy",
        "retry.max_retries",
        "retry.countdown_seconds",
        "retry.redelivery_policy",
        "maintenance.task_identity",
        "openclaw.gateway_route",
        "openclaw.model_identity",
        "openclaw.fallback_policy",
        "openclaw.cli.executable",
        "openclaw.cli.args",
        "d37.binding.route_behavior",
        "beat.schedule",
    }
)
SECRET_KEY_MARKERS = (
    "key",
    "token",
    "password",
    "secret",
    "credential",
    "auth",
    "oauth",
    "cookie",
    "private",
)


class WorkerIdentityError(RuntimeError):
    """Base error for fail-closed identity evidence operations."""


class CandidateManifestError(WorkerIdentityError):
    """The candidate source manifest is invalid or cannot be verified."""


class ManifestWriteError(WorkerIdentityError):
    """An identity record could not be written atomically."""


class ConfigurationFingerprintError(WorkerIdentityError):
    """An unapproved or secret-bearing setting was supplied."""


CONFIG_PROJECTION_INCOMPLETE = "CONFIG_PROJECTION_INCOMPLETE"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise CandidateManifestError(f"cannot hash source path: {path}") from exc
    return digest.hexdigest()


def _safe_source_root(source_root: str | Path) -> Path:
    root = Path(source_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise CandidateManifestError(f"source root is not a directory: {root}")
    return root


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _module_source_path(root: Path, module_name: str) -> Path:
    if not module_name or module_name.startswith("."):
        raise CandidateManifestError(f"invalid affected module: {module_name!r}")
    parts = module_name.split(".")
    if any(not part or part in {".", ".."} for part in parts):
        raise CandidateManifestError(f"invalid affected module: {module_name!r}")
    package_path = root.joinpath(*parts)
    source_path = package_path.with_suffix(".py")
    if not source_path.exists():
        source_path = package_path / "__init__.py"
    resolved = source_path.resolve(strict=True)
    if not _under_root(resolved, root):
        raise CandidateManifestError(
            f"affected module escapes candidate source root: {module_name}"
        )
    if not resolved.is_file():
        raise CandidateManifestError(f"affected module is not a file: {module_name}")
    return resolved


def _git_output(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CandidateManifestError(
            f"cannot read candidate Git identity: git {' '.join(arguments)}"
        ) from exc
    return result.stdout.strip()


def _dirty_entries(root: Path) -> tuple[str, ...]:
    output = _git_output(root, "status", "--porcelain", "--untracked-files=all")
    entries = []
    for line in output.splitlines():
        if not line:
            continue
        # Roadmap/workflow documentation is deliberately local and ignored
        # from the deployed source policy.  Every source/config/test entry is
        # still fail-closed.
        path_text = line[3:] if len(line) >= 4 else line
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        if path_text.startswith(ROADMAP_DOC_PREFIX):
            continue
        entries.append(line)
    return tuple(entries)


def _git_identity(root: Path) -> tuple[str, str]:
    return (
        _git_output(root, "rev-parse", "HEAD"),
        _git_output(root, "rev-parse", "HEAD^{tree}"),
    )


def _validate_launcher_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"executable", "cwd", "argv", "launcher"}
    unknown = set(value) - allowed
    if unknown:
        raise CandidateManifestError(
            f"unexpected launcher identity fields: {sorted(unknown)}"
        )
    result = dict(value)
    if not isinstance(result.get("executable"), str) or not result["executable"]:
        raise CandidateManifestError("launcher executable is required")
    if not isinstance(result.get("cwd"), str) or not result["cwd"]:
        raise CandidateManifestError("launcher cwd is required")
    argv = result.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise CandidateManifestError("launcher argv must be a non-empty string list")
    if any(_secret_like_key(item) for item in argv):
        raise CandidateManifestError("launcher argv contains a secret-bearing field")
    if "launcher" in result and not isinstance(result["launcher"], str):
        raise CandidateManifestError("launcher name must be a string")
    return result


def _canonical_launcher_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize independent launcher evidence without executing it."""

    validated = _validate_launcher_identity(value)
    return {
        "launcher": str(validated.get("launcher", "")),
        "executable": str(
            Path(validated["executable"]).expanduser().resolve(strict=False)
        ),
        "cwd": str(Path(validated["cwd"]).expanduser().resolve(strict=False)),
        "argv": [str(item) for item in validated["argv"]],
    }


def _launcher_process_reasons(
    expected: Mapping[str, Any], actual: "ProcessIdentity", prefix: str
) -> list[str]:
    try:
        normalized = _canonical_launcher_identity(expected)
    except (CandidateManifestError, OSError, ValueError) as exc:
        return [f"{prefix}launcher_evidence_invalid:{type(exc).__name__}"]
    reasons: list[str] = []
    if normalized["executable"] != actual.executable:
        reasons.append(f"{prefix}executable_mismatch")
    if normalized["cwd"] != actual.cwd:
        reasons.append(f"{prefix}cwd_mismatch")
    if tuple(normalized["argv"]) != actual.argv:
        reasons.append(f"{prefix}argv_mismatch")
    return reasons


@dataclass(frozen=True)
class ModuleSourceRecord:
    module_name: str
    expected_source_path: str
    source_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "module_name": self.module_name,
            "expected_source_path": self.expected_source_path,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class CandidateSourceManifest:
    schema_version: int
    candidate_head: str
    candidate_tree: str
    source_root: str
    affected_modules: tuple[str, ...]
    resolved_modules: tuple[ModuleSourceRecord, ...]
    launcher_identity: Mapping[str, Any]
    source_manifest_digest: str
    dirty_source_policy: str = "tracked-and-untracked-source-must-be-clean"

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_head": self.candidate_head,
            "candidate_tree": self.candidate_tree,
            "source_root": self.source_root,
            "dirty_source_policy": self.dirty_source_policy,
            "affected_modules": list(self.affected_modules),
            "resolved_modules": [item.to_dict() for item in self.resolved_modules],
            "launcher_identity": dict(self.launcher_identity),
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.payload()
        result["source_manifest_digest"] = self.source_manifest_digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateSourceManifest":
        required = {
            "schema_version",
            "candidate_head",
            "candidate_tree",
            "source_root",
            "dirty_source_policy",
            "affected_modules",
            "resolved_modules",
            "launcher_identity",
            "source_manifest_digest",
        }
        missing = required - set(value)
        if missing:
            raise CandidateManifestError(
                f"candidate manifest missing: {sorted(missing)}"
            )
        modules = tuple(
            ModuleSourceRecord(
                module_name=str(item["module_name"]),
                expected_source_path=str(item["expected_source_path"]),
                source_sha256=str(item["source_sha256"]),
            )
            for item in value["resolved_modules"]
        )
        manifest = cls(
            schema_version=int(value["schema_version"]),
            candidate_head=str(value["candidate_head"]),
            candidate_tree=str(value["candidate_tree"]),
            source_root=str(value["source_root"]),
            dirty_source_policy=str(value["dirty_source_policy"]),
            affected_modules=tuple(str(item) for item in value["affected_modules"]),
            resolved_modules=modules,
            launcher_identity=_validate_launcher_identity(value["launcher_identity"]),
            source_manifest_digest=str(value["source_manifest_digest"]),
        )
        if manifest.schema_version != SCHEMA_VERSION:
            raise CandidateManifestError("unsupported candidate manifest schema")
        if _sha256_bytes(_canonical_json(manifest.payload()).encode()) != (
            manifest.source_manifest_digest
        ):
            raise CandidateManifestError("candidate source manifest digest mismatch")
        return manifest


@dataclass(frozen=True)
class CandidateIdentityPin:
    """Externally approved candidate identity used by activation gates.

    The pin is deliberately separate from the boot record and source
    manifest.  A Worker cannot promote the checkout it is currently running
    to an approved candidate merely by writing a manifest for itself.
    """

    candidate_head: str
    candidate_tree: str
    source_root: str
    source_manifest_digest: str
    launcher_identity: Mapping[str, Any]
    worker_node_identity: str

    def normalized_launcher(self) -> dict[str, Any]:
        return _canonical_launcher_identity(self.launcher_identity)


@dataclass(frozen=True)
class CandidateManifestVerification:
    classification: str
    reasons: tuple[str, ...]
    current_head: str | None = None
    current_tree: str | None = None


def build_candidate_source_manifest(
    source_root: str | Path,
    *,
    affected_modules: Sequence[str] = DEFAULT_AFFECTED_MODULES,
    launcher_identity: Mapping[str, Any] | None = None,
    expected_head: str | None = None,
    expected_tree: str | None = None,
) -> CandidateSourceManifest:
    """Build a deterministic candidate manifest from the actual checkout.

    Git is read directly; no supplied SHA environment value participates in
    the result.  The caller may provide expected identities to make a
    pre-start gate fail closed if the checkout is not the reviewed candidate.
    """

    root = _safe_source_root(source_root)
    dirty = _dirty_entries(root)
    if dirty:
        raise CandidateManifestError(f"candidate source is dirty: {dirty}")
    head, tree = _git_identity(root)
    if expected_head is not None and head != expected_head:
        raise CandidateManifestError("candidate HEAD mismatch")
    if expected_tree is not None and tree != expected_tree:
        raise CandidateManifestError("candidate tree mismatch")

    names = tuple(dict.fromkeys(str(item) for item in affected_modules))
    records = tuple(
        ModuleSourceRecord(
            module_name=name,
            expected_source_path=str(path),
            source_sha256=sha256_file(path),
        )
        for name in names
        for path in (_module_source_path(root, name),)
    )
    if launcher_identity is None:
        launcher_identity = {
            "launcher": "celery-worker",
            "executable": str(Path(sys.executable).resolve()),
            "cwd": str(root),
            "argv": [str(item) for item in sys.argv] or [str(sys.executable)],
        }
    launcher = _validate_launcher_identity(launcher_identity)
    manifest = CandidateSourceManifest(
        schema_version=SCHEMA_VERSION,
        candidate_head=head,
        candidate_tree=tree,
        source_root=str(root),
        dirty_source_policy="tracked-and-untracked-source-must-be-clean",
        affected_modules=names,
        resolved_modules=records,
        launcher_identity=launcher,
        source_manifest_digest="",
    )
    digest = _sha256_bytes(_canonical_json(manifest.payload()).encode())
    return CandidateSourceManifest(
        schema_version=manifest.schema_version,
        candidate_head=manifest.candidate_head,
        candidate_tree=manifest.candidate_tree,
        source_root=manifest.source_root,
        dirty_source_policy=manifest.dirty_source_policy,
        affected_modules=manifest.affected_modules,
        resolved_modules=manifest.resolved_modules,
        launcher_identity=manifest.launcher_identity,
        source_manifest_digest=digest,
    )


def verify_candidate_source_manifest(
    manifest: CandidateSourceManifest | Mapping[str, Any],
    *,
    expected_head: str | None = None,
    expected_tree: str | None = None,
    expected_source_root: str | None = None,
    expected_source_manifest_digest: str | None = None,
) -> CandidateManifestVerification:
    """Recompute Git/source evidence for a candidate manifest."""

    reasons: list[str] = []
    try:
        candidate = (
            manifest
            if isinstance(manifest, CandidateSourceManifest)
            else CandidateSourceManifest.from_dict(manifest)
        )
        if candidate.schema_version != SCHEMA_VERSION:
            reasons.append("candidate_schema_mismatch")
        root = _safe_source_root(candidate.source_root)
        if candidate.source_root != str(root):
            reasons.append("source_root_not_canonical")
        dirty = _dirty_entries(root)
        if dirty:
            reasons.append("dirty_source")
        current_head, current_tree = _git_identity(root)
        if current_head != candidate.candidate_head:
            reasons.append("candidate_head_mismatch")
        if current_tree != candidate.candidate_tree:
            reasons.append("candidate_tree_mismatch")
        if expected_head is not None and current_head != expected_head:
            reasons.append("expected_head_mismatch")
        if expected_tree is not None and current_tree != expected_tree:
            reasons.append("expected_tree_mismatch")
        if expected_source_root is not None and candidate.source_root != str(
            Path(expected_source_root).expanduser().resolve(strict=False)
        ):
            reasons.append("expected_source_root_mismatch")
        if (
            expected_source_manifest_digest is not None
            and candidate.source_manifest_digest != expected_source_manifest_digest
        ):
            reasons.append("expected_source_manifest_digest_mismatch")
        if tuple(candidate.affected_modules) != tuple(
            record.module_name for record in candidate.resolved_modules
        ):
            reasons.append("affected_module_manifest_mismatch")
        for record in candidate.resolved_modules:
            try:
                expected_path = _module_source_path(root, record.module_name)
            except (CandidateManifestError, OSError):
                reasons.append(f"missing_module:{record.module_name}")
                continue
            recorded_path = Path(record.expected_source_path).expanduser().resolve()
            if not _under_root(recorded_path, root):
                reasons.append(f"module_path_escape:{record.module_name}")
                continue
            if recorded_path != expected_path:
                reasons.append(f"wrong_module_path:{record.module_name}")
                continue
            actual_hash = sha256_file(recorded_path)
            if actual_hash != record.source_sha256:
                reasons.append(f"module_hash_mismatch:{record.module_name}")
        expected_digest = _sha256_bytes(_canonical_json(candidate.payload()).encode())
        if expected_digest != candidate.source_manifest_digest:
            reasons.append("source_manifest_digest_mismatch")
    except CandidateManifestError as exc:
        classification = (
            IDENTITY_MISMATCH
            if "digest mismatch" in str(exc) or "manifest" in str(exc)
            else IDENTITY_UNAVAILABLE
        )
        return CandidateManifestVerification(classification, (str(exc),))
    except (OSError, ValueError, TypeError) as exc:
        return CandidateManifestVerification(IDENTITY_UNAVAILABLE, (str(exc),))

    return CandidateManifestVerification(
        IDENTITY_MISMATCH if reasons else IDENTITY_VERIFIED,
        tuple(dict.fromkeys(reasons)),
        current_head=current_head,
        current_tree=current_tree,
    )


def write_candidate_source_manifest(
    manifest: CandidateSourceManifest,
    path: str | Path,
) -> Path:
    """Write a candidate manifest atomically without embedding secrets."""

    return _write_json_atomic(Path(path), manifest.to_dict(), overwrite=True)


def load_candidate_source_manifest(path: str | Path) -> CandidateSourceManifest:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateManifestError(
            f"cannot read candidate source manifest: {path}"
        ) from exc
    if not isinstance(value, Mapping):
        raise CandidateManifestError("candidate source manifest must be an object")
    return CandidateSourceManifest.from_dict(value)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    linux_boot_id: str
    start_ticks: str
    executable: str
    cwd: str
    argv: tuple[str, ...]
    ppid: int | None = None
    pgid: int | None = None
    sid: int | None = None

    def key(self) -> tuple[str, int, str]:
        return self.linux_boot_id, self.pid, self.start_ticks

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["argv"] = list(self.argv)
        return result


def _parse_proc_stat(value: str) -> tuple[int, int | None, int | None, int | None, str]:
    closing_paren = value.rfind(")")
    if closing_paren < 0:
        raise ValueError("malformed /proc stat")
    fields = value[closing_paren + 2 :].split()
    if len(fields) < 20:
        raise ValueError("short /proc stat")
    # fields starts at field 3: state, ppid, pgrp, session, ... starttime
    return (
        int(fields[19]),
        int(fields[1]),
        int(fields[2]),
        int(fields[3]),
        fields[0],
    )


def capture_process_identity(
    pid: int | None = None,
    *,
    proc_root: str | Path = "/proc",
) -> ProcessIdentity | None:
    """Capture PID plus Linux boot/start identity and process provenance."""

    observed_pid = os.getpid() if pid is None else int(pid)
    if observed_pid <= 0:
        return None
    root = Path(proc_root)
    proc_dir = root / str(observed_pid)
    try:
        stat = (proc_dir / "stat").read_text(encoding="utf-8")
        start_ticks, ppid, pgid, sid, _state = _parse_proc_stat(stat)
        boot_id = (
            (root / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
        if not boot_id:
            # Real /proc has this file at the top level; test fixtures may use
            # the same hierarchy or a direct boot_id file.
            boot_id = (root / "boot_id").read_text(encoding="utf-8").strip()
        executable = str((proc_dir / "exe").resolve(strict=True))
        cwd = str((proc_dir / "cwd").resolve(strict=True))
        raw_argv = (proc_dir / "cmdline").read_bytes().split(b"\0")
        argv = tuple(
            item.decode("utf-8", errors="replace") for item in raw_argv if item
        )
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return ProcessIdentity(
        pid=observed_pid,
        linux_boot_id=boot_id,
        start_ticks=str(start_ticks),
        executable=executable,
        cwd=cwd,
        argv=argv,
        ppid=ppid,
        pgid=pgid,
        sid=sid,
    )


def capture_child_identities(
    parent_pid: int,
    *,
    proc_root: str | Path = "/proc",
) -> tuple[ProcessIdentity, ...]:
    root = Path(proc_root)
    children: list[ProcessIdentity] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = capture_process_identity(int(entry.name), proc_root=root)
        if identity is not None and identity.ppid == parent_pid:
            children.append(identity)
    return tuple(sorted(children, key=lambda item: item.pid))


def _module_origin(module: ModuleType) -> tuple[str | None, str | None, str | None]:
    spec = getattr(module, "__spec__", None)
    loader = getattr(module, "__loader__", None)
    return (
        type(loader).__name__ if loader is not None else None,
        getattr(spec, "origin", None) if spec is not None else None,
        getattr(spec, "cached", None) if spec is not None else None,
    )


def capture_loaded_module_provenance(
    candidate: CandidateSourceManifest,
    *,
    modules: Mapping[str, ModuleType] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Capture file/import provenance for the candidate's fixed allowlist."""

    module_table = sys.modules if modules is None else modules
    records: list[dict[str, Any]] = []
    root = Path(candidate.source_root).resolve()
    expected_by_name = {item.module_name: item for item in candidate.resolved_modules}
    for module_name in candidate.affected_modules:
        expected = expected_by_name.get(module_name)
        module = module_table.get(module_name)
        raw_file = getattr(module, "__file__", None) if module else None
        resolved_path: Path | None = None
        source_hash: str | None = None
        loader_type = origin = cached = None
        if raw_file:
            try:
                resolved_path = Path(str(raw_file)).expanduser().resolve(strict=True)
                if resolved_path.suffix == ".pyc":
                    source_candidate = resolved_path.with_suffix(".py")
                    if source_candidate.exists():
                        resolved_path = source_candidate.resolve(strict=True)
                if resolved_path.is_file():
                    source_hash = sha256_file(resolved_path)
                loader_type, origin, cached = _module_origin(module)
            except (OSError, CandidateManifestError, ValueError):
                resolved_path = None
        records.append(
            {
                "module_name": module_name,
                "resolved_source_path": str(resolved_path) if resolved_path else None,
                "expected_source_path": (
                    expected.expected_source_path if expected else None
                ),
                "source_sha256": source_hash,
                "expected_source_sha256": expected.source_sha256 if expected else None,
                "loaded": bool(module and resolved_path),
                "loader_type": loader_type,
                "spec_origin": origin,
                "cached_path": cached,
                "in_memory_code_attestation": "unavailable",
            }
        )
    return tuple(records)


def _secret_like_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _secret_presence_indicator(settings_object: Any, field_name: str) -> bool:
    """Check approved presence metadata without reading the secret value."""

    if field_name in os.environ:
        return True
    fields_set = getattr(settings_object, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(settings_object, "__pydantic_fields_set__", ())
    return field_name in set(fields_set or ())


def _canonical_configuration_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        secret_keys = [str(key) for key in value if _secret_like_key(str(key))]
        if secret_keys:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: secret-bearing nested fields"
            )
        return {
            str(key): _canonical_configuration_value(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_configuration_value(item) for item in value]
    raise ConfigurationFingerprintError(
        f"unsupported configuration value type: {type(value).__name__}"
    )


def build_non_secret_configuration_fingerprint(
    values: Mapping[str, Any],
    *,
    secret_presence: Mapping[str, bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a stable digest over an explicit non-secret allowlist.

    The function never accepts a settings object dump.  Secret presence is
    represented only as booleans, and secret-looking keys are rejected even if
    their values are empty.
    """

    unknown = set(values) - NON_SECRET_CONFIGURATION_KEYS
    if unknown:
        secret_like = sorted(key for key in unknown if _secret_like_key(key))
        if secret_like:
            raise ConfigurationFingerprintError(
                f"secret-bearing configuration fields are forbidden: {secret_like}"
            )
        raise ConfigurationFingerprintError(
            f"configuration fields are not allowlisted: {sorted(unknown)}"
        )
    canonical_values = {
        key: _canonical_configuration_value(values[key]) for key in sorted(values)
    }
    presence: dict[str, bool] = {}
    for key, present in (secret_presence or {}).items():
        if not _secret_like_key(str(key)):
            raise ConfigurationFingerprintError(
                f"presence indicator is not credential-related: {key}"
            )
        presence[str(key)] = bool(present)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "allowlisted_values": canonical_values,
        "secret_presence": dict(sorted(presence.items())),
    }
    return _sha256_bytes(_canonical_json(payload).encode()), payload


def _schedule_value(value: Any) -> Any:
    """Canonicalize Celery schedule values without serializing objects blindly."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, timedelta):
        return {"type": "timedelta", "seconds": value.total_seconds()}
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return {"type": type(value).__name__, "seconds": total_seconds()}
    if isinstance(value, Mapping):
        return _canonical_configuration_value(value)
    if isinstance(value, (list, tuple)):
        return [_schedule_value(item) for item in value]
    raise ConfigurationFingerprintError(
        f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported Beat schedule value"
    )


def _safe_cli_args(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    try:
        args = shlex.split(value) if isinstance(value, str) else list(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: CLI argument policy is not parseable"
        ) from exc
    if not args or any(not isinstance(item, str) or not item for item in args):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: CLI argument policy is invalid"
        )
    if any(_secret_like_key(item) for item in args):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: CLI argument policy may contain secrets"
        )
    return [str(item) for item in args]


def collect_worker_configuration(
    settings_object: Any,
    celery_config: Mapping[str, Any],
    *,
    worker_runtime: Any | None = None,
    operational_values: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Collect only named operational settings from live objects."""

    schedule = celery_config.get("beat_schedule") or {}
    if not isinstance(schedule, Mapping):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: Beat schedule is not a mapping"
        )
    schedule_identity = {}
    for name, entry in sorted(schedule.items()):
        if not isinstance(entry, Mapping):
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: Beat entry is not a mapping"
            )
        unsupported = set(entry) - {
            "task",
            "schedule",
            "args",
            "kwargs",
            "options",
            "relative",
        }
        if unsupported:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported Beat entry fields"
            )
        interval = entry.get("schedule")
        schedule_identity[str(name)] = {
            "task": str(entry.get("task", "")),
            "schedule": _schedule_value(interval),
            "args": _canonical_configuration_value(entry.get("args", ())),
            "kwargs": _canonical_configuration_value(entry.get("kwargs", {})),
            "options": _canonical_configuration_value(entry.get("options", {})),
            "relative": entry.get("relative"),
        }
    operational = operational_values or {}
    maintenance_identity = {
        name: entry.get("task", "")
        for name, entry in sorted(schedule.items())
        if isinstance(entry, Mapping)
    }
    cli_path = getattr(settings_object, "OPENCLAW_CLI_PATH", None)
    cli_args = _safe_cli_args(getattr(settings_object, "OPENCLAW_CLI_ARGS", ""))
    values = {
        "queue.identity": celery_config.get("task_default_queue", "celery"),
        "worker.concurrency": getattr(worker_runtime, "concurrency", None),
        "worker.prefetch_multiplier": celery_config.get("worker_prefetch_multiplier"),
        "worker.acks_late": celery_config.get("task_acks_late"),
        "worker.reject_on_worker_lost": celery_config.get("task_reject_on_worker_lost"),
        "worker.acks_on_failure_or_timeout": celery_config.get(
            "task_acks_on_failure_or_timeout"
        ),
        "task.soft_time_limit_seconds": operational.get(
            "task.soft_time_limit_seconds",
            getattr(
                settings_object, "ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS", None
            ),
        ),
        "task.time_limit_seconds": operational.get(
            "task.time_limit_seconds",
            getattr(settings_object, "ORCHESTRATION_TASK_TIME_LIMIT_SECONDS", None),
        ),
        "runtime.profile": getattr(settings_object, "RUNTIME_PROFILE", None),
        "runtime.workspace_enabled": getattr(
            settings_object, "RUNTIME_WORKSPACE_ENABLED", None
        ),
        "runtime.max_parallel_sessions": getattr(
            settings_object, "LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS", None
        ),
        "route.planning_backend": getattr(settings_object, "PLANNING_BACKEND", None),
        "route.execution_backend": getattr(settings_object, "EXECUTION_BACKEND", None),
        "route.agent_backend": getattr(settings_object, "AGENT_BACKEND", None),
        "route.planning_provider": getattr(settings_object, "PLANNING_PROVIDER", None),
        "route.planning_model": getattr(settings_object, "PLANNER_MODEL", None),
        "route.execution_model": getattr(settings_object, "EXECUTION_MODEL", None),
        "route.repair_model": getattr(settings_object, "PLANNING_REPAIR_MODEL", None),
        "route.inline_planning": getattr(settings_object, "INLINE_PLANNING", None),
        "route.planning_repair_enabled": getattr(
            settings_object, "PLANNING_REPAIR_ENABLED", None
        ),
        "route.typed_grounding_enabled": getattr(
            settings_object, "ENABLE_TYPED_GROUNDING_COORDINATOR", None
        ),
        "route.artifact_continuation_enabled": getattr(
            settings_object, "ARTIFACT_CONTINUATION_ENABLED", None
        ),
        "celery.task_serializer": celery_config.get("task_serializer"),
        "celery.accept_content": celery_config.get("accept_content"),
        "celery.result_serializer": celery_config.get("result_serializer"),
        "celery.timezone": celery_config.get("timezone"),
        "celery.enable_utc": celery_config.get("enable_utc"),
        "celery.task_default_queue": celery_config.get("task_default_queue"),
        "celery.task_default_exchange": celery_config.get("task_default_exchange"),
        "celery.task_default_routing_key": celery_config.get(
            "task_default_routing_key"
        ),
        "celery.task_routes": celery_config.get("task_routes", {}),
        "celery.worker_send_task_events": celery_config.get("worker_send_task_events"),
        "celery.task_publish_retry": celery_config.get("task_publish_retry"),
        "celery.task_publish_retry_policy": celery_config.get(
            "task_publish_retry_policy", {}
        ),
        "retry.max_retries": operational.get("retry.max_retries"),
        "retry.countdown_seconds": operational.get("retry.countdown_seconds"),
        "retry.redelivery_policy": operational.get("retry.redelivery_policy"),
        "maintenance.task_identity": maintenance_identity,
        "openclaw.gateway_route": getattr(
            settings_object, "OPENCLAW_GATEWAY_URL", None
        ),
        "openclaw.model_identity": {
            "execution": getattr(settings_object, "EXECUTION_MODEL", None),
            "planning": getattr(settings_object, "PLANNER_MODEL", None),
            "repair": getattr(settings_object, "PLANNING_REPAIR_MODEL", None),
        },
        "openclaw.fallback_policy": {
            "agent_backend": getattr(settings_object, "AGENT_BACKEND", None),
            "secondary_backend": getattr(
                settings_object, "AGENT_SECONDARY_BACKEND", None
            ),
        },
        "openclaw.cli.executable": cli_path or None,
        "openclaw.cli.args": cli_args,
        "d37.binding.route_behavior": operational.get("d37.binding.route_behavior"),
        "beat.schedule": schedule_identity,
    }
    # Remove absent optional values only when the live object does not expose
    # them; a configured None remains a meaningful canonical value.
    secret_presence = {
        "OPENCLAW_API_KEY": _secret_presence_indicator(
            settings_object, "OPENCLAW_API_KEY"
        ),
        "OPENAI_API_KEY": _secret_presence_indicator(settings_object, "OPENAI_API_KEY"),
        "PLANNING_DIRECT_API_KEY": _secret_presence_indicator(
            settings_object, "PLANNING_DIRECT_API_KEY"
        ),
    }
    return build_non_secret_configuration_fingerprint(
        values,
        secret_presence=secret_presence,
    )


@dataclass(frozen=True)
class WorkerBootIdentityManifest:
    schema_version: int
    candidate_head: str
    candidate_tree: str
    candidate_source_root: str
    source_manifest_digest: str
    boot_timestamp: str
    linux_boot_id: str
    worker_pid: int
    worker_start_ticks: str
    worker_executable: str
    worker_cwd: str
    worker_argv: tuple[str, ...]
    worker_node_identity: str
    expected_task_registrations: tuple[str, ...]
    registered_task_registrations: tuple[str, ...]
    worker_children: tuple[Mapping[str, Any], ...]
    affected_module_provenance: tuple[Mapping[str, Any], ...]
    configuration_fingerprint: str
    configuration_projection: Mapping[str, Any]
    identity_status: str
    record_id: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["worker_argv"] = list(self.worker_argv)
        result["expected_task_registrations"] = list(self.expected_task_registrations)
        result["registered_task_registrations"] = list(
            self.registered_task_registrations
        )
        result["worker_children"] = [dict(item) for item in self.worker_children]
        result["affected_module_provenance"] = [
            dict(item) for item in self.affected_module_provenance
        ]
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerBootIdentityManifest":
        required = {
            "schema_version",
            "candidate_head",
            "candidate_tree",
            "candidate_source_root",
            "source_manifest_digest",
            "boot_timestamp",
            "linux_boot_id",
            "worker_pid",
            "worker_start_ticks",
            "worker_executable",
            "worker_cwd",
            "worker_argv",
            "worker_node_identity",
            "expected_task_registrations",
            "registered_task_registrations",
            "worker_children",
            "affected_module_provenance",
            "configuration_fingerprint",
            "configuration_projection",
            "identity_status",
            "record_id",
        }
        missing = required - set(value)
        if missing:
            raise WorkerIdentityError(
                f"Worker identity manifest missing: {sorted(missing)}"
            )
        return cls(
            schema_version=int(value["schema_version"]),
            candidate_head=str(value["candidate_head"]),
            candidate_tree=str(value["candidate_tree"]),
            candidate_source_root=str(value["candidate_source_root"]),
            source_manifest_digest=str(value["source_manifest_digest"]),
            boot_timestamp=str(value["boot_timestamp"]),
            linux_boot_id=str(value["linux_boot_id"]),
            worker_pid=int(value["worker_pid"]),
            worker_start_ticks=str(value["worker_start_ticks"]),
            worker_executable=str(value["worker_executable"]),
            worker_cwd=str(value["worker_cwd"]),
            worker_argv=tuple(str(item) for item in value["worker_argv"]),
            worker_node_identity=str(value["worker_node_identity"]),
            expected_task_registrations=tuple(
                str(item) for item in value["expected_task_registrations"]
            ),
            registered_task_registrations=tuple(
                str(item) for item in value["registered_task_registrations"]
            ),
            worker_children=tuple(dict(item) for item in value["worker_children"]),
            affected_module_provenance=tuple(
                dict(item) for item in value["affected_module_provenance"]
            ),
            configuration_fingerprint=str(value["configuration_fingerprint"]),
            configuration_projection=dict(value["configuration_projection"]),
            identity_status=str(value["identity_status"]),
            record_id=str(value["record_id"]),
        )


def _write_json_atomic(
    path: Path, payload: Mapping[str, Any], *, overwrite: bool
) -> Path:
    path = path.expanduser()
    try:
        cursor = path.parent.absolute()
        while True:
            if cursor.is_symlink():
                raise OSError(f"manifest parent is a symlink: {cursor}")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if path.is_symlink():
            raise OSError(f"manifest destination is a symlink: {path}")
        path = path.absolute()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if not overwrite and path.exists():
            temporary.unlink(missing_ok=True)
            raise FileExistsError(path)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError) as exc:
        try:
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ManifestWriteError(f"atomic manifest write failed: {path}") from exc
    return path


def _registered_tasks_from_sender(sender: Any) -> tuple[str, ...]:
    app = getattr(sender, "app", None)
    tasks = getattr(app, "tasks", None)
    if tasks is None:
        return ()
    try:
        return tuple(sorted(str(name) for name in tasks.keys()))
    except (AttributeError, TypeError):
        return ()


def create_worker_boot_identity(
    *,
    source_root: str | Path,
    identity_dir: str | Path = DEFAULT_WORKER_IDENTITY_DIR,
    candidate_manifest: CandidateSourceManifest | None = None,
    candidate_pin: CandidateIdentityPin | None = None,
    sender: Any | None = None,
    worker_node_identity: str | None = None,
    registered_tasks: Iterable[str] | None = None,
    configuration_fingerprint: tuple[str, Mapping[str, Any]],
    process_identity: ProcessIdentity | None = None,
    child_identities: Sequence[ProcessIdentity] | None = None,
    boot_timestamp: datetime | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[Path, WorkerBootIdentityManifest]:
    """Create one immutable record for the current Worker process.

    A record is written even when module/registration evidence is incomplete;
    its explicit status remains non-verified.  Process or source-manifest
    evidence that cannot be captured aborts the write entirely.
    """

    root = _safe_source_root(source_root)
    candidate = candidate_manifest or build_candidate_source_manifest(root)
    candidate_check = verify_candidate_source_manifest(
        candidate,
        expected_head=candidate_pin.candidate_head if candidate_pin else None,
        expected_tree=candidate_pin.candidate_tree if candidate_pin else None,
        expected_source_root=candidate_pin.source_root if candidate_pin else None,
        expected_source_manifest_digest=(
            candidate_pin.source_manifest_digest if candidate_pin else None
        ),
    )
    if candidate_check.classification != IDENTITY_VERIFIED:
        raise WorkerIdentityError(
            "candidate source manifest is not verified: "
            + ",".join(candidate_check.reasons)
        )
    process = process_identity or capture_process_identity()
    if process is None:
        raise WorkerIdentityError("current Worker process identity unavailable")
    if any(_secret_like_key(item) for item in process.argv):
        raise WorkerIdentityError("Worker argv contains a secret-bearing field")
    children = tuple(child_identities or capture_child_identities(process.pid))
    projection = dict(configuration_fingerprint[1])
    fingerprint = str(configuration_fingerprint[0])
    actual_tasks = tuple(
        sorted(
            str(item)
            for item in (
                registered_tasks
                if registered_tasks is not None
                else _registered_tasks_from_sender(sender)
            )
        )
    )
    expected_tasks = tuple(sorted(EXPECTED_TASK_REGISTRATIONS))
    provenance = capture_loaded_module_provenance(candidate)
    # A boot hook may record useful evidence without having authority to
    # approve its own checkout.  Without an external pin it is explicitly
    # identity-inconclusive, never verified.
    status = IDENTITY_INCONCLUSIVE if candidate_pin is None else IDENTITY_VERIFIED
    if candidate_pin is not None:
        status_reasons = _launcher_process_reasons(
            candidate_pin.launcher_identity, process, "boot_launcher_"
        )
        if process_node := (
            worker_node_identity
            or getattr(sender, "hostname", None)
            or f"celery@{socket.gethostname()}"
        ):
            if process_node != candidate_pin.worker_node_identity:
                status_reasons.append("boot_worker_node_identity_mismatch")
        if status_reasons:
            status = IDENTITY_MISMATCH
    if set(actual_tasks) != EXPECTED_TASK_REGISTRATIONS:
        status = IDENTITY_MISMATCH
    if any(not item.get("loaded") for item in provenance):
        status = IDENTITY_INCONCLUSIVE if status == IDENTITY_VERIFIED else status
    observed_at = boot_timestamp or now()
    record_id = f"{process.linux_boot_id}-{process.pid}-{process.start_ticks}"
    manifest = WorkerBootIdentityManifest(
        schema_version=SCHEMA_VERSION,
        candidate_head=candidate.candidate_head,
        candidate_tree=candidate.candidate_tree,
        candidate_source_root=candidate.source_root,
        source_manifest_digest=candidate.source_manifest_digest,
        boot_timestamp=_timestamp(observed_at),
        linux_boot_id=process.linux_boot_id,
        worker_pid=process.pid,
        worker_start_ticks=process.start_ticks,
        worker_executable=process.executable,
        worker_cwd=process.cwd,
        worker_argv=process.argv,
        worker_node_identity=(
            worker_node_identity
            or getattr(sender, "hostname", None)
            or f"celery@{socket.gethostname()}"
        ),
        expected_task_registrations=expected_tasks,
        registered_task_registrations=actual_tasks,
        worker_children=tuple(child.to_dict() for child in children),
        affected_module_provenance=provenance,
        configuration_fingerprint=fingerprint,
        configuration_projection=projection,
        identity_status=status,
        record_id=record_id,
    )
    target_dir = Path(identity_dir)
    target = target_dir / f"worker-{record_id}.json"
    path = _write_json_atomic(target, manifest.to_dict(), overwrite=False)
    return path, manifest


@dataclass(frozen=True)
class IdentityVerification:
    classification: str
    reasons: tuple[str, ...]
    candidate: CandidateManifestVerification | None = None


def _identity_from_dict(value: Mapping[str, Any]) -> ProcessIdentity:
    return ProcessIdentity(
        pid=int(value["pid"]),
        linux_boot_id=str(value["linux_boot_id"]),
        start_ticks=str(value["start_ticks"]),
        executable=str(value["executable"]),
        cwd=str(value["cwd"]),
        argv=tuple(str(item) for item in value["argv"]),
        ppid=int(value["ppid"]) if value.get("ppid") is not None else None,
        pgid=int(value["pgid"]) if value.get("pgid") is not None else None,
        sid=int(value["sid"]) if value.get("sid") is not None else None,
    )


def _compare_process(expected: ProcessIdentity, actual: ProcessIdentity) -> list[str]:
    reasons = []
    for field in (
        "pid",
        "linux_boot_id",
        "start_ticks",
        "executable",
        "cwd",
        "argv",
    ):
        if getattr(expected, field) != getattr(actual, field):
            reasons.append(f"process_{field}_mismatch")
    return reasons


def verify_worker_identity(
    boot_manifest: WorkerBootIdentityManifest | Mapping[str, Any],
    candidate_manifest: CandidateSourceManifest | Mapping[str, Any],
    *,
    current_process: ProcessIdentity | None = None,
    current_registered_tasks: Iterable[str] | None = None,
    current_child_identities: Sequence[ProcessIdentity] | None = None,
    current_worker_identities: Sequence[ProcessIdentity] | None = None,
    current_module_provenance: Sequence[Mapping[str, Any]] | None = None,
    current_configuration_fingerprint: str | None = None,
    expected_service_identities: Mapping[str, ProcessIdentity] | None = None,
    current_service_identities: Mapping[str, ProcessIdentity] | None = None,
    candidate_pin: CandidateIdentityPin | None = None,
    current_worker_node_identity: str | None = None,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
) -> IdentityVerification:
    """Independently classify a boot record without mutating runtime state."""

    reasons: list[str] = []
    try:
        boot = (
            boot_manifest
            if isinstance(boot_manifest, WorkerBootIdentityManifest)
            else WorkerBootIdentityManifest.from_dict(boot_manifest)
        )
    except (WorkerIdentityError, KeyError, TypeError, ValueError) as exc:
        return IdentityVerification(IDENTITY_UNAVAILABLE, (str(exc),))

    candidate_check = verify_candidate_source_manifest(
        candidate_manifest,
        expected_head=candidate_pin.candidate_head if candidate_pin else None,
        expected_tree=candidate_pin.candidate_tree if candidate_pin else None,
        expected_source_root=candidate_pin.source_root if candidate_pin else None,
        expected_source_manifest_digest=(
            candidate_pin.source_manifest_digest if candidate_pin else None
        ),
    )
    if candidate_check.classification != IDENTITY_VERIFIED:
        reasons.extend(candidate_check.reasons)
    try:
        candidate = (
            candidate_manifest
            if isinstance(candidate_manifest, CandidateSourceManifest)
            else CandidateSourceManifest.from_dict(candidate_manifest)
        )
    except (WorkerIdentityError, KeyError, TypeError, ValueError) as exc:
        classification = (
            IDENTITY_MISMATCH
            if "digest mismatch" in str(exc) or "manifest" in str(exc)
            else IDENTITY_UNAVAILABLE
        )
        return IdentityVerification(classification, (str(exc),), candidate_check)

    if boot.schema_version != SCHEMA_VERSION:
        reasons.append("boot_schema_mismatch")
    if boot.candidate_head != candidate.candidate_head:
        reasons.append("boot_candidate_head_mismatch")
    if boot.candidate_tree != candidate.candidate_tree:
        reasons.append("boot_candidate_tree_mismatch")
    if boot.candidate_source_root != candidate.source_root:
        reasons.append("boot_source_root_mismatch")
    if boot.source_manifest_digest != candidate.source_manifest_digest:
        reasons.append("boot_source_manifest_mismatch")
    if candidate_pin is None:
        reasons.append("candidate_pin_unavailable")
    else:
        if boot.candidate_head != candidate_pin.candidate_head:
            reasons.append("boot_pinned_candidate_head_mismatch")
        if boot.candidate_tree != candidate_pin.candidate_tree:
            reasons.append("boot_pinned_candidate_tree_mismatch")
        if boot.candidate_source_root != candidate_pin.source_root:
            reasons.append("boot_pinned_source_root_mismatch")
        if boot.source_manifest_digest != candidate_pin.source_manifest_digest:
            reasons.append("boot_pinned_source_manifest_mismatch")
        try:
            if _canonical_launcher_identity(candidate.launcher_identity) != (
                candidate_pin.normalized_launcher()
            ):
                reasons.append("candidate_launcher_pin_mismatch")
        except (CandidateManifestError, OSError, ValueError) as exc:
            reasons.append(f"candidate_launcher_pin_invalid:{type(exc).__name__}")

    actual = current_process or capture_process_identity(boot.worker_pid)
    if actual is None:
        try:
            boot_time = _parse_timestamp(boot.boot_timestamp)
            observed_at = now or _utc_now()
            if observed_at - boot_time > stale_after:
                reasons.append("stale_manifest")
                return IdentityVerification(
                    IDENTITY_MISMATCH, tuple(reasons), candidate_check
                )
        except (TypeError, ValueError):
            reasons.append("invalid_boot_timestamp")
        return IdentityVerification(
            IDENTITY_UNAVAILABLE, tuple(reasons), candidate_check
        )
    expected_process = ProcessIdentity(
        pid=boot.worker_pid,
        linux_boot_id=boot.linux_boot_id,
        start_ticks=boot.worker_start_ticks,
        executable=boot.worker_executable,
        cwd=boot.worker_cwd,
        argv=boot.worker_argv,
    )
    reasons.extend(_compare_process(expected_process, actual))
    if boot.worker_node_identity == "":
        reasons.append("worker_node_identity_missing")
    if candidate_pin is None or not candidate_pin.worker_node_identity:
        reasons.append("expected_worker_node_identity_unavailable")
    elif boot.worker_node_identity != candidate_pin.worker_node_identity:
        reasons.append("boot_worker_node_identity_mismatch")
    if current_worker_node_identity is None:
        reasons.append("current_worker_node_identity_unavailable")
    elif candidate_pin is not None and current_worker_node_identity != (
        candidate_pin.worker_node_identity
    ):
        reasons.append("current_worker_node_identity_mismatch")
    if candidate_pin is not None:
        reasons.extend(
            _launcher_process_reasons(
                candidate_pin.launcher_identity, expected_process, "boot_launcher_"
            )
        )
        reasons.extend(
            _launcher_process_reasons(
                candidate_pin.launcher_identity, actual, "current_launcher_"
            )
        )

    if current_registered_tasks is None:
        reasons.append("registrations_unavailable")
    else:
        current_tasks = set(str(item) for item in current_registered_tasks)
        if current_tasks != set(boot.expected_task_registrations):
            reasons.append("registration_drift")
        if current_tasks != set(boot.registered_task_registrations):
            reasons.append("boot_registration_mismatch")

    if current_child_identities is None:
        reasons.append("child_identity_unavailable")
    else:
        recorded_children = {
            _identity_from_dict(item).key() for item in boot.worker_children
        }
        actual_children = {item.key() for item in current_child_identities}
        if recorded_children != actual_children:
            reasons.append("worker_child_identity_drift")

    if current_worker_identities is None:
        reasons.append("worker_duplication_evidence_unavailable")
    else:
        matching = [
            item
            for item in current_worker_identities
            if item.key() == expected_process.key()
        ]
        if len(current_worker_identities) != 1 or len(matching) != 1:
            reasons.append("unexpected_worker_duplication")

    expected_services = expected_service_identities or {}
    current_services = current_service_identities or {}
    if not expected_services or not current_services:
        reasons.append("service_identity_evidence_unavailable")
    else:
        for service_name, expected_service in expected_services.items():
            actual_service = current_services.get(service_name)
            if actual_service is None:
                reasons.append(f"missing_service_identity:{service_name}")
            else:
                reasons.extend(
                    f"{service_name}_{reason}"
                    for reason in _compare_process(expected_service, actual_service)
                )

    expected_by_name = {
        item["module_name"]: item for item in boot.affected_module_provenance
    }
    if set(expected_by_name) != set(candidate.affected_modules):
        reasons.append("affected_module_provenance_manifest_mismatch")
    for record in boot.affected_module_provenance:
        name = str(record.get("module_name", ""))
        expected = next(
            (item for item in candidate.resolved_modules if item.module_name == name),
            None,
        )
        if expected is None:
            reasons.append(f"unexpected_module:{name}")
            continue
        resolved_text = record.get("resolved_source_path")
        if not resolved_text:
            reasons.append(f"missing_module:{name}")
            continue
        try:
            resolved = Path(str(resolved_text)).resolve(strict=True)
            root = Path(candidate.source_root).resolve(strict=True)
            expected_path = _module_source_path(root, name)
            if not _under_root(resolved, root):
                reasons.append(f"module_path_escape:{name}")
            if resolved != expected_path:
                reasons.append(f"wrong_module_path:{name}")
            if record.get("expected_source_path") != expected.expected_source_path:
                reasons.append(f"module_expected_path_mismatch:{name}")
            if record.get("expected_source_sha256") != expected.source_sha256:
                reasons.append(f"module_expected_hash_mismatch:{name}")
            if record.get("source_sha256") != expected.source_sha256:
                reasons.append(f"module_boot_hash_mismatch:{name}")
            if sha256_file(resolved) != expected.source_sha256:
                reasons.append(f"source_drift:{name}")
        except (OSError, CandidateManifestError, ValueError):
            reasons.append(f"wrong_module_path:{name}")

    if current_module_provenance is not None:
        current_by_name = {
            str(item.get("module_name")): item for item in current_module_provenance
        }
        if set(current_by_name) != set(expected_by_name):
            reasons.append("current_module_provenance_drift")
        for name, record in current_by_name.items():
            if record.get("resolved_source_path") != expected_by_name.get(name, {}).get(
                "expected_source_path"
            ):
                reasons.append(f"current_module_path_mismatch:{name}")
            if record.get("source_sha256") != expected_by_name.get(name, {}).get(
                "expected_source_sha256"
            ):
                reasons.append(f"current_module_hash_mismatch:{name}")
    else:
        # The boot record plus file/import provenance is independently useful,
        # but no external verifier can claim a full in-memory attestation.
        reasons.append("in_memory_code_attestation_unavailable")

    if current_configuration_fingerprint is None:
        reasons.append("configuration_fingerprint_unavailable")
    elif current_configuration_fingerprint != boot.configuration_fingerprint:
        reasons.append("configuration_fingerprint_mismatch")

    if boot.identity_status != IDENTITY_VERIFIED:
        reasons.append(f"boot_identity_status:{boot.identity_status}")

    # The only inconclusive reason permitted for an otherwise matching record
    # is the documented in-memory limitation.  Missing independent evidence is
    # also inconclusive; contradictions remain mismatches.
    contradiction_markers = (
        "mismatch",
        "drift",
        "escape",
        "stale_manifest",
        "unexpected_worker_duplication",
        "source_drift",
        "missing_module",
        "wrong_module",
        "boot_identity_status:",
        "registration_drift",
    )
    effective_reasons = [
        reason
        for reason in reasons
        if reason != "in_memory_code_attestation_unavailable"
    ]
    if any(
        any(marker in reason for marker in contradiction_markers)
        for reason in effective_reasons
    ):
        classification = IDENTITY_MISMATCH
    elif effective_reasons:
        classification = IDENTITY_INCONCLUSIVE
    else:
        classification = IDENTITY_VERIFIED
    return IdentityVerification(
        classification,
        tuple(dict.fromkeys(reasons)),
        candidate_check,
    )


def process_identity_from_mapping(value: Mapping[str, Any]) -> ProcessIdentity:
    """Public fixture/adapter helper for controller gates."""

    return _identity_from_dict(value)


__all__ = [
    "CandidateManifestError",
    "CandidateManifestVerification",
    "CandidateIdentityPin",
    "CandidateSourceManifest",
    "CONFIG_PROJECTION_INCOMPLETE",
    "ConfigurationFingerprintError",
    "DEFAULT_AFFECTED_MODULES",
    "DEFAULT_WORKER_IDENTITY_DIR",
    "EXPECTED_TASK_REGISTRATIONS",
    "IDENTITY_INCONCLUSIVE",
    "IDENTITY_MISMATCH",
    "IDENTITY_UNAVAILABLE",
    "IDENTITY_VERIFIED",
    "IdentityVerification",
    "ManifestWriteError",
    "ModuleSourceRecord",
    "NON_SECRET_CONFIGURATION_KEYS",
    "ProcessIdentity",
    "SCHEMA_VERSION",
    "WorkerBootIdentityManifest",
    "WorkerIdentityError",
    "build_candidate_source_manifest",
    "build_non_secret_configuration_fingerprint",
    "capture_child_identities",
    "capture_loaded_module_provenance",
    "capture_process_identity",
    "collect_worker_configuration",
    "create_worker_boot_identity",
    "load_candidate_source_manifest",
    "process_identity_from_mapping",
    "sha256_file",
    "verify_candidate_source_manifest",
    "verify_worker_identity",
    "write_candidate_source_manifest",
]
