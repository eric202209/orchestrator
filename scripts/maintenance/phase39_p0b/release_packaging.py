"""Offline Debian packaging and signed APT repository checks for Phase 39 P0B.

The module creates release candidates only. It never creates a signing key,
installs a package, writes host APT configuration, or activates a service.
Production use must obtain repository signing keys through an independent
operator-controlled channel.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from . import release_manager


SCHEMA_VERSION = 2
MANIFEST_NAME = "release-package-manifest.json"
RELEASE_IDENTITY_NAME = ".orchestrator-release-identity.json"
PACKAGE_PREFIX = "orchestrator-phase39-p0b-"
PACKAGE_VERSION_PREFIX = "39.1.0+"
RELEASE_ROOT = PurePosixPath(
    "usr/lib/orchestrator-maintenance/phase39-p0b"
)
ARCHITECTURE = "arm64"
CODENAME = "noble"
ORIGIN = "Orchestrator-P39-P0B"
APT_COMPONENT = "main"
APT_INDEX = "main/binary-arm64/Packages"
DEPENDS = "bash, libc6 (>= 2.39), python3.12-minimal (>= 3.12.3~)"
GIT_ID_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
EMPTY_SOURCE_DELTA_SHA256 = hashlib.sha256(b"").hexdigest()
SOURCE_SNAPSHOT_KIND = "git-archive-committed-clean"
RELEASE_ID_RE = re.compile(r"r1-[a-f0-9]{16}-[a-f0-9]{16}\Z")
WORKER_RELEASE_ID_RE = re.compile(r"wr2-[a-f0-9]{64}\Z")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,79}\Z")
VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.+:~\-]*\Z")
DPKG_DEB = Path("/usr/bin/dpkg-deb")
DPKG_SCANPACKAGES = Path("/usr/bin/dpkg-scanpackages")
GPG = Path("/usr/bin/gpg")
GPGV = Path("/usr/bin/gpgv")
WORKER_RELEASE_MANIFEST = "release.json"
WORKER_RELEASE_SIGNATURE = "release.json.sig"
WORKER_RELEASE_KIND = "worker-release/v2"
WORKER_RELEASE_SCHEMA_VERSION = 2
WORKER_RELEASE_STATE_ROLES = frozenset(
    {
        "database",
        "runtime",
        "workspace",
        "checkpoints",
        "candidate_content",
        "worker_logs",
        "openclaw_state",
        "redis_data",
        "qdrant_data",
    }
)
WORKER_RELEASE_PROVENANCE_ROLES = {
    "runtime_lock": "runtime_lock_digest",
    "wheel_inventory": "wheel_inventory_digest",
    "native_extension_inventory": "native_extension_inventory_digest",
    "elf_closure": "elf_closure_digest",
    "import_origin_inventory": "import_origin_inventory_digest",
}


class PackagingError(RuntimeError):
    """A package, repository, or trust-policy validation failure."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _worker_canonical_json(value: Any) -> bytes:
    """Encode the worker-release identity using its closed canonical rules."""

    def reject_floats(item: Any) -> None:
        if isinstance(item, float):
            raise PackagingError("worker_manifest_float_forbidden")
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise PackagingError("worker_manifest_object_key_invalid")
            for child in item.values():
                reject_floats(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                reject_floats(child)
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise PackagingError("worker_manifest_value_type_invalid")

    reject_floats(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PackagingError("worker_manifest_canonical_encoding_invalid") from exc


def _worker_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackagingError("worker_manifest_duplicate_json_key")
        result[key] = value
    return result


def _worker_parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_worker_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                PackagingError("worker_manifest_nonstandard_number")
            ),
        )
    except PackagingError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError("worker_manifest_json_invalid") from exc
    if not isinstance(value, dict):
        raise PackagingError("worker_manifest_root_not_object")
    canonical = _worker_canonical_json(value)
    if canonical != raw:
        raise PackagingError("worker_manifest_bytes_not_canonical")
    return value


def _worker_exact_fields(
    value: Any, fields: set[str], *, label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PackagingError(f"worker_manifest_{label}_not_object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing:
        raise PackagingError(f"worker_manifest_{label}_missing_fields")
    if unknown:
        raise PackagingError(f"worker_manifest_{label}_unknown_fields")
    return value


def _worker_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PackagingError(f"worker_manifest_{label}_digest_invalid")
    return value


def _worker_identity_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"execution_artifact_digest", "release_id"}
    }


def build_worker_release_manifest(core: Mapping[str, Any]) -> dict[str, Any]:
    """Return a v2 manifest with deterministic inventory/artifact identities.

    This is an offline manifest finalizer, not a release builder or signer.
    """

    manifest = dict(core)
    if "execution_artifact_digest" in manifest or "release_id" in manifest:
        raise PackagingError("worker_manifest_self_identity_input_forbidden")
    entries = manifest.get("payload_inventory")
    if not isinstance(entries, list):
        raise PackagingError("worker_manifest_payload_inventory_invalid")
    manifest["payload_inventory_digest"] = sha256_bytes(
        _worker_canonical_json(entries)
    )
    digest = sha256_bytes(_worker_canonical_json(manifest))
    manifest["execution_artifact_digest"] = digest
    manifest["release_id"] = "wr2-" + digest
    _validate_worker_release_manifest(manifest)
    return manifest


def _validate_worker_release_manifest(manifest: Mapping[str, Any]) -> None:
    fields = {
        "manifest_kind",
        "schema_version",
        "source",
        "payload_inventory",
        "payload_inventory_digest",
        "platform_target",
        "python_runtime",
        "runtime_lock_digest",
        "wheel_inventory_digest",
        "native_extension_inventory_digest",
        "elf_closure_digest",
        "import_origin_inventory_digest",
        "launcher",
        "interpreter",
        "celery_entrypoint",
        "configuration",
        "state_path_roles",
        "build_provenance",
        "execution_artifact_digest",
        "release_id",
    }
    value = _worker_exact_fields(dict(manifest), fields, label="root")
    if value["manifest_kind"] != WORKER_RELEASE_KIND:
        raise PackagingError("worker_manifest_kind_unsupported")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != WORKER_RELEASE_SCHEMA_VERSION
    ):
        raise PackagingError("worker_manifest_schema_unsupported")

    source = _worker_exact_fields(
        value["source"], {"head", "tree", "source_manifest_digest"}, label="source"
    )
    if not isinstance(source["head"], str) or not GIT_ID_RE.fullmatch(source["head"]):
        raise PackagingError("worker_manifest_source_head_invalid")
    if not isinstance(source["tree"], str) or not GIT_ID_RE.fullmatch(source["tree"]):
        raise PackagingError("worker_manifest_source_tree_invalid")
    _worker_digest(source["source_manifest_digest"], label="source_manifest")

    platform = _worker_exact_fields(
        value["platform_target"],
        {"distribution", "release", "architecture", "libc_baseline"},
        label="platform_target",
    )
    if not all(isinstance(item, str) and item for item in platform.values()):
        raise PackagingError("worker_manifest_platform_target_invalid")
    runtime = _worker_exact_fields(
        value["python_runtime"],
        {"implementation", "version", "abi", "soabi"},
        label="python_runtime",
    )
    if not all(isinstance(item, str) and item for item in runtime.values()):
        raise PackagingError("worker_manifest_python_runtime_invalid")

    for field in (
        "payload_inventory_digest",
        "runtime_lock_digest",
        "wheel_inventory_digest",
        "native_extension_inventory_digest",
        "elf_closure_digest",
        "import_origin_inventory_digest",
        "execution_artifact_digest",
    ):
        _worker_digest(value[field], label=field)
    if not isinstance(value["release_id"], str) or not WORKER_RELEASE_ID_RE.fullmatch(
        value["release_id"]
    ):
        raise PackagingError("worker_manifest_release_id_invalid")

    for name in ("launcher", "celery_entrypoint"):
        item = _worker_exact_fields(value[name], {"path", "sha256"}, label=name)
        _safe_worker_path(item["path"])
        _worker_digest(item["sha256"], label=name)
    interpreter = _worker_exact_fields(
        value["interpreter"],
        {"path", "sha256", "link_policy", "symlink_chain"},
        label="interpreter",
    )
    _safe_worker_path(interpreter["path"])
    _worker_digest(interpreter["sha256"], label="interpreter")
    if (
        interpreter["link_policy"] != "copied-regular-file"
        or interpreter["symlink_chain"] != []
    ):
        raise PackagingError("worker_manifest_interpreter_link_policy_unsupported")
    executable_paths = {
        value["launcher"]["path"],
        value["interpreter"]["path"],
        value["celery_entrypoint"]["path"],
    }
    if len(executable_paths) != 3:
        raise PackagingError("worker_manifest_executable_paths_not_distinct")

    configuration = _worker_exact_fields(
        value["configuration"], {"policy_id", "profile_reference"}, label="configuration"
    )
    if not isinstance(configuration["policy_id"], str) or not configuration[
        "policy_id"
    ]:
        raise PackagingError("worker_manifest_configuration_policy_invalid")
    profile = _worker_exact_fields(
        configuration["profile_reference"], {"path", "sha256"}, label="profile_reference"
    )
    profile_path = profile["path"]
    if (
        not isinstance(profile_path, str)
        or not profile_path.startswith("/opt/orchestrator/worker-config/")
        or ".." in PurePosixPath(profile_path).parts
        or PurePosixPath(profile_path).as_posix() != profile_path
        or not profile_path.endswith("/profile.json")
    ):
        raise PackagingError("worker_manifest_profile_reference_invalid")
    _worker_digest(profile["sha256"], label="configuration_profile")

    roles = value["state_path_roles"]
    if not isinstance(roles, dict) or set(roles) != WORKER_RELEASE_STATE_ROLES:
        raise PackagingError("worker_manifest_state_roles_invalid")
    if any(item != f"state-role:{key}" for key, item in roles.items()):
        raise PackagingError("worker_manifest_state_role_reference_invalid")

    provenance = _worker_exact_fields(
        value["build_provenance"],
        {"builder_id", "recipe_digest", "toolchain_digest", "built_at_utc", "references"},
        label="build_provenance",
    )
    for field in ("builder_id", "built_at_utc"):
        if not isinstance(provenance[field], str) or not provenance[field]:
            raise PackagingError(f"worker_manifest_{field}_invalid")
    try:
        built_at = dt.datetime.fromisoformat(
            provenance["built_at_utc"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PackagingError("worker_manifest_build_time_invalid") from exc
    if built_at.tzinfo is None or built_at.utcoffset() != dt.timedelta(0):
        raise PackagingError("worker_manifest_build_time_invalid")
    _worker_digest(provenance["recipe_digest"], label="recipe")
    _worker_digest(provenance["toolchain_digest"], label="toolchain")
    references = provenance["references"]
    required_reference_roles = set(WORKER_RELEASE_PROVENANCE_ROLES) | {
        "source_manifest"
    }
    if not isinstance(references, dict) or set(references) != required_reference_roles:
        raise PackagingError("worker_manifest_provenance_references_invalid")
    for role in required_reference_roles:
        reference = _worker_exact_fields(
            references[role], {"path", "sha256"}, label=f"provenance_{role}"
        )
        _safe_worker_path(reference["path"])
        _worker_digest(reference["sha256"], label=f"provenance_{role}")
    for role, digest_field in WORKER_RELEASE_PROVENANCE_ROLES.items():
        if references[role]["sha256"] != value[digest_field]:
            raise PackagingError("worker_manifest_provenance_digest_mismatch")

    inventory = value["payload_inventory"]
    if not isinstance(inventory, list) or not inventory:
        raise PackagingError("worker_manifest_payload_inventory_empty")
    seen: set[str] = set()
    previous: bytes | None = None
    for entry in inventory:
        if not isinstance(entry, dict):
            raise PackagingError("worker_manifest_inventory_entry_invalid")
        kind = entry.get("type")
        fields_by_kind = {
            "file": {"path", "type", "mode", "size", "sha256"},
            "directory": {"path", "type", "mode", "size", "sha256"},
            "symlink": {"path", "type", "mode", "size", "sha256", "target"},
        }
        if kind not in fields_by_kind:
            raise PackagingError("worker_manifest_inventory_type_unsupported")
        _worker_exact_fields(entry, fields_by_kind[kind], label="inventory_entry")
        path = _safe_worker_path(entry["path"])
        path_text = path.as_posix()
        if path_text in seen:
            raise PackagingError("worker_manifest_duplicate_inventory_path")
        seen.add(path_text)
        encoded_path = path_text.encode("utf-8")
        if previous is not None and encoded_path <= previous:
            raise PackagingError("worker_manifest_inventory_not_bytewise_sorted")
        previous = encoded_path
        if type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o777:
            raise PackagingError("worker_manifest_inventory_mode_invalid")
        if kind != "symlink" and entry["mode"] & 0o222:
            raise PackagingError("worker_manifest_inventory_writable_mode")
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise PackagingError("worker_manifest_inventory_size_invalid")
        _worker_digest(entry["sha256"], label="payload_entry")
        if kind == "directory" and (
            entry["size"] != 0 or entry["sha256"] != sha256_bytes(b"")
        ):
            raise PackagingError("worker_manifest_directory_identity_invalid")
        if kind == "symlink":
            target = entry["target"]
            if not isinstance(target, str) or not target or "\\" in target or "\x00" in target:
                raise PackagingError("worker_manifest_symlink_target_invalid")
            target_bytes = target.encode("utf-8")
            if entry["size"] != len(target_bytes) or entry["sha256"] != sha256_bytes(
                target_bytes
            ):
                raise PackagingError("worker_manifest_symlink_identity_invalid")
        elif "target" in entry:
            raise PackagingError("worker_manifest_unexpected_symlink_target")

    by_path = {entry["path"]: entry for entry in inventory}
    for entry in inventory:
        parts = PurePosixPath(entry["path"]).parts
        for end in range(1, len(parts)):
            parent = "/".join(parts[:end])
            if parent not in by_path or by_path[parent]["type"] != "directory":
                raise PackagingError("worker_manifest_inventory_parent_invalid")

    if value["payload_inventory_digest"] != sha256_bytes(
        _worker_canonical_json(inventory)
    ):
        raise PackagingError("worker_manifest_payload_inventory_digest_mismatch")
    digest = sha256_bytes(_worker_canonical_json(_worker_identity_core(value)))
    if value["execution_artifact_digest"] != digest:
        raise PackagingError("worker_manifest_execution_artifact_digest_mismatch")
    if value["release_id"] != "wr2-" + digest:
        raise PackagingError("worker_manifest_release_id_digest_mismatch")


def _safe_worker_path(value: Any) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
    ):
        raise PackagingError("worker_manifest_path_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise PackagingError("worker_manifest_path_unsafe")
    return path


def _worker_open_release_root(path: str | Path) -> tuple[int, str]:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not os.path.isabs(raw):
        raise PackagingError("worker_release_path_must_be_absolute")
    if PurePosixPath(raw).as_posix() != raw.rstrip("/"):
        raise PackagingError("worker_release_path_not_normalized")
    parts = raw.split("/")
    if any(part in {".", ".."} for part in parts):
        raise PackagingError("worker_release_path_traversal")
    fd = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for component in (part for part in parts if part):
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise PackagingError("worker_release_root_not_directory")
        if stat.S_IMODE(info.st_mode) & 0o222:
            raise PackagingError("worker_release_root_writable")
        return fd, raw.rstrip("/") or "/"
    except Exception:
        os.close(fd)
        raise


def _worker_read_regular_at(
    root_fd: int,
    path: PurePosixPath,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, os.stat_result]:
    parent_fd = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            child_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        file_fd = os.open(
            path.parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PackagingError("worker_release_metadata_file_invalid")
            if stat.S_IMODE(before.st_mode) & 0o222:
                raise PackagingError("worker_release_metadata_file_writable")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(file_fd, 1024 * 1024)
                if not block:
                    break
                total += len(block)
                if max_bytes is not None and total > max_bytes:
                    raise PackagingError("worker_release_metadata_file_too_large")
                chunks.append(block)
            after = os.fstat(file_fd)
            if _worker_stat_identity(before) != _worker_stat_identity(after):
                raise PackagingError("worker_release_metadata_changed_during_read")
            return b"".join(chunks), after
        finally:
            os.close(file_fd)
    except PackagingError:
        raise
    except OSError as exc:
        raise PackagingError("worker_release_metadata_unavailable") from exc
    finally:
        os.close(parent_fd)


def _worker_stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _worker_inventory_release(root_fd: int) -> list[dict[str, Any]]:
    empty_digest = sha256_bytes(b"")
    entries: list[dict[str, Any]] = []
    root_before = os.fstat(root_fd)
    root_device = root_before.st_dev

    def visit(directory_fd: int, parent: str) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                names = sorted((entry.name for entry in iterator), key=lambda name: name.encode("utf-8"))
        except (OSError, UnicodeEncodeError) as exc:
            raise PackagingError("worker_release_directory_scan_failed") from exc
        for name in names:
            if not parent and name in {WORKER_RELEASE_MANIFEST, WORKER_RELEASE_SIGNATURE}:
                continue
            if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
                raise PackagingError("worker_release_entry_name_invalid")
            relative = f"{parent}/{name}" if parent else name
            _safe_worker_path(relative)
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise PackagingError("worker_release_entry_stat_failed") from exc
            if before.st_dev != root_device:
                raise PackagingError("worker_release_mount_boundary_rejected")
            mode = stat.S_IMODE(before.st_mode)
            if stat.S_ISDIR(before.st_mode):
                if mode & 0o222:
                    raise PackagingError("worker_release_directory_writable")
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if _worker_stat_identity(opened) != _worker_stat_identity(before):
                        raise PackagingError("worker_release_directory_changed_during_scan")
                    entries.append(
                        {
                            "path": relative,
                            "type": "directory",
                            "mode": mode,
                            "size": 0,
                            "sha256": empty_digest,
                        }
                    )
                    visit(child_fd, relative)
                    after = os.fstat(child_fd)
                    if _worker_stat_identity(opened) != _worker_stat_identity(after):
                        raise PackagingError("worker_release_directory_changed_during_scan")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise PackagingError("worker_release_hard_link_rejected")
                if mode & 0o222:
                    raise PackagingError("worker_release_file_writable")
                try:
                    file_fd = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise PackagingError("worker_release_file_open_failed") from exc
                try:
                    opened = os.fstat(file_fd)
                    if (
                        _worker_stat_identity(opened) != _worker_stat_identity(before)
                        or not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                    ):
                        raise PackagingError("worker_release_file_changed_during_scan")
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        block = os.read(file_fd, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                    after = os.fstat(file_fd)
                    if _worker_stat_identity(opened) != _worker_stat_identity(after):
                        raise PackagingError("worker_release_file_changed_during_scan")
                    entries.append(
                        {
                            "path": relative,
                            "type": "file",
                            "mode": mode,
                            "size": size,
                            "sha256": digest.hexdigest(),
                        }
                    )
                finally:
                    os.close(file_fd)
            elif stat.S_ISLNK(before.st_mode):
                try:
                    target = os.readlink(name, dir_fd=directory_fd)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise PackagingError("worker_release_symlink_unreadable") from exc
                if _worker_stat_identity(before) != _worker_stat_identity(after):
                    raise PackagingError("worker_release_symlink_changed_during_scan")
                target_bytes = target.encode("utf-8")
                entries.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "mode": mode,
                        "size": len(target_bytes),
                        "sha256": sha256_bytes(target_bytes),
                        "target": target,
                    }
                )
            else:
                raise PackagingError("worker_release_special_file_rejected")

    visit(root_fd, "")
    if _worker_stat_identity(root_before) != _worker_stat_identity(os.fstat(root_fd)):
        raise PackagingError("worker_release_root_changed_during_scan")
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return entries


def _worker_resolved_symlinks(inventory: list[Mapping[str, Any]]) -> None:
    by_path = {str(entry["path"]): entry for entry in inventory}

    def target_path(link_path: str, target: str) -> str:
        if target.startswith("/") or "\\" in target or "\x00" in target:
            raise PackagingError("worker_release_symlink_escape")
        stack = link_path.split("/")[:-1]
        for part in target.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not stack:
                    raise PackagingError("worker_release_symlink_escape")
                stack.pop()
            else:
                stack.append(part)
        if not stack:
            raise PackagingError("worker_release_symlink_target_invalid")
        return "/".join(stack)

    resolved: dict[str, str] = {}

    def resolve(path: str, trail: set[str]) -> str:
        if path in resolved:
            return resolved[path]
        if path in trail:
            raise PackagingError("worker_release_symlink_loop")
        entry = by_path.get(path)
        if entry is None:
            raise PackagingError("worker_release_symlink_broken")
        if entry["type"] != "symlink":
            resolved[path] = path
            return path
        destination = target_path(path, str(entry["target"]))
        result = resolve(destination, trail | {path})
        resolved[path] = result
        return result

    for path, entry in by_path.items():
        if entry["type"] == "symlink":
            resolve(path, set())


def _worker_validate_source_manifest(
    raw: bytes,
    *,
    source_head: str,
    source_tree: str,
    file_digest: str,
    identity_digest: str,
) -> dict[str, Any]:
    if sha256_bytes(raw) != file_digest:
        raise PackagingError("worker_release_source_manifest_digest_mismatch")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_worker_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                PackagingError("worker_source_manifest_nonstandard_number")
            ),
        )
    except PackagingError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError("worker_release_source_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise PackagingError("worker_release_source_manifest_invalid")
    try:
        if raw != json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8"):
            raise PackagingError("worker_release_source_manifest_not_canonical")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PackagingError("worker_release_source_manifest_invalid") from exc
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
    if set(value) != required:
        raise PackagingError("worker_release_source_manifest_schema_invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["candidate_head"] != source_head
        or value["candidate_tree"] != source_tree
        or value["dirty_source_policy"]
        != "tracked-and-untracked-source-must-be-clean"
    ):
        raise PackagingError("worker_release_source_manifest_source_mismatch")
    internal_digest = value["source_manifest_digest"]
    payload = {key: item for key, item in value.items() if key != "source_manifest_digest"}
    try:
        expected_internal = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PackagingError("worker_release_source_manifest_invalid") from exc
    if (
        internal_digest != expected_internal
        or internal_digest != identity_digest
        or not isinstance(internal_digest, str)
        or not SHA256_RE.fullmatch(internal_digest)
    ):
        raise PackagingError("worker_release_source_manifest_internal_digest_mismatch")
    if not isinstance(value["resolved_modules"], list) or not isinstance(
        value["affected_modules"], list
    ):
        raise PackagingError("worker_release_source_manifest_modules_invalid")
    if (
        not all(isinstance(item, str) and item for item in value["affected_modules"])
        or len(value["affected_modules"]) != len(set(value["affected_modules"]))
    ):
        raise PackagingError("worker_release_source_manifest_modules_invalid")
    resolved_names = [
        item.get("module_name") if isinstance(item, dict) else None
        for item in value["resolved_modules"]
    ]
    if resolved_names != value["affected_modules"]:
        raise PackagingError("worker_release_source_manifest_modules_mismatch")
    for module in value["resolved_modules"]:
        if (
            not isinstance(module, dict)
            or set(module) != {"module_name", "expected_source_path", "source_sha256"}
            or not isinstance(module["module_name"], str)
            or not isinstance(module["expected_source_path"], str)
            or not SHA256_RE.fullmatch(str(module["source_sha256"]))
        ):
            raise PackagingError("worker_release_source_manifest_module_invalid")
    launcher = value["launcher_identity"]
    if (
        not isinstance(launcher, dict)
        or not {"executable", "cwd", "argv"} <= set(launcher)
        or set(launcher) - {"executable", "cwd", "argv", "launcher"}
        or not isinstance(launcher.get("executable"), str)
        or not launcher["executable"]
        or not isinstance(launcher.get("cwd"), str)
        or not launcher["cwd"]
        or not isinstance(launcher.get("argv"), list)
        or not launcher["argv"]
        or not all(isinstance(item, str) and item for item in launcher["argv"])
    ):
        raise PackagingError("worker_release_source_manifest_launcher_invalid")
    return value


def verify_worker_release_integrity(
    release_path: str | Path,
    *,
    expected_source_head: str | None = None,
    expected_source_tree: str | None = None,
) -> dict[str, Any]:
    """Verify v2 manifest and payload bytes without executing or installing them."""

    root_fd, absolute_path = _worker_open_release_root(release_path)
    try:
        raw, _manifest_stat = _worker_read_regular_at(
            root_fd, PurePosixPath(WORKER_RELEASE_MANIFEST)
        )
        manifest = _worker_parse_manifest(raw)
        _validate_worker_release_manifest(manifest)
        if os.path.basename(absolute_path) != manifest["release_id"]:
            raise PackagingError("worker_release_directory_id_mismatch")
        source = manifest["source"]
        if expected_source_head is not None and source["head"] != expected_source_head:
            raise PackagingError("worker_release_expected_source_head_mismatch")
        if expected_source_tree is not None and source["tree"] != expected_source_tree:
            raise PackagingError("worker_release_expected_source_tree_mismatch")

        actual_inventory = _worker_inventory_release(root_fd)
        _worker_resolved_symlinks(actual_inventory)
        if actual_inventory != manifest["payload_inventory"]:
            raise PackagingError("worker_release_payload_inventory_mismatch")
        by_path = {item["path"]: item for item in actual_inventory}
        for name in ("launcher", "interpreter", "celery_entrypoint"):
            reference = manifest[name]
            entry = by_path.get(reference["path"])
            if (
                entry is None
                or entry["type"] != "file"
                or entry["sha256"] != reference["sha256"]
                or entry["mode"] & 0o111 == 0
            ):
                raise PackagingError(f"worker_release_{name}_identity_mismatch")
        for role, reference in manifest["build_provenance"]["references"].items():
            entry = by_path.get(reference["path"])
            if (
                entry is None
                or entry["type"] != "file"
                or entry["sha256"] != reference["sha256"]
            ):
                raise PackagingError(f"worker_release_provenance_reference_mismatch:{role}")
        source_reference = manifest["build_provenance"]["references"]["source_manifest"]
        source_raw, _ = _worker_read_regular_at(
            root_fd, _safe_worker_path(source_reference["path"])
        )
        source_manifest = _worker_validate_source_manifest(
            source_raw,
            source_head=source["head"],
            source_tree=source["tree"],
            file_digest=source_reference["sha256"],
            identity_digest=source["source_manifest_digest"],
        )
        source_root = source_manifest["source_root"]
        if (
            not isinstance(source_root, str)
            or not os.path.isabs(source_root)
            or ".." in PurePosixPath(source_root).parts
        ):
            raise PackagingError("worker_release_source_root_invalid")
        module_names: set[str] = set()
        for module in source_manifest["resolved_modules"]:
            module_name = module["module_name"]
            if module_name in module_names:
                raise PackagingError("worker_release_source_module_duplicate")
            module_names.add(module_name)
            module_path = module["expected_source_path"]
            if not os.path.isabs(module_path) or ".." in PurePosixPath(module_path).parts:
                raise PackagingError("worker_release_source_module_path_invalid")
            try:
                source_relative = PurePosixPath(module_path).relative_to(
                    PurePosixPath(source_root)
                )
            except ValueError as exc:
                raise PackagingError("worker_release_source_module_outside_root") from exc
            payload_path = PurePosixPath("source").joinpath(source_relative).as_posix()
            source_entry = by_path.get(payload_path)
            if (
                source_entry is None
                or source_entry["type"] != "file"
                or source_entry["sha256"] != module["source_sha256"]
            ):
                raise PackagingError("worker_release_source_module_digest_mismatch")

        signature_status = "MISSING"
        signature_result: dict[str, Any] = {
            "signature_verified": False,
            "promotion_approval_verified": False,
            "approval_purpose": None,
            "durable_one_time_claim_consumed": False,
        }
        try:
            signature_stat = os.stat(
                WORKER_RELEASE_SIGNATURE, dir_fd=root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            envelope_raw = None
        except OSError as exc:
            raise PackagingError("worker_release_signature_stat_failed") from exc
        else:
            if not stat.S_ISREG(signature_stat.st_mode) or signature_stat.st_nlink != 1:
                raise PackagingError("worker_release_signature_file_invalid")
            if stat.S_IMODE(signature_stat.st_mode) & 0o222:
                raise PackagingError("worker_release_signature_file_writable")
            envelope_raw, _signature_stat = _worker_read_regular_at(
                root_fd,
                PurePosixPath(WORKER_RELEASE_SIGNATURE),
                max_bytes=128 * 1024,
            )
        if envelope_raw is not None:
            from . import release_entrypoint

            try:
                signature_result = release_entrypoint.verify_worker_release_v2_approval(
                    manifest,
                    manifest_raw=raw,
                    envelope_raw=envelope_raw,
                )
            except release_entrypoint.EntryPointError as exc:
                return {
                    "status": "INTEGRITY_VERIFIED_SIGNATURE_REJECTED",
                    "integrity_verified": True,
                    "release_id": manifest["release_id"],
                    "execution_artifact_digest": manifest[
                        "execution_artifact_digest"
                    ],
                    "source_head": source["head"],
                    "source_tree": source["tree"],
                    "signature_status": "INVALID",
                    "signature_verified": False,
                    "promotion_approval_verified": False,
                    "approval_purpose": None,
                    "durable_one_time_claim_consumed": False,
                    "activation_eligible": False,
                    "runtime_closure_complete": False,
                    "mount_boundary_verified": False,
                    "storage_boundary_verified": False,
                    "worker_identity_verified": False,
                    "quiescence_verified": False,
                    "release_path": absolute_path,
                    "error": str(exc),
                }
            signature_status = "VERIFIED"
        if signature_status == "MISSING":
            status = "INTEGRITY_VERIFIED_UNSIGNED_REJECTED"
        else:
            status = "INTEGRITY_AND_PROMOTION_APPROVAL_VERIFIED"
        activation_eligible = False
        return {
            "status": status,
            "integrity_verified": True,
            "release_id": manifest["release_id"],
            "execution_artifact_digest": manifest["execution_artifact_digest"],
            "source_head": source["head"],
            "source_tree": source["tree"],
            "signature_status": signature_status,
            "activation_eligible": activation_eligible,
            **signature_result,
            "runtime_closure_complete": False,
            "mount_boundary_verified": False,
            "storage_boundary_verified": False,
            "worker_identity_verified": False,
            "quiescence_verified": False,
            "release_path": absolute_path,
        }
    finally:
        os.close(root_fd)


def verify_worker_release(
    release_path: str | Path,
    *,
    expected_source_head: str | None = None,
    expected_source_tree: str | None = None,
) -> dict[str, Any]:
    """Structured fail-closed verifier for a worker-release/v2 candidate."""

    try:
        return verify_worker_release_integrity(
            release_path,
            expected_source_head=expected_source_head,
            expected_source_tree=expected_source_tree,
        )
    except (PackagingError, OSError, ValueError, RecursionError) as exc:
        return {
            "status": "REJECTED",
            "integrity_verified": False,
            "signature_status": "NOT_CHECKED",
            "signature_verified": False,
            "promotion_approval_verified": False,
            "approval_purpose": None,
            "durable_one_time_claim_consumed": False,
            "activation_eligible": False,
            "runtime_closure_complete": False,
            "mount_boundary_verified": False,
            "storage_boundary_verified": False,
            "worker_identity_verified": False,
            "quiescence_verified": False,
            "error": str(exc),
        }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def package_name(release_id: str) -> str:
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise PackagingError("release_id_invalid")
    return PACKAGE_PREFIX + release_id


def package_version(release_id: str) -> str:
    package_name(release_id)
    return PACKAGE_VERSION_PREFIX + release_id


def _safe_relpath(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PackagingError("unsafe_package_path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackagingError("unsafe_package_path")
    if path.as_posix() != value:
        raise PackagingError("noncanonical_package_path")
    return path


def _validate_source_identity(
    source_head: str,
    source_tree: str,
    commit_object_b64: str,
    source_delta_sha256: str,
) -> bytes:
    if not isinstance(source_head, str) or not GIT_ID_RE.fullmatch(source_head):
        raise PackagingError("source_head_invalid")
    if not isinstance(source_tree, str) or not GIT_ID_RE.fullmatch(source_tree):
        raise PackagingError("source_tree_invalid")
    if not isinstance(source_delta_sha256, str) or not SHA256_RE.fullmatch(
        source_delta_sha256
    ):
        raise PackagingError("source_delta_digest_invalid")
    try:
        commit_object = base64.b64decode(commit_object_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise PackagingError("source_commit_object_invalid") from exc
    actual_head = hashlib.sha1(
        b"commit " + str(len(commit_object)).encode() + b"\0" + commit_object
    ).hexdigest()
    if actual_head != source_head:
        raise PackagingError("source_head_does_not_match_commit_object")
    first_line = commit_object.splitlines()[0] if commit_object else b""
    if first_line != f"tree {source_tree}".encode("ascii"):
        raise PackagingError("source_tree_does_not_match_commit_object")
    return commit_object


def _commit_timestamp(commit_object: bytes) -> int:
    for line in commit_object.splitlines():
        if line.startswith(b"committer "):
            fields = line.rsplit(b" ", 2)
            if len(fields) != 3:
                break
            try:
                return int(fields[-2])
            except ValueError:
                break
    raise PackagingError("source_commit_timestamp_invalid")


def _release_identity_marker(
    source_head: str, source_tree: str, commit_object_b64: str
) -> bytes:
    return canonical_json(
        {
            "schema_version": 2,
            "kind": SOURCE_SNAPSHOT_KIND,
            "candidate_head": source_head,
            "candidate_tree": source_tree,
            "commit_object": commit_object_b64,
        }
    )


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackagingError(f"source_file_open_failed:{path.name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PackagingError(f"source_file_type_or_link_invalid:{path.name}")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise PackagingError(f"source_file_changed_while_reading:{path.name}")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _tracked_file_inputs(
    source_root: Path,
    tracked_modes: Mapping[str, int] | None,
) -> list[tuple[str, bytes, int]]:
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise PackagingError("source_root_not_directory")
    if tracked_modes is None:
        relative_paths: list[str] = []
        for current, dirs, names in os.walk(source_root, topdown=True, followlinks=False):
            base = Path(current)
            dirs.sort()
            names.sort()
            for name in tuple(dirs):
                info = (base / name).lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise PackagingError(f"source_symlink_or_special_directory:{name}")
            for name in names:
                relative_paths.append((base / name).relative_to(source_root).as_posix())
    else:
        relative_paths = list(tracked_modes)
    if not relative_paths:
        raise PackagingError("empty_source_payload")
    if len(relative_paths) != len(set(relative_paths)):
        raise PackagingError("duplicate_source_path")

    result: list[tuple[str, bytes, int]] = []
    for rel in sorted(relative_paths):
        path = _safe_relpath(rel)
        if str(path) == MANIFEST_NAME:
            raise PackagingError("reserved_package_manifest_path")
        source_path = source_root.joinpath(*path.parts)
        current = source_root
        for part in path.parts[:-1]:
            current = current / part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise PackagingError(f"source_path_escapes_or_is_not_directory:{rel}")
        source_info = source_path.lstat()
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
            raise PackagingError(f"source_file_type_or_link_invalid:{rel}")
        data, info = _read_regular(source_path)
        git_mode = tracked_modes.get(rel) if tracked_modes is not None else None
        executable = bool(git_mode == 0o100755) if git_mode is not None else bool(info.st_mode & 0o111)
        result.append((rel, data, 0o555 if executable else 0o444))
    return result


def _git_tree_from_files(files: Sequence[tuple[str, bytes, int]]) -> str:
    """Recompute a Git tree object from the exact source bytes and modes."""
    tree: dict[str, Any] = {}
    for relative, contents, normalized_mode in files:
        parts = _safe_relpath(relative).parts
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise PackagingError("source_path_file_directory_collision")
            node = child
        name = parts[-1]
        if name in node:
            raise PackagingError("source_path_duplicate_or_directory_collision")
        blob = hashlib.sha1(
            b"blob " + str(len(contents)).encode("ascii") + b"\0" + contents
        ).digest()
        node[name] = (b"100755" if normalized_mode == 0o555 else b"100644", blob)

    def object_id(kind: bytes, payload: bytes) -> bytes:
        return hashlib.sha1(
            kind + b" " + str(len(payload)).encode("ascii") + b"\0" + payload
        ).digest()

    def write_tree(directory: Mapping[str, Any]) -> bytes:
        entries: list[tuple[bytes, bytes]] = []
        for name, item in directory.items():
            encoded_name = os.fsencode(name)
            if isinstance(item, dict):
                mode = b"40000"
                digest = write_tree(item)
                sort_name = encoded_name + b"/"
            else:
                mode, digest = item
                sort_name = encoded_name
            entries.append(
                (sort_name, mode + b" " + encoded_name + b"\0" + digest)
            )
        entries.sort(key=lambda row: row[0])
        return object_id(b"tree", b"".join(payload for _sort, payload in entries))

    if not files:
        raise PackagingError("empty_source_payload")
    return write_tree(tree).hex()


def derive_release_id(
    source_root: str | Path,
    *,
    source_head: str,
    tracked_modes: Mapping[str, int] | None = None,
) -> str:
    """Bind the release ID to the committed head and payload inventory."""
    if not isinstance(source_head, str) or not GIT_ID_RE.fullmatch(source_head):
        raise PackagingError("source_head_invalid")
    source_files = _tracked_file_inputs(Path(source_root), tracked_modes)
    payload_entries = [
        {
            "path": rel,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "mode": mode,
            "nlink": 1,
        }
        for rel, data, mode in source_files
    ]
    digest = sha256_bytes(canonical_json(payload_entries))
    return f"r1-{source_head[:16]}-{digest[:16]}"


def _preinst_script(release_id: str) -> bytes:
    root = "/" + (RELEASE_ROOT / release_id).as_posix()
    script = f'''#!/bin/sh
set -eu
if [ "$#" -lt 1 ] || [ "$1" != install ]; then
    echo "Phase 39 P0B release IDs are immutable; use a new release ID" >&2
    exit 1
fi
exec /usr/bin/python3.12 -I -S - <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path({root!r})
current = Path("/")
for component in root.parts[1:-1]:
    current = current / component
    try:
        info = current.lstat()
    except FileNotFoundError:
        continue
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("unsafe release parent: " + str(current))
if root.exists() or root.is_symlink():
    raise SystemExit("release ID already exists or is partial: " + str(root))
PY
'''
    return script.encode("utf-8")


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackagingError(f"subprocess_failed:{Path(argv[0]).name}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackagingError(f"subprocess_rejected:{Path(argv[0]).name}:{detail[:240]}")
    return result


def _write_file(path: Path, data: bytes, mode: int, epoch: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise PackagingError("package_file_write_failed")
            view = view[count:]
        os.fchmod(fd, mode)
        os.utime(fd, (epoch, epoch))
        os.fsync(fd)
    finally:
        os.close(fd)


def _normalise_directory_modes(root: Path, epoch: int) -> None:
    for current, dirs, _ in os.walk(root, topdown=False, followlinks=False):
        path = Path(current)
        os.chmod(path, 0o555)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
        for name in dirs:
            child = path / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise PackagingError("package_directory_substitution")


def _package_control(
    *, package: str, version: str, installed_size: int
) -> bytes:
    fields = [
        ("Package", package),
        ("Version", version),
        ("Section", "admin"),
        ("Priority", "optional"),
        ("Architecture", ARCHITECTURE),
        ("Maintainer", "Orchestrator Release Engineering <release@example.invalid>"),
        ("Installed-Size", str(installed_size)),
        ("Depends", DEPENDS),
        ("Description", "Versioned Phase 39 P0B candidate; NOT_ACTIVATABLE"),
    ]
    return ("".join(f"{key}: {value}\n" for key, value in fields) + "\n").encode()


def build_deb(
    source_root: str | Path,
    output_dir: str | Path,
    *,
    release_id: str,
    source_head: str,
    source_tree: str,
    commit_object_b64: str,
    source_delta_sha256: str,
    source_date_epoch: int,
    tracked_modes: Mapping[str, int] | None = None,
    source_snapshot_kind: str = SOURCE_SNAPSHOT_KIND,
) -> Path:
    """Build one deterministic candidate .deb without installing it."""
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise PackagingError("source_date_epoch_invalid")
    commit_object = _validate_source_identity(
        source_head, source_tree, commit_object_b64, source_delta_sha256
    )
    if source_delta_sha256 != EMPTY_SOURCE_DELTA_SHA256:
        raise PackagingError("dirty_source_candidate_not_publishable")
    if source_snapshot_kind != SOURCE_SNAPSHOT_KIND:
        raise PackagingError("publishable_git_archive_snapshot_required")
    if source_date_epoch != _commit_timestamp(commit_object):
        raise PackagingError("source_date_epoch_must_match_commit_timestamp")
    source_files = _tracked_file_inputs(Path(source_root), tracked_modes)
    if _git_tree_from_files(source_files) != source_tree:
        raise PackagingError("source_payload_does_not_match_committed_tree")
    payload_entries = [
        {
            "path": rel,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "mode": mode,
            "nlink": 1,
        }
        for rel, data, mode in source_files
    ]
    payload_digest = sha256_bytes(canonical_json(payload_entries))
    expected_release_id = f"r1-{source_head[:16]}-{payload_digest[:16]}"
    if release_id != expected_release_id:
        raise PackagingError("release_id_not_bound_to_source_and_payload")
    package = package_name(release_id)
    version = package_version(release_id)
    if not VERSION_RE.fullmatch(version):
        raise PackagingError("package_version_invalid")
    preinst = _preinst_script(release_id)
    release_identity = _release_identity_marker(
        source_head, source_tree, commit_object_b64
    )
    manifest = {
        "manifest_kind": "orchestrator-phase39-p0b-deb",
        "schema_version": SCHEMA_VERSION,
        "source_date_epoch": source_date_epoch,
        "package": {
            "name": package,
            "version": version,
            "architecture": ARCHITECTURE,
            "release_id": release_id,
        },
        "source": {
            "head": source_head,
            "tree": source_tree,
            "commit_object_b64": commit_object_b64,
            "tracked_worktree_delta_sha256": source_delta_sha256,
            "snapshot_kind": source_snapshot_kind,
        },
        "platform": {
            "distribution": "Ubuntu",
            "codename": CODENAME,
            "architecture": ARCHITECTURE,
            "python_abi": "CPython 3.12; python3.12-minimal >= 3.12.3~",
            "glibc_abi": "libc6 >= 2.39; arm64 ELF ABI",
            "kernel_abi": "Linux; pidfd_open and pidfd_send_signal capability checks required",
            "native_extension_closure": "DEFERRED_TO_R2",
        },
        "activation_status": "NOT_ACTIVATABLE",
        "payload_inventory_sha256": payload_digest,
        "payload_files": payload_entries,
        "release_identity_marker_sha256": sha256_bytes(release_identity),
        "maintainer_script_policy": {
            "allowed": ["preinst"],
            "preinst_sha256": sha256_bytes(preinst),
            "purpose": "reject reused release IDs and unsafe existing parents only",
            "service_activation": False,
            "trust_key_changes": False,
            "approval_or_pin_changes": False,
        },
    }
    output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{package}_{version}_{ARCHITECTURE}.deb"
    if any(output_root.glob(f"{package}_*.deb")):
        raise PackagingError("release_id_already_packaged")
    if output_path.exists() or output_path.is_symlink():
        raise PackagingError("package_output_exists")

    with tempfile.TemporaryDirectory(prefix="phase39-p0b-deb-") as temporary:
        work = Path(temporary)
        root = work / "root"
        root.mkdir()
        control = root / "DEBIAN"
        control.mkdir(mode=0o755)
        release_root = root.joinpath(*RELEASE_ROOT.parts, release_id)
        release_root.mkdir(parents=True)
        total_size = 0
        for rel, data, mode in source_files:
            _write_file(release_root.joinpath(*_safe_relpath(rel).parts), data, mode, source_date_epoch)
            total_size += len(data)
        _write_file(
            release_root / MANIFEST_NAME,
            canonical_json(manifest),
            0o444,
            source_date_epoch,
        )
        _write_file(
            release_root / RELEASE_IDENTITY_NAME,
            release_identity,
            0o444,
            source_date_epoch,
        )
        for current, dirs, _ in os.walk(root, topdown=False, followlinks=False):
            for name in dirs:
                directory = Path(current) / name
                os.chmod(directory, 0o555)
                os.utime(directory, (source_date_epoch, source_date_epoch), follow_symlinks=False)
        os.chmod(root, 0o555)
        os.chmod(root, 0o755)
        os.chmod(control, 0o755)

        _write_file(
            control / "control",
            _package_control(
                package=package,
                version=version,
                installed_size=(total_size + 1023) // 1024,
            ),
            0o644,
            source_date_epoch,
        )
        _write_file(control / "preinst", preinst, 0o755, source_date_epoch)
        os.chmod(control, 0o755)
        os.utime(control, (source_date_epoch, source_date_epoch))
        os.utime(root, (source_date_epoch, source_date_epoch))
        env = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
        _run(
            [
                str(DPKG_DEB),
                "--build",
                "--root-owner-group",
                "--uniform-compression",
                "-Zgzip",
                "-z9",
                str(root),
                str(work / output_path.name),
            ],
            cwd=work,
            env=env,
        )
        shutil.copyfile(work / output_path.name, output_path)
        os.chmod(output_path, 0o644)
    verify_package_archive(
        output_path,
        expected_release_id=release_id,
        expected_package_version=version,
        expected_source_head=source_head,
        expected_source_tree=source_tree,
    )
    return output_path


def _parse_control(raw: bytes, *, allow_continuations: bool = False) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise PackagingError("control_metadata_not_utf8") from exc
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line[0] in " \t":
            if not allow_continuations or current is None:
                raise PackagingError("control_metadata_continuation_invalid")
            fields[current] += "\n" + line[1:]
            continue
        if ": " not in line:
            raise PackagingError("control_metadata_line_invalid")
        key, value = line.split(": ", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", key) or key in fields:
            raise PackagingError("control_metadata_field_invalid")
        fields[key] = value
        current = key
    if not fields:
        raise PackagingError("control_metadata_empty")
    return fields


def _deb_fields(package_path: Path) -> dict[str, str]:
    result = _run([str(DPKG_DEB), "--field", str(package_path)])
    return _parse_control(result.stdout)


def _normalised_tar_name(name: str) -> PurePosixPath:
    if name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if name in {"", "."}:
        return PurePosixPath(".")
    path = _safe_relpath(name)
    return path


def _inspect_deb_tar(package_path: Path, *, release_id: str) -> list[dict[str, Any]]:
    result = _run([str(DPKG_DEB), "--fsys-tarfile", str(package_path)])
    base = RELEASE_ROOT / release_id
    allowed_dirs = {
        PurePosixPath("."),
        PurePosixPath("usr"),
        PurePosixPath("usr/lib"),
        PurePosixPath("usr/lib/orchestrator-maintenance"),
        PurePosixPath("usr/lib/orchestrator-maintenance/phase39-p0b"),
        base,
    }
    seen: set[PurePosixPath] = set()
    inventory: list[dict[str, Any]] = []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise PackagingError("package_data_archive_invalid") from exc
    with archive:
        for member in archive:
            rel = _normalised_tar_name(member.name)
            if rel in seen:
                raise PackagingError("duplicate_package_archive_path")
            seen.add(rel)
            if member.uid != 0 or member.gid != 0:
                raise PackagingError("package_payload_not_root_owned")
            if member.isdir():
                if rel not in allowed_dirs and base not in rel.parents:
                    raise PackagingError("unexpected_package_directory:" + rel.as_posix())
                if member.mode & 0o022:
                    raise PackagingError("package_directory_group_or_world_writable")
                continue
            if not member.isreg():
                raise PackagingError("package_symlink_hardlink_or_special_file")
            if base not in rel.parents or member.mode not in {0o444, 0o555}:
                raise PackagingError("package_file_path_or_mode_invalid")
            if member.mode == 0o555 and member.mode & 0o022:
                raise PackagingError("writable_executable_file")
            if member.size < 0:
                raise PackagingError("package_file_size_invalid")
            inventory.append(
                {
                    "path": rel.relative_to(base).as_posix(),
                    "sha256": "",
                    "size": member.size,
                    "mode": member.mode,
                    "nlink": 1,
                }
            )
    if not inventory:
        raise PackagingError("empty_package_payload")
    return inventory


def _control_files(package_path: Path) -> tuple[dict[str, str], bytes]:
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-control-") as temporary:
        target = Path(temporary)
        _run([str(DPKG_DEB), "--control", str(package_path), str(target)])
        names = sorted(path.name for path in target.iterdir())
        if names != ["control", "preinst"]:
            raise PackagingError("unexpected_maintainer_script_or_control_file")
        fields = _parse_control((target / "control").read_bytes())
        preinst = (target / "preinst").read_bytes()
        return fields, preinst


def _verify_extracted_payload(
    package_path: Path,
    *,
    release_id: str,
    package: str,
    version: str,
    architecture: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-extract-") as temporary:
        root = Path(temporary)
        _run([str(DPKG_DEB), "--extract", str(package_path), str(root)])
        release = root.joinpath(*RELEASE_ROOT.parts, release_id)
        manifest_path = release / MANIFEST_NAME
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise PackagingError("release_manifest_missing") from exc
        info = manifest_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444:
            raise PackagingError("release_manifest_file_invalid")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PackagingError("release_manifest_json_invalid") from exc
        if not isinstance(manifest, dict) or canonical_json(manifest) != raw:
            raise PackagingError("release_manifest_not_canonical")
        required = {
            "manifest_kind", "schema_version", "package", "source", "platform",
            "activation_status", "payload_inventory_sha256", "payload_files",
            "maintainer_script_policy", "release_identity_marker_sha256",
            "source_date_epoch",
        }
        if set(manifest) != required:
            raise PackagingError("release_manifest_schema_invalid")
        if manifest["manifest_kind"] != "orchestrator-phase39-p0b-deb" or manifest["schema_version"] != SCHEMA_VERSION:
            raise PackagingError("release_manifest_schema_unsupported")
        package_identity = manifest["package"]
        if package_identity != {
            "name": package,
            "version": version,
            "architecture": architecture,
            "release_id": release_id,
        }:
            raise PackagingError("release_manifest_package_identity_mismatch")
        if manifest["activation_status"] != "NOT_ACTIVATABLE":
            raise PackagingError("candidate_activation_status_invalid")
        if manifest["platform"] != {
            "distribution": "Ubuntu",
            "codename": CODENAME,
            "architecture": architecture,
            "python_abi": "CPython 3.12; python3.12-minimal >= 3.12.3~",
            "glibc_abi": "libc6 >= 2.39; arm64 ELF ABI",
            "kernel_abi": "Linux; pidfd_open and pidfd_send_signal capability checks required",
            "native_extension_closure": "DEFERRED_TO_R2",
        }:
            raise PackagingError("package_platform_policy_invalid")
        source = manifest["source"]
        if not isinstance(source, dict) or set(source) != {
            "head", "tree", "commit_object_b64", "tracked_worktree_delta_sha256",
            "snapshot_kind",
        }:
            raise PackagingError("package_source_identity_schema_invalid")
        _validate_source_identity(
            source["head"], source["tree"], source["commit_object_b64"],
            source["tracked_worktree_delta_sha256"],
        )
        if (
            source["snapshot_kind"] != SOURCE_SNAPSHOT_KIND
            or source["tracked_worktree_delta_sha256"] != EMPTY_SOURCE_DELTA_SHA256
        ):
            raise PackagingError("package_source_snapshot_not_publishable")
        if manifest["source_date_epoch"] != _commit_timestamp(
            base64.b64decode(source["commit_object_b64"], validate=True)
        ):
            raise PackagingError("package_source_date_epoch_mismatch")
        expected_marker = _release_identity_marker(
            source["head"], source["tree"], source["commit_object_b64"]
        )
        marker_path = release / RELEASE_IDENTITY_NAME
        try:
            marker_raw = marker_path.read_bytes()
        except OSError as exc:
            raise PackagingError("release_identity_marker_missing") from exc
        marker_info = marker_path.lstat()
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_nlink != 1
            or stat.S_IMODE(marker_info.st_mode) != 0o444
            or marker_raw != expected_marker
            or manifest.get("release_identity_marker_sha256") != sha256_bytes(expected_marker)
        ):
            raise PackagingError("release_identity_marker_invalid")
        files = manifest["payload_files"]
        if not isinstance(files, list) or not files:
            raise PackagingError("package_payload_inventory_missing")
        expected: dict[str, Mapping[str, Any]] = {}
        last_path = ""
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {
                "path", "sha256", "size", "mode", "nlink"
            }:
                raise PackagingError("package_payload_entry_invalid")
            rel = str(_safe_relpath(entry["path"]))
            if rel <= last_path or rel in expected:
                raise PackagingError("package_payload_inventory_order_invalid")
            last_path = rel
            if (
                not SHA256_RE.fullmatch(entry["sha256"])
                or type(entry["size"]) is not int
                or entry["size"] < 0
                or entry["mode"] not in {0o444, 0o555}
                or entry["nlink"] != 1
            ):
                raise PackagingError("package_payload_entry_metadata_invalid")
            expected[rel] = entry
        if manifest["payload_inventory_sha256"] != sha256_bytes(canonical_json(files)):
            raise PackagingError("package_payload_inventory_digest_invalid")
        script_policy = manifest["maintainer_script_policy"]
        expected_preinst = _preinst_script(release_id)
        if script_policy != {
            "allowed": ["preinst"],
            "preinst_sha256": sha256_bytes(expected_preinst),
            "purpose": "reject reused release IDs and unsafe existing parents only",
            "service_activation": False,
            "trust_key_changes": False,
            "approval_or_pin_changes": False,
        }:
            raise PackagingError("maintainer_script_policy_invalid")

        found: set[str] = set()
        dirs_expected: set[str] = {"."}
        for rel in expected:
            parts = PurePosixPath(rel).parts[:-1]
            for index in range(1, len(parts) + 1):
                dirs_expected.add("/".join(parts[:index]))
        for current, dirs, names in os.walk(release, topdown=True, followlinks=False):
            current_path = Path(current)
            current_rel = "." if current_path == release else current_path.relative_to(release).as_posix()
            if current_rel not in dirs_expected:
                raise PackagingError("unexpected_package_directory")
            for name in tuple(dirs):
                child = current_path / name
                child_info = child.lstat()
                if not stat.S_ISDIR(child_info.st_mode) or child_info.st_mode & 0o022:
                    raise PackagingError("package_symlink_or_writable_directory")
            for name in names:
                child = current_path / name
                rel = child.relative_to(release).as_posix()
                if rel in {MANIFEST_NAME, RELEASE_IDENTITY_NAME}:
                    continue
                if rel not in expected:
                    raise PackagingError("unexpected_package_file")
                data, file_info = _read_regular(child)
                entry = expected[rel]
                if (
                    len(data) != entry["size"]
                    or sha256_bytes(data) != entry["sha256"]
                    or stat.S_IMODE(file_info.st_mode) != entry["mode"]
                ):
                    raise PackagingError(f"package_payload_hash_or_mode_mismatch:{rel}")
                found.add(rel)
        if found != set(expected):
            raise PackagingError("package_payload_file_missing")
        return manifest


def verify_package_archive(
    package_path: str | Path,
    *,
    expected_release_id: str | None = None,
    expected_package_version: str | None = None,
    expected_architecture: str = ARCHITECTURE,
    expected_source_head: str | None = None,
    expected_source_tree: str | None = None,
) -> dict[str, Any]:
    package_path = Path(package_path)
    if package_path.is_symlink() or not package_path.is_file():
        raise PackagingError("package_file_invalid")
    fields = _deb_fields(package_path)
    release_id = fields.get("Package", "").removeprefix(PACKAGE_PREFIX)
    if not RELEASE_ID_RE.fullmatch(release_id) or fields.get("Package") != package_name(release_id):
        raise PackagingError("package_name_or_release_id_invalid")
    version = package_version(release_id)
    if fields.get("Version") != version:
        raise PackagingError("package_version_invalid")
    if expected_release_id is not None and release_id != expected_release_id:
        raise PackagingError("wrong_release_id")
    if expected_package_version is not None and version != expected_package_version:
        raise PackagingError("wrong_package_version")
    if fields.get("Architecture") != expected_architecture or expected_architecture != ARCHITECTURE:
        raise PackagingError("unapproved_package_architecture")
    required_control = {
        "Package", "Version", "Section", "Priority", "Architecture", "Maintainer",
        "Installed-Size", "Depends", "Description",
    }
    if set(fields) != required_control or fields.get("Depends") != DEPENDS:
        raise PackagingError("package_control_metadata_invalid")
    if fields.get("Description") != "Versioned Phase 39 P0B candidate; NOT_ACTIVATABLE":
        raise PackagingError("package_description_invalid")
    _inspect_deb_tar(package_path, release_id=release_id)
    control_fields, preinst = _control_files(package_path)
    if control_fields != fields or preinst != _preinst_script(release_id):
        raise PackagingError("unexpected_maintainer_script_or_control_mutation")
    manifest = _verify_extracted_payload(
        package_path,
        release_id=release_id,
        package=fields["Package"],
        version=fields["Version"],
        architecture=fields["Architecture"],
    )
    source = manifest["source"]
    if expected_source_head is not None and source["head"] != expected_source_head:
        raise PackagingError("wrong_source_head")
    if expected_source_tree is not None and source["tree"] != expected_source_tree:
        raise PackagingError("wrong_source_tree")
    return {
        "package": fields["Package"],
        "version": fields["Version"],
        "architecture": fields["Architecture"],
        "release_id": release_id,
        "activation_status": manifest["activation_status"],
        "source_head": source["head"],
        "source_tree": source["tree"],
        "tracked_worktree_delta_sha256": source["tracked_worktree_delta_sha256"],
        "payload_inventory_sha256": manifest["payload_inventory_sha256"],
        "manifest": manifest,
    }


def verify_installed_package_release(
    release_root: str | Path,
    *,
    expected_package: str,
    expected_version: str,
    expected_source_head: str,
    expected_source_tree: str,
    expected_installation_parent: str | Path = "/usr/lib/orchestrator-maintenance/phase39-p0b",
    expected_uid: int = 0,
    expected_gid: int = 0,
    verify_dpkg_status: bool = True,
) -> dict[str, Any]:
    """Verify an unpacked release candidate; this does not authorize activation."""
    root = Path(release_root)
    expected_parent = Path(expected_installation_parent)
    if not root.is_absolute() or not expected_parent.is_absolute() or root.parent != expected_parent:
        raise PackagingError("installed_release_path_not_fixed")
    if expected_parent == Path("/usr/lib/orchestrator-maintenance/phase39-p0b") and expected_uid == 0:
        release_manager.validate_parent_directories(root, uid=0)
    parent_info = expected_parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != expected_uid or parent_info.st_gid != expected_gid or parent_info.st_mode & 0o022:
        raise PackagingError("installed_release_parent_owner_or_mode_invalid")
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != expected_uid or root_info.st_gid != expected_gid:
        raise PackagingError("installed_release_owner_or_type_invalid")
    if root_info.st_mode & 0o022:
        raise PackagingError("installed_release_root_writable")
    try:
        raw = (root / MANIFEST_NAME).read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackagingError("installed_release_manifest_invalid") from exc
    if not isinstance(manifest, dict) or canonical_json(manifest) != raw:
        raise PackagingError("installed_release_manifest_not_canonical")
    required = {
        "manifest_kind", "schema_version", "package", "source", "platform",
        "activation_status", "payload_inventory_sha256", "payload_files",
        "maintainer_script_policy", "release_identity_marker_sha256",
        "source_date_epoch",
    }
    if set(manifest) != required or manifest.get("manifest_kind") != "orchestrator-phase39-p0b-deb" or manifest.get("schema_version") != SCHEMA_VERSION:
        raise PackagingError("installed_release_manifest_schema_invalid")
    package_identity = manifest.get("package")
    if not isinstance(package_identity, dict):
        raise PackagingError("installed_release_identity_mismatch")
    release_id = package_identity.get("release_id")
    if root.name != release_id or manifest.get("package") != {
        "name": expected_package,
        "version": expected_version,
        "architecture": ARCHITECTURE,
        "release_id": release_id,
    } or expected_package != package_name(release_id) or expected_version != package_version(release_id):
        raise PackagingError("installed_release_identity_mismatch")
    if manifest.get("activation_status") != "NOT_ACTIVATABLE":
        raise PackagingError("candidate_activation_status_invalid")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise PackagingError("installed_release_source_identity_mismatch")
    _validate_source_identity(
        source.get("head"), source.get("tree"), source.get("commit_object_b64"),
        source.get("tracked_worktree_delta_sha256"),
    )
    if (
        set(source) != {
            "head", "tree", "commit_object_b64", "tracked_worktree_delta_sha256",
            "snapshot_kind",
        }
        or source.get("snapshot_kind") != SOURCE_SNAPSHOT_KIND
        or source.get("tracked_worktree_delta_sha256") != EMPTY_SOURCE_DELTA_SHA256
    ):
        raise PackagingError("installed_release_source_snapshot_not_publishable")
    if manifest["source_date_epoch"] != _commit_timestamp(
        base64.b64decode(source["commit_object_b64"], validate=True)
    ):
        raise PackagingError("installed_release_source_date_epoch_mismatch")
    if source["head"] != expected_source_head or source["tree"] != expected_source_tree:
        raise PackagingError("installed_release_source_identity_mismatch")
    files = manifest.get("payload_files")
    if not isinstance(files, list) or not files or manifest.get("payload_inventory_sha256") != sha256_bytes(canonical_json(files)):
        raise PackagingError("installed_release_payload_inventory_invalid")
    expected_files: dict[str, Mapping[str, Any]] = {}
    previous = ""
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mode", "nlink"}:
            raise PackagingError("installed_release_payload_entry_invalid")
        rel = str(_safe_relpath(entry["path"]))
        if rel <= previous or rel in expected_files:
            raise PackagingError("installed_release_payload_inventory_order_invalid")
        previous = rel
        if not SHA256_RE.fullmatch(entry["sha256"]) or type(entry["size"]) is not int or entry["size"] < 0 or entry["mode"] not in {0o444, 0o555} or entry["nlink"] != 1:
            raise PackagingError("installed_release_payload_entry_invalid")
        expected_files[rel] = entry
    expected_marker = _release_identity_marker(
        source["head"], source["tree"], source["commit_object_b64"]
    )
    marker_path = root / RELEASE_IDENTITY_NAME
    try:
        marker_raw = marker_path.read_bytes()
        marker_info = marker_path.lstat()
    except OSError as exc:
        raise PackagingError("installed_release_identity_marker_missing") from exc
    if (
        not stat.S_ISREG(marker_info.st_mode)
        or marker_info.st_uid != expected_uid
        or marker_info.st_gid != expected_gid
        or marker_info.st_nlink != 1
        or stat.S_IMODE(marker_info.st_mode) != 0o444
        or marker_raw != expected_marker
        or manifest.get("release_identity_marker_sha256") != sha256_bytes(expected_marker)
    ):
        raise PackagingError("installed_release_identity_marker_invalid")
    if manifest.get("maintainer_script_policy") != {
        "allowed": ["preinst"],
        "preinst_sha256": sha256_bytes(_preinst_script(release_id)),
        "purpose": "reject reused release IDs and unsafe existing parents only",
        "service_activation": False,
        "trust_key_changes": False,
        "approval_or_pin_changes": False,
    }:
        raise PackagingError("installed_release_script_policy_invalid")
    seen: set[str] = set()
    expected_dirs: set[str] = {"."}
    for rel in expected_files:
        for index in range(1, len(PurePosixPath(rel).parts)):
            expected_dirs.add("/".join(PurePosixPath(rel).parts[:index]))
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        rel_dir = "." if directory == root else directory.relative_to(root).as_posix()
        if rel_dir not in expected_dirs:
            raise PackagingError("installed_unexpected_directory")
        dinfo = directory.lstat()
        if not stat.S_ISDIR(dinfo.st_mode) or dinfo.st_uid != expected_uid or dinfo.st_gid != expected_gid or dinfo.st_mode & 0o022:
            raise PackagingError("installed_directory_owner_or_mode_invalid")
        for name in tuple(dirs):
            child = directory / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid or info.st_gid != expected_gid or info.st_mode & 0o022:
                raise PackagingError("installed_symlink_or_directory_mode_invalid")
        for name in names:
            child = directory / name
            rel = child.relative_to(root).as_posix()
            info = child.lstat()
            if rel in {MANIFEST_NAME, RELEASE_IDENTITY_NAME}:
                if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_gid != expected_gid or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444:
                    raise PackagingError("installed_generated_metadata_owner_or_mode_invalid")
                if rel == RELEASE_IDENTITY_NAME and child.read_bytes() != expected_marker:
                    raise PackagingError("installed_release_identity_marker_invalid")
                continue
            if rel not in expected_files:
                raise PackagingError("installed_unexpected_file")
            data, info = _read_regular(child)
            entry = expected_files[rel]
            if (
                info.st_uid != expected_uid or info.st_gid != expected_gid
                or stat.S_IMODE(info.st_mode) != entry["mode"]
                or len(data) != entry["size"] or sha256_bytes(data) != entry["sha256"]
            ):
                raise PackagingError("installed_payload_owner_mode_or_hash_invalid")
            seen.add(rel)
    if seen != set(expected_files):
        raise PackagingError("installed_payload_file_missing")
    if verify_dpkg_status:
        result = _run(
            [
                "/usr/bin/dpkg-query",
                "--show",
                "--showformat=${Status}\\t${Version}\\t${Architecture}\\n",
                expected_package,
            ],
            timeout=15,
        )
        expected_status = f"install ok installed\t{expected_version}\t{ARCHITECTURE}\n".encode()
        if result.stdout != expected_status:
            raise PackagingError("installed_dpkg_status_or_version_mismatch")
    return {
        "status": "RELEASE_INSTALLED_NOT_APPROVED",
        "activation_status": "NOT_ACTIVATABLE",
        "package": expected_package,
        "version": expected_version,
        "release_id": release_id,
        "source_head": source["head"],
        "source_tree": source["tree"],
    }


def _format_utc(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise PackagingError("repository_time_must_be_utc")
    return email.utils.format_datetime(value.astimezone(dt.timezone.utc), usegmt=True)


def _parse_utc(value: str, field: str) -> dt.datetime:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PackagingError(f"repository_{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise PackagingError(f"repository_{field}_timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def _packages_stanza(
    deb_path: Path,
    *,
    filename: str,
    expected: Mapping[str, str],
) -> bytes:
    fields = _deb_fields(deb_path)
    for key in ("Package", "Version", "Architecture", "Maintainer", "Depends", "Section", "Priority", "Description"):
        if key not in fields:
            raise PackagingError("package_index_field_missing:" + key)
    if any("\n" in value or "\r" in value for value in fields.values()):
        raise PackagingError("unsupported_multiline_package_control")
    if dict((key, fields[key]) for key in ("Package", "Version", "Architecture")) != dict(expected):
        raise PackagingError("package_index_identity_mismatch")
    data, _ = _read_regular(deb_path)
    index_fields = {
        **fields,
        "Filename": filename,
        "Size": str(len(data)),
        "SHA256": sha256_bytes(data),
    }
    order = [
        "Package", "Version", "Architecture", "Maintainer", "Depends", "Section",
        "Priority", "Installed-Size", "Description", "Filename", "Size", "SHA256",
    ]
    return ("".join(f"{key}: {index_fields[key]}\n" for key in order if key in index_fields) + "\n").encode()


def _release_metadata(
    *, origin: str, date: dt.datetime, valid_until: dt.datetime, packages: bytes
) -> bytes:
    digest = sha256_bytes(packages)
    fields = [
        ("Origin", origin),
        ("Label", "Orchestrator Phase 39 P0B"),
        ("Suite", CODENAME),
        ("Codename", CODENAME),
        ("Date", _format_utc(date)),
        ("Valid-Until", _format_utc(valid_until)),
        ("Architectures", ARCHITECTURE),
        ("Components", APT_COMPONENT),
        ("Description", "Signed Phase 39 P0B release candidates"),
    ]
    text = "".join(f"{key}: {value}\n" for key, value in fields)
    text += "SHA256:\n " + digest + " " + str(len(packages)) + " " + APT_INDEX + "\n"
    return text.encode("utf-8")


def apt_source_line(repository_uri: str, signed_by_path: str) -> str:
    if not repository_uri.startswith(("https://", "file:/")):
        raise PackagingError("apt_repository_uri_must_be_https_or_file")
    key_path = Path(signed_by_path)
    if not key_path.is_absolute() or "]" in signed_by_path or any(c.isspace() for c in signed_by_path):
        raise PackagingError("signed_by_path_invalid")
    return (
        f"deb [arch={ARCHITECTURE} signed-by={signed_by_path}] "
        f"{repository_uri.rstrip('/')} {CODENAME} {APT_COMPONENT}"
    )


def apt_pin_preferences(package: str) -> str:
    if not PACKAGE_RE.fullmatch(package) or not package.startswith(PACKAGE_PREFIX):
        raise PackagingError("apt_pin_package_invalid")
    return (
        f"Package: {package}\n"
        f"Pin: release o={ORIGIN},n={CODENAME},c={APT_COMPONENT}\n"
        "Pin-Priority: 1001\n"
    )


def publish_repository(
    repository_root: str | Path,
    package_path: str | Path,
    *,
    signing_key_fingerprint: str,
    gnupghome: str | Path,
    origin: str = ORIGIN,
    date: dt.datetime,
    valid_until: dt.datetime,
) -> Path:
    """Publish a fresh local APT repository using an already provisioned key."""
    if origin != ORIGIN:
        raise PackagingError("repository_origin_not_approved")
    now_date = date.astimezone(dt.timezone.utc) if date.tzinfo else date
    now_expiry = valid_until.astimezone(dt.timezone.utc) if valid_until.tzinfo else valid_until
    if now_expiry <= now_date:
        raise PackagingError("repository_valid_until_not_after_date")
    package_path = Path(package_path)
    verified = verify_package_archive(package_path)
    fingerprint = signing_key_fingerprint.replace(" ", "").upper()
    if not re.fullmatch(r"[A-F0-9]{40,64}", fingerprint):
        raise PackagingError("signing_key_fingerprint_invalid")
    gnupghome_path = Path(gnupghome).resolve(strict=True)
    target = Path(repository_root).absolute()
    if target.exists() or target.is_symlink():
        raise PackagingError("repository_destination_must_be_new")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-apt-stage-", dir=target.parent) as temporary:
        stage = Path(temporary) / "repo"
        pool = stage / "pool/main" / verified["package"][0] / verified["package"]
        pool.mkdir(parents=True)
        destination_package = pool / package_path.name
        shutil.copyfile(package_path, destination_package)
        os.chmod(destination_package, 0o644)
        filename = destination_package.relative_to(stage).as_posix()
        packages = _packages_stanza(
            destination_package,
            filename=filename,
            expected={
                "Package": verified["package"],
                "Version": verified["version"],
                "Architecture": verified["architecture"],
            },
        )
        index = stage / "dists" / CODENAME / APT_INDEX
        index.parent.mkdir(parents=True)
        index.write_bytes(packages)
        os.chmod(index, 0o644)
        release_dir = index.parent.parent.parent
        release = release_dir / "Release"
        release.write_bytes(
            _release_metadata(
                origin=origin,
                date=now_date,
                valid_until=now_expiry,
                packages=packages,
            )
        )
        os.chmod(release, 0o644)
        signed = release_dir / "InRelease"
        env = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
            "GNUPGHOME": str(gnupghome_path),
        }
        _run(
            [
                str(GPG), "--batch", "--no-tty", "--yes", "--local-user", fingerprint,
                "--digest-algo", "SHA256", "--clearsign", "--output", str(signed),
                str(release),
            ],
            env=env,
        )
        os.chmod(signed, 0o644)
        for current, dirs, files in os.walk(stage, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in files:
                os.chmod(current_path / name, 0o644)
            for name in dirs:
                os.chmod(current_path / name, 0o755)
            os.chmod(current_path, 0o755)
        os.replace(stage, target)
    return target


def _public_key_fingerprints(keyring: Path) -> list[str]:
    result = _run([str(GPG), "--batch", "--with-colons", "--show-keys", str(keyring)])
    fingerprints = [
        line.split(":")[9].upper()
        for line in result.stdout.decode("utf-8", errors="strict").splitlines()
        if line.startswith("fpr:") and len(line.split(":")) > 9
    ]
    return fingerprints


def _require_repository_parents(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PackagingError("repository_path_escapes_root") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PackagingError("repository_parent_missing") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise PackagingError("repository_symlink_or_non_directory_parent")


def _parse_release(raw: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    checksums: list[str] = []
    in_hashes = False
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise PackagingError("release_metadata_not_utf8") from exc
    for line in lines:
        if line.startswith("SHA256:"):
            if in_hashes or line != "SHA256:":
                raise PackagingError("release_sha256_section_invalid")
            in_hashes = True
            continue
        if in_hashes:
            if not line.startswith(" "):
                raise PackagingError("release_sha256_entry_invalid")
            checksums.append(line[1:])
            continue
        if ": " not in line:
            raise PackagingError("release_metadata_line_invalid")
        key, value = line.split(": ", 1)
        if key in fields or not key:
            raise PackagingError("release_metadata_duplicate_field")
        fields[key] = value
    if not in_hashes or not checksums:
        raise PackagingError("release_metadata_sha256_missing")
    fields["__SHA256__"] = "\n".join(checksums)
    return fields


def _parse_packages(raw: bytes) -> list[dict[str, str]]:
    if not raw.endswith(b"\n"):
        raise PackagingError("packages_index_not_canonical")
    stanzas = raw.decode("utf-8", errors="strict").strip().split("\n\n")
    parsed: list[dict[str, str]] = []
    for stanza in stanzas:
        fields = _parse_control((stanza + "\n\n").encode())
        required = {
            "Package", "Version", "Architecture", "Maintainer", "Depends", "Section",
            "Priority", "Description", "Filename", "Size", "SHA256",
            "Installed-Size",
        }
        if set(fields) != required:
            raise PackagingError("packages_index_fields_invalid")
        if not SHA256_RE.fullmatch(fields["SHA256"]):
            raise PackagingError("packages_index_package_hash_missing_or_invalid")
        parsed.append(fields)
    if not parsed:
        raise PackagingError("packages_index_empty")
    canonical = b"".join(
        _stanza_from_fields(item)
        for item in sorted(parsed, key=lambda row: (row["Package"], row["Version"], row["Architecture"]))
    )
    if canonical != raw:
        raise PackagingError("packages_index_not_canonical")
    return parsed


def _stanza_from_fields(fields: Mapping[str, str]) -> bytes:
    order = [
        "Package", "Version", "Architecture", "Maintainer", "Depends", "Section",
        "Priority", "Installed-Size", "Description", "Filename", "Size", "SHA256",
    ]
    return ("".join(f"{key}: {fields[key]}\n" for key in order if key in fields) + "\n").encode()


def verify_repository(
    repository_root: str | Path,
    *,
    keyring: str | Path,
    expected_fingerprint: str,
    expected_origin: str = ORIGIN,
    expected_package: str,
    expected_version: str,
    expected_release_id: str,
    expected_architecture: str = ARCHITECTURE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Verify signed APT metadata, its package index, and the exact .deb bytes."""
    root_argument = Path(repository_root)
    if root_argument.is_symlink():
        raise PackagingError("repository_root_symlink_rejected")
    root = root_argument.resolve(strict=True)
    ring_argument = Path(keyring)
    try:
        ring_info = ring_argument.lstat()
    except OSError as exc:
        raise PackagingError("dedicated_signed_by_keyring_invalid") from exc
    if not stat.S_ISREG(ring_info.st_mode) or ring_info.st_nlink != 1:
        raise PackagingError("dedicated_signed_by_keyring_invalid")
    expected_fingerprint = expected_fingerprint.replace(" ", "").upper()
    ring_data, _ = _read_regular(ring_argument)
    release_dir = root / "dists" / CODENAME
    inrelease = release_dir / "InRelease"
    _require_repository_parents(root, inrelease)
    try:
        inrelease_info = inrelease.lstat()
    except OSError as exc:
        raise PackagingError("signed_inrelease_missing") from exc
    if not stat.S_ISREG(inrelease_info.st_mode) or inrelease_info.st_nlink != 1:
        raise PackagingError("signed_inrelease_missing")
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-inrelease-") as temporary:
        temp_root = Path(temporary)
        ring = temp_root / "trusted.gpg"
        ring.write_bytes(ring_data)
        os.chmod(ring, 0o600)
        if _public_key_fingerprints(ring) != [expected_fingerprint]:
            raise PackagingError("dedicated_signed_by_fingerprint_mismatch")
        inrelease_snapshot = temp_root / "InRelease"
        inrelease_raw, _ = _read_regular(inrelease)
        inrelease_snapshot.write_bytes(inrelease_raw)
        os.chmod(inrelease_snapshot, 0o600)
        cleartext = temp_root / "Release"
        result = subprocess.run(
            [
                str(GPGV), "--status-fd", "1", "--keyring", str(ring),
                "--output", str(cleartext), str(inrelease_snapshot),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
        )
        if result.returncode != 0:
            raise PackagingError("inrelease_signature_invalid")
        statuses = result.stdout.decode("utf-8", errors="replace").splitlines()
        valid_signers = [
            line.split()[2].upper()
            for line in statuses
            if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) > 2
        ]
        if valid_signers != [expected_fingerprint]:
            raise PackagingError("inrelease_signer_fingerprint_mismatch")
        release_raw = cleartext.read_bytes()
    release_fields = _parse_release(release_raw)
    expected_fields = {
        "Origin": expected_origin,
        "Suite": CODENAME,
        "Codename": CODENAME,
        "Architectures": expected_architecture,
        "Components": APT_COMPONENT,
    }
    for key, value in expected_fields.items():
        if release_fields.get(key) != value:
            raise PackagingError("repository_" + key.lower() + "_mismatch")
    if expected_origin != ORIGIN or expected_architecture != ARCHITECTURE:
        raise PackagingError("repository_origin_or_architecture_not_approved")
    date = _parse_utc(release_fields.get("Date", ""), "date")
    valid_until = _parse_utc(release_fields.get("Valid-Until", ""), "valid_until")
    instant = now or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise PackagingError("verification_time_must_be_aware")
    instant = instant.astimezone(dt.timezone.utc)
    if date > instant + dt.timedelta(minutes=5) or valid_until <= instant or valid_until <= date:
        raise PackagingError("repository_metadata_expired_or_not_yet_valid")
    checksum_rows = release_fields["__SHA256__"].splitlines()
    if len(checksum_rows) != 1:
        raise PackagingError("repository_index_hash_count_invalid")
    hash_parts = checksum_rows[0].split()
    if len(hash_parts) != 3 or hash_parts[2] != APT_INDEX or not SHA256_RE.fullmatch(hash_parts[0]):
        raise PackagingError("repository_index_hash_missing_or_invalid")
    index = root / "dists" / CODENAME / APT_INDEX
    _require_repository_parents(root, index)
    try:
        index_info = index.lstat()
    except OSError as exc:
        raise PackagingError("repository_packages_index_missing") from exc
    if not stat.S_ISREG(index_info.st_mode) or index_info.st_nlink != 1:
        raise PackagingError("repository_packages_index_missing")
    index_raw, _ = _read_regular(index)
    if len(index_raw) != int(hash_parts[1]) or sha256_bytes(index_raw) != hash_parts[0]:
        raise PackagingError("repository_packages_index_hash_mismatch")
    packages = _parse_packages(index_raw)
    matching = [row for row in packages if row.get("Package") == expected_package]
    if len(matching) != 1:
        raise PackagingError("exact_package_not_found_or_duplicated")
    row = matching[0]
    if row.get("Version") != expected_version:
        raise PackagingError("unapproved_package_version")
    if row.get("Architecture") != expected_architecture:
        raise PackagingError("unapproved_package_architecture")
    if row.get("Package") != package_name(expected_release_id) or expected_version != package_version(expected_release_id):
        raise PackagingError("expected_package_release_binding_invalid")
    if not row.get("SHA256") or not SHA256_RE.fullmatch(row["SHA256"]):
        raise PackagingError("package_sha256_missing_or_invalid")
    if not row.get("Size", "").isdigit():
        raise PackagingError("package_size_missing_or_invalid")
    filename = _safe_relpath(row.get("Filename", ""))
    package_path = root.joinpath(*filename.parts)
    _require_repository_parents(root, package_path)
    parent = root
    for part in filename.parts[:-1]:
        parent = parent / part
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PackagingError("package_repository_path_symlink_or_non_directory")
    package_data, package_info = _read_regular(package_path)
    if (
        len(package_data) != int(row["Size"])
        or sha256_bytes(package_data) != row["SHA256"]
        or package_info.st_size != int(row["Size"])
    ):
        raise PackagingError("repository_package_hash_mismatch")
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-verified-deb-") as temporary:
        package_snapshot = Path(temporary) / package_path.name
        package_snapshot.write_bytes(package_data)
        os.chmod(package_snapshot, 0o600)
        package = verify_package_archive(
            package_snapshot,
            expected_release_id=expected_release_id,
            expected_package_version=expected_version,
            expected_architecture=expected_architecture,
        )
    return {
        "signature": "VALID",
        "signer_fingerprint": valid_signers[0],
        "origin": release_fields["Origin"],
        "codename": CODENAME,
        "architecture": expected_architecture,
        "package": package["package"],
        "version": package["version"],
        "release_id": package["release_id"],
        "activation_status": package["activation_status"],
        "valid_until": release_fields["Valid-Until"],
    }


def _git_output(repo: Path, *args: str) -> bytes:
    result = _run(
        ["/usr/bin/git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        timeout=30,
    )
    return result.stdout


def _read_committed_source_modes(
    repo: Path, source_head: str
) -> dict[str, tuple[int, str]]:
    entries = _git_output(
        repo, "ls-tree", "-r", "-z", "--full-tree", source_head
    ).split(b"\0")
    modes: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode_raw, kind, object_id_raw = metadata.split(b" ", 2)
        except ValueError as exc:
            raise PackagingError("committed_source_tree_entry_invalid") from exc
        if kind != b"blob" or mode_raw not in {b"100644", b"100755"}:
            raise PackagingError("committed_source_has_symlink_or_submodule")
        if not re.fullmatch(rb"[0-9a-f]{40}", object_id_raw):
            raise PackagingError("committed_source_blob_identity_invalid")
        try:
            path = os.fsdecode(raw_path)
            _safe_relpath(path)
        except (UnicodeError, PackagingError) as exc:
            raise PackagingError("committed_source_path_invalid") from exc
        if path in modes:
            raise PackagingError("committed_source_path_duplicate")
        modes[path] = (int(mode_raw, 8), object_id_raw.decode("ascii"))
    if not modes:
        raise PackagingError("empty_source_payload")
    return modes


def _materialize_git_archive(
    repo: Path,
    source_head: str,
    expected_modes: Mapping[str, tuple[int, str]],
    destination: Path,
    *,
    source_date_epoch: int,
) -> dict[str, int]:
    archive_bytes = _git_output(repo, "archive", "--format=tar", source_head)
    destination.mkdir(mode=0o700)
    found: dict[str, int] = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise PackagingError("committed_source_archive_invalid") from exc
    with archive:
        for member in archive:
            raw_name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
            try:
                relative = str(_safe_relpath(raw_name))
            except PackagingError as exc:
                raise PackagingError("committed_source_archive_path_invalid") from exc
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                if relative in expected_modes:
                    raise PackagingError("committed_source_archive_type_mismatch")
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile() or relative in found:
                raise PackagingError("committed_source_archive_special_or_duplicate")
            expected = expected_modes.get(relative)
            if expected is None:
                raise PackagingError("committed_source_archive_has_untracked_path")
            git_mode, expected_blob = expected
            normalized_mode = 0o755 if git_mode == 0o100755 else 0o644
            archive_mode = stat.S_IMODE(member.mode)
            if archive_mode & 0o7000 or bool(archive_mode & 0o111) != bool(
                git_mode & 0o111
            ):
                raise PackagingError(
                    "committed_source_archive_mode_mismatch:"
                    f"{relative}:{archive_mode:o}:{normalized_mode:o}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise PackagingError("committed_source_archive_file_unreadable")
            data = stream.read()
            blob_id = hashlib.sha1(
                b"blob " + str(len(data)).encode("ascii") + b"\0" + data
            ).hexdigest()
            if blob_id != expected_blob:
                raise PackagingError("committed_source_archive_blob_mismatch")
            _write_file(target, data, normalized_mode, source_date_epoch)
            found[relative] = git_mode
    if found != {path: mode for path, (mode, _blob) in expected_modes.items()}:
        raise PackagingError("committed_source_archive_inventory_mismatch")
    return found


def build_from_checkout(
    checkout: str | Path,
    output_dir: str | Path,
    *,
    expected_source_head: str,
    release_id: str,
    source_date_epoch: int | None = None,
) -> Path:
    """Build only from a clean, explicitly identified immutable Git snapshot."""
    repo = Path(checkout).resolve(strict=True)
    head = _git_output(repo, "rev-parse", "HEAD").decode().strip()
    if not isinstance(expected_source_head, str) or not GIT_ID_RE.fullmatch(
        expected_source_head
    ):
        raise PackagingError("explicit_source_head_required")
    if head != expected_source_head:
        raise PackagingError("checkout_head_does_not_match_explicit_source")
    tree = _git_output(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    commit = _git_output(repo, "cat-file", "commit", "HEAD")
    if source_date_epoch is None:
        source_date_epoch = _commit_timestamp(commit)
    commit_b64 = base64.b64encode(commit).decode("ascii")
    status = _git_output(
        repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
    )
    if status:
        raise PackagingError("publishable_source_worktree_dirty")
    expected_modes = _read_committed_source_modes(repo, head)
    source_delta = EMPTY_SOURCE_DELTA_SHA256
    with tempfile.TemporaryDirectory(prefix="phase39-p0b-git-archive-") as temp:
        snapshot = Path(temp) / "source"
        modes = _materialize_git_archive(
            repo,
            head,
            expected_modes,
            snapshot,
            source_date_epoch=source_date_epoch,
        )
        if _git_output(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        ):
            raise PackagingError("publishable_source_worktree_changed_during_build")
        if release_id == "auto":
            release_id = derive_release_id(snapshot, source_head=head, tracked_modes=modes)
        return build_deb(
            snapshot,
            output_dir,
            release_id=release_id,
            source_head=head,
            source_tree=tree,
            commit_object_b64=commit_b64,
            source_delta_sha256=source_delta,
            source_date_epoch=source_date_epoch,
            tracked_modes=modes,
        )


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--checkout", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--expected-source-head", required=True)
    build.add_argument("--release-id", default="auto")
    build.add_argument("--source-date-epoch", type=int)
    verify = commands.add_parser("verify-package")
    verify.add_argument("--package", required=True)
    worker_release = commands.add_parser("verify-worker-release")
    worker_release.add_argument("--release", required=True)
    worker_release.add_argument("--expected-source-head")
    worker_release.add_argument("--expected-source-tree")
    repo = commands.add_parser("verify-repository")
    repo.add_argument("--repository", required=True)
    repo.add_argument("--keyring", required=True)
    repo.add_argument("--fingerprint", required=True)
    repo.add_argument("--release-id", required=True)
    repo.add_argument("--now", help="UTC ISO-8601 verification time")
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build_from_checkout(
                args.checkout, args.output_dir,
                expected_source_head=args.expected_source_head,
                release_id=args.release_id,
                source_date_epoch=args.source_date_epoch,
            )
            print(json.dumps({"package": str(result), "status": "NOT_ACTIVATABLE"}, sort_keys=True))
            return 0
        if args.command == "verify-package":
            verified = verify_package_archive(args.package)
            verified.pop("manifest")
            print(json.dumps(verified, sort_keys=True))
            return 0
        if args.command == "verify-worker-release":
            result = verify_worker_release(
                args.release,
                expected_source_head=args.expected_source_head,
                expected_source_tree=args.expected_source_tree,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("activation_eligible") is True else 2
        now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        result = verify_repository(
            args.repository,
            keyring=args.keyring,
            expected_fingerprint=args.fingerprint,
            expected_package=package_name(args.release_id),
            expected_version=package_version(args.release_id),
            expected_release_id=args.release_id,
            now=now,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PackagingError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
