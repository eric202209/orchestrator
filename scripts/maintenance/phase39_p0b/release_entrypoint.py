"""Protected, isolated entry point for the Phase 39 P0B operator commands."""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

_INITIAL_RELEASE_ROOT = Path(__file__).absolute().parents[3]
sys.path.insert(0, str(_INITIAL_RELEASE_ROOT))
from scripts.maintenance.phase39_p0b import release_manager as releases


APPROVAL_ROOT = Path("/etc/orchestrator-maintenance/phase39-p0b/approvals")
WORKSHEET_ROOT = Path("/etc/orchestrator-maintenance/phase39-p0b/worksheets")
TRUSTED_KEY = Path("/etc/orchestrator-maintenance/phase39-p0b/approval-ed25519.pub")
OPENSSL = Path("/usr/bin/openssl")
APPROVED_PIN_DIR = Path("/root/.orchestrator/worker-pin")
APPROVED_PIN_PATH = APPROVED_PIN_DIR / "candidate-pin.json"
STALE_APPROVAL_IDS = frozenset({"P39-P0B-GATEB-001", "P39-P0B-GATEB-002"})
APPROVAL_SCHEMA_VERSION = 1
WORKER_APPROVAL_ENVELOPE_KIND = "orchestrator-worker-release-approval/v2"
WORKER_APPROVAL_SUBJECT_KIND = "orchestrator-phase39-p0b-worker-release-promotion-approval"
WORKER_APPROVAL_PURPOSE = "worker-release-promotion"
WORKER_APPROVAL_ACTION = "promote-worker-release"
WORKER_APPROVAL_SCHEMA_VERSION = 2
WORKER_APPROVAL_MAX_BYTES = 128 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_ID_RE = re.compile(r"[0-9a-f]{40}\Z")
WORKER_APPROVAL_ID_RE = re.compile(r"P39-P0B-WR2-[A-Z0-9][A-Z0-9-]{4,62}\Z")


class EntryPointError(RuntimeError):
    """A protected execution prerequisite failed."""


def _read_protected_file(path: Path, *, uid: int = 0) -> tuple[bytes, os.stat_result]:
    releases.validate_parent_directories(path.parent, uid=uid)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise EntryPointError(f"protected_file_open_failed:{path.name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1 or info.st_mode & 0o222:
            raise EntryPointError(f"protected_file_owner_mode_or_link_invalid:{path.name}")
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _parse_utc(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise EntryPointError(f"approval_{field}_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EntryPointError(f"approval_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise EntryPointError(f"approval_{field}_not_utc")
    return parsed.astimezone(dt.timezone.utc)


def _verify_signature(approval_path: Path, signature_path: Path, key_path: Path) -> None:
    if not OPENSSL.is_absolute():
        raise EntryPointError("openssl_path_invalid")
    openssl_hash, openssl_info = releases._hash_regular_file(OPENSSL)
    if openssl_info.st_uid != 0 or openssl_info.st_mode & 0o022 or not os.access(OPENSSL, os.X_OK):
        raise EntryPointError("openssl_unprotected")
    # The digest is checked by the signed release dependency inventory too;
    # this immediate check rejects a substituted verifier before use.
    del openssl_hash
    try:
        result = subprocess.run(
            [str(OPENSSL), "pkeyutl", "-verify", "-pubin", "-inkey", str(key_path), "-rawin", "-in", str(approval_path), "-sigfile", str(signature_path)],
            cwd="/",
            env={"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EntryPointError("approval_signature_verifier_failed") from exc
    if result.returncode != 0:
        raise EntryPointError("approval_signature_invalid")


def validate_operator_approval(
    approval: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    required = {
        "approval_kind", "schema_version", "approval_id", "operator_identity",
        "valid_from", "valid_until", "source_head", "source_tree",
        "release_id", "release_manifest_sha256", "launcher_identity",
        "runtime_dependency_inventory_sha256", "source_manifest_sha256",
        "configuration_fingerprint", "worksheet_sha256", "identity_policy",
    }
    if set(approval) != required:
        raise EntryPointError("approval_fields_incomplete_or_unknown")
    if (
        approval["approval_kind"] != "orchestrator-phase39-p0b-operator-approval"
        or type(approval["schema_version"]) is not int
        or approval["schema_version"] != APPROVAL_SCHEMA_VERSION
    ):
        raise EntryPointError("approval_schema_unsupported")
    approval_id = approval["approval_id"]
    if not isinstance(approval_id, str) or not re.fullmatch(r"P39-P0B-[A-Z0-9][A-Z0-9-]{5,63}", approval_id):
        raise EntryPointError("approval_id_invalid")
    if approval_id in STALE_APPROVAL_IDS:
        raise EntryPointError("historical_approval_invalid_for_release")
    if not isinstance(approval["operator_identity"], str) or not approval["operator_identity"].strip():
        raise EntryPointError("operator_identity_missing")
    start = _parse_utc(approval["valid_from"], "valid_from")
    end = _parse_utc(approval["valid_until"], "valid_until")
    instant = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if end <= start or instant < start or instant >= end:
        raise EntryPointError("approval_outside_validity_period")
    if not GIT_ID_RE.fullmatch(approval["source_head"]) or not GIT_ID_RE.fullmatch(approval["source_tree"]):
        raise EntryPointError("approval_source_identity_invalid")
    for key in ("release_manifest_sha256", "runtime_dependency_inventory_sha256", "source_manifest_sha256", "configuration_fingerprint", "worksheet_sha256"):
        if not isinstance(approval[key], str) or not SHA256_RE.fullmatch(approval[key]):
            raise EntryPointError(f"approval_{key}_invalid")
    if (
        approval["release_id"] != manifest["release_id"]
        or approval["source_head"] != manifest["source"]["head"]
        or approval["source_tree"] != manifest["source"]["tree"]
        or approval["release_manifest_sha256"] != manifest_sha256
        or approval["launcher_identity"] != manifest["launcher_identity"]
        or approval["runtime_dependency_inventory_sha256"] != manifest["runtime_dependency_inventory_sha256"]
        or approval["source_manifest_sha256"] != manifest["source_manifest_sha256"]
        or approval["configuration_fingerprint"] != manifest["configuration_fingerprint"]
    ):
        raise EntryPointError("approval_release_binding_mismatch")
    policy = approval["identity_policy"]
    policy_fields = {
        "configuration_schema_version", "configuration_policy_id", "worker_node_identity",
        "affected_module_count", "affected_module_names",
    }
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise EntryPointError("approval_identity_policy_invalid")
    if (
        type(policy["configuration_schema_version"]) is not int
        or policy["configuration_schema_version"] <= 0
        or not isinstance(policy["configuration_policy_id"], str)
        or not policy["configuration_policy_id"]
        or not isinstance(policy["worker_node_identity"], str)
        or not policy["worker_node_identity"]
        or type(policy["affected_module_count"]) is not int
        or policy["affected_module_count"] <= 0
        or not isinstance(policy["affected_module_names"], list)
        or len(policy["affected_module_names"]) != policy["affected_module_count"]
        or not all(isinstance(item, str) and item for item in policy["affected_module_names"])
        or len(set(policy["affected_module_names"])) != len(policy["affected_module_names"])
    ):
        raise EntryPointError("approval_identity_policy_invalid")
    return dict(approval)


def _worker_approval_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EntryPointError("worker_approval_duplicate_json_field")
        result[key] = value
    return result


def _parse_worker_approval_json(raw: bytes, *, label: str) -> dict[str, Any]:
    if not raw or len(raw) > WORKER_APPROVAL_MAX_BYTES:
        raise EntryPointError(f"worker_approval_{label}_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_worker_approval_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                EntryPointError("worker_approval_nonstandard_number")
            ),
        )
    except EntryPointError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EntryPointError(f"worker_approval_{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise EntryPointError(f"worker_approval_{label}_root_invalid")
    from scripts.maintenance.phase39_p0b import release_packaging

    try:
        canonical = release_packaging._worker_canonical_json(value)
    except release_packaging.PackagingError as exc:
        raise EntryPointError(f"worker_approval_{label}_value_invalid") from exc
    if canonical != raw:
        raise EntryPointError(f"worker_approval_{label}_noncanonical")
    return value


def _validate_worker_release_v2_subject(
    subject: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_raw: bytes,
    now: dt.datetime | None,
) -> dict[str, Any]:
    required = {
        "approval_kind",
        "schema_version",
        "approval_id",
        "operator_identity",
        "purpose",
        "action",
        "valid_from",
        "valid_until",
        "manifest_kind",
        "manifest_schema_version",
        "release_id",
        "execution_artifact_digest",
        "release_manifest_sha256",
        "source_head",
        "source_tree",
        "source_manifest_digest",
        "payload_inventory_digest",
        "platform_target",
        "configuration_policy_id",
        "configuration_profile_reference",
    }
    if set(subject) != required:
        raise EntryPointError("worker_approval_subject_fields_incomplete_or_unknown")
    if (
        subject["approval_kind"] != WORKER_APPROVAL_SUBJECT_KIND
        or type(subject["schema_version"]) is not int
        or subject["schema_version"] != WORKER_APPROVAL_SCHEMA_VERSION
        or subject["manifest_kind"] != "worker-release/v2"
        or type(subject["manifest_schema_version"]) is not int
        or subject["manifest_schema_version"] != 2
    ):
        raise EntryPointError("worker_approval_subject_schema_unsupported")
    if (
        subject["purpose"] != WORKER_APPROVAL_PURPOSE
        or subject["action"] != WORKER_APPROVAL_ACTION
    ):
        raise EntryPointError("worker_approval_purpose_or_action_mismatch")
    approval_id = subject["approval_id"]
    if not isinstance(approval_id, str) or not WORKER_APPROVAL_ID_RE.fullmatch(approval_id):
        raise EntryPointError("worker_approval_id_invalid")
    operator_identity = subject["operator_identity"]
    if (
        not isinstance(operator_identity, str)
        or not operator_identity.strip()
        or len(operator_identity) > 256
    ):
        raise EntryPointError("worker_approval_operator_identity_invalid")
    start = _parse_utc(subject["valid_from"], "valid_from")
    end = _parse_utc(subject["valid_until"], "valid_until")
    if end <= start:
        raise EntryPointError("worker_approval_validity_window_invalid")
    instant = now or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() != dt.timedelta(0):
        raise EntryPointError("worker_approval_verification_time_not_utc")
    instant = instant.astimezone(dt.timezone.utc)
    if instant < start:
        raise EntryPointError("worker_approval_not_yet_valid")
    if instant >= end:
        raise EntryPointError("worker_approval_expired")

    for field in (
        "source_head",
        "source_tree",
    ):
        if not isinstance(subject[field], str) or not GIT_ID_RE.fullmatch(subject[field]):
            raise EntryPointError(f"worker_approval_{field}_invalid")
    for field in (
        "execution_artifact_digest",
        "release_manifest_sha256",
        "source_manifest_digest",
        "payload_inventory_digest",
    ):
        if not isinstance(subject[field], str) or not SHA256_RE.fullmatch(subject[field]):
            raise EntryPointError(f"worker_approval_{field}_invalid")

    source = manifest["source"]
    configuration = manifest["configuration"]
    bindings = {
        "manifest_kind": manifest["manifest_kind"],
        "manifest_schema_version": manifest["schema_version"],
        "release_id": manifest["release_id"],
        "execution_artifact_digest": manifest["execution_artifact_digest"],
        "release_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "source_head": source["head"],
        "source_tree": source["tree"],
        "source_manifest_digest": source["source_manifest_digest"],
        "payload_inventory_digest": manifest["payload_inventory_digest"],
        "platform_target": manifest["platform_target"],
        "configuration_policy_id": configuration["policy_id"],
        "configuration_profile_reference": configuration["profile_reference"],
    }
    if any(subject[field] != expected for field, expected in bindings.items()):
        raise EntryPointError("worker_approval_release_binding_mismatch")
    return dict(subject)


def verify_worker_release_v2_approval(
    manifest: Mapping[str, Any],
    *,
    manifest_raw: bytes,
    envelope_raw: bytes,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Verify a v2 promotion approval using the existing fixed Ed25519 anchor.

    This proves the signature and its bounded promotion subject. It does not
    consume a durable one-time operation claim or authorize activation.
    """
    from scripts.maintenance.phase39_p0b import release_packaging

    release_packaging._validate_worker_release_manifest(manifest)
    if release_packaging._worker_canonical_json(manifest) != manifest_raw:
        raise EntryPointError("worker_approval_manifest_not_canonical")
    envelope = _parse_worker_approval_json(envelope_raw, label="envelope")
    envelope_fields = {
        "envelope_kind",
        "schema_version",
        "key_id",
        "signed_subject",
        "signature",
    }
    if set(envelope) != envelope_fields:
        raise EntryPointError("worker_approval_envelope_fields_incomplete_or_unknown")
    if (
        envelope["envelope_kind"] != WORKER_APPROVAL_ENVELOPE_KIND
        or type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != WORKER_APPROVAL_SCHEMA_VERSION
    ):
        raise EntryPointError("worker_approval_envelope_schema_unsupported")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", key_id):
        raise EntryPointError("worker_approval_key_id_invalid")
    encoded_subject = envelope["signed_subject"]
    encoded_signature = envelope["signature"]
    if not isinstance(encoded_subject, str) or not isinstance(encoded_signature, str):
        raise EntryPointError("worker_approval_envelope_encoding_invalid")
    try:
        subject_raw = base64.b64decode(encoded_subject, validate=True)
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise EntryPointError("worker_approval_envelope_encoding_invalid") from exc
    if (
        base64.b64encode(subject_raw).decode("ascii") != encoded_subject
        or base64.b64encode(signature).decode("ascii") != encoded_signature
        or len(signature) != 64
    ):
        raise EntryPointError("worker_approval_envelope_encoding_invalid")
    subject = _parse_worker_approval_json(subject_raw, label="subject")
    approval = _validate_worker_release_v2_subject(
        subject,
        manifest=manifest,
        manifest_raw=manifest_raw,
        now=now,
    )

    trusted_key, _key_info = _read_protected_file(TRUSTED_KEY)
    expected_key_id = "sha256:" + hashlib.sha256(trusted_key).hexdigest()
    if key_id != expected_key_id:
        raise EntryPointError("worker_approval_unknown_key_id")

    # Keep the verifier and trust anchor unchanged; private temporary copies
    # avoid passing attacker-controlled paths to OpenSSL.
    with tempfile.TemporaryDirectory(prefix="p39-worker-approval-") as directory:
        temporary = Path(directory)
        subject_path = temporary / "subject.json"
        signature_path = temporary / "subject.sig"
        key_path = temporary / "trusted.pub"
        for path, content in (
            (subject_path, subject_raw),
            (signature_path, signature),
            (key_path, trusted_key),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(content)
                os.fsync(fd)
            finally:
                os.close(fd)
        _verify_signature(subject_path, signature_path, key_path)

    return {
        "signature_verified": True,
        "promotion_approval_verified": True,
        "approval_id": approval["approval_id"],
        "operator_identity": approval["operator_identity"],
        "approval_valid_from": approval["valid_from"],
        "approval_valid_until": approval["valid_until"],
        "approval_purpose": WORKER_APPROVAL_PURPOSE,
        "approval_action": WORKER_APPROVAL_ACTION,
        "key_id": key_id,
        "durable_one_time_claim_consumed": False,
    }


def _release_root() -> Path:
    script = Path(__file__)
    root = script.parents[3]
    if not root.is_absolute():
        raise EntryPointError("release_root_not_absolute")
    return root


def validate_package_activation_manifest(
    manifest: Mapping[str, Any], *, release_id: str
) -> None:
    """Reject an R1 candidate before any operator action can be selected."""
    if (
        manifest.get("manifest_kind") != "orchestrator-phase39-p0b-deb"
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("activation_status") != "ACTIVATABLE"
    ):
        raise EntryPointError("release_package_not_activatable")
    package = manifest.get("package")
    if not isinstance(package, dict) or package.get("release_id") != release_id:
        raise EntryPointError("release_package_identity_mismatch")


def _approval_paths(release_id: str, approval_root: Path = APPROVAL_ROOT) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}", release_id):
        raise EntryPointError("release_id_invalid")
    path = approval_root / f"{release_id}.approval.json"
    return path, path.with_suffix(path.suffix + ".sig")


def _configure_tooling(module: Any, approval: Mapping[str, Any], root: Path) -> None:
    policy_data = approval["identity_policy"]
    module.APPROVED_SOURCE_ROOT = str(root)
    module.APPROVED_HEAD = approval["source_head"]
    module.APPROVED_TREE = approval["source_tree"]
    module.APPROVED_SOURCE_MANIFEST_DIGEST = approval["source_manifest_sha256"]
    module.APPROVED_CONFIGURATION_FINGERPRINT = approval["configuration_fingerprint"]
    module.APPROVED_CONFIGURATION_SCHEMA_VERSION = policy_data["configuration_schema_version"]
    module.APPROVED_CONFIGURATION_POLICY_ID = policy_data["configuration_policy_id"]
    module.APPROVED_WORKER_NODE = policy_data["worker_node_identity"]
    module.APPROVED_OPERATOR = approval["operator_identity"]
    module.APPROVED_APPROVAL_ID = approval["approval_id"]
    module.APPROVED_WORKSHEET_SHA256 = approval["worksheet_sha256"]
    module.APPROVED_MODULE_COUNT = policy_data["affected_module_count"]
    module.APPROVED = module.ApprovedIdentity(
        source_root=str(root),
        candidate_head=approval["source_head"],
        candidate_tree=approval["source_tree"],
        source_manifest_digest=approval["source_manifest_sha256"],
        configuration_fingerprint=approval["configuration_fingerprint"],
        configuration_schema_version=policy_data["configuration_schema_version"],
        configuration_policy_id=policy_data["configuration_policy_id"],
        worker_node_identity=policy_data["worker_node_identity"],
        operator_identity=approval["operator_identity"],
        approval_id=approval["approval_id"],
        worksheet_sha256=approval["worksheet_sha256"],
        affected_module_count=policy_data["affected_module_count"],
        affected_module_names=tuple(policy_data["affected_module_names"]),
    )


def _validate_operator_arguments(command: str, arguments: list[str]) -> None:
    blocked = {"--source-root", "--runtime-root"}
    if command == "install":
        blocked.update({"--worksheet", "--pin-dir"})
    else:
        blocked.add("--pin-path")
    if any(
        option == blocked_option
        or (len(option) > 2 and blocked_option.startswith(option))
        for argument in arguments
        for option in (argument.split("=", 1)[0],)
        for blocked_option in blocked
    ):
        raise EntryPointError("operator_source_runtime_worksheet_or_pin_override_rejected")


def invoke_after_release_verification(
    release_root: Path,
    *,
    expected_manifest_sha256: str,
    expected_source_head: str,
    expected_source_tree: str,
    expected_launcher_identity: Mapping[str, Any],
    action: Any,
    uid: int = 0,
) -> Any:
    """Call a privileged action only after the whole release is reverified."""
    verified = releases.verify_installed_release(
        release_root,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_head=expected_source_head,
        expected_source_tree=expected_source_tree,
        expected_launcher_identity=expected_launcher_identity,
        uid=uid,
    )
    return action(verified)


def execute(
    argv: list[str] | None = None,
    *,
    approval_root: Path = APPROVAL_ROOT,
    trusted_key: Path = TRUSTED_KEY,
    openssl: Path = OPENSSL,
    now: dt.datetime | None = None,
) -> int:
    """Verify the protected release and approval before importing operator code."""
    if sys.flags.isolated != 1:
        raise EntryPointError("python_isolated_mode_required")
    if os.geteuid() != 0:
        raise EntryPointError("protected_operator_entrypoint_requires_root")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "refresh", "gate-a"))
    parser.add_argument("operator_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    root = _release_root()
    if root.parent != releases.PROTECTED_INSTALLATION_PARENT:
        raise EntryPointError("release_root_not_in_fixed_apt_installation_path")
    package_manifest_raw, _ = _read_protected_file(
        root / releases.PACKAGE_MANIFEST_NAME
    )
    try:
        package_manifest = json.loads(package_manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EntryPointError("release_package_manifest_invalid") from exc
    if (
        not isinstance(package_manifest, dict)
        or releases.canonical_json(package_manifest) != package_manifest_raw
    ):
        raise EntryPointError("release_package_manifest_not_canonical")
    validate_package_activation_manifest(package_manifest, release_id=root.name)
    manifest_path = root / releases.MANIFEST_NAME
    manifest_raw, _ = _read_protected_file(manifest_path)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EntryPointError("release_manifest_invalid") from exc
    if not isinstance(manifest, dict) or releases.canonical_json(manifest) != manifest_raw:
        raise EntryPointError("release_manifest_not_canonical")
    approval_path, signature_path = _approval_paths(manifest.get("release_id", ""), approval_root)
    approval_raw, _ = _read_protected_file(approval_path)
    signature_raw, _ = _read_protected_file(signature_path)
    key_raw, _ = _read_protected_file(trusted_key)
    try:
        approval = json.loads(approval_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EntryPointError("approval_json_invalid") from exc
    if not isinstance(approval, dict) or releases.canonical_json(approval) != approval_raw:
        raise EntryPointError("approval_not_canonical")
    if not signature_raw or len(signature_raw) > 4096 or not key_raw:
        raise EntryPointError("approval_signature_or_key_missing")
    if openssl != OPENSSL:
        raise EntryPointError("openssl_path_must_be_fixed")
    # Authenticate the approval with the fixed OS trust root first.  Do not
    # select an executable from an unauthenticated release manifest.
    _verify_signature(approval_path, signature_path, trusted_key)
    digest = hashlib.sha256(manifest_raw).hexdigest()
    approval = validate_operator_approval(approval, manifest=manifest, manifest_sha256=digest, now=now)
    dependency = next((item for item in manifest.get("runtime_dependencies", []) if isinstance(item, dict) and item.get("name") == "openssl"), None)
    if not dependency or dependency.get("kind") != "system-file" or dependency.get("path") != str(OPENSSL):
        raise EntryPointError("openssl_missing_from_runtime_inventory")
    openssl_digest, openssl_info = releases._hash_regular_file(OPENSSL)
    if openssl_info.st_uid != 0 or openssl_info.st_mode & 0o022 or not os.access(OPENSSL, os.X_OK):
        raise EntryPointError("openssl_unprotected")
    if openssl_digest != dependency.get("sha256"):
        raise EntryPointError("openssl_runtime_dependency_changed")
    worksheet_path = WORKSHEET_ROOT / f"{manifest['release_id']}.worksheet.json"
    worksheet_raw, _ = _read_protected_file(worksheet_path)
    if hashlib.sha256(worksheet_raw).hexdigest() != approval["worksheet_sha256"]:
        raise EntryPointError("approved_worksheet_digest_mismatch")
    launcher = manifest.get("launcher_identity")

    # Isolated mode has no caller PYTHONPATH, user site, or sitecustomize.
    # Add only the verified immutable release and its inventoried dependency
    # directory after the whole release has passed ownership/hash checks.
    def import_verified_operator(verified: Mapping[str, Any]) -> Any:
        if verified["release_id"] != manifest["release_id"]:
            raise EntryPointError("installed_release_identity_mismatch")
        root_text = str(root)
        sys.path.insert(0, root_text)
        for dependency_item in manifest["runtime_dependencies"]:
            if dependency_item["name"] == "python_dependencies" and dependency_item["kind"] == "release-tree":
                # Direct insertion avoids processing .pth files as executable
                # Python startup configuration. Their presence is rejected by
                # the release manifest validator.
                sys.path.append(str(root / dependency_item["path"]))
        os.environ.clear()
        os.environ.update({"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        os.chdir(root)
        return importlib.import_module("scripts.maintenance.phase39_p0b.operator_tooling")

    module = invoke_after_release_verification(
        root,
        expected_manifest_sha256=digest,
        expected_source_head=approval["source_head"],
        expected_source_tree=approval["source_tree"],
        expected_launcher_identity=launcher,
        action=import_verified_operator,
    )
    _configure_tooling(module, approval, root)
    _validate_operator_arguments(args.command, args.operator_args)
    fixed_args = ["--source-root", str(root), "--runtime-root", "/root/.orchestrator"]
    if args.command == "install":
        fixed_args.extend(
            ["--worksheet", str(worksheet_path), "--pin-dir", str(APPROVED_PIN_DIR)]
        )
    else:
        fixed_args.extend(["--pin-path", str(APPROVED_PIN_PATH)])
    sys.argv = [str(root / "scripts/maintenance/phase39_p0b/operator_tooling.py"), args.command, *fixed_args, *args.operator_args]
    return int(module._cli())


def main() -> int:
    try:
        return execute()
    except (EntryPointError, releases.ReleaseError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
