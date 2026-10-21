"""Offline source-review packet for Phase 39 P0B WP2B2.

This module reads the WP2B1 source/evidence files only. It does not import the
application, resolve distributions, access indexes, invoke subprocesses, or
install/build packages.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "phase39-p0b-runtime-resolution-approval-packet"
SCHEMA_VERSION = 1
MISSION = "PHASE39_P0B_WP2B2_RUNTIME_RESOLUTION_REVIEW_AND_APPROVAL_PACKET"
CANDIDATE_INDEX_URL = "https://pypi.org/simple"
ARTIFACT_HOSTS = ["files.pythonhosted.org"]

SOURCE_PATHS = {
    "worker_source": "scripts/maintenance/phase39_p0b/worker-requirements.in",
    "classification": "scripts/maintenance/phase39_p0b/worker-dependency-classification.json",
    "import_graph": "scripts/maintenance/phase39_p0b/worker-import-graph.json",
    "resolution_inputs": "scripts/maintenance/phase39_p0b/worker-resolution-inputs.json",
}

DIRECT_IMPORT_ROOTS = {
    "fastapi": "fastapi",
    "sqlalchemy": "sqlalchemy",
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "typing-extensions": "typing_extensions",
    "python-jose": "jose",
    "bcrypt": "bcrypt",
    "httpx": "httpx",
    "pynacl": "nacl",
    "email-validator": None,
    "jsonschema": "jsonschema",
    "celery": "celery",
    "redis": "redis",
    "langfuse": "langfuse",
    "qdrant-client": "qdrant_client",
    "openai": "openai",
    "pyyaml": "yaml",
    "tzdata": "tzdata",
}

CONDITIONAL_REVIEW = {
    "langfuse": (
        "Runtime tracing use is conditional on configuration; the proposed "
        "source has no environment marker, so resolution includes it for the "
        "fixed Worker source."
    ),
    "pyyaml": (
        "Workflow-template loading is conditional; the proposed source has no "
        "environment marker, so resolution includes it for the fixed Worker source."
    ),
    "tzdata": (
        "Recommend unconditional inclusion in the protected runtime. The source "
        "documents a corrupted system zoneinfo database and the fallback does "
        "not validate system files. The protected target image was not inspected, "
        "so OS zoneinfo cannot be treated as sufficient evidence."
    ),
}

UNRESOLVED_DECISION_IDS = {
    "bcrypt_compatibility_range",
    "tzdata_unconditional_inclusion_confirmation",
    "operator_origin_approval",
    "resolved_artifact_and_package_reviews",
    "native_elf_and_base_image_closure",
}

PACKAGE_REVIEW_FIELDS = [
    "distribution_name",
    "exact_version",
    "artifact_filename",
    "artifact_sha256",
    "license_metadata",
    "license_review_decision",
    "security_advisory_evidence",
    "security_review_decision",
    "native_extension_status",
    "maintainer_origin_evidence",
    "review_timestamp_utc",
    "reviewer_identity",
]

NATIVE_REVIEW_FIELDS = [
    "wheel_filename_and_tags",
    "wheel_sha256",
    "native_member_inventory_and_hashes",
    "elf_architecture_and_interpreter",
    "dt_needed",
    "rpath_and_runpath",
    "dynamic_loader_identity",
    "glibc_compatibility_and_base_image_identity",
    "openssl_compatibility",
    "native_shared_library_provenance",
    "review_timestamp_utc",
    "reviewer_identity",
]

TOP_FIELDS = {
    "schema",
    "schema_version",
    "mission",
    "decision_status",
    "decision_status_definitions",
    "packet_sha256",
    "source_bindings",
    "target",
    "worker_source_review",
    "resolution_policy",
    "candidate_origin_policy",
    "package_review_contract",
    "native_review_policy",
    "unresolved_decisions",
    "acquisition_restrictions",
    "operator_authorization",
}


class ApprovalPacketError(ValueError):
    """Raised when source evidence or the closed packet contract is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApprovalPacketError("canonical_json_invalid") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def packet_identity(packet: dict[str, Any]) -> str:
    body = dict(packet)
    body.pop("packet_sha256", None)
    return sha256_bytes(canonical_json_bytes(body))


def _read_json(root: Path, relative_path: str, code: str) -> tuple[dict[str, Any], bytes]:
    raw = (root / relative_path).read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalPacketError(code) from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ApprovalPacketError(code + "_noncanonical")
    return value, raw


def _source_inputs(root: Path) -> dict[str, Any]:
    raw_sources = {key: (root / path).read_bytes() for key, path in SOURCE_PATHS.items()}
    classification, classification_bytes = _read_json(
        root, SOURCE_PATHS["classification"], "classification_json_invalid"
    )
    graph, graph_bytes = _read_json(root, SOURCE_PATHS["import_graph"], "import_graph_json_invalid")
    inputs, inputs_bytes = _read_json(
        root, SOURCE_PATHS["resolution_inputs"], "resolution_inputs_json_invalid"
    )

    actual = {key: sha256_bytes(raw) for key, raw in raw_sources.items()}
    expected_source_digests = {
        row["path"]: row["sha256"]
        for row in inputs.get("source_declarations", [])
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    for key in ("worker_source", "classification", "import_graph"):
        path = SOURCE_PATHS[key]
        if expected_source_digests.get(path) != actual[key]:
            raise ApprovalPacketError("source_digest_mismatch:" + key)

    if classification.get("proposed_worker_source") != {
        "path": SOURCE_PATHS["worker_source"],
        "sha256": actual["worker_source"],
    }:
        raise ApprovalPacketError("classification_worker_source_digest_mismatch")
    if classification.get("worker_import_graph", {}).get("path") != SOURCE_PATHS["import_graph"]:
        raise ApprovalPacketError("classification_import_graph_path_mismatch")
    if classification.get("worker_import_graph", {}).get("sha256") != actual["import_graph"]:
        raise ApprovalPacketError("classification_import_graph_digest_mismatch")
    if classification.get("worker_import_graph", {}).get("local_module_count") != len(
        graph.get("local_modules", [])
    ):
        raise ApprovalPacketError("classification_import_graph_count_mismatch")

    try:
        declarations = inputs["direct_requirements"]
        source_lines = raw_sources["worker_source"].decode("utf-8").splitlines()
        classification_rows = classification["classifications"]
    except (KeyError, TypeError, UnicodeDecodeError) as exc:
        raise ApprovalPacketError("source_evidence_shape_invalid") from exc
    if not isinstance(declarations, list) or len(declarations) != 18:
        raise ApprovalPacketError("direct_requirement_count_invalid")
    if not isinstance(classification_rows, list):
        raise ApprovalPacketError("classification_rows_invalid")

    class_by_name: dict[str, dict[str, Any]] = {}
    for row in classification_rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ApprovalPacketError("classification_row_invalid")
        name = row["name"]
        if name in class_by_name:
            raise ApprovalPacketError("classification_package_duplicate:" + name)
        class_by_name[name] = row

    direct_names: set[str] = set()
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise ApprovalPacketError("direct_requirement_invalid")
        name = declaration.get("name")
        line_number = declaration.get("line")
        if not isinstance(name, str) or name in direct_names:
            raise ApprovalPacketError("direct_requirement_duplicate_or_invalid")
        if not isinstance(line_number, int) or not 1 <= line_number <= len(source_lines):
            raise ApprovalPacketError("direct_requirement_line_invalid:" + name)
        raw_line = source_lines[line_number - 1].strip()
        source_declaration = raw_line.split(" #", 1)[0].strip()
        if source_declaration != declaration.get("original_declaration"):
            raise ApprovalPacketError("direct_requirement_line_mismatch:" + name)
        if name not in class_by_name:
            raise ApprovalPacketError("direct_requirement_classification_missing:" + name)
        direct_names.add(name)

    graph_roots = graph.get("third_party_imports")
    if not isinstance(graph_roots, dict):
        raise ApprovalPacketError("third_party_import_graph_invalid")
    return {
        "raw_sources": raw_sources,
        "actual_digests": actual,
        "classification": classification,
        "classification_bytes": classification_bytes,
        "graph": graph,
        "graph_bytes": graph_bytes,
        "inputs": inputs,
        "inputs_bytes": inputs_bytes,
        "declarations": declarations,
        "class_by_name": class_by_name,
    }


def _clean_evidence_path(value: str) -> str:
    path, separator, suffix = value.partition(":")
    return path if separator and re.fullmatch(r"\d+(?:-\d+)?", suffix) else value


def _graph_evidence(
    graph_roots: dict[str, Any], name: str, evidence_paths: list[str]
) -> list[dict[str, Any]]:
    import_root = DIRECT_IMPORT_ROOTS.get(name)
    if import_root is None:
        return []
    allowed_paths = {_clean_evidence_path(path) for path in evidence_paths}
    rows = graph_roots.get(import_root, [])
    if not isinstance(rows, list):
        raise ApprovalPacketError("third_party_import_rows_invalid:" + name)
    return [
        {
            "module": row["module"],
            "path": row["path"],
            "line": row["line"],
            "deferred_to_runtime": row["deferred_to_runtime"],
            "optional_guard": row["optional_guard"],
        }
        for row in rows
        if isinstance(row, dict) and row.get("path") in allowed_paths
    ]


def _direct_review_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    graph_roots = evidence["graph"]["third_party_imports"]
    rows: list[dict[str, Any]] = []
    for declaration in evidence["declarations"]:
        name = declaration["name"]
        classification = evidence["class_by_name"][name]
        source_paths = classification.get("evidence_source", [])
        if not isinstance(source_paths, list) or not all(
            isinstance(path, str) for path in source_paths
        ):
            raise ApprovalPacketError("worker_import_evidence_invalid:" + name)
        graph_evidence = _graph_evidence(graph_roots, name, source_paths)
        if name != "email-validator" and not graph_evidence:
            raise ApprovalPacketError("worker_import_graph_evidence_missing:" + name)
        condition_note = CONDITIONAL_REVIEW.get(name)
        rows.append(
            {
                "distribution_name": name,
                "source_declaration": declaration["original_declaration"],
                "source_line": declaration["line"],
                "source_constraints": declaration["constraints"],
                "requested_extras": declaration["extras"],
                "environment_marker": declaration["marker"],
                "applies_to_target": next(
                    (
                        result["target_result"]
                        for result in evidence["inputs"].get("target_marker_results", [])
                        if result.get("name") == name
                    ),
                    None,
                ),
                "classification": classification["classification"],
                "runtime_condition": classification["runtime_condition"],
                "inclusion_rationale": classification["reason"],
                "worker_import_evidence_paths": source_paths,
                "worker_import_graph_evidence": graph_evidence,
                "import_evidence_kind": (
                    "PYDANTIC_EMAILSTR_INDIRECT_PLUGIN"
                    if name == "email-validator"
                    else "STATIC_WORKER_IMPORT_GRAPH"
                ),
                "resolution_disposition": "INCLUDED_UNCONDITIONALLY"
                if declaration["marker"] is None
                else "MARKER_EVALUATED_FOR_TARGET",
                "conditional_runtime_review": condition_note,
                "version_bound_disposition": (
                    "UNRESOLVED_NO_SAFE_OFFLINE_BOUND_ESTABLISHED"
                    if name == "bcrypt"
                    else "SOURCE_CONSTRAINT_PRESERVED_EXACT_VERSION_STILL_REQUIRED"
                ),
            }
        )
    return rows


def _excluded_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for classification in evidence["class_by_name"].values():
        if classification.get("classification") != "NOT_REQUIRED_FOR_WORKER":
            continue
        rows.append(
            {
                "distribution_name": classification["name"],
                "source_declaration": classification.get("original_declaration"),
                "source_line": classification.get("line"),
                "exclusion_rationale": classification["reason"],
                "worker_import_evidence_paths": classification.get("evidence_source", []),
                "resolution_disposition": "EXCLUDED_FROM_WORKER_SOURCE",
                "replaced_by": "bcrypt" if classification["name"] == "passlib" else None,
            }
        )
    return sorted(rows, key=lambda row: row["distribution_name"])


def _origin_policy() -> dict[str, Any]:
    return {
        "candidate_index_url": CANDIDATE_INDEX_URL,
        "reviewed_origin": {
            "index_url": CANDIDATE_INDEX_URL,
            "index_host": "pypi.org",
            "artifact_hosts": ARTIFACT_HOSTS,
            "review_state": "OFFLINE_POLICY_REVIEWED_CANDIDATE",
            "network_service_checked": False,
        },
        "operator_approved_origin": None,
        "operator_approval_status": "NOT_APPROVED",
        "https_policy": "HTTPS_REQUIRED_FOR_INDEX_AND_ARTIFACTS; reject downgrade, userinfo, non-allowlisted host, and unexpected port",
        "redirect_policy": {
            "index_request": "REJECT_REDIRECTS",
            "artifact_request": "VALIDATE_EVERY_HOP_AS_HTTPS_AND_ALLOWLISTED; reject downgrade or off-list host",
            "record_all_hops": True,
        },
        "allowed_artifact_hosts": ARTIFACT_HOSTS,
        "hash_verification": "Require SHA-256 in the operator-reviewed exact lock and recompute over downloaded wheel bytes before any use",
        "origin_recording": [
            "exact_index_url",
            "initial_artifact_url",
            "final_artifact_url",
            "redirect_chain",
            "retrieved_at_utc",
            "origin_and_provenance_status",
            "artifact_sha256",
        ],
        "artifact_provenance_limitations": "HTTPS and a matching hash bind transport and bytes; they do not establish maintainer identity, build provenance, or license acceptance. Require independent package publisher/maintainer and provenance evidence.",
        "mirror_policy": "NO_MIRROR_AUTHORIZED; any mirror requires separate explicit approval binding exact URL, TLS identity, content snapshot, upstream synchronization evidence, and per-artifact origin",
        "cache_policy": "Use an empty isolated per-run cache; disable fallback and shared mutable caches; if a cache is explicitly approved later, bind its inventory digest and verify every wheel against the reviewed lock",
        "dependency_confusion_policy": "Use one approved index only; forbid extra-index-url, trusted-host, direct URL/VCS requirements, and silent public/private source mixing; bind any internal namespace to an explicitly approved index before resolution",
    }


def _unresolved_decisions() -> list[dict[str, str]]:
    return [
        {
            "id": "bcrypt_compatibility_range",
            "status": "UNRESOLVED",
            "question": "What bounded bcrypt source range is supported by this Worker code?",
            "evidence": "app.auth calls bcrypt.hashpw, bcrypt.gensalt, and bcrypt.checkpw directly; the proposed direct declaration is unbounded. No offline compatibility test or safe version range was established.",
            "blocks": "DEPENDENCY_RESOLUTION_AND_ACQUISITION",
            "recommended_next_step": "Operator supplies a bounded range backed by source/API evidence or separately authorizes an offline compatibility-test work package; do not infer from the mutable venv.",
        },
        {
            "id": "tzdata_unconditional_inclusion_confirmation",
            "status": "RECOMMENDATION_PENDING_OPERATOR_CONFIRMATION",
            "question": "Accept unconditional tzdata inclusion for the protected Worker runtime?",
            "evidence": "The direct source has no environment marker and therefore resolves tzdata unconditionally. app.celery_app documents corrupted system zoneinfo and its fallback does not validate OS data; the protected target image was not inspected.",
            "blocks": "SOURCE_SET_ACCEPTANCE",
            "recommended_next_step": "Keep tzdata unconditional; do not treat the target OS zoneinfo database as sufficient without image identity and integrity evidence.",
        },
        {
            "id": "operator_origin_approval",
            "status": "NOT_APPROVED",
            "question": "Approve the exact PyPI index and the bounded artifact-host/redirect policy for a separate future action?",
            "evidence": "https://pypi.org/simple is the WP2A contract candidate; no operator authorization was supplied in this mission.",
            "blocks": "DEPENDENCY_RESOLUTION_AND_ACQUISITION",
            "recommended_next_step": "Operator explicitly approves or replaces the candidate origin. This packet is not that approval.",
        },
        {
            "id": "resolved_artifact_and_package_reviews",
            "status": "PENDING_NO_RESOLVED_GRAPH",
            "question": "Which exact package versions, wheels, hashes, license decisions, and current advisory evidence are approved?",
            "evidence": "No package resolver ran, no transitive graph or wheel artifacts exist, and current license/security findings were not queried.",
            "blocks": "DOWNLOAD_ONLY_ACQUISITION",
            "recommended_next_step": "After source, bcrypt, tzdata, and origin decisions, prepare and review a complete exact graph and per-distribution evidence before any acquisition authorization.",
        },
        {
            "id": "native_elf_and_base_image_closure",
            "status": "PENDING_NO_ARTIFACTS_OR_IMAGE_IDENTITY",
            "question": "Do all selected native wheels and shared-library dependencies fit the protected runtime image?",
            "evidence": "No selected wheel, ELF inspection, protected base-image digest, dynamic-loader identity, glibc/OpenSSL mapping, or native shared-library provenance was established.",
            "blocks": "RUNTIME_ASSEMBLY_AND_ACTIVATION",
            "recommended_next_step": "Collect the exact artifact and base-image/OS-package evidence under the later native-closure work package; make no closure claim from this packet.",
        },
    ]


def build_packet(repository_root: str | Path) -> dict[str, Any]:
    root = Path(repository_root)
    evidence = _source_inputs(root)
    inputs = evidence["inputs"]
    target = inputs.get("target")
    if not isinstance(target, dict):
        raise ApprovalPacketError("target_missing")
    if inputs.get("origin_policy", {}).get("candidate_index_url") != CANDIDATE_INDEX_URL:
        raise ApprovalPacketError("candidate_index_mismatch")
    if inputs.get("acquisition_policy", {}).get("acquisition_authorized") is not False:
        raise ApprovalPacketError("wp2b1_acquisition_state_invalid")
    direct = _direct_review_rows(evidence)
    excluded = _excluded_rows(evidence)
    packet: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission": MISSION,
        "decision_status": "READY_FOR_OPERATOR_REVIEW",
        "decision_status_definitions": {
            "READY_FOR_OPERATOR_REVIEW": "Offline source/evidence bindings are internally consistent and all choices are stated for operator review; this is not authorization or proof that dependencies are resolved.",
            "REVIEW_CORRECTIONS_REQUIRED": "A known source, classification, target, or origin-policy contradiction must be corrected before operator review.",
            "INSUFFICIENT_EVIDENCE": "Required source identity or evidence is missing, malformed, or cannot be bound to this packet.",
        },
        "source_bindings": {
            key: {"path": SOURCE_PATHS[key], "sha256": evidence["actual_digests"][key]}
            for key in SOURCE_PATHS
        },
        "target": target,
        "worker_source_review": {
            "direct_requirement_count": len(direct),
            "direct_requirements": direct,
            "excluded_distributions": excluded,
            "conditional_resolution_note": "Runtime use may be conditional while the proposed direct source resolves a package unconditionally. All 18 current declarations have no environment marker. Environment markers in transitive Requires-Dist metadata must still be evaluated against the bound target and requested extras.",
            "bcrypt_disposition": "Direct bcrypt is required by app.auth; passlib remains excluded. No safe bounded bcrypt compatibility range can be established offline, so the unbounded source declaration is unresolved and blocks resolution.",
            "tzdata_disposition": "Recommend tzdata be included unconditionally. The source currently does so because it has no marker. OS zoneinfo is not evidenced as sufficient for the protected image.",
        },
        "resolution_policy": {
            "final_lock_versions": "EXACT_VERSION_FOR_EVERY_DIRECT_AND_TRANSITIVE_DISTRIBUTION",
            "artifact_identity": "EXACT_COMPATIBLE_WHEEL_FILENAME_AND_SHA256_FOR_EVERY_DISTRIBUTION",
            "dependency_graph": "COMPLETE_DIRECT_AND_TRANSITIVE_REQUIRES_DIST_GRAPH_REQUIRED",
            "marker_evaluation": "EVALUATE_EACH_REQUIRES_DIST_MARKER_AGAINST_FIXED_TARGET_AND_PARENT_EXTRA; RECORD_EXPRESSION_AND_RESULT",
            "extras_closure": "EXPAND_REQUESTED_EXTRAS_TRANSITIVELY; direct extras are python-jose[cryptography] and celery[redis]",
            "wheel_tags": inputs.get("wheel_tag_policy"),
            "artifact_type": "WHEEL_ONLY",
            "source_distributions": "FORBIDDEN",
            "source_builds": "FORBIDDEN_IN_THIS_RESOLUTION_OR_ACQUISITION; any later build is a separate work package and explicit approval",
            "missing_compatible_wheel": "STOP_AND_RECORD_UNRESOLVED; no sdist fallback, substitution, or alternate host",
            "automatic_version_substitution": "FORBIDDEN",
            "dependency_upgrades_during_assembly": "FORBIDDEN",
            "network_fallback": "FORBIDDEN",
            "resolver_provenance_required": [
                "resolver name/version and exact arguments",
                "resolver execution image and digest",
                "target marker environment",
                "approved index identity and metadata snapshot evidence",
                "resolution timestamp and full selected graph",
            ],
        },
        "candidate_origin_policy": _origin_policy(),
        "package_review_contract": {
            "selection_status": "NOT_STARTED_NO_RESOLVED_GRAPH",
            "selected_distributions": [],
            "required_fields_for_each_selected_distribution": PACKAGE_REVIEW_FIELDS,
            "field_semantics": {
                "license_metadata": "Record wheel METADATA License-Expression/License fields, classifiers, and metadata source; metadata alone is not acceptance.",
                "license_review_decision": "A reviewer decision with policy basis; values such as ACCEPT/REJECT/CONDITIONAL/UNRESOLVED must be justified, never inferred from metadata alone.",
                "security_advisory_evidence": "Record advisory databases/providers, data snapshot or version, query timestamp, exact package/version match, advisory identifiers and evidence digest. Empty/unqueried evidence is NOT_REVIEWED, not vulnerability-free.",
                "security_review_decision": "Explicit reviewer conclusion scoped to the exact version and evidence timestamp; do not claim vulnerability-free without current evidence.",
                "maintainer_origin_evidence": "Record publisher/maintainer identity evidence, project ownership/source links, exact index/artifact URLs, TLS/redirect provenance, and evidence digest.",
                "review_timestamp_utc": "Timestamp of the actual package review; null until performed.",
                "reviewer_identity": "Named human or accountable review identity; null until performed.",
            },
            "current_package_reviews": "NONE; no exact package selection or current advisory/license lookup was performed",
        },
        "native_review_policy": {
            "state": "POLICY_DEFINED_CLOSURE_NOT_ESTABLISHED",
            "likely_or_possible_native_candidates": [
                {"distribution_name": "bcrypt", "basis": "direct cryptographic extension candidate; inspect selected wheel members"},
                {"distribution_name": "pynacl", "basis": "native binding/libsodium closure candidate"},
                {"distribution_name": "cryptography", "basis": "requested python-jose extra; native/OpenSSL compatibility candidate"},
                {"distribution_name": "pydantic-core", "basis": "Pydantic runtime native extension candidate"},
                {"distribution_name": "pyyaml", "basis": "selected wheel may include the optional LibYAML C extension"},
                {"distribution_name": "cffi", "basis": "possible native dependency if present in the resolved cryptography/PyNaCl graph"},
                {"distribution_name": "greenlet", "basis": "possible SQLAlchemy native accelerator if present in the resolved graph"},
                {"distribution_name": "all other locked distributions", "basis": "inspect every wheel; the list above is not exhaustive"},
            ],
            "required_evidence": NATIVE_REVIEW_FIELDS,
            "wheel_compatibility_policy": "Compare the exact wheel filename and expanded tags with the WP2A cp312/aarch64/manylinux/glibc-2.39 policy; reject incompatible architecture, ABI, or platform tags.",
            "elf_review_policy": "For each native member, record SHA-256, ELF class/machine, interpreter, DT_NEEDED, RPATH/RUNPATH, and inspect every transitive shared object against the identified target image.",
            "glibc_and_openssl_policy": "Verify required glibc symbol versions against the exact glibc 2.39 target and OpenSSL ABI against the exact target libraries; record the image and OS package identities.",
            "shared_library_provenance_policy": "Bind every non-bundled shared library to its exact OS package/version/hash/origin; identify bundled libraries and their wheel/member hashes and upstream provenance.",
            "actual_elf_closure_claim": False,
        },
        "unresolved_decisions": _unresolved_decisions(),
        "acquisition_restrictions": {
            "package_downloads": "PROHIBITED_IN_WP2B2",
            "package_installs": "PROHIBITED_IN_WP2B2",
            "package_builds": "PROHIBITED_IN_WP2B2",
            "dependency_upgrades": "PROHIBITED_IN_WP2B2",
            "production_wheelhouse": "NOT_CREATED",
            "runtime_assembly": "NOT_AUTHORIZED",
            "missing_or_incompatible_artifacts": "UNRESOLVED; no automatic substitution or fallback",
        },
        "operator_authorization": {
            "status": "NOT_GRANTED",
            "approval_id": None,
            "signed": False,
            "packet_is_authorization": False,
            "download_only_acquisition_authorized": False,
        },
    }
    packet["packet_sha256"] = packet_identity(packet)
    validate_packet(packet, repository_root=root)
    return packet


def _require_fields(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ApprovalPacketError(code)
    return value


def validate_packet(packet: Any, repository_root: str | Path | None = None) -> None:
    top = _require_fields(packet, TOP_FIELDS, "packet_fields_invalid")
    if top["schema"] != SCHEMA or top["schema_version"] != SCHEMA_VERSION:
        raise ApprovalPacketError("packet_schema_invalid")
    if top["mission"] != MISSION:
        raise ApprovalPacketError("packet_mission_invalid")
    if top["decision_status"] not in {
        "READY_FOR_OPERATOR_REVIEW",
        "REVIEW_CORRECTIONS_REQUIRED",
        "INSUFFICIENT_EVIDENCE",
    }:
        raise ApprovalPacketError("packet_decision_status_invalid")
    definitions = _require_fields(
        top["decision_status_definitions"],
        {"READY_FOR_OPERATOR_REVIEW", "REVIEW_CORRECTIONS_REQUIRED", "INSUFFICIENT_EVIDENCE"},
        "decision_status_definitions_invalid",
    )
    if not all(isinstance(value, str) and value for value in definitions.values()):
        raise ApprovalPacketError("decision_status_definitions_invalid")
    if not isinstance(top["packet_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", top["packet_sha256"]
    ):
        raise ApprovalPacketError("packet_digest_invalid")
    if packet_identity(top) != top["packet_sha256"]:
        raise ApprovalPacketError("packet_digest_mismatch")

    bindings = _require_fields(top["source_bindings"], set(SOURCE_PATHS), "source_bindings_invalid")
    for key, path in SOURCE_PATHS.items():
        binding = _require_fields(bindings[key], {"path", "sha256"}, "source_binding_invalid")
        if binding["path"] != path or not isinstance(binding["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", binding["sha256"]
        ):
            raise ApprovalPacketError("source_binding_invalid:" + key)

    target = _require_fields(
        top["target"],
        {
            "marker_environment",
            "platform",
            "python",
            "python_patch_version",
            "runtime_observation_is_not_target_authority",
        },
        "target_fields_invalid",
    )
    marker_environment = _require_fields(
        target["marker_environment"],
        {
            "implementation_name",
            "os_name",
            "platform_machine",
            "platform_python_implementation",
            "platform_system",
            "python_version",
            "sys_platform",
        },
        "target_marker_environment_invalid",
    )
    if marker_environment != {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "aarch64",
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_version": "3.12",
        "sys_platform": "linux",
    }:
        raise ApprovalPacketError("target_marker_environment_invalid")
    platform = _require_fields(
        target["platform"],
        {"distribution", "release", "architecture", "libc_name", "libc_baseline"},
        "target_platform_invalid",
    )
    if platform != {
        "distribution": "Ubuntu",
        "release": "24.04",
        "architecture": "aarch64",
        "libc_name": "glibc",
        "libc_baseline": "2.39",
    }:
        raise ApprovalPacketError("target_platform_invalid")
    python_target = _require_fields(
        target["python"],
        {"implementation", "version", "abi", "soabi"},
        "target_python_invalid",
    )
    if python_target != {
        "implementation": "CPython",
        "version": "3.12",
        "abi": "cp312",
        "soabi": "cpython-312-aarch64-linux-gnu",
    }:
        raise ApprovalPacketError("target_python_invalid")
    if target.get("python_patch_version") is not None or target.get(
        "runtime_observation_is_not_target_authority"
    ) is not True:
        raise ApprovalPacketError("target_observation_policy_invalid")

    review = _require_fields(
        top["worker_source_review"],
        {
            "direct_requirement_count",
            "direct_requirements",
            "excluded_distributions",
            "conditional_resolution_note",
            "bcrypt_disposition",
            "tzdata_disposition",
        },
        "worker_source_review_fields_invalid",
    )
    direct_rows = review["direct_requirements"]
    exclusions = review["excluded_distributions"]
    if review["direct_requirement_count"] != 18 or not isinstance(direct_rows, list) or len(direct_rows) != 18:
        raise ApprovalPacketError("direct_requirement_count_invalid")
    direct_fields = {
        "distribution_name",
        "source_declaration",
        "source_line",
        "source_constraints",
        "requested_extras",
        "environment_marker",
        "applies_to_target",
        "classification",
        "runtime_condition",
        "inclusion_rationale",
        "worker_import_evidence_paths",
        "worker_import_graph_evidence",
        "import_evidence_kind",
        "resolution_disposition",
        "conditional_runtime_review",
        "version_bound_disposition",
    }
    names: set[str] = set()
    for row in direct_rows:
        row = _require_fields(row, direct_fields, "direct_requirement_fields_invalid")
        name = row["distribution_name"]
        if not isinstance(name, str) or name in names:
            raise ApprovalPacketError("direct_requirement_duplicate")
        names.add(name)
        if row["applies_to_target"] is not True:
            raise ApprovalPacketError("direct_requirement_target_result_invalid:" + name)
        if row["resolution_disposition"] != "INCLUDED_UNCONDITIONALLY" and row[
            "resolution_disposition"
        ] != "MARKER_EVALUATED_FOR_TARGET":
            raise ApprovalPacketError("direct_requirement_disposition_invalid:" + name)
        if not isinstance(row["worker_import_evidence_paths"], list) or not row[
            "worker_import_evidence_paths"
        ]:
            raise ApprovalPacketError("worker_import_evidence_missing:" + name)
        graph_evidence = row["worker_import_graph_evidence"]
        if not isinstance(graph_evidence, list) or (not graph_evidence and name != "email-validator"):
            raise ApprovalPacketError("worker_import_graph_evidence_missing:" + name)
        for graph_row in graph_evidence:
            _require_fields(
                graph_row,
                {"module", "path", "line", "deferred_to_runtime", "optional_guard"},
                "import_graph_evidence_fields_invalid",
            )
    if names != set(DIRECT_IMPORT_ROOTS):
        raise ApprovalPacketError("direct_requirement_set_invalid")

    if not isinstance(exclusions, list):
        raise ApprovalPacketError("exclusions_invalid")
    exclusion_names: set[str] = set()
    exclusion_fields = {
        "distribution_name",
        "source_declaration",
        "source_line",
        "exclusion_rationale",
        "worker_import_evidence_paths",
        "resolution_disposition",
        "replaced_by",
    }
    for row in exclusions:
        row = _require_fields(row, exclusion_fields, "exclusion_fields_invalid")
        name = row["distribution_name"]
        if not isinstance(name, str) or name in exclusion_names or name in names:
            raise ApprovalPacketError("excluded_package_duplicate")
        exclusion_names.add(name)
        if row["resolution_disposition"] != "EXCLUDED_FROM_WORKER_SOURCE":
            raise ApprovalPacketError("exclusion_disposition_invalid")

    origin = _require_fields(
        top["candidate_origin_policy"],
        {
            "candidate_index_url",
            "reviewed_origin",
            "operator_approved_origin",
            "operator_approval_status",
            "https_policy",
            "redirect_policy",
            "allowed_artifact_hosts",
            "hash_verification",
            "origin_recording",
            "artifact_provenance_limitations",
            "mirror_policy",
            "cache_policy",
            "dependency_confusion_policy",
        },
        "origin_policy_fields_invalid",
    )
    if origin["candidate_index_url"] != CANDIDATE_INDEX_URL:
        raise ApprovalPacketError("candidate_origin_invalid")
    reviewed = _require_fields(
        origin["reviewed_origin"],
        {"index_url", "index_host", "artifact_hosts", "review_state", "network_service_checked"},
        "reviewed_origin_fields_invalid",
    )
    if (
        reviewed["index_url"] != CANDIDATE_INDEX_URL
        or reviewed["index_host"] != "pypi.org"
        or reviewed["artifact_hosts"] != ARTIFACT_HOSTS
        or reviewed["review_state"] != "OFFLINE_POLICY_REVIEWED_CANDIDATE"
        or reviewed["network_service_checked"] is not False
        or origin["operator_approved_origin"] is not None
        or origin["operator_approval_status"] != "NOT_APPROVED"
    ):
        raise ApprovalPacketError("candidate_origin_approval_confusion")
    _require_fields(
        origin["redirect_policy"],
        {"index_request", "artifact_request", "record_all_hops"},
        "redirect_policy_fields_invalid",
    )
    if origin["allowed_artifact_hosts"] != ARTIFACT_HOSTS:
        raise ApprovalPacketError("artifact_host_policy_invalid")

    resolution = _require_fields(
        top["resolution_policy"],
        {
            "final_lock_versions",
            "artifact_identity",
            "dependency_graph",
            "marker_evaluation",
            "extras_closure",
            "wheel_tags",
            "artifact_type",
            "source_distributions",
            "source_builds",
            "missing_compatible_wheel",
            "automatic_version_substitution",
            "dependency_upgrades_during_assembly",
            "network_fallback",
            "resolver_provenance_required",
        },
        "resolution_policy_fields_invalid",
    )
    if (
        resolution["artifact_type"] != "WHEEL_ONLY"
        or resolution["source_distributions"] != "FORBIDDEN"
        or resolution["automatic_version_substitution"] != "FORBIDDEN"
        or resolution["dependency_upgrades_during_assembly"] != "FORBIDDEN"
        or resolution["network_fallback"] != "FORBIDDEN"
    ):
        raise ApprovalPacketError("resolution_policy_invalid")
    wheel_tags = _require_fields(
        resolution["wheel_tags"],
        {
            "generic_linux_other_architectures_other_python_abis",
            "interpreter_abi",
            "platform",
            "pure",
            "stable_abi",
        },
        "wheel_tag_policy_invalid",
    )
    if wheel_tags["interpreter_abi"] != ["cp312-cp312"] or wheel_tags["pure"] != ["py3-none-any"]:
        raise ApprovalPacketError("wheel_tag_policy_invalid")

    package_contract = _require_fields(
        top["package_review_contract"],
        {
            "selection_status",
            "selected_distributions",
            "required_fields_for_each_selected_distribution",
            "field_semantics",
            "current_package_reviews",
        },
        "package_review_contract_fields_invalid",
    )
    if (
        package_contract["selection_status"] != "NOT_STARTED_NO_RESOLVED_GRAPH"
        or package_contract["selected_distributions"] != []
        or package_contract["required_fields_for_each_selected_distribution"] != PACKAGE_REVIEW_FIELDS
    ):
        raise ApprovalPacketError("package_review_contract_invalid")
    field_semantics = _require_fields(
        package_contract["field_semantics"],
        {
            "license_metadata",
            "license_review_decision",
            "security_advisory_evidence",
            "security_review_decision",
            "maintainer_origin_evidence",
            "review_timestamp_utc",
            "reviewer_identity",
        },
        "package_review_semantics_invalid",
    )
    if not all(isinstance(value, str) and value for value in field_semantics.values()):
        raise ApprovalPacketError("package_review_semantics_invalid")

    native = _require_fields(
        top["native_review_policy"],
        {
            "state",
            "likely_or_possible_native_candidates",
            "required_evidence",
            "wheel_compatibility_policy",
            "elf_review_policy",
            "glibc_and_openssl_policy",
            "shared_library_provenance_policy",
            "actual_elf_closure_claim",
        },
        "native_review_policy_fields_invalid",
    )
    if native["required_evidence"] != NATIVE_REVIEW_FIELDS or native["actual_elf_closure_claim"] is not False:
        raise ApprovalPacketError("native_review_requirements_invalid")
    if not isinstance(native["likely_or_possible_native_candidates"], list):
        raise ApprovalPacketError("native_candidate_list_invalid")
    native_names: set[str] = set()
    for candidate in native["likely_or_possible_native_candidates"]:
        candidate = _require_fields(
            candidate,
            {"distribution_name", "basis"},
            "native_candidate_fields_invalid",
        )
        if not isinstance(candidate["distribution_name"], str) or candidate["distribution_name"] in native_names:
            raise ApprovalPacketError("native_candidate_duplicate")
        native_names.add(candidate["distribution_name"])

    decisions = top["unresolved_decisions"]
    if not isinstance(decisions, list):
        raise ApprovalPacketError("unresolved_decisions_invalid")
    decision_ids: set[str] = set()
    decision_fields = {"id", "status", "question", "evidence", "blocks", "recommended_next_step"}
    for decision in decisions:
        decision = _require_fields(decision, decision_fields, "unresolved_decision_fields_invalid")
        decision_id = decision["id"]
        if not isinstance(decision_id, str) or decision_id in decision_ids:
            raise ApprovalPacketError("unresolved_decision_duplicate")
        decision_ids.add(decision_id)
    if decision_ids != UNRESOLVED_DECISION_IDS:
        raise ApprovalPacketError("unresolved_decision_set_invalid")

    restrictions = _require_fields(
        top["acquisition_restrictions"],
        {
            "package_downloads",
            "package_installs",
            "package_builds",
            "dependency_upgrades",
            "production_wheelhouse",
            "runtime_assembly",
            "missing_or_incompatible_artifacts",
        },
        "acquisition_restrictions_fields_invalid",
    )
    if restrictions["package_downloads"] != "PROHIBITED_IN_WP2B2":
        raise ApprovalPacketError("acquisition_restrictions_invalid")

    authorization = _require_fields(
        top["operator_authorization"],
        {"status", "approval_id", "signed", "packet_is_authorization", "download_only_acquisition_authorized"},
        "operator_authorization_fields_invalid",
    )
    if authorization != {
        "status": "NOT_GRANTED",
        "approval_id": None,
        "signed": False,
        "packet_is_authorization": False,
        "download_only_acquisition_authorized": False,
    }:
        raise ApprovalPacketError("operator_authorization_must_remain_ungranted")

    if repository_root is not None:
        evidence = _source_inputs(Path(repository_root))
        for key in SOURCE_PATHS:
            if bindings[key]["sha256"] != evidence["actual_digests"][key]:
                raise ApprovalPacketError("packet_source_binding_mismatch:" + key)


def write_packet(repository_root: str | Path, output_path: str | Path) -> dict[str, Any]:
    packet = build_packet(repository_root)
    Path(output_path).write_bytes(canonical_json_bytes(packet))
    return packet


if __name__ == "__main__":
    repository = Path(__file__).resolve().parents[3]
    output = Path(__file__).with_name("runtime-resolution-approval-packet.json")
    write_packet(repository, output)
