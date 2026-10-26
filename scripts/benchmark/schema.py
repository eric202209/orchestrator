"""Versioned JSON schemas for benchmark cases, attempts, and run configs."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

CASE_SCHEMA_VERSION = "benchmark.case.v1"
ATTEMPT_SCHEMA_VERSION = "benchmark.attempt.v1"
RUN_CONFIG_SCHEMA_VERSION = "benchmark.run_config.v1"
REGISTRY_SCHEMA_VERSION = "benchmark.history_registry.v1"

_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

CASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "case_id",
        "title",
        "status",
        "incomplete_reasons",
        "original_task",
        "seed",
        "baseline",
        "acceptance",
        "authorized_source_paths",
        "protected_test_paths",
        "behavioral_contract",
        "historical_evidence_refs",
    ],
    "properties": {
        "schema_version": {"const": CASE_SCHEMA_VERSION},
        "case_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "title": {"type": "string", "minLength": 1},
        "status": {"enum": ["complete", "incomplete"]},
        "incomplete_reasons": _STR_LIST,
        "original_task": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "description", "origin_task_id", "evidence_ref"],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string", "minLength": 1},
                "origin_task_id": {"type": "integer"},
                "evidence_ref": _NULLABLE_STR,
            },
        },
        "seed": {
            "type": "object",
            "additionalProperties": False,
            "required": ["repository", "commit", "tree", "evidence_ref"],
            "properties": {
                "repository": {"type": "string"},
                "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "tree": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "evidence_ref": _NULLABLE_STR,
            },
        },
        "baseline": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "command",
                "expected_failed_tests",
                "expected_failed",
                "expected_passed",
                "evidence_ref",
            ],
            "properties": {
                "command": {"type": "string"},
                "expected_failed_tests": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "expected_failed": _NULLABLE_INT,
                "expected_passed": _NULLABLE_INT,
                "evidence_ref": _NULLABLE_STR,
            },
        },
        "acceptance": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "requires_unchanged_tests"],
            "properties": {
                "command": {"type": "string"},
                "requires_unchanged_tests": {"type": "boolean"},
            },
        },
        "authorized_source_paths": {**_STR_LIST, "minItems": 1},
        "protected_test_paths": {**_STR_LIST, "minItems": 1},
        "behavioral_contract": {"type": "string", "minLength": 1},
        "historical_evidence_refs": _STR_LIST,
    },
}

_ATTEMPT_SECTIONS = {
    "identity": {
        "type": "object",
        "required": [
            "case_id",
            "attempt_id",
            "phase",
            "slot",
            "project_id",
            "task_id",
            "task_execution_id",
            "session_id",
            "source_identity",
            "runtime_identity",
            "provider_identity",
        ],
        "properties": {
            "case_id": {"type": "string"},
            "attempt_id": {"type": "string"},
            "phase": {"type": "string"},
            "slot": {"type": "string"},
            "project_id": _NULLABLE_INT,
            "task_id": _NULLABLE_INT,
            "task_execution_id": _NULLABLE_INT,
            "session_id": _NULLABLE_INT,
            "source_identity": {"type": "object"},
            "runtime_identity": {"type": "object"},
            "provider_identity": {"type": "object"},
        },
    },
    "planning": {
        "type": "object",
        "required": [
            "reached",
            "discovery_status",
            "grounding_status",
            "provider_response_status",
            "json_parse_result",
            "schema_validation_result",
            "plan_validation_result",
            "repair_count",
            "outcome",
        ],
    },
    "execution": {
        "type": "object",
        "required": [
            "reached",
            "steps_executed",
            "source_mutation_captured",
            "debug_feedback_count",
            "debug_repair_count",
            "debug_repair_rejections",
            "verification_result",
            "task_status",
            "task_execution_status",
        ],
    },
    "change_set": {
        "type": "object",
        "required": [
            "exists",
            "identity",
            "changed_paths",
            "retention_policy",
            "promoted",
        ],
    },
    "acceptance": {
        "type": "object",
        "required": [
            "attempted",
            "result",
            "command",
            "passed",
            "failed",
            "errors",
            "tests_modified",
            "unauthorized_source_mutation",
            "evidence_ref",
        ],
    },
    "reliability": {
        "type": "object",
        "required": [
            "failure_stage",
            "failure_category_raw",
            "error_message_raw",
            "observed_errors",
            "safety_rejections",
            "terminal_safety_rejections",
            "verification_failure",
            "attribution",
            "execution_authority_violation",
            "retry_count",
            "recovery_count",
            "duration_seconds",
            "independently_accepted",
            "evidence_completeness",
            "evidence_limitations",
            "conflicts",
            "evidence_refs",
        ],
    },
}

ATTEMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "attempted",
        "not_attempted_reason",
        *_ATTEMPT_SECTIONS,
    ],
    "properties": {
        "schema_version": {"const": ATTEMPT_SCHEMA_VERSION},
        "attempted": {"type": "boolean"},
        "not_attempted_reason": _NULLABLE_STR,
        **_ATTEMPT_SECTIONS,
    },
}

RUN_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "case_id", "profile", "max_attempts", "mode"],
    "properties": {
        "schema_version": {"const": RUN_CONFIG_SCHEMA_VERSION},
        "case_id": {"type": "string"},
        "profile": {"const": "isolated-development"},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 2},
        "mode": {"enum": ["dry_run", "live"]},
    },
}


def _errors(schema: dict[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(value)
    )


def validate_case(value: Any) -> list[str]:
    return _errors(CASE_SCHEMA, value)


def validate_attempt(value: Any) -> list[str]:
    return _errors(ATTEMPT_SCHEMA, value)


def validate_run_config(value: Any) -> list[str]:
    return _errors(RUN_CONFIG_SCHEMA, value)
