"""Offline validation primitives for a future P0B runtime wheel lock.

This module deliberately does not resolve, download, build, or install packages.
The fixture-only validation switch is for isolated tests and is not exposed by
the command-line interface.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


APPROVED_INDEX = "https://pypi.org/simple"
TARGET = {
    "distribution": "Ubuntu",
    "release": "24.04",
    "architecture": "aarch64",
    "implementation": "CPython",
    "python_version": "3.12",
    "abi": "cp312",
    "glibc_baseline": "2.39",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")
PLATFORM_RE = re.compile(r"^manylinux_(2_[0-9]+)_aarch64$")


class WheelhousePolicyError(ValueError):
    """A wheelhouse does not satisfy the P0B offline artifact policy."""


def _fail(code: str) -> None:
    raise WheelhousePolicyError(code)


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _parse_wheel_tags(filename: str) -> tuple[str, str, str, str]:
    if not isinstance(filename, str) or not filename.endswith(".whl"):
        _fail("wheel_filename_required")
    stem = filename[:-4]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4 or any(not part for part in parts):
        _fail("wheel_filename_invalid")
    prefix, python_tag, abi_tag, platform_tag = parts
    return prefix, python_tag, abi_tag, platform_tag


def _tag_is_supported(python_tag: str, abi_tag: str, platform_tag: str) -> bool:
    python_tags = set(python_tag.split("."))
    abi_tags = set(abi_tag.split("."))
    platform_tags = set(platform_tag.split("."))
    pure = (
        python_tags <= {"py2", "py3"}
        and "py3" in python_tags
        and abi_tags == {"none"}
        and platform_tags == {"any"}
    )
    if pure:
        return True
    if not python_tags or not python_tags <= {"cp312", "cp311", "cp310", "cp39", "cp38"}:
        return False
    if not abi_tags or not abi_tags <= {"cp312", "abi3"}:
        return False
    for platform_tag in platform_tags:
        if platform_tag == "manylinux2014_aarch64":
            continue  # PEP 599 alias for the glibc 2.17 aarch64 baseline.
        match = PLATFORM_RE.fullmatch(platform_tag)
        if not match or int(match.group(1).split("_")[1]) > 39:
            return False
    return bool(platform_tags)


def _metadata_from_wheel(path: Path) -> tuple[dict[str, str], set[str], set[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                _fail("wheel_duplicate_member")
            metadata_members = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_members = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_members) != 1 or len(wheel_members) != 1:
                _fail("wheel_metadata_layout_invalid")
            metadata = _parse_headers(archive.read(metadata_members[0]).decode("utf-8"))
            wheel_text = archive.read(wheel_members[0]).decode("utf-8")
            wheel_tags = {
                line.partition(":")[2].strip()
                for line in wheel_text.splitlines()
                if line.startswith("Tag:")
            }
            native_members = {
                name for name in names
                if name.lower().endswith((".so", ".so.0", ".dylib", ".dll"))
            }
            for name in names:
                if name.endswith(".pth"):
                    _fail("wheel_pth_forbidden")
                if Path(name).name in {"sitecustomize.py", "usercustomize.py"}:
                    _fail("wheel_sitecustomize_forbidden")
                info = archive.getinfo(name)
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    _fail("wheel_symlink_forbidden")
            return metadata, wheel_tags, native_members
    except WheelhousePolicyError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise WheelhousePolicyError("wheel_archive_invalid") from exc


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, _separator, value = line.partition(":")
        headers[key.lower()] = value.strip()
    return headers


def validate_wheelhouse(
    lock: Mapping[str, Any],
    wheelhouse: str | Path,
    authorization: Mapping[str, Any] | None,
    *,
    fixture_only: bool = False,
    approved_index: str = APPROVED_INDEX,
) -> None:
    """Validate lock, approval binding, inventory, and wheel bytes offline.

    ``fixture_only=True`` accepts only explicitly synthetic fixture status and
    exists solely so tests can exercise the policy without implying artifact
    provenance. Production callers must leave it false.
    """

    status = "fixture-only" if fixture_only else "reviewed"
    if not isinstance(lock, Mapping) or lock.get("schema_version") != 1:
        _fail("runtime_lock_schema_invalid")
    if lock.get("status") != status:
        _fail("runtime_lock_status_unreviewed")
    if lock.get("target") != TARGET:
        _fail("runtime_target_mismatch")
    if lock.get("index_url") != approved_index:
        _fail("runtime_index_unapproved")
    resolver = lock.get("resolver")
    if not isinstance(resolver, Mapping) or not all(
        isinstance(resolver.get(key), str) and resolver[key]
        for key in ("name", "version")
    ):
        _fail("runtime_resolver_provenance_missing")
    if not _utc_timestamp(lock.get("resolved_at")):
        _fail("runtime_resolution_timestamp_invalid")
    if not isinstance(authorization, Mapping):
        _fail("acquisition_authorization_missing")
    auth_status = "fixture-only" if fixture_only else "approved"
    auth_id = lock.get("authorization_id")
    if (
        authorization.get("status") != auth_status
        or not isinstance(auth_id, str)
        or not auth_id
        or authorization.get("id") != auth_id
        or authorization.get("approved_index") != approved_index
        or authorization.get("target") != TARGET
        or authorization.get("action") != "download-only"
    ):
        _fail("acquisition_authorization_mismatch")

    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        _fail("runtime_package_inventory_empty")
    root = Path(wheelhouse)
    if not root.is_dir() or root.is_symlink():
        _fail("wheelhouse_directory_invalid")
    by_name: dict[str, Mapping[str, Any]] = {}
    listed_files: set[str] = set()
    for package in packages:
        if not isinstance(package, Mapping):
            _fail("runtime_package_record_invalid")
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not DIST_RE.fullmatch(name):
            _fail("runtime_package_name_invalid")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            _fail("runtime_package_version_not_exact")
        canonical = _canonical_name(name)
        if canonical in by_name:
            _fail("runtime_package_duplicate_or_conflicting_version")
        by_name[canonical] = package
        if package.get("editable") is not False:
            _fail("runtime_editable_dependency_forbidden")
        if package.get("direct_url") is not None:
            _fail("runtime_direct_url_forbidden")
        if package.get("source_index") != approved_index:
            _fail("runtime_package_index_unapproved")
        provenance = "fixture-only" if fixture_only else "verified"
        review = "fixture-only" if fixture_only else "approved"
        if package.get("provenance_status") != provenance:
            _fail("runtime_artifact_provenance_unknown")
        if package.get("artifact_review_status") != review:
            _fail("runtime_artifact_review_incomplete")
        if package.get("license_review_status") != review:
            _fail("runtime_license_review_incomplete")
        if package.get("security_review_status") != review:
            _fail("runtime_security_review_incomplete")
        if not _utc_timestamp(package.get("acquired_at")):
            _fail("runtime_acquisition_timestamp_invalid")
        if package.get("resolver_version") != resolver["version"]:
            _fail("runtime_package_resolver_mismatch")
        digest = package.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            _fail("runtime_wheel_hash_missing_or_invalid")
        filename = package.get("filename")
        prefix, python_tag, abi_tag, platform_tag = _parse_wheel_tags(filename)
        listed_files.add(filename)
        tags = package.get("tags")
        if not isinstance(tags, Mapping) or tags != {
            "python": python_tag,
            "abi": abi_tag,
            "platform": platform_tag,
        }:
            _fail("runtime_wheel_filename_tag_mismatch")
        if not _tag_is_supported(python_tag, abi_tag, platform_tag):
            _fail("runtime_wheel_target_tag_unsupported")
        prefix_name = prefix.rsplit("-", 1)[0] if prefix.count("-") >= 2 else prefix
        expected_dist_prefix = _canonical_name(name).replace("-", "_")
        if not prefix.startswith(expected_dist_prefix + "-"):
            _fail("runtime_wheel_filename_name_mismatch")
        path = root / filename
        if not path.is_file() or path.is_symlink():
            _fail("runtime_wheel_missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            _fail("runtime_wheel_hash_mismatch")
        metadata, wheel_tags, native_members = _metadata_from_wheel(path)
        if _canonical_name(metadata.get("name", "")) != canonical:
            _fail("runtime_wheel_metadata_name_mismatch")
        if metadata.get("version") != version:
            _fail("runtime_wheel_metadata_version_mismatch")
        expected_wheel_tags = {
            f"{python_item}-{abi_item}-{platform_item}"
            for python_item in python_tag.split(".")
            for abi_item in abi_tag.split(".")
            for platform_item in platform_tag.split(".")
        }
        if wheel_tags != expected_wheel_tags:
            _fail("runtime_wheel_metadata_tags_mismatch")
        expected_prefix = f"{expected_dist_prefix}-{version}"
        if prefix != expected_prefix and not prefix.startswith(expected_prefix + "-"):
            _fail("runtime_wheel_filename_version_mismatch")
        expected_native = package.get("native_members")
        if not isinstance(expected_native, list) or set(expected_native) != native_members:
            _fail("runtime_native_extension_inventory_mismatch")
        native_review = "fixture-only" if fixture_only else "approved"
        if native_members:
            if package.get("native_code") is not True:
                _fail("runtime_unexpected_native_extension")
            if package.get("native_review_status") != native_review:
                _fail("runtime_native_extension_unreviewed")
        elif package.get("native_code") is not False:
            _fail("runtime_native_code_declaration_mismatch")
        requirements = package.get("requires")
        if not isinstance(requirements, list) or any(
            not isinstance(item, str) or not DIST_RE.fullmatch(item)
            for item in requirements
        ):
            _fail("runtime_dependency_graph_invalid")

    for package in packages:
        if any(_canonical_name(dep) not in by_name for dep in package["requires"]):
            _fail("runtime_transitive_dependency_missing")
    actual_files = {p.name for p in root.iterdir() if p.is_file() or p.is_symlink()}
    if actual_files != listed_files:
        if any(name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip")) for name in actual_files - listed_files):
            _fail("runtime_sdist_forbidden")
        _fail("runtime_wheelhouse_inventory_mismatch")


def offline_install_argv(
    python: str,
    requirements_file: str,
    wheelhouse: str,
    *,
    network_enabled: bool = False,
) -> list[str]:
    """Return a construction command that cannot resolve from package indexes."""

    if not all(isinstance(value, str) and value for value in (python, requirements_file, wheelhouse)):
        _fail("offline_install_argument_invalid")
    if network_enabled is not False:
        _fail("offline_network_forbidden")
    return [
        "/usr/bin/env",
        "-i",
        "HOME=/nonexistent",
        "PATH=/usr/bin:/bin",
        "PIP_CONFIG_FILE=/dev/null",
        "PYTHONNOUSERSITE=1",
        python,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        wheelhouse,
        "--only-binary=:all:",
        "--no-deps",
        "--require-hashes",
        "--disable-pip-version-check",
        "-r",
        requirements_file,
    ]


def main() -> int:
    """Validate a reviewed lock and acquired wheelhouse without networking."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--wheelhouse", required=True)
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()
    try:
        lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
        authorization = json.loads(Path(args.authorization).read_text(encoding="utf-8"))
        validate_wheelhouse(lock, args.wheelhouse, authorization)
    except (OSError, json.JSONDecodeError, WheelhousePolicyError) as exc:
        parser.error(str(exc))
    print("offline_wheelhouse_policy=valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
