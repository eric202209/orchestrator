"""Bounded metadata-only PyPI resolver for the Phase 39 P0B Worker target.

The collector speaks HTTPS directly to pypi.org and accepts only official
PyPI JSON metadata paths. It never follows redirects and has no artifact URL
fetch path. It does not download, inspect, build, or install distributions.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import packaging
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version


SCHEMA = "phase39-p0b-worker-runtime-metadata-resolution"
SCHEMA_VERSION = 1
RESOLVER_NAME = "phase39-p0b-bounded-pypi-json-resolver"
RESOLVER_VERSION = "1.1.0"
AUTHORIZED_HOST = "pypi.org"
AUTHORIZED_ORIGIN = "https://pypi.org"
AUTHORIZED_SIMPLE_INDEX = "https://pypi.org/simple"
MAX_METADATA_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SEARCH_NODES = 10000
MAX_METADATA_REQUESTS = 2000
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")
MARKER_VARIABLE_RE = re.compile(r"\b[a-z_][a-z0-9_]*\b", re.IGNORECASE)
MARKER_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


class MetadataResolutionError(ValueError):
    """Metadata input, network, or resolution evidence is incomplete/invalid."""


class MetadataUnavailable(MetadataResolutionError):
    """Official PyPI metadata could not be obtained or did not prove closure."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def canonical_digest(value: Mapping[str, Any], digest_field: str = "manifest_sha256") -> str:
    body = dict(value)
    body.pop(digest_field, None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def normalized_name(value: str) -> str:
    return str(canonicalize_name(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strict_json(raw: bytes) -> Any:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MetadataResolutionError("metadata_json_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataResolutionError("metadata_json_invalid") from exc
    if not isinstance(value, dict):
        raise MetadataResolutionError("metadata_json_not_object")
    return value


def _metadata_path(name: str, version: str | None = None) -> str:
    package = normalized_name(name)
    if not NAME_RE.fullmatch(package):
        raise MetadataResolutionError("metadata_project_name_invalid")
    if version is None:
        return f"/pypi/{package}/json"
    if not VERSION_PATH_RE.fullmatch(version):
        raise MetadataResolutionError("metadata_version_path_invalid")
    return f"/pypi/{package}/{version}/json"


class BoundedPyPIMetadataClient:
    """One-origin JSON client. Redirects and non-metadata paths fail closed."""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] | None = None,
        clock: Callable[[], str] = _utc_now,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = MAX_METADATA_RESPONSE_BYTES,
    ) -> None:
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self._clock = clock
        self._timeout = timeout_seconds
        self._max_bytes = max_response_bytes
        self.evidence: list[dict[str, Any]] = []
        self.redirect_attempts: list[dict[str, Any]] = []

    @staticmethod
    def validate_request_target(target: str) -> None:
        if not isinstance(target, str) or not target.startswith("/"):
            raise MetadataResolutionError("unauthorized_metadata_url")
        if target.startswith("//") or "\\" in target or "?" in target or "#" in target:
            raise MetadataResolutionError("unauthorized_metadata_url")
        if re.fullmatch(
            r"/pypi/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[A-Za-z0-9][A-Za-z0-9.!+_-]*)?/json",
            target,
        ) is None:
            raise MetadataResolutionError("artifact_or_unapproved_endpoint_rejected")

    def fetch_json(
        self, name: str, version: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target = _metadata_path(name, version)
        self.validate_request_target(target)
        timestamp = self._clock()
        connection = self._connection_factory(
            AUTHORIZED_HOST,
            timeout=self._timeout,
            context=ssl.create_default_context(),
        )
        status: int | None = None
        location: str | None = None
        response_digest: str | None = None
        response_bytes = 0
        content_type: str | None = None
        error: str | None = None
        payload: dict[str, Any] | None = None
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Orchestrator-Phase39-P0B-metadata-only/1.0",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = int(response.status)
            location = response.getheader("Location")
            content_type = response.getheader("Content-Type")
            raw = response.read(self._max_bytes + 1)
            response_bytes = len(raw)
            if response_bytes > self._max_bytes:
                raw = raw[: self._max_bytes]
                error = "metadata_response_too_large"
            response_digest = hashlib.sha256(raw).hexdigest()
            if 300 <= status < 400:
                error = "redirect_rejected"
            elif status != 200:
                error = f"metadata_http_status_{status}"
            elif error is None:
                if content_type is None or "json" not in content_type.lower():
                    error = "metadata_content_type_invalid"
                else:
                    try:
                        payload = _strict_json(raw)
                    except MetadataResolutionError as exc:
                        error = str(exc)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            error = f"metadata_transport_error:{type(exc).__name__}"
        finally:
            connection.close()

        evidence = {
            "url": AUTHORIZED_ORIGIN + target,
            "host": AUTHORIZED_HOST,
            "method": "GET",
            "response_status": status,
            "retrieved_at_utc": timestamp,
            "response_bytes": response_bytes,
            "response_sha256": response_digest,
            "content_type": content_type,
            "redirect_location": location,
            "redirect_followed": False,
            "error": error,
        }
        self.evidence.append(evidence)
        if status is not None and 300 <= status < 400:
            self.redirect_attempts.append(
                {"url": evidence["url"], "status": status, "location": location, "followed": False}
            )
        if error is not None or payload is None:
            raise MetadataUnavailable(error or "metadata_unavailable")
        return payload, evidence


def target_wheel_tags(filename: str, target: Mapping[str, Any]) -> list[str]:
    """Return compatible tags under the fixed WP2A Noble/cp312/glibc policy."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidVersion, ValueError) as exc:
        raise MetadataResolutionError("wheel_filename_invalid") from exc
    compatible: list[str] = []
    maximum_glibc_minor = int(str(target["platform"]["libc_baseline"]).split(".")[-1])
    for tag in tags:
        value = str(tag)
        python_tag, abi_tag, platform_tag = value.split("-", 2)
        if (
            python_tag in {"py3", "py2.py3"}
            and abi_tag == "none"
            and platform_tag == "any"
        ):
            compatible.append(value)
            continue
        if platform_tag == "manylinux2014_aarch64":
            glibc_minor = 17
        else:
            match = re.fullmatch(r"manylinux_2_([0-9]+)_aarch64", platform_tag)
            if match is None:
                continue
            glibc_minor = int(match.group(1))
        if not 17 <= glibc_minor <= maximum_glibc_minor:
            continue
        if python_tag == "cp312" and abi_tag == "cp312":
            compatible.append(value)
        elif abi_tag == "abi3" and python_tag in {
            "cp36", "cp37", "cp38", "cp39", "cp310", "cp311", "cp312"
        }:
            compatible.append(value)
    return sorted(set(compatible))


def _parse_wheel_tags(filename: str) -> frozenset[Any]:
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidVersion, ValueError) as exc:
        raise MetadataResolutionError("wheel_filename_invalid") from exc
    return tags


def _wp2a_tag_supported(tag: str) -> bool:
    """Mirror WP2A runtime_lock_contract._is_supported_tag for evidence only."""
    parts = tag.split("-")
    if len(parts) != 3:
        return False
    python_tag, abi_tag, platform_tag = parts
    if tag == "py3-none-any":
        return True
    if platform_tag == "manylinux2014_aarch64":
        glibc_minor = 17
    else:
        match = re.fullmatch(r"manylinux_2_([0-9]+)_aarch64", platform_tag)
        if match is None:
            return False
        glibc_minor = int(match.group(1))
    if not 17 <= glibc_minor <= 39:
        return False
    if python_tag == "cp312" and abi_tag == "cp312":
        return True
    return abi_tag == "abi3" and python_tag in {
        "cp36", "cp37", "cp38", "cp39", "cp310", "cp311", "cp312"
    }


def _wheel_rank(filename: str, tags: list[str]) -> tuple[int, int, str]:
    ranks: list[tuple[int, int]] = []
    for tag in tags:
        _python_tag, abi_tag, platform_tag = tag.split("-", 2)
        if platform_tag == "any":
            ranks.append((0, 0))
        else:
            glibc_minor = (
                17
                if platform_tag == "manylinux2014_aarch64"
                else int(platform_tag.split("_")[2])
            )
            ranks.append((1 if abi_tag == "cp312" else 2, glibc_minor))
    best = min(ranks) if ranks else (9, 999)
    return best[0], best[1], filename


def _requires_python_accepts(specifier: str | None, python_full_version: str) -> bool:
    if not specifier:
        return True
    try:
        return SpecifierSet(specifier).contains(
            Version(python_full_version), prereleases=True
        )
    except (InvalidVersion, ValueError) as exc:
        raise MetadataResolutionError("requires_python_invalid") from exc


def _marker_vars(marker: str) -> set[str]:
    without_literals = MARKER_STRING_RE.sub(" ", marker)
    known = {
        "implementation_name", "implementation_version", "os_name",
        "platform_machine", "platform_python_implementation", "platform_release",
        "platform_system", "platform_version", "python_full_version",
        "python_version", "sys_platform", "extra",
    }
    return {
        word.lower()
        for word in MARKER_VARIABLE_RE.findall(without_literals)
        if word.lower() in known
    }


def evaluate_requirement_marker(
    marker: str | None,
    environment: Mapping[str, str],
    extras: list[str] | tuple[str, ...] = ("",),
) -> tuple[bool, list[dict[str, Any]]]:
    if marker is None:
        return True, [{"extra": extra, "result": True} for extra in sorted(set(extras))]
    missing = sorted(_marker_vars(marker) - set(environment) - {"extra"})
    if missing:
        raise MetadataResolutionError(
            "target_marker_environment_incomplete:" + ",".join(missing)
        )
    try:
        parsed = Marker(marker)
    except Exception as exc:
        raise MetadataResolutionError("requires_dist_marker_invalid") from exc
    evaluations = []
    for extra in sorted(set(extras)):
        marker_environment = dict(environment)
        marker_environment["extra"] = extra
        try:
            matched = parsed.evaluate(environment=marker_environment, context="metadata")
        except Exception as exc:
            raise MetadataResolutionError("target_marker_evaluation_failed") from exc
        evaluations.append({"extra": extra, "result": bool(matched)})
    return any(item["result"] for item in evaluations), evaluations


@dataclass(frozen=True)
class DirectRequirement:
    name: str
    specifier: str
    extras: tuple[str, ...]
    original: str
    line: int


@dataclass(frozen=True)
class CandidateWheel:
    filename: str
    sha256: str
    tags: tuple[str, ...]
    upload_time: str | None
    requires_python: str | None
    yanked: bool
    yanked_reason: str | None


class MetadataOnlyResolver:
    def __init__(
        self,
        *,
        target: Mapping[str, Any],
        marker_environment: Mapping[str, str],
        client: BoundedPyPIMetadataClient,
        max_search_nodes: int = MAX_SEARCH_NODES,
    ) -> None:
        self.target = deepcopy(dict(target))
        self.marker_environment = dict(marker_environment)
        self.client = client
        self.max_search_nodes = max_search_nodes
        self.project_cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.metadata_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.metadata_failures: dict[tuple[str, str], str] = {}
        self.candidate_rejections: list[dict[str, Any]] = []
        self.conflicts: list[dict[str, Any]] = []
        self.search_nodes = 0
        self.request_limit_hit = False
        self._project_counts: dict[str, Any] = {}

    def _project(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        name = normalized_name(name)
        if name not in self.project_cache:
            if len(self.client.evidence) >= MAX_METADATA_REQUESTS:
                self.request_limit_hit = True
                raise MetadataUnavailable("metadata_request_bound_exceeded")
            self.project_cache[name] = self.client.fetch_json(name)
        return self.project_cache[name]

    def _candidate_metadata(
        self,
        name: str,
        version: str,
        project_payload: dict[str, Any],
        project_provenance: dict[str, Any],
    ) -> dict[str, Any]:
        key = (normalized_name(name), version)
        if key in self.metadata_cache:
            return self.metadata_cache[key]
        if key in self.metadata_failures:
            raise MetadataUnavailable(self.metadata_failures[key])
        info = project_payload.get("info")
        if isinstance(info, dict) and info.get("version") == version:
            payload, provenance = project_payload, project_provenance
        else:
            if len(self.client.evidence) >= MAX_METADATA_REQUESTS:
                self.request_limit_hit = True
                raise MetadataUnavailable("metadata_request_bound_exceeded")
            payload, provenance = self.client.fetch_json(name, version)
        metadata_info = payload.get("info")
        if not isinstance(metadata_info, dict):
            raise MetadataUnavailable("distribution_metadata_info_missing")
        if normalized_name(str(metadata_info.get("name", ""))) != key[0]:
            raise MetadataUnavailable("distribution_metadata_name_mismatch")
        if metadata_info.get("version") != version:
            raise MetadataUnavailable("distribution_metadata_version_mismatch")
        if "requires_dist" not in metadata_info:
            raise MetadataUnavailable("distribution_requires_dist_unavailable")
        requires_dist_metadata_value = metadata_info.get("requires_dist")
        if requires_dist_metadata_value is None:
            requires_dist = []
            requires_dist_interpretation = (
                "PYPI_JSON_NULL_MEANS_NO_REQUIRES_DIST_FIELDS"
            )
        elif isinstance(requires_dist_metadata_value, list) and all(
            isinstance(item, str) for item in requires_dist_metadata_value
        ):
            requires_dist = list(requires_dist_metadata_value)
            requires_dist_interpretation = "PYPI_JSON_REQUIRES_DIST_LIST"
        else:
            raise MetadataUnavailable("distribution_requires_dist_unavailable")
        files = payload.get("urls")
        if not isinstance(files, list):
            raise MetadataUnavailable("distribution_files_metadata_missing")
        metadata_projection = {
            key: metadata_info.get(key)
            for key in (
                "name", "version", "metadata_version", "requires_python",
                "requires_dist", "provides_extra", "license", "license_expression",
                "license_files", "classifiers",
            )
        }
        result = {
            "info": metadata_info,
            "requires_dist": requires_dist,
            "requires_dist_metadata_value": requires_dist_metadata_value,
            "requires_dist_interpretation": requires_dist_interpretation,
            "metadata_identity_sha256": hashlib.sha256(
                canonical_json_bytes(metadata_projection)
            ).hexdigest(),
            "metadata_provenance": provenance,
            "version_files": files,
        }
        self.metadata_cache[key] = result
        return result

    def _candidate_wheels(
        self, name: str, version: str, release_files: Any
    ) -> tuple[list[CandidateWheel], dict[str, int]]:
        counts = {
            "non_wheel_files": 0,
            "yanked_files": 0,
            "incompatible_wheels": 0,
            "invalid_hash_wheels": 0,
            "requires_python_rejected_wheels": 0,
            "target_tag_compatible_wheels": 0,
        }
        if not isinstance(release_files, list):
            return [], counts
        wheels: list[CandidateWheel] = []
        for item in release_files:
            if not isinstance(item, dict) or item.get("packagetype") != "bdist_wheel":
                counts["non_wheel_files"] += 1
                continue
            filename = item.get("filename")
            if not isinstance(filename, str):
                counts["incompatible_wheels"] += 1
                continue
            try:
                wheel_name, wheel_version, _build, _tags = parse_wheel_filename(filename)
            except (InvalidVersion, ValueError):
                counts["incompatible_wheels"] += 1
                continue
            if (
                normalized_name(str(wheel_name)) != normalized_name(name)
                or str(wheel_version) != version
            ):
                counts["incompatible_wheels"] += 1
                continue
            try:
                compatible = target_wheel_tags(filename, self.target)
            except MetadataResolutionError:
                compatible = []
            if not compatible:
                counts["incompatible_wheels"] += 1
                continue
            counts["target_tag_compatible_wheels"] += 1
            if item.get("yanked", False):
                counts["yanked_files"] += 1
                continue
            digests = item.get("digests")
            digest = digests.get("sha256") if isinstance(digests, dict) else None
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                counts["invalid_hash_wheels"] += 1
                continue
            requires_python = item.get("requires_python")
            if not _requires_python_accepts(
                requires_python, self.marker_environment["python_full_version"]
            ):
                counts["requires_python_rejected_wheels"] += 1
                continue
            wheels.append(CandidateWheel(
                filename=filename,
                sha256=digest,
                tags=tuple(compatible),
                upload_time=item.get("upload_time_iso_8601") or item.get("upload_time"),
                requires_python=requires_python,
                yanked=False,
                yanked_reason=None,
            ))
        return sorted(
            wheels, key=lambda wheel: _wheel_rank(wheel.filename, list(wheel.tags))
        ), counts

    def _candidate_versions(
        self, name: str, specifiers: list[str], override: str | None
    ) -> tuple[
        list[tuple[Version, str, list[CandidateWheel], dict[str, Any], dict[str, Any]]],
        dict[str, Any],
    ]:
        payload, project_provenance = self._project(name)
        releases = payload.get("releases")
        if not isinstance(releases, dict):
            raise MetadataUnavailable("project_release_index_missing")
        all_versions: list[Version] = []
        invalid_version_count = 0
        for raw_version in releases:
            try:
                all_versions.append(Version(raw_version))
            except InvalidVersion:
                invalid_version_count += 1
        specifier_texts = [item for item in specifiers if item]
        if override:
            specifier_texts.append(override)
        try:
            specifier_sets = [SpecifierSet(item) for item in specifier_texts]
        except ValueError as exc:
            raise MetadataResolutionError("candidate_specifier_invalid") from exc
        accepted = [
            version for version in all_versions
            if all(s.contains(version, prereleases=True) for s in specifier_sets)
        ]
        stable = sorted(
            (
                version for version in accepted
                if not version.is_prerelease and not version.is_devrelease
            ),
            reverse=True,
        )
        prereleases = sorted(
            (
                version for version in accepted
                if version.is_prerelease or version.is_devrelease
            ),
            reverse=True,
        )
        ordered = stable + prereleases
        candidates = []
        counts_total = {
            "versions_listed": len(releases),
            "invalid_versions": invalid_version_count,
            "versions_satisfying_constraints": len(accepted),
            "versions_without_compatible_wheel": 0,
            "versions_with_missing_hash": 0,
            "versions_rejected_by_requires_python": 0,
            "versions_rejected_yanked": 0,
            "compatible_wheel_candidates": 0,
        }
        for version in ordered:
            version_text = str(version)
            wheels, wheel_counts = self._candidate_wheels(
                name, version_text, releases.get(version_text)
            )
            if not wheels:
                if wheel_counts["target_tag_compatible_wheels"] == 0:
                    counts_total["versions_without_compatible_wheel"] += 1
                if wheel_counts["invalid_hash_wheels"]:
                    counts_total["versions_with_missing_hash"] += 1
                if wheel_counts["requires_python_rejected_wheels"]:
                    counts_total["versions_rejected_by_requires_python"] += 1
                if wheel_counts["yanked_files"]:
                    counts_total["versions_rejected_yanked"] += 1
                if releases.get(version_text):
                    self.candidate_rejections.append(
                        {
                            "name": normalized_name(name),
                            "version": version_text,
                            "reason": (
                                "NO_COMPATIBLE_TARGET_WHEEL"
                                if wheel_counts["target_tag_compatible_wheels"] == 0
                                else "REQUIRES_PYTHON_REJECTED"
                                if wheel_counts["requires_python_rejected_wheels"]
                                else "MISSING_INDEX_ARTIFACT_HASH"
                                if wheel_counts["invalid_hash_wheels"]
                                else "YANKED_CANDIDATE_REJECTED"
                                if wheel_counts["yanked_files"]
                                else "CANDIDATE_REJECTED_BY_METADATA_POLICY"
                            ),
                            "file_rejection_counts": wheel_counts,
                        }
                    )
                continue
            counts_total["compatible_wheel_candidates"] += 1
            candidates.append(
                (version, version_text, wheels, payload, project_provenance)
            )
        return candidates, counts_total

    def resolve(
        self, direct: list[DirectRequirement], overrides: Mapping[str, str]
    ) -> dict[str, Any]:
        direct_by_name: dict[str, DirectRequirement] = {}
        for requirement in direct:
            if requirement.name in direct_by_name:
                raise MetadataResolutionError("duplicate_direct_distribution")
            direct_by_name[requirement.name] = requirement
        self._direct = direct_by_name
        self._overrides = {
            normalized_name(name): value for name, value in overrides.items()
        }
        self._project_counts = {}
        outcome, failure = self._search({})
        if outcome is None:
            return {
                "status": "METADATA_RESOLUTION_INCOMPLETE",
                "failure": failure,
                "packages": [],
                "dependency_evaluations": [],
                "dependency_edges": [],
                "unresolved": self._unresolved_records(failure),
            }
        packages, evaluations, edges = self._build_graph(outcome)
        return {
            "status": "METADATA_RESOLUTION_COMPLETE",
            "failure": None,
            "packages": packages,
            "dependency_evaluations": evaluations,
            "dependency_edges": edges,
            "unresolved": [],
        }

    def _derive(
        self, selected: Mapping[str, Any]
    ) -> tuple[dict[str, list[tuple[str, str]]], dict[str, set[str]], list[dict[str, Any]]]:
        requested_extras: dict[str, set[str]] = {}
        for name, direct in self._direct.items():
            requested_extras.setdefault(name, set()).update(direct.extras)
        while True:
            before = {name: set(extras) for name, extras in requested_extras.items()}
            for name in sorted(selected):
                _version, version_text, _wheel, metadata, _project = selected[name]
                info = metadata["info"]
                extras_for_marker = ["", *sorted(requested_extras.get(name, set()))]
                for raw in metadata["requires_dist"]:
                    try:
                        requirement = Requirement(raw)
                    except Exception as exc:
                        raise MetadataUnavailable("requires_dist_invalid") from exc
                    if requirement.url is not None:
                        raise MetadataUnavailable("requires_dist_direct_url_forbidden")
                    marker_text = str(requirement.marker) if requirement.marker else None
                    active, _decisions = evaluate_requirement_marker(
                        marker_text, self.marker_environment, extras_for_marker
                    )
                    if active:
                        child = normalized_name(requirement.name)
                        requested_extras.setdefault(child, set()).update(
                            normalized_name(item) for item in requirement.extras
                        )
            if before == requested_extras:
                break
        constraints: dict[str, list[tuple[str, str]]] = {}
        for name, direct in self._direct.items():
            constraints.setdefault(name, []).append(
                (direct.specifier, f"direct:{direct.original}")
            )
        for name, specifier in self._overrides.items():
            constraints.setdefault(name, []).append(
                (specifier, f"operator_override:{specifier}")
            )
        evaluations: list[dict[str, Any]] = []
        for name in sorted(selected):
            _version, version_text, _wheel, metadata, _project = selected[name]
            info = metadata["info"]
            extras_for_marker = ["", *sorted(requested_extras.get(name, set()))]
            for raw in metadata["requires_dist"]:
                try:
                    requirement = Requirement(raw)
                except Exception as exc:
                    raise MetadataUnavailable("requires_dist_invalid") from exc
                if requirement.url is not None:
                    raise MetadataUnavailable("requires_dist_direct_url_forbidden")
                child = normalized_name(requirement.name)
                marker_text = str(requirement.marker) if requirement.marker else None
                active, decisions = evaluate_requirement_marker(
                    marker_text, self.marker_environment, extras_for_marker
                )
                row = {
                    "parent": name,
                    "parent_version": version_text,
                    "requirement": raw,
                    "child": child,
                    "specifier": str(requirement.specifier),
                    "extras": sorted(normalized_name(item) for item in requirement.extras),
                    "marker": marker_text,
                    "marker_evaluations": decisions,
                    "active": active,
                }
                evaluations.append(row)
                if active:
                    constraints.setdefault(child, []).append(
                        (str(requirement.specifier), f"{name}=={version_text}: {raw}")
                    )
        return constraints, requested_extras, evaluations

    def _selected_conflict(
        self, selected: Mapping[str, Any], constraints: Mapping[str, list[tuple[str, str]]]
    ) -> str | None:
        for name in sorted(selected):
            version = selected[name][0]
            for specifier, source in constraints.get(name, []):
                if specifier and not SpecifierSet(specifier).contains(
                    version, prereleases=True
                ):
                    return f"{name}=={version} violates {specifier} from {source}"
        return None

    def _bind_candidate(
        self,
        name: str,
        version_text: str,
        wheels: list[CandidateWheel],
        project_payload: dict[str, Any],
        project_provenance: dict[str, Any],
    ) -> tuple[Any, ...] | None:
        try:
            metadata = self._candidate_metadata(
                name, version_text, project_payload, project_provenance
            )
        except MetadataUnavailable as exc:
            self.metadata_failures[(normalized_name(name), version_text)] = str(exc)
            self.candidate_rejections.append(
                {
                    "name": normalized_name(name),
                    "version": version_text,
                    "reason": "METADATA_UNAVAILABLE",
                    "detail": str(exc),
                }
            )
            return None
        info = metadata["info"]
        if not _requires_python_accepts(
            info.get("requires_python"),
            self.marker_environment["python_full_version"],
        ):
            self.candidate_rejections.append(
                {
                    "name": normalized_name(name),
                    "version": version_text,
                    "reason": "REQUIRES_PYTHON_REJECTED",
                    "requires_python": info.get("requires_python"),
                }
            )
            return None
        files = {
            item.get("filename"): item
            for item in metadata["version_files"]
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        }
        matching_wheels = []
        for wheel in wheels:
            version_file = files.get(wheel.filename)
            digests = version_file.get("digests") if isinstance(version_file, dict) else None
            if (
                version_file is None
                or not isinstance(digests, dict)
                or digests.get("sha256") != wheel.sha256
                or version_file.get("yanked", False)
            ):
                continue
            matching_wheels.append(wheel)
        if not matching_wheels:
            self.candidate_rejections.append(
                {
                    "name": normalized_name(name),
                    "version": version_text,
                    "reason": "VERSION_WHEEL_METADATA_MISMATCH",
                }
            )
            return None
        return (
            Version(version_text),
            version_text,
            matching_wheels[0],
            metadata,
            project_provenance,
        )

    def _search(
        self, selected: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        self.search_nodes += 1
        if self.search_nodes > self.max_search_nodes:
            return None, "resolution_search_bound_exceeded"
        try:
            constraints, _extras, _evaluations = self._derive(selected)
        except MetadataResolutionError as exc:
            return None, str(exc)
        conflict = self._selected_conflict(selected, constraints)
        if conflict:
            return None, conflict
        pending = sorted(set(constraints) - set(selected))
        if not pending:
            return selected, None
        name = pending[0]
        override = self._overrides.get(name)
        try:
            candidates, counts = self._candidate_versions(
                name,
                [specifier for specifier, _source in constraints[name]],
                override,
            )
            self._project_counts[name] = counts
        except MetadataUnavailable as exc:
            return None, f"{name}:METADATA_UNAVAILABLE:{exc}"
        if not candidates:
            if self.request_limit_hit:
                return None, f"{name}:METADATA_UNAVAILABLE:metadata_request_bound_exceeded"
            if counts.get("versions_satisfying_constraints", 0) == 0:
                return None, f"{name}:RESOLUTION_CONFLICT"
            if (
                counts.get("versions_without_compatible_wheel", 0)
                == counts.get("versions_satisfying_constraints", 0)
            ):
                return None, f"{name}:NO_COMPATIBLE_TARGET_WHEEL"
            if counts.get("versions_rejected_by_requires_python", 0):
                return None, f"{name}:RESOLUTION_CONFLICT:requires_python"
            if counts.get("versions_rejected_yanked", 0):
                return None, f"{name}:RESOLUTION_CONFLICT:yanked_only"
            return None, f"{name}:METADATA_UNAVAILABLE"
        branches: list[str] = []
        for _version, version_text, wheels, project_payload, project_provenance in candidates:
            candidate = self._bind_candidate(
                name, version_text, wheels, project_payload, project_provenance
            )
            if candidate is None:
                if self.request_limit_hit:
                    return None, f"{name}:METADATA_UNAVAILABLE:metadata_request_bound_exceeded"
                continue
            trial = dict(selected)
            trial[name] = candidate
            result, failure = self._search(trial)
            if result is not None:
                return result, None
            reason = failure or "RESOLUTION_CONFLICT"
            branches.append(reason)
            self.candidate_rejections.append(
                {
                    "name": name,
                    "version": version_text,
                    "reason": "RESOLUTION_CONFLICT",
                    "detail": reason,
                }
            )
            self.conflicts.append(
                {"name": name, "candidate_version": version_text, "conflict": reason}
            )
            if reason in {
                "resolution_search_bound_exceeded",
            } or reason.endswith("metadata_request_bound_exceeded"):
                return None, reason
        return None, branches[-1] if branches else f"{name}:METADATA_UNAVAILABLE"

    def _unresolved_records(self, failure: str | None) -> list[dict[str, Any]]:
        detail = failure or "METADATA_RESOLUTION_INCOMPLETE"
        if "NO_COMPATIBLE_TARGET_WHEEL" in detail:
            classification = "NO_COMPATIBLE_TARGET_WHEEL"
        elif "RESOLUTION_CONFLICT" in detail or "violates" in detail:
            classification = "RESOLUTION_CONFLICT"
        else:
            classification = "METADATA_UNAVAILABLE"
        return [{"classification": classification, "detail": detail}]

    def _build_graph(
        self, selected: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        constraints, requested_extras, evaluations = self._derive(selected)
        packages: list[dict[str, Any]] = []
        package_map: dict[str, dict[str, Any]] = {}
        for name in sorted(selected):
            _version, version_text, chosen_wheel, metadata, project_provenance = selected[name]
            info = metadata["info"]
            version_file = next(
                item for item in metadata["version_files"]
                if isinstance(item, dict) and item.get("filename") == chosen_wheel.filename
            )
            artifact_record = {
                "filename": version_file.get("filename"),
                "packagetype": version_file.get("packagetype"),
                "sha256": (
                    version_file.get("digests", {}).get("sha256")
                    if isinstance(version_file.get("digests"), dict)
                    else None
                ),
                "yanked": bool(version_file.get("yanked", False)),
                "requires_python": version_file.get("requires_python"),
                "upload_time": version_file.get("upload_time_iso_8601")
                or version_file.get("upload_time"),
                "artifact_url_field_present": "url" in version_file,
            }
            _wheel_name, _wheel_version, _build, filename_tags = parse_wheel_filename(
                chosen_wheel.filename
            )
            record = {
                "name": name,
                "version": version_text,
                "requires_python": info.get("requires_python"),
                "requires_dist": list(metadata["requires_dist"]),
                "requires_dist_metadata_value": metadata["requires_dist_metadata_value"],
                "requires_dist_interpretation": metadata["requires_dist_interpretation"],
                "provides_extra": sorted(info.get("provides_extra") or []),
                "requested_extras": sorted(requested_extras.get(name, set())),
                "license_metadata": {
                    "license": info.get("license"),
                    "license_expression": info.get("license_expression"),
                    "license_files": info.get("license_files"),
                    "classifiers": [
                        item for item in (info.get("classifiers") or [])
                        if str(item).startswith("License ::")
                    ],
                    "review_status": "LICENSE_REVIEW_NOT_COMPLETE",
                },
                "distribution_metadata": {
                    "metadata_version": info.get("metadata_version"),
                    "identity_sha256": metadata["metadata_identity_sha256"],
                    "endpoint": metadata["metadata_provenance"]["url"],
                    "response_status": metadata["metadata_provenance"]["response_status"],
                    "retrieved_at_utc": metadata["metadata_provenance"]["retrieved_at_utc"],
                    "response_sha256": metadata["metadata_provenance"]["response_sha256"],
                },
                "project_listing": {
                    "endpoint": project_provenance["url"],
                    "response_status": project_provenance["response_status"],
                    "retrieved_at_utc": project_provenance["retrieved_at_utc"],
                    "response_sha256": project_provenance["response_sha256"],
                },
                "yanked": bool(version_file.get("yanked", False)),
                "wheel": {
                    "filename": chosen_wheel.filename,
                    "sha256": chosen_wheel.sha256,
                    "index_advertised_sha256": chosen_wheel.sha256,
                    "tags": sorted(str(tag) for tag in filename_tags),
                    "target_compatible_tags": list(chosen_wheel.tags),
                    "filename_tags_accepted_by_wp2a_runtime_lock_contract": all(
                        _wp2a_tag_supported(str(tag)) for tag in filename_tags
                    ),
                    "upload_time": version_file.get("upload_time_iso_8601") or version_file.get("upload_time"),
                    "requires_python": version_file.get("requires_python"),
                    "yanked": bool(version_file.get("yanked", False)),
                    "artifact_url_requested": False,
                    "artifact_bytes_acquired": False,
                },
                "index_artifact_record": {
                    **artifact_record,
                    "identity_sha256": hashlib.sha256(
                        canonical_json_bytes(artifact_record)
                    ).hexdigest(),
                    "project_listing_endpoint": project_provenance["url"],
                    "project_listing_response_sha256": project_provenance["response_sha256"],
                    "version_metadata_endpoint": metadata["metadata_provenance"]["url"],
                    "version_metadata_response_sha256": metadata["metadata_provenance"]["response_sha256"],
                    "sha256_agrees_across_index_records": (
                        artifact_record["sha256"] == chosen_wheel.sha256
                    ),
                },
                "effective_constraints": sorted(
                    {item[0] for item in constraints.get(name, []) if item[0]}
                ),
                "candidate_role": "DIRECT_AND_TRANSITIVE" if name in self._direct else "TRANSITIVE",
            }
            packages.append(record)
            package_map[name] = record
        edges = []
        for name, direct in sorted(self._direct.items()):
            edges.append(
                {
                    "parent": None,
                    "parent_version": None,
                    "child": name,
                    "child_version": package_map[name]["version"],
                    "requirement": direct.original,
                    "specifier": direct.specifier,
                    "extras": list(direct.extras),
                    "marker": None,
                    "marker_evaluations": [{"extra": "", "result": True}],
                    "active": True,
                    "relationship": "DIRECT",
                }
            )
        for row in evaluations:
            if not row["active"]:
                continue
            if row["child"] not in package_map:
                raise MetadataResolutionError("resolved_dependency_edge_target_missing")
            edges.append(
                {
                    "parent": row["parent"],
                    "parent_version": row["parent_version"],
                    "child": row["child"],
                    "child_version": package_map[row["child"]]["version"],
                    "requirement": row["requirement"],
                    "specifier": row["specifier"],
                    "extras": row["extras"],
                    "marker": row["marker"],
                    "marker_evaluations": row["marker_evaluations"],
                    "active": True,
                    "relationship": "TRANSITIVE",
                }
            )
        return packages, evaluations, edges


def load_authoritative_inputs(
    root: Path, config: Mapping[str, Any]
) -> tuple[list[DirectRequirement], dict[str, Any], dict[str, str]]:
    p0b = root / "scripts/maintenance/phase39_p0b"
    inputs_path = p0b / "worker-resolution-inputs.json"
    requirements_path = p0b / "worker-requirements.in"
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    source_bindings = config.get("source_bindings")
    if not isinstance(source_bindings, dict):
        raise MetadataResolutionError("source_bindings_missing")
    declared = {item["path"]: item["sha256"] for item in inputs.get("source_declarations", [])}
    expected = dict(declared)
    expected["scripts/maintenance/phase39_p0b/worker-resolution-inputs.json"] = hashlib.sha256(
        inputs_path.read_bytes()
    ).hexdigest()
    packet_path = p0b / "runtime-resolution-approval-packet.json"
    expected["scripts/maintenance/phase39_p0b/runtime-resolution-approval-packet.json"] = hashlib.sha256(
        packet_path.read_bytes()
    ).hexdigest()
    additional_bindings = config.get("additional_source_bindings", {})
    if not isinstance(additional_bindings, dict):
        raise MetadataResolutionError("additional_source_bindings_invalid")
    expected.update(additional_bindings)
    if source_bindings != expected:
        raise MetadataResolutionError("source_binding_configuration_mismatch")
    for relative, digest in expected.items():
        path = root / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise MetadataResolutionError("source_digest_mismatch:" + relative)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet_binding = config.get("approval_packet_binding")
    expected_packet_binding = {
        "packet_sha256": packet.get("packet_sha256"),
        "operator_authorization_status": packet.get("operator_authorization", {}).get("status"),
        "packet_is_authorization": packet.get("operator_authorization", {}).get("packet_is_authorization"),
    }
    if packet_binding != expected_packet_binding:
        raise MetadataResolutionError("approval_packet_binding_mismatch")
    if packet.get("operator_authorization", {}).get("packet_is_authorization") is not False:
        raise MetadataResolutionError("approval_packet_misclassified")
    direct_by_line = {
        int(row["line"]): row for row in inputs.get("direct_requirements", [])
    }
    direct: list[DirectRequirement] = []
    for line_number, raw in enumerate(
        requirements_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        try:
            parsed = Requirement(value)
        except Exception as exc:
            raise MetadataResolutionError("authoritative_direct_requirement_invalid") from exc
        row = direct_by_line.get(line_number)
        if row is None or row.get("original_declaration") != value:
            raise MetadataResolutionError("authoritative_requirement_input_mismatch")
        name = normalized_name(parsed.name)
        try:
            same_constraints = SpecifierSet(str(parsed.specifier)) == SpecifierSet(
                row["constraints"]
            )
        except ValueError as exc:
            raise MetadataResolutionError("authoritative_requirement_fields_mismatch") from exc
        if name != normalized_name(row["name"]) or not same_constraints:
            raise MetadataResolutionError("authoritative_requirement_fields_mismatch")
        extras = tuple(sorted(normalized_name(item) for item in parsed.extras))
        if list(extras) != sorted(normalized_name(item) for item in row.get("extras", [])):
            raise MetadataResolutionError("authoritative_requirement_extras_mismatch")
        if parsed.marker is not None:
            raise MetadataResolutionError("direct_requirement_marker_not_preapproved")
        if parsed.url is not None:
            raise MetadataResolutionError("direct_url_requirement_forbidden")
        direct.append(DirectRequirement(name, row["constraints"], extras, value, line_number))
    if len(direct) != 18 or len(direct_by_line) != len(direct):
        raise MetadataResolutionError("authoritative_direct_requirement_count_mismatch")
    if "tzdata" not in {item.name for item in direct}:
        raise MetadataResolutionError("tzdata_direct_candidate_missing")
    overrides = config.get("candidate_overrides")
    if not isinstance(overrides, list) or len(overrides) != 1:
        raise MetadataResolutionError("candidate_override_configuration_invalid")
    if overrides[0] != {
        "name": "bcrypt",
        "specifier": "==5.0.0",
        "authorization": "operator-authorized-candidate-only",
        "source_requirement_unchanged": True,
    }:
        raise MetadataResolutionError("bcrypt_override_configuration_invalid")
    if not any(item.name == "bcrypt" and item.specifier == "" for item in direct):
        raise MetadataResolutionError("bcrypt_source_requirement_not_unbounded")
    return direct, inputs, expected


def build_document(
    *,
    config: Mapping[str, Any],
    inputs: Mapping[str, Any],
    source_bindings: Mapping[str, str],
    direct: list[DirectRequirement],
    resolution: Mapping[str, Any],
    resolver: MetadataOnlyResolver,
    config_sha256: str,
) -> dict[str, Any]:
    network_evidence = list(resolver.client.evidence)
    host_set = sorted({item["host"] for item in network_evidence})
    source_direct = [
        {
            "name": item.name,
            "original_declaration": item.original,
            "constraints": item.specifier,
            "extras": list(item.extras),
            "marker": None,
            "line": item.line,
            "candidate_override": "==5.0.0" if item.name == "bcrypt" else None,
            "effective_constraints": sorted(
                filter(None, [item.specifier, "==5.0.0" if item.name == "bcrypt" else ""])
            ),
        }
        for item in direct
    ]
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission": "PHASE39_P0B_WP2B3_M1_METADATA_ONLY_DEPENDENCY_RESOLUTION",
        "decision": resolution["status"],
        "metadata_only_evidence": True,
        "production_lock": False,
        "source_binding_digests": dict(sorted(source_bindings.items())),
        "configuration": {
            "path": "scripts/maintenance/phase39_p0b/worker-metadata-resolution-config.json",
            "sha256": config_sha256,
            "candidate_selection": config["candidate_selection"],
            "candidate_overrides": config["candidate_overrides"],
            "tzdata_disposition": "RETAINED_UNCONDITIONAL_DIRECT_CANDIDATE",
            "python_full_version_marker_assumption": config["target"]["marker_environment"]["python_full_version"],
        },
        "target_platform": config["target"],
        "authorized_index": {
            "simple_index_url": AUTHORIZED_SIMPLE_INDEX,
            "metadata_api_origin": AUTHORIZED_ORIGIN,
            "authorized_host_allowlist": [AUTHORIZED_HOST],
            "tls_verification": "DEFAULT_CA_AND_HOSTNAME_VALIDATION",
            "redirect_policy": "REJECT_WITHOUT_FOLLOWING",
            "artifact_url_policy": "NEVER_REQUEST_OR_FOLLOW",
        },
        "resolver": {
            "name": RESOLVER_NAME,
            "version": RESOLVER_VERSION,
            "packaging_version": packaging.__version__,
            "algorithm": "deterministic-backtracking; lexicographic-unresolved-name; stable-non-yanked-highest-first; prereleases-after-stable-candidates",
            "exact_arguments": [
                "--metadata-only",
                "--config scripts/maintenance/phase39_p0b/worker-metadata-resolution-config.json",
            ],
            "execution_platform_observation_used_for_candidates": False,
        },
        "direct_requirements": source_direct,
        "packages": resolution["packages"],
        "wp2a_runtime_lock_tag_policy": {
            "all_selected_wheels_accepted": all(
                item["wheel"]["filename_tags_accepted_by_wp2a_runtime_lock_contract"]
                for item in resolution["packages"]
            ) if resolution["packages"] else False,
            "incompatible_selected_wheel_filenames": [
                item["wheel"]["filename"]
                for item in resolution["packages"]
                if not item["wheel"]["filename_tags_accepted_by_wp2a_runtime_lock_contract"]
            ],
            "status": "EVIDENCE_ONLY; no runtime lock created or promoted",
        },
        "dependency_evaluations": resolution["dependency_evaluations"],
        "dependency_edges": resolution["dependency_edges"],
        "candidate_rejections": sorted(
            resolver.candidate_rejections,
            key=lambda item: (
                str(item.get("name", "")), str(item.get("version", "")),
                str(item.get("reason", "")), str(item.get("detail", "")),
            ),
        ),
        "resolution_conflicts": sorted(
            resolver.conflicts,
            key=lambda item: (item["name"], item["candidate_version"], item["conflict"]),
        ),
        "project_candidate_summaries": dict(sorted(resolver._project_counts.items())),
        "unresolved": resolution["unresolved"],
        "network_evidence": {
            "hosts_contacted": host_set,
            "requests": network_evidence,
            "redirect_attempts": resolver.client.redirect_attempts,
            "metadata_request_count": len(network_evidence),
            "artifact_url_requests": 0,
            "wheel_bytes_downloaded": False,
            "sdist_bytes_downloaded": False,
            "redirects_followed": 0,
        },
        "review_limitations": [
            "SECURITY_REVIEW_NOT_COMPLETE",
            "LICENSE_REVIEW_NOT_COMPLETE",
            "ARTIFACT_BYTES_NOT_VERIFIED",
            "NATIVE_ELF_CLOSURE_NOT_VERIFIED",
            "PRODUCTION_RELEASE_NOT_APPROVED",
        ],
        "runtime_actions": "NONE",
    }
    doc["manifest_sha256"] = canonical_digest(doc)
    validate_resolution_document(doc)
    return doc


def merge_attempt_history(
    final_document: Mapping[str, Any],
    previous_documents: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind earlier metadata-only attempts into the final network transcript."""
    merged = deepcopy(dict(final_document))
    attempts = []
    requests: list[dict[str, Any]] = []
    redirects: list[dict[str, Any]] = []
    documents = [*previous_documents, final_document]
    for index, attempt in enumerate(documents, 1):
        attempt_id = f"M1-R{index}"
        network = attempt.get("network_evidence", {})
        attempt_requests = [dict(row, attempt_id=attempt_id) for row in network.get("requests", [])]
        attempt_redirects = [
            dict(row, attempt_id=attempt_id) for row in network.get("redirect_attempts", [])
        ]
        requests.extend(attempt_requests)
        redirects.extend(attempt_redirects)
        rejection_counts: dict[str, int] = {}
        for rejection in attempt.get("candidate_rejections", []):
            reason = str(rejection.get("reason", "UNKNOWN"))
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        attempts.append(
            {
                "attempt_id": attempt_id,
                "decision": attempt.get("decision"),
                "resolver_version": attempt.get("resolver", {}).get("version"),
                "configuration_sha256": attempt.get("configuration", {}).get("sha256"),
                "manifest_sha256": attempt.get("manifest_sha256"),
                "metadata_request_count": len(attempt_requests),
                "hosts_contacted": sorted(
                    {row.get("host") for row in attempt_requests if row.get("host")}
                ),
                "redirect_attempt_count": len(attempt_redirects),
                "unresolved": attempt.get("unresolved", []),
                "candidate_rejection_counts": dict(sorted(rejection_counts.items())),
            }
        )
    merged["resolution_attempt_history"] = attempts
    merged_network = merged["network_evidence"]
    merged_network["requests"] = requests
    merged_network["redirect_attempts"] = redirects
    merged_network["hosts_contacted"] = sorted(
        {row.get("host") for row in requests if row.get("host")}
    )
    merged_network["metadata_request_count"] = len(requests)
    merged_network["redirects_followed"] = sum(
        1 for row in redirects if row.get("followed") is True
    )
    merged["manifest_sha256"] = canonical_digest(merged)
    validate_resolution_document(merged)
    return merged


def validate_resolution_document(doc: Mapping[str, Any]) -> None:
    if doc.get("schema") != SCHEMA or doc.get("schema_version") != SCHEMA_VERSION:
        raise MetadataResolutionError("resolution_document_schema_invalid")
    if doc.get("manifest_sha256") != canonical_digest(doc):
        raise MetadataResolutionError("resolution_document_digest_invalid")
    if doc.get("production_lock") is not False:
        raise MetadataResolutionError("production_lock_promotion_forbidden")
    network = doc.get("network_evidence")
    if not isinstance(network, Mapping):
        raise MetadataResolutionError("network_evidence_missing")
    if any(host != AUTHORIZED_HOST for host in network.get("hosts_contacted", [])):
        raise MetadataResolutionError("unauthorized_host_contacted")
    if (
        network.get("artifact_url_requests") != 0
        or network.get("wheel_bytes_downloaded") is not False
        or network.get("sdist_bytes_downloaded") is not False
    ):
        raise MetadataResolutionError("artifact_acquisition_detected")
    if network.get("redirects_followed") != 0:
        raise MetadataResolutionError("redirect_followed")
    decision = doc.get("decision")
    requests_by_url: dict[str, list[Mapping[str, Any]]] = {}
    for request in network.get("requests", []):
        if request.get("host") != AUTHORIZED_HOST or not str(request.get("url", "")).startswith(
            AUTHORIZED_ORIGIN + "/pypi/"
        ):
            raise MetadataResolutionError("unauthorized_metadata_provenance")
        if request.get("redirect_followed") is not False:
            raise MetadataResolutionError("redirect_followed")
        response_digest = request.get("response_sha256")
        if response_digest is not None and (
            not isinstance(response_digest, str) or SHA256_RE.fullmatch(response_digest) is None
        ):
            raise MetadataResolutionError("metadata_response_evidence_incomplete")
        if decision == "METADATA_RESOLUTION_COMPLETE" and (
            request.get("response_status") != 200 or response_digest is None
        ):
            raise MetadataResolutionError("metadata_response_evidence_incomplete")
        requests_by_url.setdefault(str(request.get("url")), []).append(request)
    for redirect in network.get("redirect_attempts", []):
        if redirect.get("followed") is not False:
            raise MetadataResolutionError("redirect_followed")
    packages = doc.get("packages")
    if not isinstance(packages, list):
        raise MetadataResolutionError("resolved_package_list_missing")
    by_name: dict[str, Mapping[str, Any]] = {}
    for package in packages:
        if not isinstance(package, Mapping):
            raise MetadataResolutionError("resolved_package_record_invalid")
        name = normalized_name(str(package.get("name", "")))
        if name in by_name or not NAME_RE.fullmatch(name):
            raise MetadataResolutionError("resolved_package_name_duplicate_or_invalid")
        by_name[name] = package
        try:
            version = Version(str(package.get("version", "")))
        except InvalidVersion as exc:
            raise MetadataResolutionError("resolved_package_version_invalid") from exc
        wheel = package.get("wheel")
        if not isinstance(wheel, Mapping):
            raise MetadataResolutionError("candidate_wheel_missing")
        filename = wheel.get("filename")
        digest = wheel.get("index_advertised_sha256")
        if (
            not isinstance(filename, str)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or wheel.get("sha256") != digest
        ):
            raise MetadataResolutionError("candidate_wheel_hash_or_filename_missing")
        try:
            wheel_name, wheel_version, _build, _tags = parse_wheel_filename(filename)
        except (InvalidVersion, ValueError) as exc:
            raise MetadataResolutionError("candidate_wheel_filename_invalid") from exc
        if normalized_name(str(wheel_name)) != name or wheel_version != version:
            raise MetadataResolutionError("candidate_wheel_identity_mismatch")
        compatible = target_wheel_tags(filename, doc["target_platform"])
        filename_tags = sorted(str(tag) for tag in _parse_wheel_tags(filename))
        if (
            not compatible
            or wheel.get("tags") != filename_tags
            or wheel.get("target_compatible_tags") != compatible
        ):
            raise MetadataResolutionError("candidate_wheel_incompatible")
        if wheel.get("yanked") is not False or package.get("yanked") is not False:
            raise MetadataResolutionError("yanked_candidate_selected")
        if (
            wheel.get("artifact_bytes_acquired") is not False
            or wheel.get("artifact_url_requested") is not False
        ):
            raise MetadataResolutionError("artifact_acquisition_detected")
        metadata = package.get("distribution_metadata")
        if not isinstance(metadata, Mapping) or not SHA256_RE.fullmatch(
            str(metadata.get("response_sha256", ""))
        ):
            raise MetadataResolutionError("distribution_metadata_provenance_missing")
        if not SHA256_RE.fullmatch(str(metadata.get("identity_sha256", ""))):
            raise MetadataResolutionError("distribution_metadata_identity_missing")
        if not isinstance(package.get("requires_dist"), list):
            raise MetadataResolutionError("dependency_metadata_missing")
        raw_requires_dist = package.get("requires_dist_metadata_value")
        interpretation = package.get("requires_dist_interpretation")
        if raw_requires_dist is None:
            if package["requires_dist"] != [] or interpretation != "PYPI_JSON_NULL_MEANS_NO_REQUIRES_DIST_FIELDS":
                raise MetadataResolutionError("requires_dist_null_interpretation_invalid")
        elif raw_requires_dist != package["requires_dist"] or interpretation != "PYPI_JSON_REQUIRES_DIST_LIST":
            raise MetadataResolutionError("requires_dist_value_mismatch")
        if not _requires_python_accepts(
            package.get("requires_python"),
            doc["target_platform"]["marker_environment"]["python_full_version"],
        ) or not _requires_python_accepts(
            wheel.get("requires_python"),
            doc["target_platform"]["marker_environment"]["python_full_version"],
        ):
            raise MetadataResolutionError("selected_requires_python_incompatible")
        for provenance_key in ("distribution_metadata", "project_listing"):
            provenance = package.get(provenance_key)
            if not isinstance(provenance, Mapping):
                raise MetadataResolutionError("metadata_provenance_missing")
            requests_for_endpoint = requests_by_url.get(str(provenance.get("endpoint")), [])
            if not any(
                request.get("response_sha256") == provenance.get("response_sha256")
                and request.get("response_status") == provenance.get("response_status")
                for request in requests_for_endpoint
            ):
                raise MetadataResolutionError("metadata_provenance_not_bound_to_request")
        index_record = package.get("index_artifact_record")
        if not isinstance(index_record, Mapping):
            raise MetadataResolutionError("index_artifact_record_missing")
        if (
            index_record.get("filename") != filename
            or index_record.get("packagetype") != "bdist_wheel"
            or index_record.get("sha256") != digest
            or index_record.get("yanked") is not False
            or index_record.get("sha256_agrees_across_index_records") is not True
            or not SHA256_RE.fullmatch(str(index_record.get("identity_sha256", "")))
        ):
            raise MetadataResolutionError("index_artifact_record_mismatch")
        artifact_projection = {
            key: index_record.get(key)
            for key in (
                "filename", "packagetype", "sha256", "yanked",
                "requires_python", "upload_time", "artifact_url_field_present",
            )
        }
        if hashlib.sha256(canonical_json_bytes(artifact_projection)).hexdigest() != index_record.get(
            "identity_sha256"
        ):
            raise MetadataResolutionError("index_artifact_identity_digest_invalid")
        for endpoint_key, digest_key in (
            ("project_listing_endpoint", "project_listing_response_sha256"),
            ("version_metadata_endpoint", "version_metadata_response_sha256"),
        ):
            evidence_rows = requests_by_url.get(str(index_record.get(endpoint_key)), [])
            if not any(
                row.get("response_status") == 200
                and row.get("response_sha256") == index_record.get(digest_key)
                for row in evidence_rows
            ):
                raise MetadataResolutionError("index_artifact_record_provenance_missing")
        lock_compatible = all(
            _wp2a_tag_supported(tag) for tag in filename_tags
        )
        if wheel.get("filename_tags_accepted_by_wp2a_runtime_lock_contract") is not lock_compatible:
            raise MetadataResolutionError("wp2a_tag_policy_evidence_mismatch")
    if decision == "METADATA_RESOLUTION_COMPLETE":
        if doc.get("unresolved") != [] or not packages:
            raise MetadataResolutionError("incomplete_graph_marked_complete")
        direct = doc.get("direct_requirements", [])
        if len(direct) != 18 or any(
            normalized_name(item["name"]) not in by_name for item in direct
        ):
            raise MetadataResolutionError("direct_requirement_closure_incomplete")
        direct_edges = [
            edge for edge in doc.get("dependency_edges", [])
            if edge.get("relationship") == "DIRECT"
        ]
        if len(direct_edges) != len(direct):
            raise MetadataResolutionError("direct_dependency_edges_incomplete")
        effective_overrides = doc.get("configuration", {}).get("candidate_overrides", [])
        if effective_overrides != [{
            "name": "bcrypt",
            "specifier": "==5.0.0",
            "authorization": "operator-authorized-candidate-only",
            "source_requirement_unchanged": True,
        }]:
            raise MetadataResolutionError("candidate_override_evidence_invalid")
        for requirement_record in direct:
            try:
                requirement = Requirement(requirement_record["original_declaration"])
            except Exception as exc:
                raise MetadataResolutionError("direct_requirement_evidence_invalid") from exc
            name = normalized_name(requirement.name)
            if requirement.url is not None or requirement.marker is not None:
                raise MetadataResolutionError("direct_requirement_url_or_marker_forbidden")
            if name != normalized_name(requirement_record["name"]):
                raise MetadataResolutionError("direct_requirement_name_mismatch")
            if normalized_name(requirement_record["name"]) == "bcrypt":
                if requirement_record.get("candidate_override") != "==5.0.0":
                    raise MetadataResolutionError("bcrypt_candidate_override_missing")
            elif requirement_record.get("candidate_override") is not None:
                raise MetadataResolutionError("unrecorded_candidate_override")
            version = Version(str(by_name[name]["version"]))
            for specifier in requirement_record.get("effective_constraints", []):
                if specifier and not SpecifierSet(specifier).contains(version, prereleases=True):
                    raise MetadataResolutionError("direct_requirement_constraint_unsatisfied")
            matches = [
                edge for edge in direct_edges
                if edge.get("child") == name
                and edge.get("requirement") == requirement_record["original_declaration"]
                and edge.get("child_version") == str(version)
            ]
            if len(matches) != 1:
                raise MetadataResolutionError("direct_requirement_edge_mismatch")
        evaluations = doc.get("dependency_evaluations", [])
        if len(evaluations) != sum(len(item["requires_dist"]) for item in packages):
            raise MetadataResolutionError("dependency_marker_evaluation_incomplete")
        expected_rows = Counter(
            (package["name"], package["version"], raw)
            for package in packages
            for raw in package["requires_dist"]
        )
        actual_rows = Counter(
            (row.get("parent"), row.get("parent_version"), row.get("requirement"))
            for row in evaluations
        )
        if actual_rows != expected_rows:
            raise MetadataResolutionError("dependency_metadata_identity_mismatch")
        expected_extras: dict[str, set[str]] = {
            name: set() for name in by_name
        }
        for requirement_record in direct:
            expected_extras[normalized_name(requirement_record["name"])].update(
                normalized_name(extra) for extra in requirement_record.get("extras", [])
            )
        while True:
            before = {name: set(extras) for name, extras in expected_extras.items()}
            for row in evaluations:
                parent = normalized_name(str(row.get("parent", "")))
                if parent not in by_name:
                    raise MetadataResolutionError("dependency_edge_parent_missing")
                try:
                    requirement = Requirement(row["requirement"])
                except Exception as exc:
                    raise MetadataResolutionError("requires_dist_invalid") from exc
                if requirement.url is not None:
                    raise MetadataResolutionError("requires_dist_direct_url_forbidden")
                active, _decisions = evaluate_requirement_marker(
                    str(requirement.marker) if requirement.marker else None,
                    doc["target_platform"]["marker_environment"],
                    ["", *sorted(expected_extras[parent])],
                )
                if active:
                    child = normalized_name(requirement.name)
                    if child not in expected_extras:
                        raise MetadataResolutionError("dependency_edge_target_missing")
                    expected_extras[child].update(
                        normalized_name(extra) for extra in requirement.extras
                    )
            if before == expected_extras:
                break
        for name, package in by_name.items():
            if package.get("requested_extras") != sorted(expected_extras[name]):
                raise MetadataResolutionError("extras_propagation_incomplete")
        for row in evaluations:
            parent = normalized_name(str(row["parent"]))
            package = by_name[parent]
            requirement = Requirement(row["requirement"])
            active, decisions = evaluate_requirement_marker(
                str(requirement.marker) if requirement.marker else None,
                doc["target_platform"]["marker_environment"],
                ["", *sorted(expected_extras[parent])],
            )
            if (
                normalized_name(requirement.name) != row.get("child")
                or str(requirement.specifier) != row.get("specifier")
                or (str(requirement.marker) if requirement.marker else None) != row.get("marker")
                or sorted(normalized_name(extra) for extra in requirement.extras) != row.get("extras")
                or active is not row.get("active")
                or decisions != row.get("marker_evaluations")
            ):
                raise MetadataResolutionError("dependency_marker_decision_mismatch")
            if active:
                child = by_name.get(normalized_name(requirement.name))
                if child is None or (
                    requirement.specifier
                    and not requirement.specifier.contains(
                        Version(str(child["version"])), prereleases=True
                    )
                ):
                    raise MetadataResolutionError("dependency_constraint_unsatisfied")
        edges = doc.get("dependency_edges", [])
        if len(edges) < len(direct):
            raise MetadataResolutionError("dependency_edges_incomplete")
        edge_keys = Counter(
            (edge.get("parent"), edge.get("requirement"), edge.get("child"))
            for edge in edges
        )
        expected_edge_keys = Counter(
            (None, item["original_declaration"], normalized_name(item["name"]))
            for item in direct
        )
        for evaluation in evaluations:
            if evaluation.get("active"):
                key = (evaluation.get("parent"), evaluation.get("requirement"), evaluation.get("child"))
                expected_edge_keys[key] += 1
        if edge_keys != expected_edge_keys:
            raise MetadataResolutionError("active_dependency_edge_missing_or_unexpected")
        for edge in edges:
            if normalized_name(str(edge.get("child", ""))) not in by_name:
                raise MetadataResolutionError("dependency_edge_target_missing")
            parent = edge.get("parent")
            if parent is not None and normalized_name(str(parent)) not in by_name:
                raise MetadataResolutionError("dependency_edge_parent_missing")
            child = by_name[normalized_name(str(edge["child"]))]
            if edge.get("child_version") != child.get("version"):
                raise MetadataResolutionError("dependency_edge_version_mismatch")
        lock_policy = doc.get("wp2a_runtime_lock_tag_policy", {})
        expected_lock_incompatible = sorted(
            package["wheel"]["filename"]
            for package in packages
            if not package["wheel"]["filename_tags_accepted_by_wp2a_runtime_lock_contract"]
        )
        if (
            lock_policy.get("incompatible_selected_wheel_filenames") != expected_lock_incompatible
            or lock_policy.get("all_selected_wheels_accepted") is not (not expected_lock_incompatible)
        ):
            raise MetadataResolutionError("wp2a_tag_policy_summary_mismatch")
        reachable = {
            normalized_name(str(edge["child"]))
            for edge in direct_edges
        }
        while True:
            before = set(reachable)
            reachable.update(
                normalized_name(str(edge["child"]))
                for edge in edges
                if edge.get("parent") is not None
                and normalized_name(str(edge["parent"])) in reachable
            )
            if before == reachable:
                break
        if reachable != set(by_name):
            raise MetadataResolutionError("unreachable_resolved_distribution")
    elif decision != "METADATA_RESOLUTION_INCOMPLETE":
        raise MetadataResolutionError("resolution_decision_invalid")


def resolve_from_files(
    root: Path, config_path: Path, client: BoundedPyPIMetadataClient | None = None
) -> dict[str, Any]:
    root = Path(root)
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if (
        config.get("schema") != "phase39-p0b-worker-metadata-resolution-config"
        or config.get("schema_version") != 1
    ):
        raise MetadataResolutionError("resolution_configuration_invalid")
    direct, inputs, source_bindings = load_authoritative_inputs(root, config)
    if config.get("target", {}).get("platform") != inputs.get("target", {}).get("platform"):
        raise MetadataResolutionError("target_platform_binding_mismatch")
    if config.get("target", {}).get("python") != inputs.get("target", {}).get("python"):
        raise MetadataResolutionError("target_python_binding_mismatch")
    marker_environment = dict(config["target"]["marker_environment"])
    for key, value in inputs["target"]["marker_environment"].items():
        if marker_environment.get(key) != value:
            raise MetadataResolutionError("target_marker_binding_mismatch:" + key)
    resolver = MetadataOnlyResolver(
        target=config["target"],
        marker_environment=marker_environment,
        client=client or BoundedPyPIMetadataClient(),
    )
    overrides = {
        item["name"]: item["specifier"] for item in config["candidate_overrides"]
    }
    resolution = resolver.resolve(direct, overrides)
    return build_document(
        config=config,
        inputs=inputs,
        source_bindings=source_bindings,
        direct=direct,
        resolution=resolution,
        resolver=resolver,
        config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-only", action="store_true", required=True)
    parser.add_argument(
        "--config",
        default="scripts/maintenance/phase39_p0b/worker-metadata-resolution-config.json",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    document = resolve_from_files(root, config_path)
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_json_bytes(document) + b"\n")
        output.chmod(0o666)
    else:
        print(canonical_json_bytes(document).decode("utf-8"))
    return 0 if document["decision"] == "METADATA_RESOLUTION_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
