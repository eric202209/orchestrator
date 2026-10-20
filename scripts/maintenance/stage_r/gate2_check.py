#!/usr/bin/env python3
"""Read-only Stage R Gate 2 boot identity comparison."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"


def _check(
    status: str, reasons: Sequence[str], fields: Sequence[str]
) -> dict[str, Any]:
    return {
        "check_id": "gate2_worker_identity",
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "differing_fields": sorted(set(fields)),
    }


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _worker_identity_module():
    sys.dont_write_bytecode = True
    from app.services.orchestration import worker_identity as wi

    return wi


def _compare_fields(
    expected: Mapping[str, Any] | None,
    actual: Mapping[str, Any] | None,
    fields: Sequence[str],
    *,
    prefix: str = "",
) -> list[str]:
    if expected is None or actual is None:
        return [prefix.rstrip(".") or "identity_fields"]
    return [
        f"{prefix}{name}" for name in fields if expected.get(name) != actual.get(name)
    ]


def _canonical_launcher(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        wi = _worker_identity_module()
        return wi.canonical_launcher_identity(value)
    except Exception:
        return None


def _pin_projection(pin: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "candidate_head",
        "candidate_tree",
        "source_root",
        "source_manifest_digest",
        "worker_node_identity",
        "configuration_schema_version",
        "configuration_fingerprint",
        "configuration_policy_id",
    )
    result = {name: pin.get(name) for name in fields}
    result["launcher_identity"] = _canonical_launcher(pin.get("launcher_identity"))
    return result


def evaluate_gate2(
    *,
    worksheet: Any,
    pin: Mapping[str, Any] | None,
    pin_identity: Mapping[str, Any] | None,
    pin_status: str | None,
    manifest: Mapping[str, Any] | None,
    source_identity: Mapping[str, Any] | None,
    source_classification: str | None,
    live_process: Mapping[str, Any] | None,
    process_complete: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    fields: list[str] = []
    unavailable = False
    if not isinstance(worksheet, Mapping):
        unavailable = True
        reasons.append("approved_worksheet_unavailable")
    if pin is None or pin_identity is None:
        if pin_status == "PIN_ABSENT":
            reasons.append("pin_absent")
        elif pin_status:
            reasons.append("candidate_pin_invalid")
        else:
            unavailable = True
            reasons.append("candidate_pin_unavailable")
    if not isinstance(manifest, Mapping):
        unavailable = True
        reasons.append("boot_manifest_unavailable")
    if not isinstance(source_identity, Mapping):
        unavailable = True
        reasons.append("worker_source_identity_unavailable")
    if source_classification is None:
        unavailable = True
        reasons.append("worker_source_verification_unavailable")
    elif source_classification != "IDENTITY_VERIFIED":
        reasons.append("worker_source_identity_mismatch")
        fields.append("worker_source_identity")
    if not process_complete or not isinstance(live_process, Mapping):
        unavailable = True
        reasons.append("worker_process_identity_unavailable")

    if isinstance(worksheet, Mapping):
        if (
            worksheet.get("worksheet_kind")
            != "orchestrator-worker-candidate-pin-worksheet"
        ):
            reasons.append("worksheet_schema_invalid")
            fields.append("worksheet_kind")
        if worksheet.get("worksheet_schema_version") != 1:
            reasons.append("worksheet_schema_invalid")
            fields.append("worksheet_schema_version")
        if worksheet.get("approval_state") != "APPROVED":
            reasons.append("worksheet_not_approved")
            fields.append("approval_state")
        approved_source = _mapping(worksheet.get("OPERATOR_APPROVED_SOURCE"))
        if approved_source is None:
            reasons.append("worksheet_approval_source_missing")
            fields.append("OPERATOR_APPROVED_SOURCE")

    if isinstance(pin, Mapping) and isinstance(worksheet, Mapping):
        proposed = _mapping(worksheet.get("proposed_pin_body"))
        if proposed is None:
            reasons.append("worksheet_pin_projection_missing")
            fields.append("proposed_pin_body")
        else:
            pin_projection = _pin_projection(pin)
            proposed_projection = _pin_projection(proposed)
            for name in (
                "candidate_head",
                "candidate_tree",
                "source_root",
                "source_manifest_digest",
                "worker_node_identity",
                "configuration_schema_version",
                "configuration_fingerprint",
                "configuration_policy_id",
            ):
                if proposed_projection[name] != pin_projection[name]:
                    fields.append(name)
            proposed_launcher = proposed_projection["launcher_identity"]
            actual_launcher = pin_projection["launcher_identity"]
            if proposed_launcher is None or actual_launcher is None:
                fields.append("launcher_identity")
            else:
                for name in ("launcher", "executable", "cwd", "argv"):
                    if proposed_launcher.get(name) != actual_launcher.get(name):
                        fields.append(f"launcher_identity.{name}")
        approved_source = _mapping(worksheet.get("OPERATOR_APPROVED_SOURCE"))
        if approved_source is not None:
            approved_fields = (
                "candidate_head",
                "candidate_tree",
                "source_manifest_digest",
            )
            fields.extend(
                _compare_fields(
                    approved_source,
                    pin,
                    approved_fields,
                    prefix="OPERATOR_APPROVED_SOURCE.",
                )
            )

    if isinstance(source_identity, Mapping):
        source_fields = (
            "candidate_head",
            "candidate_tree",
            "source_root",
            "source_manifest_digest",
        )
        if isinstance(pin, Mapping):
            fields.extend(
                _compare_fields(
                    pin, source_identity, source_fields, prefix="worker_source."
                )
            )
        if isinstance(manifest, Mapping):
            boot_source = {
                "candidate_head": manifest.get("candidate_head"),
                "candidate_tree": manifest.get("candidate_tree"),
                "source_root": manifest.get("candidate_source_root"),
                "source_manifest_digest": manifest.get("source_manifest_digest"),
            }
            fields.extend(
                _compare_fields(
                    source_identity, boot_source, source_fields, prefix="boot_manifest."
                )
            )
        if isinstance(worksheet, Mapping):
            observed_source = _mapping(worksheet.get("OBSERVED_SOURCE"))
            fields.extend(
                _compare_fields(
                    observed_source,
                    source_identity,
                    source_fields,
                    prefix="OBSERVED_SOURCE.",
                )
            )
            if observed_source is not None:
                expected_launcher = _canonical_launcher(
                    observed_source.get("digest_launcher_identity")
                )
                current_launcher = _canonical_launcher(
                    source_identity.get("launcher_identity")
                )
                if expected_launcher is None or current_launcher is None:
                    fields.append("OBSERVED_SOURCE.digest_launcher_identity")
                else:
                    for name in ("launcher", "executable", "cwd", "argv"):
                        if expected_launcher.get(name) != current_launcher.get(name):
                            fields.append(
                                f"OBSERVED_SOURCE.digest_launcher_identity.{name}"
                            )

    if isinstance(manifest, Mapping):
        status = manifest.get("identity_status")
        pin_state = manifest.get("candidate_pin_status")
        if status is None:
            unavailable = True
            reasons.append("boot_identity_status_missing")
        elif status != "IDENTITY_VERIFIED":
            reasons.append("boot_identity_not_verified")
            fields.append("identity_status")
        if pin_state is None:
            unavailable = True
            reasons.append("boot_pin_status_missing")
        elif pin_state != "PIN_MATCHED":
            reasons.append("boot_pin_not_matched")
            fields.append("candidate_pin_status")
        boot_reasons = manifest.get("identity_status_reasons")
        if not isinstance(boot_reasons, list):
            unavailable = True
            reasons.append("boot_identity_reasons_unavailable")
        elif boot_reasons:
            reasons.append("boot_identity_reasons_present")
            fields.append("identity_status_reasons")

        if isinstance(pin, Mapping):
            pin_fields = {
                "candidate_head": pin.get("candidate_head"),
                "candidate_tree": pin.get("candidate_tree"),
                "source_root": pin.get("source_root"),
                "source_manifest_digest": pin.get("source_manifest_digest"),
                "worker_node_identity": pin.get("worker_node_identity"),
                "configuration_fingerprint": pin.get("configuration_fingerprint"),
                "configuration_schema_version": pin.get("configuration_schema_version"),
                "configuration_policy_id": pin.get("configuration_policy_id"),
            }
            boot_fields = {
                "candidate_head": manifest.get("candidate_head"),
                "candidate_tree": manifest.get("candidate_tree"),
                "source_root": manifest.get("candidate_source_root"),
                "source_manifest_digest": manifest.get("source_manifest_digest"),
                "worker_node_identity": manifest.get("worker_node_identity"),
                "configuration_fingerprint": manifest.get("configuration_fingerprint"),
                "configuration_schema_version": manifest.get(
                    "configuration_schema_version"
                ),
                "configuration_policy_id": manifest.get("configuration_policy_id"),
            }
            for name in pin_fields:
                if pin_fields[name] != boot_fields[name]:
                    fields.append(name)
            if isinstance(pin_identity, Mapping):
                observed_pin = _mapping(manifest.get("candidate_pin_identity"))
                if observed_pin is None:
                    unavailable = True
                    reasons.append("boot_pin_identity_unavailable")
                    fields.append("candidate_pin_identity")
                else:
                    for name in (
                        "pin_sha256",
                        "candidate_head",
                        "candidate_tree",
                        "source_manifest_digest",
                        "operator_identity",
                        "approval_id",
                        "approved_at",
                        "expires_at",
                    ):
                        if observed_pin.get(name) != pin_identity.get(name):
                            fields.append(f"candidate_pin_identity.{name}")

        projection = _mapping(manifest.get("configuration_projection"))
        if projection is None:
            unavailable = True
            reasons.append("configuration_projection_unavailable")
            fields.append("configuration_projection")
        else:
            wi = _worker_identity_module()

            projection_checks = {
                "schema_version": manifest.get("configuration_schema_version"),
                "projection_policy_id": manifest.get("configuration_policy_id"),
                "identity_scope": wi.IDENTITY_SCOPE,
                "invocation_identity": wi.INVOCATION_IDENTITY,
                "invocation_identity_reason": wi.INVOCATION_IDENTITY_REASON,
            }
            for name, expected in projection_checks.items():
                if projection.get(name) != expected:
                    fields.append(f"configuration_projection.{name}")
            if isinstance(pin, Mapping) and isinstance(worksheet, Mapping):
                configuration = _mapping(worksheet.get("configuration_identity"))
                if configuration is None:
                    fields.append("configuration_identity")
                else:
                    for name in (
                        "configuration_fingerprint",
                        "configuration_schema_version",
                        "configuration_policy_id",
                    ):
                        if configuration.get(name) != pin.get(name):
                            fields.append(f"configuration_identity.{name}")

        if isinstance(source_identity, Mapping):
            source_modules = {
                str(item.get("module_name")): item
                for item in source_identity.get("resolved_modules", ())
                if isinstance(item, Mapping)
            }
            boot_modules = manifest.get("affected_module_provenance")
            if not isinstance(boot_modules, list):
                unavailable = True
                reasons.append("affected_module_provenance_unavailable")
                fields.append("affected_module_provenance")
            else:
                boot_names = {
                    str(item.get("module_name"))
                    for item in boot_modules
                    if isinstance(item, Mapping)
                }
                if boot_names != set(source_modules):
                    fields.append("affected_module_provenance.module_set")
                for item in boot_modules:
                    if not isinstance(item, Mapping):
                        fields.append("affected_module_provenance.record")
                        continue
                    name = str(item.get("module_name", ""))
                    source_module = source_modules.get(name)
                    if source_module is None:
                        continue
                    for boot_field, source_field in (
                        ("expected_source_path", "expected_source_path"),
                        ("expected_source_sha256", "source_sha256"),
                        ("source_sha256", "source_sha256"),
                        ("resolved_source_path", "expected_source_path"),
                        ("spec_origin", "expected_source_path"),
                    ):
                        if item.get(boot_field) != source_module.get(source_field):
                            fields.append(
                                f"affected_module_provenance.{name}.{boot_field}"
                            )
                    if item.get("loaded") is not True:
                        fields.append(f"affected_module_provenance.{name}.loaded")

        if isinstance(live_process, Mapping):
            process_fields = {
                "worker_pid": "pid",
                "linux_boot_id": "linux_boot_id",
                "worker_start_ticks": "start_ticks",
                "worker_executable": "executable",
                "worker_cwd": "cwd",
                "worker_argv": "argv",
            }
            for boot_name, process_name in process_fields.items():
                if manifest.get(boot_name) != live_process.get(process_name):
                    fields.append(boot_name)
            if isinstance(pin, Mapping):
                try:
                    wi = _worker_identity_module()
                    process = wi.process_identity_from_mapping(live_process)
                    launcher_reasons = wi._launcher_process_reasons(
                        pin.get("launcher_identity", {}), process, "gate2_"
                    )
                    for reason in launcher_reasons:
                        if reason.endswith("_mismatch"):
                            fields.append(
                                "launcher_identity."
                                + reason.removeprefix("gate2_").removesuffix(
                                    "_mismatch"
                                )
                            )
                        else:
                            fields.append("launcher_identity")
                except Exception:
                    unavailable = True
                    reasons.append("launcher_identity_verification_unavailable")

    if fields and not any(
        reason in reasons for reason in ("candidate_pin_invalid", "pin_absent")
    ):
        reasons.append("identity_fields_differ")
    if reasons and not fields and not unavailable:
        fields.append("identity_status")
    if any(
        reason in reasons
        for reason in (
            "pin_absent",
            "candidate_pin_invalid",
            "boot_identity_not_verified",
            "boot_pin_not_matched",
            "boot_identity_reasons_present",
            "worker_source_identity_mismatch",
            "identity_fields_differ",
            "worksheet_not_approved",
            "worksheet_schema_invalid",
        )
    ):
        status = FAIL
    elif unavailable:
        status = INCONCLUSIVE
    elif reasons or fields:
        status = FAIL
    else:
        status = PASS
    return {
        "schema_version": 1,
        "tool": "stage-r-gate2-check",
        "overall": status,
        "checks": [_check(status, reasons, fields)],
    }


def _read_json(path: str | Path, max_bytes: int) -> Any:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise OSError("input_file_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        raise OSError("input_file_too_large")
    return json.loads(raw.decode("utf-8"))


def _load_gate2_inputs(args: argparse.Namespace) -> dict[str, Any]:
    wi = _worker_identity_module()

    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    tool_root = Path(__file__).absolute().parents[3]
    if args.source_root is None or Path(os.path.abspath(args.source_root)) != tool_root:
        raise ValueError("source_root_not_tool_checkout")
    worksheet = _read_json(args.worksheet, 256 * 1024)
    raw_manifest = _read_json(args.manifest, 16 * 1024 * 1024)
    boot = wi.WorkerBootIdentityManifest.from_dict(raw_manifest)
    manifest = boot.to_dict()
    pin_load = wi.load_candidate_identity_pin(
        args.pin,
        source_root=args.source_root,
        runtime_root=args.runtime_root,
    )
    pin = None
    pin_identity = pin_load.identity
    if pin_load.pin is not None:
        source_pin = pin_load.pin
        pin = {
            "candidate_head": source_pin.candidate_head,
            "candidate_tree": source_pin.candidate_tree,
            "source_root": source_pin.source_root,
            "source_manifest_digest": source_pin.source_manifest_digest,
            "launcher_identity": dict(source_pin.launcher_identity),
            "worker_node_identity": source_pin.worker_node_identity,
            "configuration_schema_version": source_pin.configuration_schema_version,
            "configuration_fingerprint": source_pin.configuration_fingerprint,
            "configuration_policy_id": source_pin.configuration_policy_id,
        }
    observed_source = _mapping(worksheet.get("OBSERVED_SOURCE"))
    if observed_source is None or not isinstance(
        observed_source.get("digest_launcher_identity"), Mapping
    ):
        raise ValueError("worksheet_source_identity_unavailable")
    source = wi.build_candidate_source_manifest(
        args.source_root,
        launcher_identity=observed_source["digest_launcher_identity"],
    )
    verification = wi.verify_candidate_source_manifest(
        source,
        expected_head=pin.get("candidate_head") if pin else None,
        expected_tree=pin.get("candidate_tree") if pin else None,
        expected_source_root=pin.get("source_root") if pin else None,
        expected_source_manifest_digest=(
            pin.get("source_manifest_digest") if pin else None
        ),
    )
    live = wi.capture_process_identity(boot.worker_pid)
    return {
        "worksheet": worksheet,
        "pin": pin,
        "pin_identity": pin_identity,
        "pin_status": pin_load.status,
        "manifest": manifest,
        "source_identity": source.to_dict(),
        "source_classification": verification.classification,
        "live_process": live.to_dict() if live is not None else None,
        "process_complete": live is not None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Stage R Gate 2 identity evidence read-only."
    )
    parser.add_argument("--worksheet", type=Path)
    parser.add_argument("--pin", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args(argv)
    try:
        values = _load_gate2_inputs(args)
        result = evaluate_gate2(**values)
    except Exception:
        result = {
            "schema_version": 1,
            "tool": "stage-r-gate2-check",
            "overall": INCONCLUSIVE,
            "checks": [_check(INCONCLUSIVE, ["gate2_evidence_unavailable"], [])],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return {PASS: 0, FAIL: 1, INCONCLUSIVE: 2}[result["overall"]]


if __name__ == "__main__":
    raise SystemExit(main())
