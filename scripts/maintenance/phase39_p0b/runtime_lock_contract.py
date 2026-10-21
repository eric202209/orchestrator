"""Closed offline Worker runtime-lock and artifact acquisition contract.

This module validates lock documents and already-present wheel files only. It
does not resolve, download, build, install, import application code, or invoke
external tools. A complete Python wheel set still does not prove ELF or host
runtime closure.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


LOCK_KIND = "orchestrator-worker-runtime-lock"
LOCK_SCHEMA_VERSION = 1
AUTHORIZATION_KIND = "orchestrator-worker-runtime-acquisition-authorization"
AUTHORIZATION_SCHEMA_VERSION = 1
APPROVED_INDEX = "https://pypi.org/simple"

PLATFORM_TARGET = {
    "distribution": "Ubuntu",
    "release": "24.04",
    "architecture": "aarch64",
    "libc_name": "glibc",
    "libc_baseline": "2.39",
    "supported_wheel_tag_policy": {
        "pure_tag": "py3-none-any",
        "cpython_tag": "cp312",
        "cpython_abi": "cp312",
        "abi3_min_python_tag": "cp36",
        "manylinux_architecture": "aarch64",
        "manylinux_min_glibc": "2.17",
        "manylinux_max_glibc": "2.39",
    },
}
PYTHON_RUNTIME_POLICY = {
    "implementation": "CPython",
    "abi": "cp312",
    "soabi": "cpython-312-aarch64-linux-gnu",
}

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")
WHEEL_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl\Z")
PYTHON_FULL_VERSION_RE = re.compile(r"3\.12\.[0-9]+(?:[ab]|rc[0-9]+)?\Z")
PEP440_SIMPLE_VERSION_RE = re.compile(
    r"^(?P<epoch>[0-9]+!)?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>a[0-9]+|b[0-9]+|rc[0-9]+)?"
    r"(?P<post>\.post[0-9]+)?(?P<dev>\.dev[0-9]+)?"
    r"(?P<local>\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?$",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\Z")
MARKER_TOKEN_RE = re.compile(
    r"\s*(?:(?P<operator>not\s+in|and|or|in|===|~=|==|!=|<=|>=|<|>|\(|\))"
    r"|(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)
REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9][A-Za-z0-9._-]*(?:\s*,\s*[A-Za-z0-9][A-Za-z0-9._-]*)*)\])?"
    r"(?P<specifier>[^;@]*)"
    r"(?:;\s*(?P<marker>.+))?\s*$"
)
MANYLINUX_RE = re.compile(r"manylinux_2_([0-9]+)_aarch64\Z")
SIMPLE_MANYLINUX_RE = re.compile(r"manylinux2014_aarch64\Z")


class RuntimeLockError(ValueError):
    """The runtime lock, authorization, or wheelhouse violates its contract."""


@dataclass(frozen=True)
class AcquisitionDecision:
    """Read-only result for the exact, already-present artifact set."""

    status: str
    missing_artifacts: tuple[str, ...]
    blockers: tuple[str, ...]
    runtime_assembly_eligible: bool = False


def _fail(code: str) -> None:
    raise RuntimeLockError(code)


def _expect_fields(value: Any, expected: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(code)
    return value


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or TIME_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _reject_ambiguous_numbers(value: Any) -> None:
    if isinstance(value, float):
        _fail("runtime_lock_float_forbidden")
    if isinstance(value, dict):
        for nested in value.values():
            _reject_ambiguous_numbers(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_ambiguous_numbers(nested)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON with the WP1-compatible UTF-8 canonical rules."""

    _reject_ambiguous_numbers(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeLockError("runtime_lock_json_invalid") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("runtime_lock_duplicate_json_key")
        result[key] = value
    return result


def parse_canonical_runtime_lock(raw: bytes) -> dict[str, Any]:
    """Parse a canonical lock file, rejecting duplicate keys and alternate JSON."""

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_float=lambda _value: _fail("runtime_lock_float_forbidden"),
            parse_constant=lambda _value: _fail("runtime_lock_constant_forbidden"),
        )
    except RuntimeLockError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeLockError("runtime_lock_json_invalid") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        _fail("runtime_lock_json_noncanonical")
    validate_runtime_lock(value, fixture_only=(value.get("review_status") == "fixture-only"))
    return value


def runtime_lock_digest(lock: Mapping[str, Any]) -> str:
    """Return SHA-256 over the deterministic canonical lock representation."""

    validate_runtime_lock(lock, fixture_only=(lock.get("review_status") == "fixture-only"))
    return hashlib.sha256(canonical_json_bytes(lock)).hexdigest()


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None and not parsed.fragment


def _tokenize_marker(value: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(value):
        match = MARKER_TOKEN_RE.match(value, position)
        if match is None:
            if value[position:].strip() == "":
                break
            _fail("runtime_marker_syntax_invalid")
        position = match.end()
        if match.group("operator") is not None:
            tokens.append(("op", re.sub(r"\s+", " ", match.group("operator").lower())))
        elif match.group("string") is not None:
            raw = match.group("string")
            quote = raw[0]
            body = raw[1:-1]
            body = body.replace("\\" + quote, quote).replace("\\\\", "\\")
            tokens.append(("value", body))
        else:
            tokens.append(("identifier", match.group("identifier")))
    return tokens


def _version_tuple(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) is None:
        _fail("runtime_marker_version_value_unsupported")
    return tuple(int(part) for part in value.split("."))


def _marker_value_compare(left: str, operator: str, right: str, variable: str) -> bool:
    if operator in {"in", "not in"}:
        result = left in right
        return result if operator == "in" else not result
    if variable in {"python_version", "python_full_version", "implementation_version"}:
        left_value, right_value = _version_tuple(left), _version_tuple(right)
        length = max(len(left_value), len(right_value))
        left_value += (0,) * (length - len(left_value))
        right_value += (0,) * (length - len(right_value))
    else:
        left_value, right_value = left, right
    if operator == "==":
        return left_value == right_value
    if operator == "===":
        return left == right
    if operator == "!=":
        return left_value != right_value
    if operator == "<":
        return left_value < right_value
    if operator == "<=":
        return left_value <= right_value
    if operator == ">":
        return left_value > right_value
    if operator == ">=":
        return left_value >= right_value
    if operator == "~=":
        if variable not in {"python_version", "python_full_version", "implementation_version"}:
            _fail("runtime_marker_operator_unsupported")
        lower = _version_tuple(left)
        upper = _version_tuple(right)
        if len(upper) == 1:
            ceiling = (upper[0] + 1,)
        else:
            ceiling = upper[:-2] + (upper[-2] + 1,)
        width = max(len(lower), len(upper), len(ceiling))
        lower += (0,) * (width - len(lower))
        upper += (0,) * (width - len(upper))
        ceiling += (0,) * (width - len(ceiling))
        return lower >= upper and lower < ceiling
    _fail("runtime_marker_operator_unsupported")


def evaluate_marker(marker: str | None, environment: Mapping[str, str]) -> bool:
    """Evaluate the deterministic PEP 508 marker subset for the locked target."""

    if marker is None:
        return True
    if not isinstance(marker, str) or not marker.strip():
        _fail("runtime_marker_syntax_invalid")
    tokens = _tokenize_marker(marker)
    index = 0

    def parse_operand() -> tuple[str, str]:
        nonlocal index
        if index >= len(tokens):
            _fail("runtime_marker_syntax_invalid")
        kind, value = tokens[index]
        index += 1
        if kind == "value":
            return "literal", value
        if kind == "identifier":
            if value not in environment:
                _fail("runtime_marker_variable_unsupported")
            return value, environment[value]
        _fail("runtime_marker_syntax_invalid")

    def parse_comparison() -> bool:
        nonlocal index
        if index < len(tokens) and tokens[index] == ("op", "("):
            index += 1
            nested = parse_or()
            if index >= len(tokens) or tokens[index] != ("op", ")"):
                _fail("runtime_marker_syntax_invalid")
            index += 1
            return nested
        left_kind, left = parse_operand()
        if index >= len(tokens) or tokens[index][0] != "op" or tokens[index][1] in {"and", "or", ")", "("}:
            _fail("runtime_marker_syntax_invalid")
        operator = tokens[index][1]
        index += 1
        right_kind, right = parse_operand()
        if left_kind == "literal" and right_kind != "literal":
            left_kind, right_kind = right_kind, left_kind
            left, right = right, left
        if left_kind == "literal" and right_kind == "literal":
            _fail("runtime_marker_syntax_invalid")
        return _marker_value_compare(left, operator, right, left_kind)

    def parse_and() -> bool:
        nonlocal index
        result = parse_comparison()
        while index < len(tokens) and tokens[index] == ("op", "and"):
            index += 1
            next_result = parse_comparison()
            result = result and next_result
        return result

    def parse_or() -> bool:
        nonlocal index
        result = parse_and()
        while index < len(tokens) and tokens[index] == ("op", "or"):
            index += 1
            next_result = parse_and()
            result = result or next_result
        return result

    result = parse_or()
    if index != len(tokens):
        _fail("runtime_marker_syntax_invalid")
    return result


def _parse_requirement(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("runtime_requirement_invalid")
    if "@" in value:
        _fail("runtime_direct_url_forbidden")
    match = REQUIREMENT_RE.fullmatch(value)
    if match is None:
        _fail("runtime_requirement_invalid")
    name = _canonical_name(match.group("name"))
    extras_raw = match.group("extras")
    extras = sorted({_canonical_name(item.strip()) for item in extras_raw.split(",")}) if extras_raw else []
    if not NAME_RE.fullmatch(name) or any(not NAME_RE.fullmatch(item) for item in extras):
        _fail("runtime_requirement_invalid")
    specifier = match.group("specifier").strip()
    if any(char in specifier for char in "\n\r#"):
        _fail("runtime_requirement_invalid")
    marker = match.group("marker")
    return {"name": name, "extras": extras, "specifier": specifier, "marker": marker.strip() if marker else None}


def _version_order(value: str) -> tuple[Any, ...]:
    """Compare the common PEP 440 release forms; unsupported forms fail closed."""

    match = PEP440_SIMPLE_VERSION_RE.fullmatch(value)
    if match is None or match.group("local") is not None:
        _fail("runtime_package_version_format_unsupported")
    epoch = int((match.group("epoch") or "0!")[:-1])
    release = tuple(int(part) for part in match.group("release").split("."))
    if len(release) > 8:
        _fail("runtime_package_version_format_unsupported")
    release += (0,) * (8 - len(release))
    pre = match.group("pre")
    if pre:
        pre_match = re.fullmatch(r"(a|b|rc)([0-9]+)", pre, re.IGNORECASE)
        assert pre_match is not None
        phase = {"a": 0, "b": 1, "rc": 2}[pre_match.group(1).lower()]
        pre_order = (phase, int(pre_match.group(2)))
    elif match.group("dev") and not match.group("post"):
        pre_order = (-1, 0)
    else:
        pre_order = (3, 0)
    dev = match.group("dev")
    dev_order = (0, int(dev[4:])) if dev else (1, 0)
    post = match.group("post")
    post_order = int(post[5:]) if post else -1
    local = (match.group("local") or "").lower()
    return epoch, release, pre_order, dev_order, post_order, local


def _satisfies_specifier(version: str, specifier: str) -> bool:
    if not specifier:
        return True
    selected: tuple[Any, ...] | None = None
    for clause in specifier.split(","):
        clause = clause.strip()
        match = re.fullmatch(r"(===|==|!=|~=|<=|>=|<|>)([^\s,]+)", clause)
        if match is None:
            _fail("runtime_requirement_specifier_unsupported")
        operator, requested = match.groups()
        wildcard = requested.endswith(".*")
        if wildcard:
            if operator not in {"==", "!="}:
                _fail("runtime_requirement_specifier_unsupported")
            prefix = requested[:-2]
            prefix_parts = prefix.split(".")
            selected_parts = version.split(".")
            matches = selected_parts[: len(prefix_parts)] == prefix_parts
            if (operator == "==" and not matches) or (operator == "!=" and matches):
                return False
            continue
        if operator == "===" and version != requested:
            return False
        if operator == "===":
            continue
        if selected is None:
            selected = _version_order(version)
        if operator == "==" and selected != _version_order(requested):
            return False
        if operator == "!=" and selected == _version_order(requested):
            return False
        if operator in {"<", "<=", ">", ">="}:
            bound = _version_order(requested)
            if operator == "<" and not selected < bound:
                return False
            if operator == "<=" and not selected <= bound:
                return False
            if operator == ">" and not selected > bound:
                return False
            if operator == ">=" and not selected >= bound:
                return False
        if operator == "~=":
            requested_order = _version_order(requested)
            release_raw = requested.split("+", 1)[0].split(".")
            if len(release_raw) == 1:
                ceiling = str(int(release_raw[0]) + 1)
            else:
                ceiling = ".".join(
                    release_raw[:-2] + [str(int(release_raw[-2]) + 1)]
                )
            if not selected >= requested_order or not selected < _version_order(ceiling):
                return False
    return True


def _marker_environment(lock: Mapping[str, Any], extra: str = "") -> dict[str, str]:
    python = lock["python_runtime"]
    return {
        "python_version": "3.12",
        "python_full_version": python["version"],
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "os_name": "posix",
        "sys_platform": "linux",
        "platform_machine": "aarch64",
        "platform_system": "Linux",
        "implementation_version": python["version"],
        "extra": extra,
    }


def _is_supported_tag(tag: str) -> bool:
    parts = tag.split("-")
    if len(parts) != 3:
        return False
    python_tag, abi_tag, platform_tag = parts
    if tag == "py3-none-any":
        return True
    if platform_tag == "manylinux2014_aarch64":
        glibc_minor = 17
    else:
        match = MANYLINUX_RE.fullmatch(platform_tag)
        if match is None:
            return False
        glibc_minor = int(match.group(1))
    if not 17 <= glibc_minor <= 39:
        return False
    if python_tag == "cp312" and abi_tag == "cp312":
        return True
    if abi_tag == "abi3" and python_tag in {
        "cp36", "cp37", "cp38", "cp39", "cp310", "cp311", "cp312"
    }:
        return True
    return False


def _wheel_identity(filename: Any) -> tuple[str, str, list[str]]:
    if not isinstance(filename, str) or WHEEL_FILENAME_RE.fullmatch(filename) is None:
        _fail("runtime_wheel_filename_unsafe")
    if filename.startswith(".") or "/" in filename or "\\" in filename:
        _fail("runtime_wheel_filename_unsafe")
    stem = filename[:-4]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4 or any(not part for part in parts):
        _fail("runtime_wheel_filename_invalid")
    dist_version, python_tags, abi_tags, platform_tags = parts
    dist_version_parts = dist_version.rsplit("-", 1)
    if len(dist_version_parts) != 2:
        _fail("runtime_wheel_filename_invalid")
    name = _canonical_name(dist_version_parts[0])
    version = dist_version_parts[1]
    tags = sorted(
        f"{py}-{abi}-{platform}"
        for py in python_tags.split(".")
        for abi in abi_tags.split(".")
        for platform in platform_tags.split(".")
    )
    if not tags or any(not _is_supported_tag(tag) for tag in tags):
        _fail("runtime_wheel_tag_unsupported")
    return name, version, tags


def _validate_origin(value: Any, fixture_only: bool) -> None:
    origin = _expect_fields(
        value,
        {"index_url", "artifact_url", "acquired_at", "provenance_status"},
        "runtime_origin_fields_invalid",
    )
    if origin["index_url"] != APPROVED_INDEX or not _https_url(origin["artifact_url"]):
        _fail("runtime_origin_unapproved")
    if urlsplit(origin["artifact_url"]).hostname != "files.pythonhosted.org":
        _fail("runtime_origin_unapproved")
    if not _is_utc_timestamp(origin["acquired_at"]):
        _fail("runtime_origin_timestamp_invalid")
    expected = "fixture-only" if fixture_only else "verified"
    if origin["provenance_status"] != expected:
        _fail("runtime_origin_provenance_unreviewed")


def _validate_external_dependency(value: Any) -> tuple[str, str]:
    item = _expect_fields(
        value,
        {"kind", "name", "identity_status", "path", "endpoint", "version", "sha256", "origin"},
        "runtime_external_dependency_fields_invalid",
    )
    if not isinstance(item["kind"], str) or item["kind"] not in {"executable", "service"} or not isinstance(item["name"], str) or not item["name"]:
        _fail("runtime_external_dependency_invalid")
    if not isinstance(item["identity_status"], str) or item["identity_status"] not in {"resolved", "unresolved"}:
        _fail("runtime_external_dependency_status_invalid")
    if item["kind"] == "executable":
        if item["endpoint"] is not None:
            _fail("runtime_external_dependency_invalid")
        if item["identity_status"] == "resolved":
            if (
                not isinstance(item["path"], str)
                or not item["path"].startswith("/")
                or "\\" in item["path"]
                or any(part in {"", ".", ".."} for part in item["path"][1:].split("/"))
            ):
                _fail("runtime_external_executable_identity_missing")
            if not isinstance(item["version"], str) or not item["version"] or not _is_sha256(item["sha256"]):
                _fail("runtime_external_executable_identity_missing")
            origin = _expect_fields(item["origin"], {"kind", "package_name", "package_version", "package_sha256"}, "runtime_external_origin_invalid")
            if origin["kind"] not in {"host-package", "base-image", "separately-managed"}:
                _fail("runtime_external_origin_invalid")
            if not isinstance(origin["package_name"], str) or not origin["package_name"] or not isinstance(origin["package_version"], str) or not origin["package_version"] or not _is_sha256(origin["package_sha256"]):
                _fail("runtime_external_origin_invalid")
        elif any(item[field] is not None for field in ("path", "version", "sha256", "origin")):
            _fail("runtime_external_unresolved_identity_ambiguous")
    else:
        if item["path"] is not None or item["sha256"] is not None or item["origin"] is not None:
            _fail("runtime_external_service_identity_invalid")
        if item["identity_status"] == "resolved":
            if not isinstance(item["endpoint"], str) or not item["endpoint"] or not isinstance(item["version"], str) or not item["version"]:
                _fail("runtime_external_service_identity_missing")
            try:
                parsed = urlsplit(item["endpoint"])
            except ValueError:
                _fail("runtime_external_service_endpoint_invalid")
            if not parsed.scheme or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                _fail("runtime_external_service_endpoint_invalid")
        elif item["endpoint"] is not None or item["version"] is not None:
            _fail("runtime_external_unresolved_identity_ambiguous")
    return item["kind"], item["name"]


def validate_runtime_lock(lock: Mapping[str, Any], *, fixture_only: bool = False) -> None:
    """Validate a closed lock schema without consulting a package index or resolver."""

    expected_status = "fixture-only" if fixture_only else "reviewed"
    top = _expect_fields(
        lock,
        {
            "lock_kind", "schema_version", "review_status", "platform_target", "python_runtime",
            "declaration_sources", "direct_requirements", "packages", "dependency_edges",
            "external_dependencies", "native_extensions", "elf_closure", "build_provenance", "acquisition",
        },
        "runtime_lock_fields_invalid",
    )
    _reject_ambiguous_numbers(dict(top))
    if top["lock_kind"] != LOCK_KIND or type(top["schema_version"]) is not int or top["schema_version"] != LOCK_SCHEMA_VERSION:
        _fail("runtime_lock_schema_invalid")
    if top["review_status"] != expected_status:
        _fail("runtime_lock_review_status_invalid")
    if top["platform_target"] != PLATFORM_TARGET:
        _fail("runtime_target_unsupported")
    python = _expect_fields(top["python_runtime"], {"implementation", "version", "abi", "soabi"}, "runtime_python_fields_invalid")
    if (
        python["implementation"] != PYTHON_RUNTIME_POLICY["implementation"]
        or not isinstance(python["version"], str)
        or PYTHON_FULL_VERSION_RE.fullmatch(python["version"]) is None
        or python["abi"] != PYTHON_RUNTIME_POLICY["abi"]
        or python["soabi"] != PYTHON_RUNTIME_POLICY["soabi"]
    ):
        _fail("runtime_python_target_mismatch")

    sources = top["declaration_sources"]
    if not isinstance(sources, list) or not sources:
        _fail("runtime_declaration_sources_missing")
    source_by_path: dict[str, str] = {}
    source_keys: list[str] = []
    for source in sources:
        source = _expect_fields(source, {"path", "sha256", "classification"}, "runtime_declaration_source_fields_invalid")
        if not _safe_relative_path(source["path"]) or not _is_sha256(source["sha256"]):
            _fail("runtime_declaration_source_invalid")
        if not isinstance(source["classification"], str) or source["classification"] not in {"runtime", "development", "mixed"}:
            _fail("runtime_declaration_source_classification_invalid")
        if source["path"] in source_by_path:
            _fail("runtime_declaration_source_duplicate")
        source_by_path[source["path"]] = source["sha256"]
        source_keys.append(source["path"])
    if source_keys != sorted(source_keys):
        _fail("runtime_declaration_sources_unsorted")

    acquisition = _expect_fields(top["acquisition"], {"approval_id", "action", "index_url"}, "runtime_acquisition_fields_invalid")
    if not isinstance(acquisition["approval_id"], str) or not acquisition["approval_id"] or acquisition["action"] != "download-only" or acquisition["index_url"] != APPROVED_INDEX:
        _fail("runtime_acquisition_policy_invalid")

    provenance = _expect_fields(
        top["build_provenance"],
        {"builder_image_digest", "python_full_version", "resolver_name", "resolver_version", "resolved_at", "toolchain_digest"},
        "runtime_build_provenance_fields_invalid",
    )
    if (
        not _is_sha256(provenance["builder_image_digest"])
        or provenance["python_full_version"] != python["version"]
        or not isinstance(provenance["resolver_name"], str)
        or not provenance["resolver_name"]
        or not isinstance(provenance["resolver_version"], str)
        or not provenance["resolver_version"]
        or not _is_utc_timestamp(provenance["resolved_at"])
        or not _is_sha256(provenance["toolchain_digest"])
    ):
        _fail("runtime_build_provenance_invalid")

    packages = top["packages"]
    if not isinstance(packages, list) or not packages:
        _fail("runtime_packages_missing")
    package_by_name: dict[str, Mapping[str, Any]] = {}
    package_names: list[str] = []
    fixture_reviews = {
        "artifact": "fixture-only",
        "license": "fixture-only",
        "security": "fixture-only",
        "native": "fixture-only",
    }
    production_reviews = {
        "artifact": "approved",
        "license": "approved",
        "security": "approved",
        "native": "approved",
    }
    expected_reviews = fixture_reviews if fixture_only else production_reviews
    for package in packages:
        package = _expect_fields(
            package,
            {"name", "version", "wheel_filename", "wheel_sha256", "wheel_tags", "requested_extras", "origin", "review"},
            "runtime_package_fields_invalid",
        )
        name = package["name"]
        version = package["version"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or _canonical_name(name) != name:
            _fail("runtime_package_name_ambiguous")
        if (
            not isinstance(version, str)
            or VERSION_RE.fullmatch(version) is None
            or PEP440_SIMPLE_VERSION_RE.fullmatch(version) is None
            or "*" in version
        ):
            _fail("runtime_package_version_unpinned")
        if name in package_by_name:
            _fail("runtime_package_duplicate")
        wheel_name, wheel_version, parsed_tags = _wheel_identity(package["wheel_filename"])
        if wheel_name != name or wheel_version != version:
            _fail("runtime_wheel_identity_mismatch")
        if not _is_sha256(package["wheel_sha256"]):
            _fail("runtime_wheel_hash_missing_or_invalid")
        tags = package["wheel_tags"]
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags) or tags != sorted(set(tags)) or tags != parsed_tags:
            _fail("runtime_wheel_tags_mismatch")
        extras = package["requested_extras"]
        if not isinstance(extras, list) or any(not isinstance(extra, str) for extra in extras) or extras != sorted(set(extras)) or any(not NAME_RE.fullmatch(extra) for extra in extras):
            _fail("runtime_package_extras_invalid")
        _validate_origin(package["origin"], fixture_only)
        review = _expect_fields(
            package["review"],
            {"artifact", "license", "security", "native"},
            "runtime_package_review_fields_invalid",
        )
        if dict(review) != expected_reviews:
            _fail("runtime_package_review_incomplete")
        package_by_name[name] = package
        package_names.append(name)
    if package_names != sorted(package_names):
        _fail("runtime_packages_unsorted")

    direct = top["direct_requirements"]
    if not isinstance(direct, list) or not direct:
        _fail("runtime_direct_requirements_missing")
    direct_sort_keys: list[tuple[str, int, str]] = []
    direct_names: set[str] = set()
    extra_requests: dict[str, set[str]] = {name: set() for name in package_by_name}
    marker_env = _marker_environment(top)
    for requirement in direct:
        requirement = _expect_fields(
            requirement,
            {"requirement", "resolved_name", "extras", "marker", "applies_to_target", "source_path", "source_sha256", "line"},
            "runtime_direct_requirement_fields_invalid",
        )
        parsed = _parse_requirement(requirement["requirement"])
        source_path, source_sha = requirement["source_path"], requirement["source_sha256"]
        if source_path not in source_by_path or source_by_path[source_path] != source_sha:
            _fail("runtime_requirement_source_mismatch")
        if not isinstance(requirement["line"], int) or isinstance(requirement["line"], bool) or requirement["line"] < 1:
            _fail("runtime_requirement_source_line_invalid")
        if requirement["resolved_name"] != parsed["name"] or requirement["extras"] != parsed["extras"] or requirement["marker"] != parsed["marker"]:
            _fail("runtime_requirement_identity_mismatch")
        applies = evaluate_marker(parsed["marker"], marker_env)
        if requirement["applies_to_target"] is not applies:
            _fail("runtime_environment_marker_mismatch")
        if applies:
            name = parsed["name"]
            if name not in package_by_name:
                _fail("runtime_direct_package_missing")
            if not _satisfies_specifier(package_by_name[name]["version"], parsed["specifier"]):
                _fail("runtime_version_constraint_mismatch")
            direct_names.add(name)
            extra_requests[name].update(parsed["extras"])
        direct_sort_keys.append((source_path, requirement["line"], requirement["requirement"]))
    if direct_sort_keys != sorted(direct_sort_keys) or len(direct_sort_keys) != len(set(direct_sort_keys)):
        _fail("runtime_direct_requirements_unsorted")

    edges = top["dependency_edges"]
    if not isinstance(edges, list):
        _fail("runtime_dependency_edges_invalid")
    edge_sort_keys: list[tuple[str, str, str]] = []
    edge_requirements: dict[str, list[str]] = {name: [] for name in package_by_name}
    adjacency: dict[str, set[str]] = {name: set() for name in package_by_name}
    for edge in edges:
        edge = _expect_fields(edge, {"parent", "requirement", "child", "extras", "marker", "applies_to_target"}, "runtime_dependency_edge_fields_invalid")
        parent, child = edge["parent"], edge["child"]
        if parent not in package_by_name:
            _fail("runtime_dependency_parent_missing")
        parsed = _parse_requirement(edge["requirement"])
        if child != parsed["name"] or edge["extras"] != parsed["extras"] or edge["marker"] != parsed["marker"]:
            _fail("runtime_dependency_edge_identity_mismatch")
        parent_extras = package_by_name[parent]["requested_extras"]
        applies = any(
            evaluate_marker(parsed["marker"], _marker_environment(top, extra=extra))
            for extra in (parent_extras or [""])
        )
        if edge["applies_to_target"] is not applies:
            _fail("runtime_environment_marker_mismatch")
        edge_requirements[parent].append(edge["requirement"])
        if applies:
            if child not in package_by_name:
                _fail("runtime_transitive_dependency_missing")
            if not _satisfies_specifier(
                package_by_name[child]["version"], parsed["specifier"]
            ):
                _fail("runtime_version_constraint_mismatch")
            adjacency[parent].add(child)
            extra_requests[child].update(parsed["extras"])
        edge_sort_keys.append((parent, child, edge["requirement"]))
    if edge_sort_keys != sorted(edge_sort_keys) or len(edge_sort_keys) != len(set(edge_sort_keys)):
        _fail("runtime_dependency_edges_unsorted")
    for parent, package in package_by_name.items():
        if package["requested_extras"] != sorted(extra_requests[parent]):
            _fail("runtime_extras_mismatch")

    reachable = set(direct_names)
    frontier = list(direct_names)
    while frontier:
        parent = frontier.pop()
        for child in adjacency[parent] - reachable:
            reachable.add(child)
            frontier.append(child)
    if reachable != set(package_by_name):
        _fail("runtime_dependency_closure_incomplete")

    external = top["external_dependencies"]
    if not isinstance(external, list):
        _fail("runtime_external_dependencies_invalid")
    external_keys = [_validate_external_dependency(item) for item in external]
    if external_keys != sorted(set(external_keys)):
        _fail("runtime_external_dependencies_unsorted_or_duplicate")

    native = top["native_extensions"]
    if not isinstance(native, list):
        _fail("runtime_native_extensions_invalid")
    native_keys: list[tuple[str, str]] = []
    for item in native:
        item = _expect_fields(item, {"package", "member", "sha256"}, "runtime_native_extension_fields_invalid")
        if item["package"] not in package_by_name or not _safe_relative_path(item["member"]) or not _is_sha256(item["sha256"]):
            _fail("runtime_native_extension_invalid")
        native_keys.append((item["package"], item["member"]))
    if native_keys != sorted(set(native_keys)):
        _fail("runtime_native_extensions_unsorted_or_duplicate")

    elf = _expect_fields(top["elf_closure"], {"status", "evidence_sha256", "inspector", "base_image_digest", "system_packages"}, "runtime_elf_closure_fields_invalid")
    if elf["status"] == "verified":
        if not _is_sha256(elf["evidence_sha256"]) or not isinstance(elf["inspector"], str) or not elf["inspector"] or not _is_sha256(elf["base_image_digest"]):
            _fail("runtime_elf_closure_evidence_missing")
        system_packages = elf["system_packages"]
        if not isinstance(system_packages, list) or any(not isinstance(item, str) or not item for item in system_packages) or system_packages != sorted(set(system_packages)):
            _fail("runtime_elf_system_packages_invalid")
    elif elf["status"] == "unverified":
        if any(elf[field] is not None for field in ("evidence_sha256", "inspector", "base_image_digest")) or elf["system_packages"] != []:
            _fail("runtime_elf_unverified_record_ambiguous")
    else:
        _fail("runtime_elf_closure_status_invalid")


def _validate_authorization(
    authorization: Mapping[str, Any], lock: Mapping[str, Any], *, fixture_only: bool
) -> None:
    expected_status = "fixture-only" if fixture_only else "approved"
    auth = _expect_fields(
        authorization,
        {"authorization_kind", "schema_version", "status", "approval_id", "action", "runtime_lock_digest", "platform_target", "python_runtime", "approved_index_url", "artifacts"},
        "runtime_acquisition_authorization_fields_invalid",
    )
    if auth["authorization_kind"] != AUTHORIZATION_KIND or auth["schema_version"] != AUTHORIZATION_SCHEMA_VERSION or auth["status"] != expected_status:
        _fail("runtime_acquisition_authorization_status_invalid")
    if auth["approval_id"] != lock["acquisition"]["approval_id"] or auth["action"] != "download-only":
        _fail("runtime_acquisition_authorization_mismatch")
    if auth["runtime_lock_digest"] != runtime_lock_digest(lock):
        _fail("runtime_acquisition_lock_digest_mismatch")
    if auth["platform_target"] != lock["platform_target"] or auth["python_runtime"] != lock["python_runtime"]:
        _fail("runtime_acquisition_target_mismatch")
    if auth["approved_index_url"] != lock["acquisition"]["index_url"] or auth["approved_index_url"] != APPROVED_INDEX:
        _fail("runtime_acquisition_origin_mismatch")
    artifacts = auth["artifacts"]
    if not isinstance(artifacts, list):
        _fail("runtime_authorized_artifacts_invalid")
    expected = []
    for package in lock["packages"]:
        origin = package["origin"]
        expected.append({
            "name": package["name"],
            "version": package["version"],
            "filename": package["wheel_filename"],
            "sha256": package["wheel_sha256"],
            "index_url": origin["index_url"],
            "artifact_url": origin["artifact_url"],
        })
    expected.sort(key=lambda item: item["name"])
    if artifacts != expected:
        _fail("runtime_authorized_artifact_identity_mismatch")


def _validate_declaration_source_contents(
    lock: Mapping[str, Any], contents: Mapping[str, bytes]
) -> None:
    """Match the declared source digests and each direct requirement line."""

    expected_paths = {source["path"] for source in lock["declaration_sources"]}
    if set(contents) != expected_paths:
        _fail("runtime_declaration_source_inventory_mismatch")
    for source in lock["declaration_sources"]:
        raw = contents[source["path"]]
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != source["sha256"]:
            _fail("runtime_declaration_source_digest_mismatch")
    decoded: dict[str, list[str]] = {}
    try:
        decoded = {
            path: raw.decode("utf-8").splitlines()
            for path, raw in contents.items()
        }
    except UnicodeDecodeError as exc:
        raise RuntimeLockError("runtime_declaration_source_encoding_invalid") from exc
    for requirement in lock["direct_requirements"]:
        lines = decoded[requirement["source_path"]]
        line_number = requirement["line"]
        if line_number > len(lines):
            _fail("runtime_requirement_source_line_missing")
        line = lines[line_number - 1].strip()
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if line != requirement["requirement"]:
            _fail("runtime_requirement_source_line_mismatch")


def _wheel_contents(raw: bytes) -> tuple[dict[str, str], set[str], dict[str, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                _fail("runtime_wheel_duplicate_member")
            if any(not _safe_relative_path(name[:-1] if name.endswith("/") else name) for name in names):
                _fail("runtime_wheel_member_unsafe")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                _fail("runtime_wheel_metadata_layout_invalid")
            metadata_bytes = archive.read(metadata_names[0])
            text = metadata_bytes.decode("utf-8")
            fields: dict[str, list[str]] = {}
            for line in text.splitlines():
                if not line or line[0].isspace() or ":" not in line:
                    continue
                key, _separator, value = line.partition(":")
                fields.setdefault(key.lower(), []).append(value.strip())
            if len(fields.get("name", [])) != 1 or len(fields.get("version", [])) != 1:
                _fail("runtime_wheel_metadata_identity_invalid")
            metadata = {key: values[0] for key, values in fields.items() if len(values) == 1}
            metadata["requires_dist"] = "\n".join(fields.get("requires-dist", []))
            wheel_text = archive.read(wheel_names[0]).decode("utf-8")
            wheel_tags: set[str] = set()
            for line in wheel_text.splitlines():
                if line.startswith("Tag:"):
                    parsed_name, parsed_version, parsed_tags = _wheel_identity_from_tag(line.partition(":")[2].strip())
                    del parsed_name, parsed_version
                    wheel_tags.update(parsed_tags)
            native_hashes: dict[str, str] = {}
            for name in names:
                leaf = name.lower()
                if leaf.endswith((".dylib", ".dll", ".pyd")):
                    _fail("runtime_wheel_native_platform_mismatch")
                member_bytes = archive.read(name)
                is_native = (
                    member_bytes.startswith(b"\x7fELF")
                    or leaf.endswith(".so")
                    or re.search(r"\.so\.[0-9.]+$", leaf) is not None
                )
                if is_native:
                    native_hashes[name] = hashlib.sha256(member_bytes).hexdigest()
                if name.endswith(".pth") or Path(name).name in {"sitecustomize.py", "usercustomize.py"}:
                    _fail("runtime_wheel_executable_customization_forbidden")
                info = archive.getinfo(name)
                if ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK:
                    _fail("runtime_wheel_symlink_forbidden")
            return metadata, wheel_tags, native_hashes
    except RuntimeLockError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise RuntimeLockError("runtime_wheel_invalid") from exc


def _wheel_identity_from_tag(tag: str) -> tuple[str, str, list[str]]:
    parts = tag.split("-")
    if len(parts) != 3:
        _fail("runtime_wheel_metadata_tag_invalid")
    expanded = sorted(
        f"{python}-{abi}-{platform}"
        for python in parts[0].split(".")
        for abi in parts[1].split(".")
        for platform in parts[2].split(".")
    )
    if any(not _is_supported_tag(item) for item in expanded):
        _fail("runtime_wheel_tag_unsupported")
    return "", "", expanded


def evaluate_offline_acquisition(
    lock: Mapping[str, Any],
    wheelhouse: str | Path,
    authorization: Mapping[str, Any],
    *,
    declaration_contents: Mapping[str, bytes] | None = None,
    fixture_only: bool = False,
) -> AcquisitionDecision:
    """Validate exact pre-existing wheel artifacts; never resolves or acquires."""

    validate_runtime_lock(lock, fixture_only=fixture_only)
    _validate_authorization(authorization, lock, fixture_only=fixture_only)
    if declaration_contents is not None:
        _validate_declaration_source_contents(lock, declaration_contents)
    root = Path(wheelhouse)
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return AcquisitionDecision(
            status="UNRESOLVED_ARTIFACTS",
            missing_artifacts=tuple(package["wheel_filename"] for package in lock["packages"]),
            blockers=("wheelhouse_absent",),
        )
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _fail("runtime_wheelhouse_path_invalid")
    package_files = {package["wheel_filename"]: package for package in lock["packages"]}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise RuntimeLockError("runtime_wheelhouse_path_invalid") from exc
    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
            _fail("runtime_wheelhouse_path_changed")
        actual_names = set(os.listdir(root_fd))
        entry_stats: dict[str, os.stat_result] = {}
        for name in actual_names:
            try:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeLockError("runtime_wheelhouse_entry_invalid") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("runtime_wheelhouse_entry_unsafe")
            entry_stats[name] = info
        extra = actual_names - set(package_files)
        if extra:
            _fail("runtime_wheelhouse_inventory_mismatch")
        missing = tuple(sorted(set(package_files) - actual_names))
        if missing:
            return AcquisitionDecision(
                status="UNRESOLVED_ARTIFACTS",
                missing_artifacts=missing,
                blockers=("locked_wheel_missing",),
            )

        contents_by_name: dict[str, bytes] = {}
        for filename in sorted(package_files):
            try:
                fd = os.open(
                    filename,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise RuntimeLockError("runtime_wheelhouse_entry_unsafe") from exc
            try:
                opened = os.fstat(fd)
                named = entry_stats[filename]
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                ):
                    _fail("runtime_wheelhouse_entry_changed")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                contents_by_name[filename] = b"".join(chunks)
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)

    expected_edges: dict[str, list[str]] = {package["name"]: [] for package in lock["packages"]}
    for edge in lock["dependency_edges"]:
        expected_edges[edge["parent"]].append(edge["requirement"])
    for filename, package in package_files.items():
        content = contents_by_name[filename]
        if hashlib.sha256(content).hexdigest() != package["wheel_sha256"]:
            _fail("runtime_wheel_hash_mismatch")
        metadata, wheel_tags, native_hashes = _wheel_contents(content)
        if _canonical_name(metadata.get("name", "")) != package["name"] or metadata.get("version") != package["version"]:
            _fail("runtime_wheel_metadata_identity_mismatch")
        if wheel_tags != set(package["wheel_tags"]):
            _fail("runtime_wheel_metadata_tags_mismatch")
        if sorted(metadata.get("requires_dist", "").splitlines()) != sorted(expected_edges[package["name"]]):
            _fail("runtime_dependency_metadata_mismatch")
        expected_native = {
            item["member"]: item["sha256"]
            for item in lock["native_extensions"]
            if item["package"] == package["name"]
        }
        if native_hashes != expected_native:
            _fail("runtime_native_extension_inventory_mismatch")

    blockers: list[str] = ["elf_closure_artifact_not_inspected_by_wheel_policy"]
    if declaration_contents is None:
        blockers.append("declaration_sources_not_verified")
    if lock["elf_closure"]["status"] != "verified":
        blockers.append("elf_closure_evidence_missing")
    unresolved_external = [
        f"external:{item['kind']}:{item['name']}"
        for item in lock["external_dependencies"]
        if item["identity_status"] != "resolved"
    ]
    blockers.extend(unresolved_external)
    status = "ARTIFACTS_AVAILABLE_CLOSURE_UNVERIFIED" if blockers else "ARTIFACTS_AVAILABLE_NOT_ASSEMBLED"
    return AcquisitionDecision(status, (), tuple(sorted(blockers)), runtime_assembly_eligible=False)

