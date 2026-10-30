"""Fresh Worker identity verification for the isolated development runtime.

The persisted boot manifest is only a starting point. Every controlled
dispatch rechecks the current pin, source tree, manifest bytes, process
identity, and the caller's dispatch-to-Worker binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from app.services.orchestration import worker_identity as wi


CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY = "_orchestrator_controlled_worker_identity_v1"
CONTROLLED_WORKER_IDENTITY_BINDING_SCHEMA = 1


class ControlledWorkerIdentityError(RuntimeError):
    """The controlled Worker has no fresh, verified identity binding."""


@dataclass(frozen=True)
class ControlledWorkerIdentityEvidence:
    process_ready: bool
    identity_verified: bool
    identity_status: str
    reasons: tuple[str, ...] = ()
    binding: Mapping[str, Any] | None = None
    manifest: wi.WorkerBootIdentityManifest | None = None
    manifest_digest: str | None = None


def binding_matches(
    expected: Mapping[str, Any] | None, actual: Mapping[str, Any]
) -> bool:
    """Compare the complete versioned binding carried with a dispatch."""

    if not isinstance(expected, Mapping):
        return False
    return dict(expected) == dict(actual)


def _failure(
    reason: str,
    *,
    process_ready: bool = False,
    status: str = wi.IDENTITY_INCONCLUSIVE,
) -> ControlledWorkerIdentityEvidence:
    return ControlledWorkerIdentityEvidence(
        process_ready=process_ready,
        identity_verified=False,
        identity_status=status,
        reasons=(reason,),
    )


def _fresh_source_matches_pin(
    *,
    source_root: Path,
    pin: wi.CandidateIdentityPin,
    manifest: wi.WorkerBootIdentityManifest,
) -> tuple[bool, tuple[str, ...]]:
    names = tuple(
        sorted(
            {
                str(item["module_name"])
                for item in manifest.affected_module_provenance
                if isinstance(item, Mapping)
                and isinstance(item.get("module_name"), str)
            }
        )
    )
    if not names:
        return False, ("boot_manifest_module_scope_missing",)
    candidate, reasons = verify_candidate_pin_source(
        source_root=source_root,
        pin=pin,
        affected_modules=names,
    )
    if candidate is None:
        return False, reasons
    if (
        candidate.candidate_head != manifest.candidate_head
        or candidate.candidate_tree != manifest.candidate_tree
        or candidate.source_root != manifest.candidate_source_root
        or candidate.source_manifest_digest != manifest.source_manifest_digest
    ):
        return False, ("boot_manifest_candidate_source_mismatch",)
    return True, ()


def verify_candidate_pin_source(
    *,
    source_root: str | Path,
    pin: wi.CandidateIdentityPin,
    affected_modules: tuple[str, ...] | None = None,
) -> tuple[wi.CandidateSourceManifest | None, tuple[str, ...]]:
    """Recompute the clean source identity represented by an approved pin."""

    root = Path(source_root).expanduser().resolve(strict=False)
    try:
        launcher = wi.worker_source_launcher_identity(
            root,
            executable=str(pin.launcher_identity["executable"]),
            argv=tuple(str(item) for item in pin.launcher_identity["argv"]),
        )
        candidate = wi.build_candidate_source_manifest(
            root,
            affected_modules=(affected_modules or wi.DEFAULT_AFFECTED_MODULES),
            launcher_identity=launcher,
            expected_head=pin.candidate_head,
            expected_tree=pin.candidate_tree,
        )
        verified = wi.verify_candidate_source_manifest(
            candidate,
            expected_head=pin.candidate_head,
            expected_tree=pin.candidate_tree,
            expected_source_root=str(root),
            expected_source_manifest_digest=pin.source_manifest_digest,
        )
    except Exception:
        return None, ("candidate_source_unavailable_or_dirty",)
    if verified.classification != wi.IDENTITY_VERIFIED:
        return None, tuple(verified.reasons) or ("candidate_source_unverified",)
    if (
        pin.source_root != str(root)
        or candidate.source_manifest_digest != pin.source_manifest_digest
    ):
        return None, ("candidate_pin_source_mismatch",)
    return candidate, ()


def _manifest_binding(
    manifest: wi.WorkerBootIdentityManifest, digest: str
) -> dict[str, Any]:
    pin_identity = manifest.candidate_pin_identity or {}
    return {
        "schema_version": CONTROLLED_WORKER_IDENTITY_BINDING_SCHEMA,
        "record_id": manifest.record_id,
        "manifest_sha256": digest,
        "worker_pid": manifest.worker_pid,
        "worker_start_ticks": manifest.worker_start_ticks,
        "linux_boot_id": manifest.linux_boot_id,
        "worker_node_identity": manifest.worker_node_identity,
        "candidate_head": manifest.candidate_head,
        "candidate_tree": manifest.candidate_tree,
        "source_manifest_digest": manifest.source_manifest_digest,
        "candidate_pin_sha256": pin_identity.get("pin_sha256"),
        "candidate_pin_approval_id": pin_identity.get("approval_id"),
    }


def verify_controlled_worker_identity(
    *,
    source_root: str | Path,
    runtime_root: str | Path,
    pin_path: str | Path | None,
    pin_protected_root: str | Path | None = None,
    expected_node_identity: str,
    expected_pid: int | None = None,
    expected_binding: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ControlledWorkerIdentityEvidence:
    """Revalidate one live Worker and optionally match a queued dispatch.

    A ready manifest is accepted only when it still belongs to the live PID
    and start time, the externally approved pin still has the same digest and
    is unexpired, and the current clean source reproduces the pinned digest.
    """

    try:
        paths = wi.resolve_worker_identity_paths(source_root, runtime_root)
    except Exception:
        return _failure("worker_identity_paths_unavailable")

    pin_load = wi.load_candidate_identity_pin(
        pin_path,
        source_root=paths.source_root,
        runtime_root=pin_protected_root or paths.runtime_root,
        require_outside_runtime=True,
        now=now,
    )
    if pin_load.status != wi.PIN_LOADED or pin_load.pin is None:
        reason = (
            pin_load.reasons[0] if pin_load.reasons else "candidate_pin_unavailable"
        )
        return _failure(reason)
    pin = pin_load.pin
    if pin.source_root != str(paths.source_root):
        return _failure(
            "candidate_pin_source_root_mismatch", status=wi.IDENTITY_MISMATCH
        )
    if pin.worker_node_identity != expected_node_identity:
        return _failure(
            "candidate_pin_worker_node_mismatch", status=wi.IDENTITY_MISMATCH
        )

    identity_dir = paths.identity_dir
    if identity_dir.is_symlink() or not identity_dir.is_dir():
        return _failure("worker_identity_manifest_directory_unavailable")

    candidates: list[tuple[wi.WorkerBootIdentityManifest, str, wi.ProcessIdentity]] = []
    rejected_reasons: list[str] = []
    for manifest_path in sorted(identity_dir.glob("worker-*.json")):
        if manifest_path.is_symlink():
            rejected_reasons.append("worker_boot_manifest_symlink")
            continue
        try:
            info = manifest_path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o022
                or info.st_uid not in {0, os.geteuid()}
            ):
                rejected_reasons.append("worker_boot_manifest_permissions_unsafe")
                continue
            raw = manifest_path.read_bytes()
            manifest = wi.WorkerBootIdentityManifest.from_dict(json.loads(raw))
        except Exception:
            rejected_reasons.append("worker_boot_manifest_invalid")
            continue
        if expected_pid is not None and manifest.worker_pid != int(expected_pid):
            continue
        if manifest.worker_node_identity != expected_node_identity:
            continue
        process = wi.capture_process_identity(manifest.worker_pid)
        if process is None:
            continue
        if (
            process.pid != manifest.worker_pid
            or process.linux_boot_id != manifest.linux_boot_id
            or process.start_ticks != manifest.worker_start_ticks
        ):
            rejected_reasons.append("worker_process_identity_changed")
            continue
        candidates.append((manifest, hashlib.sha256(raw).hexdigest(), process))

    if len(candidates) != 1:
        if len(candidates) > 1:
            return _failure("worker_identity_manifest_ambiguous", process_ready=True)
        reason = (
            rejected_reasons[0]
            if rejected_reasons
            else "worker_boot_manifest_unavailable_or_stale"
        )
        return _failure(reason)

    manifest, digest, process = candidates[0]
    if (
        manifest.record_id
        != f"{manifest.linux_boot_id}-{manifest.worker_pid}-{manifest.worker_start_ticks}"
        or manifest.worker_executable != process.executable
        or manifest.worker_cwd != process.cwd
        or tuple(manifest.worker_argv) != process.argv
    ):
        return _failure("worker_process_provenance_changed", process_ready=True)
    if manifest.identity_status != wi.IDENTITY_VERIFIED:
        return _failure(
            "worker_boot_identity_not_verified",
            process_ready=True,
            status=manifest.identity_status,
        )
    if manifest.candidate_pin_status != wi.PIN_MATCHED:
        return _failure("worker_boot_pin_not_matched", process_ready=True)
    if manifest.candidate_pin_identity != pin_load.identity:
        return _failure("candidate_pin_replaced_since_worker_boot", process_ready=True)
    if (
        manifest.configuration_fingerprint != pin.configuration_fingerprint
        or manifest.configuration_schema_version != pin.configuration_schema_version
        or manifest.configuration_policy_id != pin.configuration_policy_id
    ):
        return _failure("worker_boot_configuration_pin_mismatch", process_ready=True)

    source_matches, source_reasons = _fresh_source_matches_pin(
        source_root=paths.source_root,
        pin=pin,
        manifest=manifest,
    )
    if not source_matches:
        return ControlledWorkerIdentityEvidence(
            process_ready=True,
            identity_verified=False,
            identity_status=wi.IDENTITY_MISMATCH,
            reasons=source_reasons,
            manifest=manifest,
            manifest_digest=digest,
        )

    binding = _manifest_binding(manifest, digest)
    if expected_binding is not None and not binding_matches(expected_binding, binding):
        return ControlledWorkerIdentityEvidence(
            process_ready=True,
            identity_verified=False,
            identity_status=wi.IDENTITY_MISMATCH,
            reasons=("dispatch_worker_binding_mismatch",),
            binding=binding,
            manifest=manifest,
            manifest_digest=digest,
        )
    return ControlledWorkerIdentityEvidence(
        process_ready=True,
        identity_verified=True,
        identity_status=wi.IDENTITY_VERIFIED,
        binding=binding,
        manifest=manifest,
        manifest_digest=digest,
    )


def current_controlled_worker_identity_binding(
    *,
    source_root: str | Path,
    runtime_root: str | Path,
    pin_path: str | Path | None,
    pin_protected_root: str | Path | None,
    expected_node_identity: str,
    expected_binding: Mapping[str, Any] | None = None,
    require_responsive: bool = True,
) -> dict[str, Any]:
    """Return a fresh binding, optionally requiring an exact node ping.

    A publisher running in the Worker process itself does not ping the solo
    worker while it is occupied publishing a continuation. Its PID and boot
    manifest are checked directly. Other publishers require the configured
    Celery node to pong before authorizing dispatch.
    """

    if not expected_node_identity:
        raise ControlledWorkerIdentityError("controlled Worker node identity is absent")
    evidence = verify_controlled_worker_identity(
        source_root=source_root,
        runtime_root=runtime_root,
        pin_path=pin_path,
        pin_protected_root=pin_protected_root,
        expected_node_identity=expected_node_identity,
        expected_binding=expected_binding,
    )
    if (
        not evidence.identity_verified
        or evidence.binding is None
        or evidence.manifest is None
    ):
        reason = (
            evidence.reasons[0] if evidence.reasons else "worker_identity_unverified"
        )
        raise ControlledWorkerIdentityError(reason)
    if require_responsive and evidence.manifest.worker_pid != os.getpid():
        try:
            from app.celery_app import celery_app

            reply = (
                celery_app.control.inspect(
                    destination=[expected_node_identity], timeout=1.0
                ).ping()
                or {}
            )
        except Exception as exc:
            raise ControlledWorkerIdentityError(
                "controlled Worker is not responsive"
            ) from exc
        node_reply = (
            reply.get(expected_node_identity) if isinstance(reply, dict) else None
        )
        if not isinstance(node_reply, dict) or node_reply.get("ok") != "pong":
            raise ControlledWorkerIdentityError("controlled Worker is not responsive")
    return dict(evidence.binding)


__all__ = [
    "CONTROLLED_WORKER_IDENTITY_BINDING_SCHEMA",
    "CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY",
    "ControlledWorkerIdentityError",
    "ControlledWorkerIdentityEvidence",
    "binding_matches",
    "current_controlled_worker_identity_binding",
    "verify_candidate_pin_source",
    "verify_controlled_worker_identity",
]
