"""Build and install immutable, versioned Phase 39 P0B release candidates.

This module is stdlib-only.  A manifest digest is an integrity value, not an
authenticator: installation requires an expected digest supplied by a
separately controlled release review.  Execution additionally requires a
signed operator approval checked by :mod:`release_entrypoint`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MANIFEST_NAME = "release-manifest.json"
STATE_NAME = "release-state.json"
LOCK_PREFIX = ".phase39-p0b-install-"
MUTABLE_RUNTIME = frozenset({"logs/worker.log"})
RELEASE_IDENTITY_NAME = ".orchestrator-release-identity.json"
REQUIRED_RUNTIME_DEPENDENCIES = frozenset(
    {
        "operator_tooling",
        "worker_pidfd",
        "protected_refresh_authority",
        "release_manager",
        "release_entrypoint",
        "operator_entrypoint_wrapper",
        "release_install_entrypoint",
        "release_install_wrapper",
        "start_sh",
        "worker_pin_worksheet",
        "worker_identity",
        "invocation_provenance",
        "application_source",
        "python_interpreter",
        "venv_python",
        "celery_cli",
        "venv_configuration",
        "python_stdlib",
        "python_dependencies",
        "worker_configuration",
        "bash",
        "env",
        "nohup",
        "cmp",
        "stat",
        "openssl",
        "dynamic_loader",
        "libc",
        "libpython",
        "kernel_pidfd",
        "procfs",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_ID_RE = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z")
PROTECTED_INSTALLATION_PARENT = Path(
    "/usr/local/lib/orchestrator-maintenance/phase39-p0b"
)


class ReleaseError(RuntimeError):
    """A fail-closed release validation or installation error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relpath(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ReleaseError("unsafe_manifest_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError("unsafe_manifest_path")
    if str(path) != value:
        raise ReleaseError("noncanonical_manifest_path")
    return path


def _hash_regular_file(path: Path) -> tuple[str, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(f"file_open_failed:{path.name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReleaseError(f"file_type_or_link_count_invalid:{path.name}")
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after:
            raise ReleaseError(f"file_changed_while_hashing:{path.name}")
        return digest.hexdigest(), after
    finally:
        os.close(fd)


def _walk_release_files(root: Path, *, required_uid: int | None = None) -> list[dict[str, Any]]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ReleaseError("source_root_not_directory")
    files: list[dict[str, Any]] = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        if required_uid is not None:
            dir_info = directory.lstat()
            if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != required_uid or dir_info.st_mode & 0o022:
                raise ReleaseError(f"system_dependency_directory_unprotected:{directory.name}")
        dirs.sort()
        names.sort()
        for name in tuple(dirs):
            item = directory / name
            try:
                info = item.lstat()
            except OSError as exc:
                raise ReleaseError("source_directory_unreadable") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise ReleaseError(f"symlink_or_special_directory:{item.name}")
            if required_uid is not None and (info.st_uid != required_uid or info.st_mode & 0o022):
                raise ReleaseError(f"system_dependency_directory_unprotected:{item.name}")
        for name in names:
            item = directory / name
            rel = item.relative_to(root).as_posix()
            _safe_relpath(rel)
            if rel in {MANIFEST_NAME, STATE_NAME, RELEASE_IDENTITY_NAME} or rel in MUTABLE_RUNTIME:
                raise ReleaseError(f"reserved_release_path:{rel}")
            digest, info = _hash_regular_file(item)
            if required_uid is not None and (
                info.st_uid != required_uid or info.st_mode & 0o022
            ):
                raise ReleaseError(f"system_dependency_file_unprotected:{item.name}")
            normalized_mode = 0o600 if rel == ".env" else (0o555 if info.st_mode & 0o111 else 0o444)
            files.append(
                {
                    "path": rel,
                    "sha256": digest,
                    "size": info.st_size,
                    "mode": normalized_mode,
                    "nlink": 1,
                }
            )
    if not files:
        raise ReleaseError("empty_release_payload")
    return sorted(files, key=lambda entry: entry["path"])


def _file_table_digest(files: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_json(list(files)))


def _git_tree_id(root: Path) -> str:
    """Recompute the committed Git tree, excluding documented release data."""
    excluded_top = {
        "venv", ".env", "logs", MANIFEST_NAME, STATE_NAME, RELEASE_IDENTITY_NAME,
    }

    def git_object_id(kind: bytes, payload: bytes) -> bytes:
        return hashlib.sha1(kind + b" " + str(len(payload)).encode() + b"\0" + payload).digest()

    def tree(directory: Path, *, top: bool) -> bytes | None:
        entries: list[tuple[bytes, bytes]] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                name = entry.name
                if (top and name in excluded_top) or name == "__pycache__":
                    continue
                info = entry.stat(follow_symlinks=False)
                name_bytes = os.fsencode(name)
                if stat.S_ISDIR(info.st_mode):
                    child = tree(Path(entry.path), top=False)
                    if child is None:
                        continue
                    mode = b"40000"
                    sort_name = name_bytes + b"/"
                    object_id = child
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        file_info = os.fstat(fd)
                        if (file_info.st_dev, file_info.st_ino, file_info.st_size) != (info.st_dev, info.st_ino, info.st_size):
                            raise ReleaseError("source_file_changed_during_git_tree_hash")
                        blob_hash = hashlib.sha1(b"blob " + str(info.st_size).encode() + b"\0")
                        while True:
                            block = os.read(fd, 1024 * 1024)
                            if not block:
                                break
                            blob_hash.update(block)
                        object_id = blob_hash.digest()
                    finally:
                        os.close(fd)
                    mode = b"100755" if info.st_mode & 0o111 else b"100644"
                    sort_name = name_bytes
                else:
                    raise ReleaseError(f"git_tree_symlink_or_special_file:{name}")
                entries.append((sort_name, mode + b" " + name_bytes + b"\0" + object_id))
        if not entries:
            return None
        entries.sort(key=lambda item: item[0])
        return git_object_id(b"tree", b"".join(payload for _, payload in entries))

    result = tree(root, top=True)
    if result is None:
        raise ReleaseError("git_tree_empty")
    return result.hex()


def _validate_dependencies(
    dependencies: Any, files: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(dependencies, list) or not dependencies:
        raise ReleaseError("runtime_dependency_inventory_missing")
    by_path = {entry["path"]: entry for entry in files}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in dependencies:
        if not isinstance(item, dict) or set(item) != {"name", "kind", "path", "sha256", "version"}:
            raise ReleaseError("runtime_dependency_schema_invalid")
        name, kind, path, digest, version = (item[k] for k in ("name", "kind", "path", "sha256", "version"))
        if (
            not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name)
            or name in seen
            or not isinstance(kind, str)
            or kind not in {"release-file", "release-tree", "system-file", "system-tree", "runtime-interface"}
            or not isinstance(path, str) or not path
            or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or not isinstance(version, str) or not version
        ):
            raise ReleaseError("runtime_dependency_value_invalid")
        seen.add(name)
        if kind == "release-file":
            rel = str(_safe_relpath(path))
            if rel not in by_path or by_path[rel]["sha256"] != digest:
                raise ReleaseError(f"runtime_dependency_missing_or_changed:{name}")
        elif kind == "release-tree":
            prefix = str(_safe_relpath(path)).rstrip("/") + "/"
            members = [entry for entry in files if entry["path"].startswith(prefix)]
            if not members or _file_table_digest(members) != digest:
                raise ReleaseError(f"runtime_dependency_missing_or_changed:{name}")
            if name == "python_dependencies" and any(
                PurePosixPath(entry["path"]).suffix == ".pth" for entry in members
            ):
                raise ReleaseError("executable_python_path_configuration_rejected")
        elif kind in {"system-file", "system-tree"}:
            if not Path(path).is_absolute():
                raise ReleaseError(f"system_dependency_path_not_absolute:{name}")
        elif kind == "runtime-interface":
            if not path.startswith("linux:"):
                raise ReleaseError(f"runtime_interface_invalid:{name}")
            if sha256_bytes(path.encode()) != digest:
                raise ReleaseError(f"runtime_interface_digest_invalid:{name}")
        output.append(dict(item))
    missing = REQUIRED_RUNTIME_DEPENDENCIES - seen
    if missing:
        raise ReleaseError("runtime_dependency_inventory_incomplete:" + ",".join(sorted(missing)))
    return sorted(output, key=lambda entry: entry["name"])


def generate_source_manifest(
    payload_root: str | Path,
    *,
    release_id: str,
    source_head: str,
    source_tree: str,
    commit_object_b64: str,
    source_manifest_sha256: str,
    launcher_identity: Mapping[str, Any],
    dependencies: list[dict[str, Any]],
    configuration_fingerprint: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Inventory a candidate payload; the result is explicitly unapproved."""
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("release_id_invalid")
    if not isinstance(source_head, str) or not GIT_ID_RE.fullmatch(source_head) or not isinstance(source_tree, str) or not GIT_ID_RE.fullmatch(source_tree):
        raise ReleaseError("source_head_or_tree_invalid")
    if not isinstance(source_manifest_sha256, str) or not SHA256_RE.fullmatch(source_manifest_sha256):
        raise ReleaseError("source_manifest_sha256_invalid")
    try:
        commit_object = base64.b64decode(commit_object_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseError("source_commit_object_invalid") from exc
    if (
        hashlib.sha1(b"commit " + str(len(commit_object)).encode() + b"\0" + commit_object).hexdigest() != source_head
        or not commit_object.startswith(f"tree {source_tree}\n".encode())
    ):
        raise ReleaseError("source_commit_object_identity_mismatch")
    if not isinstance(launcher_identity, Mapping) or not launcher_identity:
        raise ReleaseError("launcher_identity_missing")
    if configuration_fingerprint is not None and (not isinstance(configuration_fingerprint, str) or not SHA256_RE.fullmatch(configuration_fingerprint)):
        raise ReleaseError("configuration_fingerprint_invalid")
    files = _walk_release_files(Path(payload_root))
    if _git_tree_id(Path(payload_root).resolve(strict=True)) != source_tree:
        raise ReleaseError("payload_git_tree_does_not_match_source_tree")
    dependency_table = _validate_dependencies(dependencies, files)
    manifest = {
        "manifest_kind": "orchestrator-phase39-p0b-release",
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "stage": "SOURCE_BUILD",
        "approval_state": "NOT_APPROVED",
        "activation_state": "BLOCKED_PENDING_APPROVAL",
        "source": {"head": source_head, "tree": source_tree, "commit_object": commit_object_b64},
        "source_manifest_sha256": source_manifest_sha256,
        "release_file_inventory_sha256": _file_table_digest(files),
        "configuration_fingerprint": configuration_fingerprint,
        "launcher_identity": dict(launcher_identity),
        "runtime_dependencies": dependency_table,
        "runtime_dependency_inventory_sha256": sha256_bytes(canonical_json(dependency_table)),
        "files": files,
        "worksheet_sha256": None,
    }
    return manifest, sha256_bytes(canonical_json(manifest))


def write_source_manifest(manifest: Mapping[str, Any], path: str | Path) -> str:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise ReleaseError("manifest_target_exists")
    data = canonical_json(manifest)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o644)
    except OSError as exc:
        raise ReleaseError("manifest_create_failed") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ReleaseError("manifest_write_failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha256_bytes(data)


def _read_manifest(path: Path, expected_digest: str) -> tuple[dict[str, Any], str]:
    if not SHA256_RE.fullmatch(expected_digest):
        raise ReleaseError("expected_manifest_digest_required")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("manifest_open_failed") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError("manifest_file_type_or_link_count_invalid")
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReleaseError("manifest_changed_while_reading")
        info = after
    finally:
        os.close(fd)
    digest = sha256_bytes(raw)
    if digest != expected_digest:
        raise ReleaseError("manifest_digest_mismatch")
    if info.st_mode & 0o022:
        raise ReleaseError("manifest_writable_by_group_or_other")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("manifest_json_invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ReleaseError("manifest_not_canonical")
    if value.get("manifest_kind") != "orchestrator-phase39-p0b-release" or value.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("manifest_schema_unsupported")
    required = {
        "manifest_kind", "schema_version", "release_id", "stage", "approval_state",
        "activation_state", "source", "source_manifest_sha256", "release_file_inventory_sha256", "configuration_fingerprint",
        "launcher_identity", "runtime_dependencies", "runtime_dependency_inventory_sha256",
        "files", "worksheet_sha256",
    }
    if set(value) != required:
        raise ReleaseError("manifest_fields_incomplete_or_unknown")
    if value["stage"] != "SOURCE_BUILD" or value["approval_state"] != "NOT_APPROVED" or value["activation_state"] != "BLOCKED_PENDING_APPROVAL":
        raise ReleaseError("source_build_cannot_claim_approval_or_activation")
    if not RELEASE_ID_RE.fullmatch(value["release_id"]):
        raise ReleaseError("release_id_invalid")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"head", "tree", "commit_object"} or not all(isinstance(source.get(k), str) and GIT_ID_RE.fullmatch(source[k]) for k in ("head", "tree")) or not isinstance(source.get("commit_object"), str):
        raise ReleaseError("manifest_source_identity_invalid")
    try:
        commit_object = base64.b64decode(source["commit_object"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseError("manifest_commit_object_invalid") from exc
    if (
        hashlib.sha1(b"commit " + str(len(commit_object)).encode() + b"\0" + commit_object).hexdigest() != source["head"]
        or not commit_object.startswith(f"tree {source['tree']}\n".encode())
    ):
        raise ReleaseError("manifest_commit_object_identity_mismatch")
    if not isinstance(value["files"], list) or not value["files"]:
        raise ReleaseError("manifest_file_inventory_missing")
    previous = ""
    paths: set[str] = set()
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mode", "nlink"}:
            raise ReleaseError("manifest_file_entry_invalid")
        rel = str(_safe_relpath(entry["path"]))
        if rel <= previous or rel in paths:
            raise ReleaseError("manifest_file_order_or_duplicate_invalid")
        previous = rel
        paths.add(rel)
        if not SHA256_RE.fullmatch(entry["sha256"]) or type(entry["size"]) is not int or entry["size"] < 0 or entry["mode"] not in {0o444, 0o555, 0o600} or entry["nlink"] != 1:
            raise ReleaseError("manifest_file_metadata_invalid")
    if not SHA256_RE.fullmatch(value["source_manifest_sha256"]) or value["release_file_inventory_sha256"] != _file_table_digest(value["files"]):
        raise ReleaseError("release_file_inventory_digest_invalid")
    if value["runtime_dependency_inventory_sha256"] != sha256_bytes(canonical_json(value["runtime_dependencies"])):
        raise ReleaseError("runtime_dependency_inventory_digest_invalid")
    _validate_dependencies(value["runtime_dependencies"], value["files"])
    if value["worksheet_sha256"] is not None:
        raise ReleaseError("source_build_must_not_embed_authoritative_worksheet")
    return value, digest


def _check_dir(path: Path, *, uid: int, allow_sticky_world_write: bool = True) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"directory_missing:{path}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise ReleaseError(f"directory_type_or_owner_invalid:{path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        sticky_exception = (
            allow_sticky_world_write and uid == 0 and info.st_uid == 0
            and bool(mode & stat.S_ISVTX)
        )
        if not sticky_exception:
            raise ReleaseError(f"directory_writable_by_untrusted_user:{path}")
    return info


def validate_parent_directories(path: str | Path, *, uid: int = 0) -> tuple[Path, ...]:
    """Check every existing component from `/` through the given directory."""
    target = Path(path)
    if not target.is_absolute():
        raise ReleaseError("protected_path_not_absolute")
    if any(part in {".", ".."} for part in target.parts):
        raise ReleaseError("protected_path_not_canonical")
    resolved = Path("/")
    checked = [resolved]
    _check_dir(resolved, uid=uid)
    for part in target.parts[1:]:
        resolved = resolved / part
        _check_dir(resolved, uid=uid)
        checked.append(resolved)
    return tuple(checked)


def _verify_system_dependency(dependency: Mapping[str, Any], *, uid: int) -> None:
    kind, path_text, expected = dependency["kind"], dependency["path"], dependency["sha256"]
    path = Path(path_text)
    if kind == "system-file":
        _validate_parent_directories(path.parent, uid=uid)
        digest, info = _hash_regular_file(path)
        executable_names = {"python_interpreter", "bash", "env", "nohup", "cmp", "stat", "openssl"}
        if info.st_uid != uid or info.st_mode & 0o022 or digest != expected or (dependency["name"] in executable_names and not info.st_mode & 0o111):
            raise ReleaseError(f"system_dependency_unprotected_or_changed:{dependency['name']}")
    elif kind == "system-tree":
        validate_parent_directories(path, uid=uid)
        files = _walk_release_files(path, required_uid=uid)
        if _file_table_digest(files) != expected:
            raise ReleaseError(f"system_dependency_tree_changed:{dependency['name']}")


def _verify_payload_tree(root: Path, manifest: Mapping[str, Any], *, uid: int) -> None:
    expected: dict[str, Mapping[str, Any]] = {entry["path"]: entry for entry in manifest["files"]}
    expected_dirs = {"." , "logs"}
    for rel in expected:
        parts = PurePosixPath(rel).parts[:-1]
        for index in range(1, len(parts) + 1):
            expected_dirs.add("/".join(parts[:index]))
    seen: set[str] = set()
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        rel_dir = directory.relative_to(root).as_posix()
        if rel_dir not in expected_dirs:
            raise ReleaseError(f"unmanifested_release_directory:{rel_dir}")
        dir_info = _check_dir(directory, uid=uid)
        if rel_dir == "." and stat.S_IMODE(dir_info.st_mode) != 0o555:
            raise ReleaseError("release_root_not_immutable")
        if rel_dir == "logs":
            if stat.S_IMODE(dir_info.st_mode) != 0o700:
                raise ReleaseError("runtime_log_directory_permissions_unsafe")
        elif rel_dir != "." and stat.S_IMODE(dir_info.st_mode) != 0o555:
            raise ReleaseError(f"release_directory_not_immutable:{rel_dir}")
        dirs.sort()
        names.sort()
        for name in tuple(dirs):
            subdir = directory / name
            if subdir.is_symlink():
                raise ReleaseError(f"symlink_directory_rejected:{name}")
            sub_info = _check_dir(subdir, uid=uid)
            rel_subdir = subdir.relative_to(root).as_posix()
            expected_mode = 0o700 if rel_subdir == "logs" else 0o555
            if stat.S_IMODE(sub_info.st_mode) != expected_mode:
                raise ReleaseError(f"release_directory_mode_invalid:{rel_subdir}")
        for name in names:
            item = directory / name
            rel = item.relative_to(root).as_posix()
            if rel in {MANIFEST_NAME, STATE_NAME, RELEASE_IDENTITY_NAME, "logs/worker.log"}:
                info = item.lstat()
                expected_mode = 0o600 if rel == "logs/worker.log" else 0o444
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid or info.st_mode & 0o022 or stat.S_IMODE(info.st_mode) != expected_mode:
                    raise ReleaseError(f"runtime_control_file_unsafe:{rel}")
                if rel == "logs/worker.log" and stat.S_IMODE(info.st_mode) & 0o077:
                    raise ReleaseError("worker_log_permissions_unsafe")
                if rel == STATE_NAME:
                    continue
                if rel == MANIFEST_NAME:
                    continue
                if rel == RELEASE_IDENTITY_NAME:
                    try:
                        marker_raw = item.read_bytes()
                        marker = json.loads(marker_raw.decode("utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise ReleaseError("release_identity_marker_invalid") from exc
                    expected_marker = {
                        "schema_version": 1,
                        "kind": "git-archive-release",
                        "candidate_head": manifest["source"]["head"],
                        "candidate_tree": manifest["source"]["tree"],
                        "commit_object": manifest["source"]["commit_object"],
                    }
                    if canonical_json(marker) != marker_raw or marker != expected_marker or stat.S_IMODE(info.st_mode) != 0o444:
                        raise ReleaseError("release_identity_marker_untrusted")
                    continue
                if rel == "logs/worker.log":
                    continue
            if rel not in expected:
                raise ReleaseError(f"unmanifested_release_file:{rel}")
            digest, info = _hash_regular_file(item)
            specification = expected[rel]
            if (
                info.st_uid != uid
                or info.st_nlink != 1
                or info.st_mode & 0o022
                or stat.S_IMODE(info.st_mode) != specification["mode"]
                or info.st_size != specification["size"]
                or digest != specification["sha256"]
            ):
                raise ReleaseError(f"release_file_unprotected_or_changed:{rel}")
            seen.add(rel)
    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise ReleaseError("release_file_missing:" + ",".join(missing[:5]))
    marker_path = root / RELEASE_IDENTITY_NAME
    try:
        marker_info = marker_path.lstat()
    except OSError as exc:
        raise ReleaseError("release_identity_marker_missing") from exc
    if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != uid or marker_info.st_nlink != 1 or marker_info.st_mode & 0o222:
        raise ReleaseError("release_identity_marker_unprotected")
    for item in manifest["runtime_dependencies"]:
        if item["kind"] in {"system-file", "system-tree"}:
            _verify_system_dependency(item, uid=uid)


def verify_installed_release(
    release_root: str | Path,
    *,
    expected_manifest_sha256: str,
    expected_source_head: str,
    expected_source_tree: str,
    expected_launcher_identity: Mapping[str, Any],
    uid: int = 0,
) -> dict[str, Any]:
    root = Path(release_root)
    if not root.is_absolute():
        raise ReleaseError("release_root_not_absolute")
    validate_parent_directories(root, uid=uid)
    info = _check_dir(root, uid=uid)
    if info.st_mode & 0o022:
        raise ReleaseError("release_root_writable_by_untrusted_user")
    manifest, digest = _read_manifest(root / MANIFEST_NAME, expected_manifest_sha256)
    if manifest["source"]["head"] != expected_source_head or manifest["source"]["tree"] != expected_source_tree:
        raise ReleaseError("source_head_or_tree_mismatch")
    if manifest["launcher_identity"] != dict(expected_launcher_identity):
        raise ReleaseError("launcher_identity_mismatch")
    if Path(root).name != manifest["release_id"]:
        raise ReleaseError("release_directory_id_mismatch")
    _verify_payload_tree(root, manifest, uid=uid)
    if _git_tree_id(root) != expected_source_tree:
        raise ReleaseError("installed_release_git_tree_mismatch")
    state_path = root / STATE_NAME
    try:
        state_raw = state_path.read_bytes()
        state_info = state_path.lstat()
        state = json.loads(state_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release_state_missing_or_invalid") from exc
    if (
        not stat.S_ISREG(state_info.st_mode) or state_info.st_uid != uid
        or state_info.st_nlink != 1 or state_info.st_mode & 0o022
        or state != {"schema_version": 1, "status": "RELEASE_INSTALLED_NOT_APPROVED", "release_id": manifest["release_id"], "manifest_sha256": digest}
        or canonical_json(state) != state_raw
    ):
        raise ReleaseError("release_state_untrusted")
    return {"status": state["status"], "release_id": manifest["release_id"], "manifest_sha256": digest, "source": manifest["source"]}


def _copy_payload(source_root: Path, stage_root: Path, files: Sequence[Mapping[str, Any]]) -> None:
    stage_root.mkdir(mode=0o700)
    for entry in files:
        rel = _safe_relpath(entry["path"])
        parent = stage_root
        for component in rel.parts[:-1]:
            parent = parent / component
            if not parent.exists():
                parent.mkdir(mode=0o755)
        src = source_root.joinpath(*rel.parts)
        destination = stage_root.joinpath(*rel.parts)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(destination, flags, entry["mode"])
        try:
            source_flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                source_fd = os.open(src, source_flags)
            except OSError as exc:
                raise ReleaseError(f"source_open_failed:{entry['path']}") from exc
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ReleaseError(f"source_type_or_link_count_invalid:{entry['path']}")
                digest = hashlib.sha256()
                copied_size = 0
                while True:
                    block = os.read(source_fd, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    copied_size += len(block)
                    view = memoryview(block)
                    while view:
                        n = os.write(fd, view)
                        if n <= 0:
                            raise ReleaseError("release_copy_short_write")
                        view = view[n:]
                after = os.fstat(source_fd)
                identity_before = (
                    before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns,
                )
                identity_after = (
                    after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns,
                )
                if (
                    identity_before != identity_after
                    or copied_size != entry["size"]
                    or digest.hexdigest() != entry["sha256"]
                ):
                    raise ReleaseError(f"source_changed_after_manifest:{entry['path']}")
            finally:
                os.close(source_fd)
            os.fchmod(fd, entry["mode"])
            os.fsync(fd)
        finally:
            os.close(fd)
    for current, dirs, _ in os.walk(stage_root, topdown=False):
        for name in dirs:
            os.chmod(Path(current) / name, 0o755)
        fd = os.open(current, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_private_file(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ReleaseError("release_metadata_short_write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_protected_installation_parent(value: str | Path) -> Path:
    parent = Path(value)
    if not parent.is_absolute() or parent != PROTECTED_INSTALLATION_PARENT:
        raise ReleaseError("installation_parent_must_be_fixed_protected_release_root")
    return parent


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    for child in path.iterdir():
        _remove_tree(child)
    path.rmdir()


def install_release(
    source_root: str | Path,
    manifest_path: str | Path,
    installation_parent: str | Path,
    *,
    expected_manifest_sha256: str,
    expected_source_head: str,
    expected_source_tree: str,
    expected_launcher_identity: Mapping[str, Any],
    uid: int = 0,
    effective_uid: int | None = None,
    fault_hook: Any = None,
) -> dict[str, Any]:
    """Atomically install an inert release candidate below a protected parent."""
    actual_euid = os.geteuid() if effective_uid is None else effective_uid
    if actual_euid != uid:
        raise ReleaseError("release_install_requires_root")
    parent = Path(installation_parent)
    validate_parent_directories(parent, uid=uid)
    _check_dir(parent, uid=uid)
    manifest, digest = _read_manifest(Path(manifest_path), expected_manifest_sha256)
    if manifest["source"]["head"] != expected_source_head or manifest["source"]["tree"] != expected_source_tree:
        raise ReleaseError("source_head_or_tree_mismatch")
    if manifest["launcher_identity"] != dict(expected_launcher_identity):
        raise ReleaseError("launcher_identity_mismatch")
    final = parent / manifest["release_id"]
    if final.exists() or final.is_symlink():
        raise ReleaseError("release_destination_exists_or_partial")
    lock_path = parent / f"{LOCK_PREFIX}{manifest['release_id']}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise ReleaseError("concurrent_or_interrupted_installation_lock") from exc
    stage = parent / f".{manifest['release_id']}.staging-{uuid.uuid4().hex}"
    renamed = False
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != uid or lock_info.st_nlink != 1:
            raise ReleaseError("installation_lock_unsafe")
        os.write(lock_fd, canonical_json({"release_id": manifest["release_id"], "manifest_sha256": digest, "status": "INSTALLING"}))
        os.fsync(lock_fd)
        source = Path(source_root).resolve(strict=True)
        source_manifest, _ = _read_manifest(Path(manifest_path), expected_manifest_sha256)
        _copy_payload(source, stage, source_manifest["files"])
        _write_private_file(stage / MANIFEST_NAME, canonical_json(manifest), 0o444)
        marker = {
            "schema_version": 1,
            "kind": "git-archive-release",
            "candidate_head": manifest["source"]["head"],
            "candidate_tree": manifest["source"]["tree"],
            "commit_object": manifest["source"]["commit_object"],
        }
        _write_private_file(stage / RELEASE_IDENTITY_NAME, canonical_json(marker), 0o444)
        (stage / "logs").mkdir(mode=0o700)
        _write_private_file(stage / "logs/worker.log", b"", 0o600)
        state = {
            "schema_version": 1,
            "status": "RELEASE_INSTALLED_NOT_APPROVED",
            "release_id": manifest["release_id"],
            "manifest_sha256": digest,
        }
        _write_private_file(stage / STATE_NAME, canonical_json(state), 0o444)
        for current, dirs, _ in os.walk(stage, topdown=False):
            current_path = Path(current)
            for name in dirs:
                candidate = current_path / name
                if candidate.relative_to(stage).as_posix() != "logs":
                    os.chmod(candidate, 0o555)
            if current_path.relative_to(stage).as_posix() != "logs":
                os.chmod(current_path, 0o555)
        fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if fault_hook is not None:
            fault_hook("after_stage")
        os.rename(stage, final)
        renamed = True
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        if fault_hook is not None:
            fault_hook("after_rename")
        checked = verify_installed_release(
            final,
            expected_manifest_sha256=digest,
            expected_source_head=expected_source_head,
            expected_source_tree=expected_source_tree,
            expected_launcher_identity=expected_launcher_identity,
            uid=uid,
        )
        return {**checked, "installation": "ATOMIC_INSTALL_VERIFIED", "activation": "BLOCKED_PENDING_APPROVAL"}
    except BaseException:
        if not renamed:
            _remove_tree(stage)
        # If rename already happened, preserve the candidate for explicit
        # inspection; it is never silently removed or promoted.
        raise
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    build = subs.add_parser("manifest")
    build.add_argument("--payload-root", required=True)
    build.add_argument("--release-id", required=True)
    build.add_argument("--source-head", required=True)
    build.add_argument("--source-tree", required=True)
    build.add_argument("--commit-object-b64", required=True)
    build.add_argument("--source-manifest-sha256", required=True)
    build.add_argument("--launcher-json", required=True)
    build.add_argument("--dependencies-json", required=True)
    build.add_argument("--configuration-fingerprint")
    build.add_argument("--output", required=True)
    install = subs.add_parser("install")
    install.add_argument("--source-root", required=True)
    install.add_argument("--manifest", required=True)
    install.add_argument("--installation-parent", required=True)
    install.add_argument("--expected-manifest-sha256", required=True)
    install.add_argument("--source-head", required=True)
    install.add_argument("--source-tree", required=True)
    install.add_argument("--launcher-json", required=True)
    verify = subs.add_parser("verify")
    verify.add_argument("--release-root", required=True)
    verify.add_argument("--expected-manifest-sha256", required=True)
    verify.add_argument("--source-head", required=True)
    verify.add_argument("--source-tree", required=True)
    verify.add_argument("--launcher-json", required=True)
    args = parser.parse_args()
    try:
        launcher = json.loads(Path(args.launcher_json).read_text(encoding="utf-8"))
        if args.command == "manifest":
            dependencies = json.loads(Path(args.dependencies_json).read_text(encoding="utf-8"))
            manifest, digest = generate_source_manifest(
                args.payload_root,
                release_id=args.release_id,
                source_head=args.source_head,
                source_tree=args.source_tree,
                commit_object_b64=args.commit_object_b64,
                source_manifest_sha256=args.source_manifest_sha256,
                launcher_identity=launcher,
                dependencies=dependencies,
                configuration_fingerprint=args.configuration_fingerprint,
            )
            write_source_manifest(manifest, args.output)
            print(json.dumps({"stage": "SOURCE_BUILD", "approval_state": "NOT_APPROVED", "manifest_sha256": digest}, sort_keys=True))
            return 0
        if args.command == "install":
            installation_parent = validate_protected_installation_parent(
                args.installation_parent
            )
            result = install_release(
                args.source_root, args.manifest, installation_parent,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_source_head=args.source_head,
                expected_source_tree=args.source_tree,
                expected_launcher_identity=launcher,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        result = verify_installed_release(
            args.release_root,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_source_head=args.source_head,
            expected_source_tree=args.source_tree,
            expected_launcher_identity=launcher,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
