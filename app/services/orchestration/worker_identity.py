"""Worker source, process, and boot-identity evidence.

This module deliberately has no application, database, broker, provider, or
process-control imports.  It is safe to import from the Celery readiness hook
and from a read-only operator verifier.

The evidence here is intentionally layered.  A source hash and Python import
origin prove the source file that was observable at the boundary; they do not
constitute an in-memory bytecode attestation.
"""

from __future__ import annotations

import ast
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
from fnmatch import fnmatchcase
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
CONFIGURATION_PROJECTION_SCHEMA_VERSION = 1
CONFIGURATION_POLICY_ID = "phase37-preb-worker-effective-v1"
IDENTITY_VERIFIED = "IDENTITY_VERIFIED"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
IDENTITY_INCONCLUSIVE = "IDENTITY_INCONCLUSIVE"
IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
MODULE_IDENTITY_VALID = "MODULE_IDENTITY_VALID"
MODULE_IDENTITY_INVALID = "MODULE_IDENTITY_INVALID"
BOUNDARY_COMPLETENESS_VALID = "BOUNDARY_COMPLETENESS_VALID"
BOUNDARY_COMPLETENESS_INVALID = "BOUNDARY_COMPLETENESS_INVALID"
BOUNDARY_COMPLETENESS_INCONCLUSIVE = "BOUNDARY_COMPLETENESS_INCONCLUSIVE"
BOUNDARY_COMPLETENESS_NOT_APPLICABLE = "BOUNDARY_COMPLETENESS_NOT_APPLICABLE"

DEFAULT_WORKER_IDENTITY_DIR = Path("run/worker-identities")
ROADMAP_DOC_PREFIX = "docs/roadmap/"

# This is the fixed, reviewable allowlist for the Phase 37 Worker identity
# contract.  It covers the Worker registration/readiness boundary, S1's
# orphan-sweep decision, D-37-13's binding projection/lifecycle, and the
# subprocess/boot helpers that can affect those paths.  It intentionally does
# not become a transitive import dump.
DEFAULT_AFFECTED_MODULES = (
    "app.config",
    "app.database",
    "app.task_intent",
    "app.services",
    "app.celery_app",
    "app.tasks.worker",
    "app.tasks.maintenance",
    # Celery includes these modules explicitly; their registered Task
    # objects and import-time policies are inside the reviewed Worker scope.
    "app.tasks.github_tasks",
    "app.tasks.planning_tasks",
    "app.models",
    "app.services.orchestration",
    "app.services.orchestration.worker_identity",
    "app.services.session.session_lifecycle_service",
    "app.services.orchestration.execution.executor_workspace_binding",
    "app.services.agents.openclaw_service",
    "app.services.agents.subprocess_lifecycle",
    "app.services.agents.backend_concurrency",
    "app.services.agents.backend_slot_reconciliation",
    # Direct load-bearing S1/D-37-13 dependencies.  This is intentionally an
    # exact boundary rather than a recursive package dump.
    "app.services.session.orphan_ownership",
    "app.services.session.recovery_coordinator",
    "app.services.orchestration.execution.runtime_context",
    "app.services.agents.runtime_configuration",
    "app.services.orchestration.lifecycle.worker_bootstrap",
    "app.services.orchestration.lifecycle.worker_capacity",
    "app.services.orchestration.execution.binding_reconciliation",
    "app.services.orchestration.lifecycle.continuation_recovery",
    # Direct Worker execution and backend dependencies.  These are reviewed
    # load-bearing imports, not a recursive package scan.
    "app.services.agents.agent_runtime",
    "app.services.agents.agent_backends",
    "app.services.agents.runtime_adapters.openclaw_adapter",
    "app.services.agents.runtime_invocation",
    "app.services.orchestration.execution.runtime",
    "app.services.orchestration.policy",
    "app.services.orchestration.coordinators.completion_coordinator",
    "app.services.orchestration.coordinators.failure_coordinator",
    "app.services.orchestration.phases.execution_loop",
    "app.services.orchestration.phases.planning_flow",
    "app.services.orchestration.phases.incremental_flow",
    "app.services.orchestration.phases.planning_guidance_enforcement",
    "app.services.orchestration.planning.incremental_classifier",
    "app.services.orchestration.planning.planner_contract_registry",
    "app.services.orchestration.repo_memory",
    "app.services.orchestration.run_state",
    "app.services.orchestration.working_memory",
    "app.services.orchestration.state.persistence",
    "app.services.orchestration.state.execution_states",
    "app.services.orchestration.state.session_state",
    "app.services.orchestration.lifecycle.transitions",
    "app.services.orchestration.lifecycle.terminal_handoff",
    "app.services.orchestration.validation.workspace_guard",
    "app.services.workspace.control_state_paths",
    "app.services.workspace.checkpoint_service",
    "app.services.workspace.project_isolation_service",
    "app.services.workspace.task_sandbox_allocator",
    "app.services.workspace.system_settings",
    "app.services.workspace.project_mutation_lock",
    "app.services.session.execution_policy",
    "app.services.session.session_execution_service",
    "app.services.session.session_runtime_service",
    "app.services.execution.process_identity",
    "app.tasks.worker_support.workspace",
    "app.services.orchestration.events.event_types",
    "app.services.orchestration.error_handler",
    "app.services.orchestration.prompt_templates",
    "app.services.tasks.service",
    "app.services.tasks.execution",
    "app.services.human_guidance.service",
    "app.services.human_guidance.activation",
    "app.services.human_guidance.conflicts",
    "app.services.observability.runtime_identity",
    "app.services.observability.maintenance_observability",
    "app.services.integrations.github",
    # Direct imports of the selected OpenClaw execution service.
    "app.services.agents.interfaces",
    "app.services.model_adaptation",
    "app.services.orchestration.task_rules",
    "app.services.orchestration.validation.git_containment_guard",
    "app.services.orchestration.validation.runtime_pollution_guard",
    "app.services.observability",
    "app.services.observability.planning_provider_evidence",
    "app.services.permissions.approval",
    "app.services.orchestration.prompt_optimization",
    "app.services.tasks.tool_tracking",
    "app.services.planning.planning_dispatch",
    "app.services.planning.planning_session_service",
    "app.services.planning",
    "app.services.project.state_summary",
    "app.services.workspace.workspace_paths",
    "app.services.agents.openclaw_response",
    "app.tasks.worker_support.worker_helpers",
    # Worker helper modules imported by worker_helpers at Worker import time.
    "app.tasks.worker_support.checkpoint",
    "app.tasks.worker_support.common",
    "app.tasks.worker_support.context",
    "app.tasks.worker_support.dispatch",
    "app.tasks.worker_support.execution_state",
    # Direct package re-exports executed by app.services.orchestration.__init__.
    "app.services.orchestration.context.assembly",
    "app.services.orchestration.execution",
    "app.services.orchestration.events",
    "app.services.orchestration.phases",
    "app.services.orchestration.planning",
    "app.services.orchestration.reporting",
    "app.services.orchestration.reporting.policy_simulation",
    "app.services.orchestration.reporting.replay",
    "app.services.orchestration.stage_engine",
    "app.services.orchestration.types",
    "app.services.orchestration.validation",
    # Literal importlib.import_module() from the Worker dispatch task.
    "app.services.execution.execution_task_runtime_ownership_service",
    # Source-backed parent package initializers executed before the bounded
    # modules above.  These are a finite package-loading boundary, not a
    # recursive transitive dependency inventory.
    "app.services.agents",
    "app.services.execution",
    "app.services.human_guidance",
    "app.services.integrations",
    "app.services.observability.build_identity",
    "app.services.observability.health",
    "app.services.observability.log_stream",
    "app.services.observability.streaming_health",
    "app.services.observability.tracing",
    "app.services.orchestration.context",
    "app.services.orchestration.coordinators",
    "app.services.orchestration.events.observability",
    "app.services.orchestration.events.telemetry",
    "app.services.orchestration.execution.execution_flow",
    "app.services.orchestration.execution.executor",
    "app.services.orchestration.execution.step_support",
    "app.services.orchestration.lifecycle",
    "app.services.orchestration.lifecycle.completion",
    "app.services.orchestration.phases.failure_flow",
    "app.services.orchestration.planning.planner",
    "app.services.orchestration.reporting.task_report",
    "app.services.orchestration.state",
    "app.services.orchestration.state.transition_table",
    "app.services.orchestration.validation.parsing",
    "app.services.orchestration.validation.validator",
    "app.services.permissions",
    "app.services.project",
    "app.services.session",
    "app.services.session.auth",
    "app.services.session.auth_service",
    "app.services.session.intervention_service",
    "app.services.session.replan_service",
    "app.services.session.resume_service",
    "app.services.session.session_inspection_service",
    "app.services.session.session_lookup",
    "app.services.session.session_stream_service",
    "app.services.tasks",
    "app.services.workspace",
    "app.services.workspace.context_service",
    "app.services.workspace.overwrite_protection_service",
    "app.tasks",
    "app.tasks.worker_support",
)

# These are the finite source files whose syntax is inspected to derive the
# Worker boundary. The checker does not recurse through arbitrary application
# or third-party dependencies. S1 and D-37-13 roots are explicitly reviewed
# safety paths; only their own identity and dynamic-import call sites are
# inspected because their transitive dependencies are outside this contract.
WORKER_PROVENANCE_REGISTRATION_ROOTS = (
    "app.celery_app",
    "app.tasks.worker",
    "app.tasks.maintenance",
    "app.tasks.github_tasks",
    "app.tasks.planning_tasks",
    "app.tasks.worker_support.worker_helpers",
    "app.tasks.worker_support.checkpoint",
    "app.tasks.worker_support.common",
    "app.tasks.worker_support.context",
    "app.tasks.worker_support.dispatch",
    "app.tasks.worker_support.execution_state",
    "app.services.orchestration",
)
WORKER_PROVENANCE_S1_ROOTS = (
    "app.services.agents.backend_concurrency",
    "app.services.agents.backend_slot_reconciliation",
    "app.services.observability.maintenance_observability",
    "app.services.orchestration.execution.binding_reconciliation",
    "app.services.orchestration.lifecycle.continuation_recovery",
    "app.services.orchestration.lifecycle.worker_bootstrap",
    "app.services.orchestration.lifecycle.worker_capacity",
    "app.services.session.orphan_ownership",
    "app.services.session.recovery_coordinator",
)
WORKER_PROVENANCE_D37_ROOTS = (
    "app.services.agents.openclaw_service",
    "app.services.agents.runtime_adapters.openclaw_adapter",
    "app.services.agents.runtime_configuration",
    "app.services.agents.runtime_invocation",
    "app.services.execution.execution_task_runtime_ownership_service",
)
WORKER_PROVENANCE_ANALYSIS_ROOTS = tuple(
    dict.fromkeys(
        WORKER_PROVENANCE_REGISTRATION_ROOTS
        + WORKER_PROVENANCE_S1_ROOTS
        + WORKER_PROVENANCE_D37_ROOTS
    )
)
WORKER_PROVENANCE_LITERAL_DYNAMIC_MODULES = frozenset(
    {"app.services.execution.execution_task_runtime_ownership_service"}
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

TASK_POLICY_PROJECTION_SCHEMA_VERSION = 1
TASK_POLICY_FIELDS = (
    "retry.max_retries",
    "retry.default_retry_delay",
    "task.soft_time_limit_seconds",
    "task.time_limit_seconds",
    "task.acks_late",
    "task.reject_on_worker_lost",
    "task.acks_on_failure_or_timeout",
    "route.queue",
    "route.exchange",
    "route.routing_key",
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
        "celery.broker_route_identity",
        "celery.result_backend_route_identity",
        "retry.max_retries",
        "retry.countdown_seconds",
        "retry.redelivery_policy",
        "task.decorator_time_limits",
        "policy.timeout_policy",
        "maintenance.task_identity",
        "openclaw.gateway_route",
        "openclaw.model_identity",
        "openclaw.fallback_policy",
        "openclaw.cli.executable",
        "openclaw.cli.args",
        "d37.binding.route_behavior",
        "d37.binding.provider_base_url_identity",
        "d37.binding.model_selection",
        "d37.binding.auth_schema_identity",
        "beat.schedule",
        "task.effective_policies",
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

REQUIRED_EFFECTIVE_CONFIGURATION_KEYS = frozenset(
    {
        "queue.identity",
        "worker.concurrency",
        "worker.prefetch_multiplier",
        "worker.acks_late",
        "worker.reject_on_worker_lost",
        "worker.acks_on_failure_or_timeout",
        "task.soft_time_limit_seconds",
        "task.time_limit_seconds",
        "celery.task_serializer",
        "celery.accept_content",
        "celery.result_serializer",
        "celery.timezone",
        "celery.enable_utc",
        "celery.task_default_queue",
        "celery.task_routes",
        "celery.broker_route_identity",
        "celery.result_backend_route_identity",
        "retry.max_retries",
        "retry.countdown_seconds",
        "retry.redelivery_policy",
        "task.decorator_time_limits",
        "policy.timeout_policy",
        "maintenance.task_identity",
        "openclaw.gateway_route",
        "openclaw.model_identity",
        "openclaw.fallback_policy",
        "openclaw.cli.executable",
        "openclaw.cli.args",
        "d37.binding.route_behavior",
        "d37.binding.provider_base_url_identity",
        "d37.binding.model_selection",
        "d37.binding.auth_schema_identity",
        "beat.schedule",
        "task.effective_policies",
    }
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


def _configuration_pin_reasons(
    candidate_pin: "CandidateIdentityPin | None",
) -> list[str]:
    """Validate independently provisioned configuration identity metadata."""

    if candidate_pin is None:
        return [
            "candidate_pin_unavailable",
            "approved_configuration_identity_unavailable",
        ]
    reasons: list[str] = []
    if candidate_pin.configuration_schema_version is None:
        reasons.append("approved_configuration_schema_unavailable")
    elif candidate_pin.configuration_schema_version != (
        CONFIGURATION_PROJECTION_SCHEMA_VERSION
    ):
        reasons.append("unsupported_configuration_projection_schema")
    if not candidate_pin.configuration_policy_id:
        reasons.append("approved_configuration_policy_unavailable")
    elif candidate_pin.configuration_policy_id != CONFIGURATION_POLICY_ID:
        reasons.append("unsupported_configuration_policy")
    digest = candidate_pin.configuration_fingerprint
    if not isinstance(digest, str) or len(digest) != 64:
        reasons.append("approved_configuration_digest_unavailable")
    elif any(character not in "0123456789abcdef" for character in digest):
        reasons.append("approved_configuration_digest_invalid")
    return reasons


def configuration_pin_reasons(
    candidate_pin: "CandidateIdentityPin | None",
) -> tuple[str, ...]:
    """Return fail-closed reasons for absent or unsupported approved config."""

    return tuple(_configuration_pin_reasons(candidate_pin))


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
    module_path = package_path.with_suffix(".py")
    package_init_path = package_path / "__init__.py"
    module_exists = module_path.exists()
    package_exists = package_init_path.exists()
    if module_exists and package_exists:
        raise CandidateManifestError(
            f"ambiguous module/package identity: {module_name}"
        )
    if module_exists:
        source_path = module_path
    elif package_exists:
        source_path = package_init_path
    else:
        source_path = module_path
    resolved = source_path.resolve(strict=True)
    if not _under_root(resolved, root):
        raise CandidateManifestError(
            f"affected module escapes candidate source root: {module_name}"
        )
    if not resolved.is_file():
        raise CandidateManifestError(f"affected module is not a file: {module_name}")
    return resolved


def _parent_package_initializers(
    root: Path, module_names: Iterable[str]
) -> tuple[set[str], set[str]]:
    """Derive source-backed parent package identities without importing code.

    Only parents of already bounded module identities are considered.  A
    namespace parent is reported separately and never gets an invented
    ``__init__.py`` identity.
    """

    initializers: set[str] = set()
    namespace_packages: set[str] = set()
    for module_name in module_names:
        parts = module_name.split(".")
        for index in range(1, len(parts)):
            package_name = ".".join(parts[:index])
            package_path = root.joinpath(*parts[:index])
            initializer_path = package_path / "__init__.py"
            if initializer_path.is_file():
                initializers.add(package_name)
            elif package_path.is_dir():
                namespace_packages.add(package_name)
    return initializers, namespace_packages


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


def canonical_launcher_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the reviewed launcher identity without executing it."""

    return _canonical_launcher_identity(value)


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
    configuration_schema_version: int | None = None
    configuration_fingerprint: str | None = None
    configuration_policy_id: str | None = None

    def normalized_launcher(self) -> dict[str, Any]:
        return _canonical_launcher_identity(self.launcher_identity)


@dataclass(frozen=True)
class CandidateManifestVerification:
    classification: str
    reasons: tuple[str, ...]
    current_head: str | None = None
    current_tree: str | None = None
    module_identity_status: str = MODULE_IDENTITY_VALID
    boundary_completeness_status: str = BOUNDARY_COMPLETENESS_NOT_APPLICABLE


@dataclass(frozen=True)
class ProvenanceBoundaryCheck:
    """Result of finite static inspection of the Worker source boundary."""

    classification: str
    required_modules: tuple[str, ...]
    inspected_modules: tuple[str, ...]
    missing_modules: tuple[str, ...] = ()
    missing_source_modules: tuple[str, ...] = ()
    literal_dynamic_imports: tuple[str, ...] = ()
    unsupported_dynamic_imports: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    package_initializer_modules: tuple[str, ...] = ()
    namespace_packages: tuple[str, ...] = ()
    lazy_dynamic_imports: tuple[str, ...] = ()


def _relative_import_module(
    source_module: str, source_path: Path, node: ast.ImportFrom
) -> str:
    package = (
        source_module
        if source_path.name == "__init__.py"
        else source_module.rsplit(".", 1)[0]
    )
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    prefix = parts[: len(parts) - node.level + 1]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _static_module_imports(
    source_module: str, source_path: Path
) -> tuple[set[str], set[str], tuple[str, ...], tuple[str, ...]]:
    """Read import edges without importing or executing the source module."""

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imports: set[str] = set()
    literal_dynamic: set[str] = set()
    unsupported_dynamic: list[str] = []
    parse_notes: list[str] = []
    lazy_dynamic_lines = {
        getattr(child, "lineno", -1)
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and function.name == "__getattr__"
        for child in ast.walk(function)
        if isinstance(child, ast.Call)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(
                alias.name for alias in node.names if alias.name.startswith("app.")
            )
        elif isinstance(node, ast.ImportFrom):
            imported_module = _relative_import_module(source_module, source_path, node)
            if node.module is None and node.level:
                package = _relative_import_module(source_module, source_path, node)
                if package.startswith("app."):
                    imports.add(package)
                imports.update(
                    f"{package}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
            elif imported_module.startswith("app."):
                imports.add(imported_module)
        elif isinstance(node, ast.Call):
            function = node.func
            is_import_module = (
                isinstance(function, ast.Name) and function.id == "import_module"
            ) or (
                isinstance(function, ast.Attribute) and function.attr == "import_module"
            )
            if not is_import_module:
                continue
            argument = node.args[0] if node.args else None
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.startswith("app."):
                    literal_dynamic.add(argument.value)
            else:
                line = getattr(node, "lineno", "?")
                if line in lazy_dynamic_lines:
                    parse_notes.append(f"lazy_dynamic_import:{source_module}:{line}")
                else:
                    unsupported_dynamic.append(f"{source_module}:{line}")
    return imports, literal_dynamic, tuple(unsupported_dynamic), tuple(parse_notes)


def inspect_worker_provenance_boundary(
    source_root: str | Path,
    approved_modules: Iterable[str],
) -> ProvenanceBoundaryCheck:
    """Independently check the finite Worker boundary against source syntax.

    The explicit seed set is the reviewed safety boundary. Only registration,
    helper and orchestration-package initialization files contribute direct
    import edges; S1 and D-37-13 roots are inspected for identity and dynamic
    import uncertainty but are not recursively expanded.
    """

    root = Path(source_root).expanduser().resolve(strict=False)
    anchor_paths = [
        root / "app" / "celery_app.py",
        root / "app" / "tasks" / "worker.py",
    ]
    if not any(path.is_file() for path in anchor_paths):
        return ProvenanceBoundaryCheck(
            BOUNDARY_COMPLETENESS_NOT_APPLICABLE,
            (),
            (),
            reasons=("worker_boundary_anchors_unavailable",),
        )

    approved = {str(item) for item in approved_modules}
    required = set(DEFAULT_AFFECTED_MODULES)
    inspected: list[str] = []
    missing_source: list[str] = []
    literal_dynamic: set[str] = set()
    unsupported_dynamic: list[str] = []
    lazy_dynamic: set[str] = set()
    reasons: list[str] = []
    resolution_errors: list[str] = []
    package_initializers: set[str] = set()
    namespace_packages: set[str] = set()
    inspected_set: set[str] = set()

    # A fixed point over parent package initializers is finite: only
    # initializers belonging to already bounded identities are analyzed.  The
    # imported modules themselves are recorded but are not recursively parsed.
    while True:
        discovered_initializers, discovered_namespaces = _parent_package_initializers(
            root, required
        )
        new_initializers = discovered_initializers - package_initializers
        package_initializers.update(discovered_initializers)
        newly_required_initializers = discovered_initializers - required
        required.update(discovered_initializers)
        namespace_packages.update(discovered_namespaces)
        analysis_roots = tuple(
            dict.fromkeys(
                WORKER_PROVENANCE_ANALYSIS_ROOTS + tuple(sorted(package_initializers))
            )
        )
        new_required = bool(newly_required_initializers)
        for module_name in analysis_roots:
            if module_name in inspected_set:
                continue
            inspected_set.add(module_name)
            try:
                source_path = _module_source_path(root, module_name)
            except CandidateManifestError as exc:
                if "ambiguous module/package identity" in str(exc):
                    resolution_errors.append(f"module_package_collision:{module_name}")
                else:
                    missing_source.append(module_name)
                continue
            except (OSError, ValueError):
                missing_source.append(module_name)
                continue
            inspected.append(module_name)
            try:
                imports, dynamic, unsupported, notes = _static_module_imports(
                    module_name, source_path
                )
            except (OSError, SyntaxError, UnicodeError) as exc:
                reasons.append(
                    f"static_inspection_unavailable:{module_name}:{type(exc).__name__}"
                )
                continue
            literal_dynamic.update(dynamic)
            unsupported_dynamic.extend(unsupported)
            for note in notes:
                if note.startswith("lazy_dynamic_import:"):
                    lazy_dynamic.add(note)
                else:
                    reasons.append(note)
            if (
                module_name in WORKER_PROVENANCE_REGISTRATION_ROOTS
                or module_name in package_initializers
            ):
                previous_required = len(required)
                required.update(imports)
                required.update(dynamic)
                new_required = new_required or len(required) != previous_required
        if not new_initializers and not new_required:
            break

    # Every required identity, including a package initializer, must resolve
    # to an in-root source file.  This keeps an omitted or deleted required
    # initializer from looking like an intentional namespace exclusion.
    for module_name in sorted(required):
        try:
            _module_source_path(root, module_name)
        except CandidateManifestError as exc:
            if "ambiguous module/package identity" in str(exc):
                resolution_errors.append(f"module_package_collision:{module_name}")
            else:
                missing_source.append(module_name)
        except (OSError, ValueError):
            missing_source.append(module_name)
    required.update(literal_dynamic)
    missing = sorted(required - approved)
    if missing_source:
        reasons.extend(f"missing_reviewed_source:{name}" for name in missing_source)
    if missing:
        reasons.extend(f"missing_approved_module:{name}" for name in missing)
    reasons.extend(resolution_errors)
    if (
        reasons
        and not unsupported_dynamic
        and not missing
        and not missing_source
        and not resolution_errors
    ):
        unsupported_dynamic.extend(reasons)
    classification = BOUNDARY_COMPLETENESS_VALID
    if missing or missing_source or resolution_errors:
        classification = BOUNDARY_COMPLETENESS_INVALID
    elif unsupported_dynamic or reasons:
        classification = BOUNDARY_COMPLETENESS_INCONCLUSIVE
    return ProvenanceBoundaryCheck(
        classification=classification,
        required_modules=tuple(sorted(required)),
        inspected_modules=tuple(inspected),
        missing_modules=tuple(missing),
        missing_source_modules=tuple(sorted(set(missing_source))),
        literal_dynamic_imports=tuple(sorted(literal_dynamic)),
        unsupported_dynamic_imports=tuple(sorted(set(unsupported_dynamic))),
        reasons=tuple(dict.fromkeys(reasons)),
        package_initializer_modules=tuple(sorted(package_initializers)),
        namespace_packages=tuple(sorted(namespace_packages)),
        lazy_dynamic_imports=tuple(sorted(lazy_dynamic)),
    )


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
    boundary_check = ProvenanceBoundaryCheck(
        BOUNDARY_COMPLETENESS_NOT_APPLICABLE, (), ()
    )
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
        module_identity_reasons = tuple(reasons)
        boundary_check = inspect_worker_provenance_boundary(
            root, candidate.affected_modules
        )
        if boundary_check.classification == BOUNDARY_COMPLETENESS_INVALID:
            reasons.extend(
                f"provenance_boundary_missing:{name}"
                for name in boundary_check.missing_modules
            )
            reasons.extend(
                f"provenance_boundary_source_missing:{name}"
                for name in boundary_check.missing_source_modules
            )
        elif boundary_check.classification == BOUNDARY_COMPLETENESS_INCONCLUSIVE:
            reasons.extend(
                f"provenance_boundary_inconclusive:{reason}"
                for reason in boundary_check.reasons
            )
    except CandidateManifestError as exc:
        classification = (
            IDENTITY_MISMATCH
            if "digest mismatch" in str(exc) or "manifest" in str(exc)
            else IDENTITY_UNAVAILABLE
        )
        return CandidateManifestVerification(
            classification,
            (str(exc),),
            module_identity_status=MODULE_IDENTITY_INVALID,
        )
    except (OSError, ValueError, TypeError) as exc:
        return CandidateManifestVerification(
            IDENTITY_UNAVAILABLE,
            (str(exc),),
            module_identity_status=MODULE_IDENTITY_INVALID,
        )

    if boundary_check.classification == BOUNDARY_COMPLETENESS_NOT_APPLICABLE:
        reasons.append("provenance_boundary_not_applicable")
    elif boundary_check.classification not in {
        BOUNDARY_COMPLETENESS_VALID,
        BOUNDARY_COMPLETENESS_INVALID,
        BOUNDARY_COMPLETENESS_INCONCLUSIVE,
    }:
        reasons.append(f"provenance_boundary_unknown:{boundary_check.classification}")

    module_identity_status = (
        MODULE_IDENTITY_VALID
        if not module_identity_reasons
        else MODULE_IDENTITY_INVALID
    )
    if module_identity_status != MODULE_IDENTITY_VALID:
        classification = IDENTITY_MISMATCH
    elif boundary_check.classification == BOUNDARY_COMPLETENESS_VALID:
        classification = IDENTITY_VERIFIED
    elif boundary_check.classification == BOUNDARY_COMPLETENESS_INVALID:
        classification = IDENTITY_MISMATCH
    else:
        classification = IDENTITY_INCONCLUSIVE
    return CandidateManifestVerification(
        classification,
        tuple(dict.fromkeys(reasons)),
        current_head=current_head,
        current_tree=current_tree,
        module_identity_status=module_identity_status,
        boundary_completeness_status=boundary_check.classification,
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
        if module is not None:
            loader_type, origin, cached = _module_origin(module)
        if raw_file:
            try:
                resolved_path = Path(str(raw_file)).expanduser().resolve(strict=True)
                if resolved_path.suffix == ".pyc":
                    source_candidate = resolved_path.with_suffix(".py")
                    if source_candidate.exists():
                        resolved_path = source_candidate.resolve(strict=True)
                if resolved_path.is_file():
                    source_hash = sha256_file(resolved_path)
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
                "load_timing": (
                    "boot"
                    if (module and resolved_path)
                    else (
                        "dynamic_literal_unloaded"
                        if module_name in WORKER_PROVENANCE_LITERAL_DYNAMIC_MODULES
                        else "lazy_or_unloaded"
                    )
                ),
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
        secret_keys = [
            str(key)
            for key in value
            if _secret_like_key(str(key)) and str(key) != "routing_key"
        ]
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
    policy_id: str = CONFIGURATION_POLICY_ID,
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
    missing = sorted(
        key
        for key in REQUIRED_EFFECTIVE_CONFIGURATION_KEYS
        if key not in values or values[key] is None
    )
    if missing:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective values unavailable: {missing}"
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
    if policy_id != CONFIGURATION_POLICY_ID:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported configuration policy"
        )
    payload = {
        "schema_version": CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        "projection_policy_id": policy_id,
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


def _safe_route_identity(value: Any, *, field_name: str) -> str | None:
    """Keep only non-secret route components for an endpoint identity."""

    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: {field_name} is not a string"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: {field_name} is not parseable"
        ) from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: {field_name} contains secret-bearing URL parts"
        )
    if parsed.scheme:
        if not parsed.netloc and parsed.scheme not in {"file", "unix"}:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: {field_name} has no route authority"
            )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if _secret_like_key(value):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: {field_name} may contain credentials"
        )
    return value


_TASK_POLICY_MISSING = object()


def _task_policy_attribute(task: Any, task_name: str, attribute: str) -> Any:
    if isinstance(task, Mapping):
        value = task.get(attribute, _TASK_POLICY_MISSING)
    else:
        value = getattr(task, attribute, _TASK_POLICY_MISSING)
    if value is _TASK_POLICY_MISSING:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective task policy unavailable "
            f"for {task_name}: {attribute}"
        )
    return value


def _task_route_value(
    task_name: str,
    task: Any,
    attribute: str,
    celery_config: Mapping[str, Any],
) -> Any:
    value = (
        task.get(attribute, _TASK_POLICY_MISSING)
        if isinstance(task, Mapping)
        else getattr(task, attribute, _TASK_POLICY_MISSING)
    )
    if value is not _TASK_POLICY_MISSING:
        return value
    routes = celery_config.get("task_routes", {})
    if routes is None:
        routes = {}
    if not isinstance(routes, Mapping):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective task routes unavailable"
        )
    route = routes.get(task_name)
    if route is None:
        matches = sorted(
            (
                (str(pattern), candidate)
                for pattern, candidate in routes.items()
                if isinstance(pattern, str) and fnmatchcase(task_name, pattern)
            ),
            key=lambda item: item[0],
        )
        route = matches[0][1] if matches else None
    if route is not None:
        if not isinstance(route, Mapping):
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported route type for {task_name}"
            )
        unknown = set(route) - {"queue", "exchange", "routing_key"}
        if unknown:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported route fields for "
                f"{task_name}: {sorted(unknown)}"
            )
        if attribute in route:
            return route[attribute]
    config_key = {
        "queue": "task_default_queue",
        "exchange": "task_default_exchange",
        "routing_key": "task_default_routing_key",
    }[attribute]
    if config_key not in celery_config:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective route value unavailable "
            f"for {task_name}: {attribute}"
        )
    return celery_config[config_key]


def _canonical_task_route(value: Any, *, task_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported route value for "
            f"{task_name}"
        )
    return value


def _canonical_task_number(
    value: Any, *, task_name: str, field_name: str, integer: bool = False
) -> int | float | None:
    if value is None:
        return None
    valid = isinstance(value, int if integer else (int, float)) and not isinstance(
        value, bool
    )
    if not valid or value < 0:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported effective policy type "
            f"for {task_name}: {field_name}"
        )
    return value


def _canonical_task_routes(routes: Any) -> dict[str, Any]:
    if routes is None:
        return {}
    if not isinstance(routes, Mapping):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported effective task routes type"
        )
    canonical: dict[str, Any] = {}
    for pattern, route in sorted(routes.items(), key=lambda item: str(item[0])):
        if not isinstance(pattern, str) or not isinstance(route, Mapping):
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported effective task route"
            )
        unknown = set(route) - {"queue", "exchange", "routing_key"}
        if unknown:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported route fields: "
                f"{sorted(unknown)}"
            )
        canonical[pattern] = {
            key: _canonical_task_route(route.get(key), task_name=pattern)
            for key in ("queue", "exchange", "routing_key")
            if key in route
        }
    return canonical


def build_effective_task_policy_projection(
    task_registry: Mapping[str, Any] | None,
    celery_config: Mapping[str, Any],
    *,
    reviewed_tasks: Iterable[str] = EXPECTED_TASK_REGISTRATIONS,
) -> dict[str, Any]:
    """Build the finite, non-secret projection of registered Celery policies.

    ``task_registry`` must be the already-registered Worker task table.  This
    function never imports task modules.  Only application task names are
    compared with the reviewed set; Celery's built-in utility tasks are not
    part of the Worker safety contract.  Missing, extra application tasks,
    unsupported values, and incomplete route evidence all fail closed.
    """

    if not isinstance(task_registry, Mapping) or not isinstance(celery_config, Mapping):
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective task policy evidence unavailable"
        )
    reviewed = tuple(sorted({str(item) for item in reviewed_tasks}))
    registry_names: set[str] = set()
    for raw_name in task_registry:
        if not isinstance(raw_name, str):
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: registered task name is not a string"
            )
        registry_names.add(raw_name)
    application_names = {
        name for name in registry_names if name.startswith("app.tasks.")
    }
    missing = sorted(set(reviewed) - registry_names)
    if missing:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: missing registered task policy: {missing}"
        )
    unexpected = sorted(application_names - set(reviewed))
    if unexpected:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unexpected safety-critical task: "
            f"{unexpected}"
        )
    policies = []
    for task_name in reviewed:
        task = task_registry[task_name]
        observed_name = _task_policy_attribute(task, task_name, "name")
        if observed_name != task_name:
            raise ConfigurationFingerprintError(
                f"{CONFIG_PROJECTION_INCOMPLETE}: task name mismatch for {task_name}"
            )
        retry = {
            "max_retries": _canonical_task_number(
                _task_policy_attribute(task, task_name, "max_retries"),
                task_name=task_name,
                field_name="retry.max_retries",
                integer=True,
            ),
            "default_retry_delay": _canonical_task_number(
                _task_policy_attribute(task, task_name, "default_retry_delay"),
                task_name=task_name,
                field_name="retry.default_retry_delay",
            ),
        }
        limits = {
            "soft": _canonical_task_number(
                _task_policy_attribute(task, task_name, "soft_time_limit"),
                task_name=task_name,
                field_name="task.soft_time_limit_seconds",
            ),
            "hard": _canonical_task_number(
                _task_policy_attribute(task, task_name, "time_limit"),
                task_name=task_name,
                field_name="task.time_limit_seconds",
            ),
        }
        acknowledgements = {}
        for attribute in (
            "acks_late",
            "reject_on_worker_lost",
            "acks_on_failure_or_timeout",
        ):
            value = _task_policy_attribute(task, task_name, attribute)
            if not isinstance(value, bool):
                raise ConfigurationFingerprintError(
                    f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported effective policy type "
                    f"for {task_name}: task.{attribute}"
                )
            acknowledgements[attribute] = value
        route = {
            key: _canonical_task_route(
                _task_route_value(task_name, task, attribute, celery_config),
                task_name=task_name,
            )
            for key, attribute in (
                ("queue", "queue"),
                ("exchange", "exchange"),
                ("routing_key", "routing_key"),
            )
        }
        policies.append(
            {
                "task_name": task_name,
                "policy": {
                    "retry": retry,
                    "time_limits": limits,
                    "acknowledgements": acknowledgements,
                    "route": route,
                },
            }
        )
    return {
        "schema_version": TASK_POLICY_PROJECTION_SCHEMA_VERSION,
        "reviewed_task_names": list(reviewed),
        "policy_fields": list(TASK_POLICY_FIELDS),
        "routing_table": _canonical_task_routes(celery_config.get("task_routes", {})),
        "policies": policies,
    }


def collect_worker_configuration(
    settings_object: Any,
    celery_config: Mapping[str, Any],
    *,
    worker_runtime: Any | None = None,
    operational_values: Mapping[str, Any] | None = None,
    task_registry: Mapping[str, Any] | None = None,
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
    task_policy_projection = build_effective_task_policy_projection(
        task_registry,
        celery_config,
    )
    maintenance_identity = {
        name: entry.get("task", "")
        for name, entry in sorted(schedule.items())
        if isinstance(entry, Mapping)
    }
    cli_path = getattr(settings_object, "OPENCLAW_CLI_PATH", None)
    cli_args = _safe_cli_args(getattr(settings_object, "OPENCLAW_CLI_ARGS", ""))
    broker_route = _safe_route_identity(
        celery_config.get("broker_url")
        or getattr(settings_object, "CELERY_BROKER_URL", None),
        field_name="celery.broker_route_identity",
    )
    result_backend_route = _safe_route_identity(
        celery_config.get("result_backend")
        or getattr(settings_object, "CELERY_RESULT_BACKEND", None),
        field_name="celery.result_backend_route_identity",
    )
    gateway_route = _safe_route_identity(
        getattr(settings_object, "OPENCLAW_GATEWAY_URL", None),
        field_name="openclaw.gateway_route",
    )
    policy_timeout_values = {
        name: getattr(settings_object, name, None)
        for name in (
            "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
            "REPLAN_SYNTHESIS_TIMEOUT_SECONDS",
            "PLANNING_REPAIR_TIMEOUT_SECONDS",
            "DEBUG_FEEDBACK_TIMEOUT_SECONDS",
            "SUMMARY_TIMEOUT_SECONDS",
            "COMPLETION_REPAIR_TIMEOUT_SECONDS",
            "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
        )
    }
    task_decorator_limits = operational.get("task.decorator_time_limits")
    d37_provider_route = _safe_route_identity(
        operational.get("d37.binding.provider_base_url_identity"),
        field_name="d37.binding.provider_base_url_identity",
    )
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
        "celery.broker_route_identity": broker_route,
        "celery.result_backend_route_identity": result_backend_route,
        "retry.max_retries": operational.get("retry.max_retries"),
        "retry.countdown_seconds": operational.get("retry.countdown_seconds"),
        "retry.redelivery_policy": operational.get("retry.redelivery_policy"),
        "task.decorator_time_limits": task_decorator_limits,
        "policy.timeout_policy": policy_timeout_values,
        "maintenance.task_identity": maintenance_identity,
        "openclaw.gateway_route": gateway_route,
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
        "d37.binding.provider_base_url_identity": d37_provider_route,
        "d37.binding.model_selection": operational.get("d37.binding.model_selection"),
        "d37.binding.auth_schema_identity": operational.get(
            "d37.binding.auth_schema_identity"
        ),
        "beat.schedule": schedule_identity,
        "task.effective_policies": task_policy_projection,
    }
    missing_effective = [
        key
        for key in sorted(REQUIRED_EFFECTIVE_CONFIGURATION_KEYS)
        if values.get(key) is None
    ]
    if missing_effective:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: effective values unavailable: "
            f"{missing_effective}"
        )
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
    configuration_schema_version: int = CONFIGURATION_PROJECTION_SCHEMA_VERSION
    configuration_policy_id: str = CONFIGURATION_POLICY_ID

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
            "configuration_schema_version",
            "configuration_policy_id",
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
            configuration_schema_version=int(value["configuration_schema_version"]),
            configuration_policy_id=str(value["configuration_policy_id"]),
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
    projection_schema = projection.get("schema_version")
    projection_policy = projection.get("projection_policy_id")
    if projection_schema != CONFIGURATION_PROJECTION_SCHEMA_VERSION:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported projection schema"
        )
    if projection_policy != CONFIGURATION_POLICY_ID:
        raise ConfigurationFingerprintError(
            f"{CONFIG_PROJECTION_INCOMPLETE}: unsupported projection policy"
        )
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
        if _configuration_pin_reasons(candidate_pin):
            status = IDENTITY_INCONCLUSIVE
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
        configuration_schema_version=int(projection_schema),
        configuration_policy_id=str(projection_policy),
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
    current_configuration_schema_version: int | None = None,
    current_configuration_policy_id: str | None = None,
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
    configuration_pin_failures: list[str] = []
    if candidate_pin is None:
        reasons.append("candidate_pin_unavailable")
    else:
        configuration_pin_failures = _configuration_pin_reasons(candidate_pin)
        reasons.extend(configuration_pin_failures)
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
            if record.get("load_timing") in {
                "dynamic_unresolved",
                "dynamic_literal_unloaded",
            }:
                reasons.append(f"dynamic_module_provenance_unavailable:{name}")
            elif record.get("load_timing") == "lazy_or_unloaded":
                reasons.append(f"lazy_module_provenance_unavailable:{name}")
            else:
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
            origin_text = record.get("spec_origin")
            if not isinstance(origin_text, str) or not origin_text:
                reasons.append(f"module_import_origin_unavailable:{name}")
            else:
                try:
                    origin = Path(origin_text).expanduser().resolve(strict=True)
                except (OSError, ValueError):
                    reasons.append(f"module_import_origin_unavailable:{name}")
                else:
                    if origin != expected_path:
                        reasons.append(f"module_import_origin_mismatch:{name}")
        except (OSError, CandidateManifestError, ValueError):
            reasons.append(f"wrong_module_path:{name}")

    if current_module_provenance is not None:
        current_by_name = {
            str(item.get("module_name")): item for item in current_module_provenance
        }
        if set(current_by_name) != set(expected_by_name):
            reasons.append("current_module_provenance_drift")
        for name, record in current_by_name.items():
            if (
                not record.get("resolved_source_path")
                and record.get("load_timing") == "lazy_or_unloaded"
            ):
                reasons.append(f"current_lazy_module_provenance_unavailable:{name}")
                continue
            if not record.get("resolved_source_path") and record.get("load_timing") in {
                "dynamic_unresolved",
                "dynamic_literal_unloaded",
            }:
                reasons.append(f"current_dynamic_module_provenance_unavailable:{name}")
                continue
            if record.get("resolved_source_path") != expected_by_name.get(name, {}).get(
                "expected_source_path"
            ):
                reasons.append(f"current_module_path_mismatch:{name}")
            if record.get("source_sha256") != expected_by_name.get(name, {}).get(
                "expected_source_sha256"
            ):
                reasons.append(f"current_module_hash_mismatch:{name}")
            origin_text = record.get("spec_origin")
            expected_path = expected_by_name.get(name, {}).get("expected_source_path")
            if not isinstance(origin_text, str) or not origin_text:
                reasons.append(f"current_module_import_origin_unavailable:{name}")
            else:
                try:
                    origin = str(Path(origin_text).expanduser().resolve(strict=True))
                except (OSError, ValueError):
                    reasons.append(f"current_module_import_origin_unavailable:{name}")
                else:
                    if origin != expected_path:
                        reasons.append(f"current_module_import_origin_mismatch:{name}")
    else:
        # The boot record plus file/import provenance is independently useful,
        # but no external verifier can claim a full in-memory attestation.
        reasons.append("in_memory_code_attestation_unavailable")

    if current_configuration_fingerprint is None:
        reasons.append("configuration_fingerprint_unavailable")
    elif current_configuration_fingerprint != boot.configuration_fingerprint:
        reasons.append("configuration_fingerprint_mismatch")
    if current_configuration_schema_version is None:
        reasons.append("configuration_schema_unavailable")
    elif current_configuration_schema_version != boot.configuration_schema_version:
        reasons.append("configuration_schema_mismatch")
    if current_configuration_policy_id is None:
        reasons.append("configuration_policy_unavailable")
    elif current_configuration_policy_id != boot.configuration_policy_id:
        reasons.append("configuration_policy_mismatch")
    if candidate_pin is not None and not configuration_pin_failures:
        if (
            boot.configuration_schema_version
            != candidate_pin.configuration_schema_version
        ):
            reasons.append("approved_configuration_boot_schema_mismatch")
        if boot.configuration_policy_id != candidate_pin.configuration_policy_id:
            reasons.append("approved_configuration_boot_policy_mismatch")
        if boot.configuration_fingerprint != candidate_pin.configuration_fingerprint:
            reasons.append("approved_configuration_boot_digest_mismatch")
        if current_configuration_fingerprint != candidate_pin.configuration_fingerprint:
            reasons.append("approved_configuration_current_digest_mismatch")
        if current_configuration_schema_version != (
            candidate_pin.configuration_schema_version
        ):
            reasons.append("approved_configuration_current_schema_mismatch")
        if current_configuration_policy_id != candidate_pin.configuration_policy_id:
            reasons.append("approved_configuration_current_policy_mismatch")

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
        "module_import_origin_mismatch",
        "current_module_import_origin_mismatch",
        "boot_identity_status:IDENTITY_MISMATCH",
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
    "BOUNDARY_COMPLETENESS_INCONCLUSIVE",
    "BOUNDARY_COMPLETENESS_INVALID",
    "BOUNDARY_COMPLETENESS_NOT_APPLICABLE",
    "BOUNDARY_COMPLETENESS_VALID",
    "CandidateManifestError",
    "CandidateManifestVerification",
    "CandidateIdentityPin",
    "CandidateSourceManifest",
    "CONFIG_PROJECTION_INCOMPLETE",
    "CONFIGURATION_POLICY_ID",
    "CONFIGURATION_PROJECTION_SCHEMA_VERSION",
    "ConfigurationFingerprintError",
    "DEFAULT_AFFECTED_MODULES",
    "DEFAULT_WORKER_IDENTITY_DIR",
    "EXPECTED_TASK_REGISTRATIONS",
    "IDENTITY_INCONCLUSIVE",
    "IDENTITY_MISMATCH",
    "IDENTITY_UNAVAILABLE",
    "IDENTITY_VERIFIED",
    "IdentityVerification",
    "MODULE_IDENTITY_INVALID",
    "MODULE_IDENTITY_VALID",
    "ManifestWriteError",
    "ModuleSourceRecord",
    "NON_SECRET_CONFIGURATION_KEYS",
    "ProcessIdentity",
    "ProvenanceBoundaryCheck",
    "REQUIRED_EFFECTIVE_CONFIGURATION_KEYS",
    "SCHEMA_VERSION",
    "TASK_POLICY_FIELDS",
    "TASK_POLICY_PROJECTION_SCHEMA_VERSION",
    "WorkerBootIdentityManifest",
    "WorkerIdentityError",
    "build_candidate_source_manifest",
    "build_effective_task_policy_projection",
    "build_non_secret_configuration_fingerprint",
    "capture_child_identities",
    "capture_loaded_module_provenance",
    "inspect_worker_provenance_boundary",
    "capture_process_identity",
    "canonical_launcher_identity",
    "configuration_pin_reasons",
    "collect_worker_configuration",
    "create_worker_boot_identity",
    "load_candidate_source_manifest",
    "process_identity_from_mapping",
    "sha256_file",
    "verify_candidate_source_manifest",
    "verify_worker_identity",
    "write_candidate_source_manifest",
]
