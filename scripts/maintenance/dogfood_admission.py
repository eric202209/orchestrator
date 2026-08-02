#!/usr/bin/env python3
"""Fail-closed deployment admission gate for the native dogfood stack."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _request_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _runtime_processes() -> dict[str, list[tuple[int, str]]]:
    processes: dict[str, list[tuple[int, str]]] = {
        "backend": [],
        "worker": [],
        "beat": [],
    }
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if "uvicorn app.main:app" in command:
            processes["backend"].append((int(proc.name), command))
        elif "celery" in command and "-A app.celery_app" in command:
            if " beat " in f" {command} ":
                processes["beat"].append((int(proc.name), command))
            elif " worker " in f" {command} ":
                processes["worker"].append((int(proc.name), command))
    return processes


def _process_environment(pid: int) -> dict[str, str]:
    entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    environment: dict[str, str] = {}
    for entry in entries:
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        environment[key.decode()] = value.decode(errors="replace")
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify every required native dogfood deployment condition."
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-build-time", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8080"),
    )
    args = parser.parse_args()

    failures: list[str] = []
    passes: list[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        message = f"{label}{': ' + detail if detail else ''}"
        (passes if condition else failures).append(message)

    try:
        from app.auth import create_access_token

        token = create_access_token(data={"sub": "eval@local.dev"})
        auth = _request_json(f"{args.base_url}/api/v1/auth/me", token)
        check(
            auth.get("email") == "eval@local.dev",
            "authentication",
            str(auth.get("email") or "unexpected identity"),
        )

        public_health = _request_json(f"{args.base_url}/health")
        checks = public_health.get("checks", {})
        check(
            public_health.get("status") == "healthy"
            and checks.get("database") == "ok"
            and checks.get("redis") == "ok"
            and checks.get("backend") == "ok",
            "backend health",
            str(public_health.get("status")),
        )

        health = _request_json(f"{args.base_url}/api/v1/ops/health", token)
        components = health.get("components", {})
        for component in ("database", "redis", "qdrant", "celery"):
            status = (components.get(component) or {}).get("status")
            check(status == "ok", f"{component} health", str(status))
        maintenance = components.get("maintenance") or {}
        beat = maintenance.get("beat") or {}
        check(
            beat.get("configuration_status") == "CONFIGURED",
            "Beat schedule",
            str(beat.get("configuration_status")),
        )

        identity = _request_json(f"{args.base_url}/api/v1/ops/build-identity", token)
        identity_checks = {
            "build SHA": (identity.get("build_git_sha"), args.expected_sha),
            "repository SHA": (identity.get("repo_git_sha"), args.expected_sha),
            "build timestamp": (
                identity.get("build_time"),
                args.expected_build_time,
            ),
            "configuration identity": (
                identity.get("configuration_sha256"),
                args.expected_config_sha256,
            ),
            "migration identity": (identity.get("migration_status"), "ok"),
            "stale-process identity": (
                identity.get("stale_container_check"),
                "ok",
            ),
        }
        for label, (actual, expected) in identity_checks.items():
            check(actual == expected, label, str(actual))

        role_matrix = identity.get("provider_role_matrix") or {}
        for role in ("planning", "execution", "repair", "debug_repair"):
            role_readiness = role_matrix.get(role) or {}
            check(
                role_readiness.get("provider_ready", role_readiness.get("ready"))
                is True,
                f"provider role readiness ({role})",
                str(
                    role_readiness.get("error_code")
                    or role_readiness.get("error")
                    or role_readiness.get("backend")
                ),
            )

        lanes = identity.get("active_backend_lanes") or {}
        configured_backends = {value for value in lanes.values() if value}
        check(bool(lanes.get("execution")), "configured execution lane")

        backend_health = _request_json(
            f"{args.base_url}/api/v1/ops/backends/health", token
        )
        check(
            (backend_health.get("runtime_lane") or {}).get("verdict") == "ok",
            "runtime lane",
            str((backend_health.get("runtime_lane") or {}).get("verdict")),
        )
        descriptors = {
            backend.get("name"): backend
            for backend in backend_health.get("backends", [])
        }
        for backend_name in sorted(configured_backends):
            descriptor = descriptors.get(backend_name) or {}
            check(
                descriptor.get("available") is True and descriptor.get("ready") is True,
                f"provider availability ({backend_name})",
                str(descriptor.get("status") or "not registered"),
            )

        # Phase 22B-1X1: a reachable provider is not usable capacity. Admission
        # observes reconciled slot ownership for the execution role and fails
        # closed on ambiguity, so a deployment can never pass while every
        # execution slot is held by a lease whose owner is gone.
        capacity = _request_json(f"{args.base_url}/api/v1/ops/backends/capacity", token)
        execution_capacity = (capacity.get("roles") or {}).get("execution") or {}
        check(
            capacity.get("redis_available") is True,
            "slot reconciliation reachable",
            str(capacity.get("status_code") or capacity.get("error")),
        )
        check(
            bool(execution_capacity),
            "execution capacity evidence present",
            str(capacity.get("execution_backend") or "no execution role configured"),
        )
        check(
            execution_capacity.get("capacity_available") is True,
            "execution backend capacity",
            "backend={} status={} max={} valid={} stale_reconciled={} available={}".format(
                execution_capacity.get("backend_id"),
                execution_capacity.get("status_code"),
                execution_capacity.get("max_slots"),
                execution_capacity.get("active_valid_count"),
                execution_capacity.get("stale_reconciled_count"),
                execution_capacity.get("available_count"),
            ),
        )

        processes = _runtime_processes()
        check(len(processes["backend"]) == 1, "backend singleton")
        check(bool(processes["worker"]), "worker process presence")
        check(len(processes["beat"]) == 1, "Beat singleton")
        for role in ("backend", "worker", "beat"):
            for pid, _command in processes[role]:
                environment = _process_environment(pid)
                check(
                    environment.get("ORCHESTRATOR_GIT_SHA") == args.expected_sha,
                    f"{role} PID {pid} SHA",
                )
                check(
                    environment.get("ORCHESTRATOR_BUILD_TIME")
                    == args.expected_build_time,
                    f"{role} PID {pid} build timestamp",
                )
                check(
                    environment.get("ORCHESTRATOR_CONFIG_SHA256")
                    == args.expected_config_sha256,
                    f"{role} PID {pid} configuration identity",
                )
    except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
        failures.append(f"admission probe error: {exc}")

    for item in passes:
        print(f"PASS: {item}")
    for item in failures:
        print(f"FAIL: {item}", file=sys.stderr)
    if failures:
        print(
            f"DEPLOYMENT_ADMISSION_FAILED ({len(failures)} failures)", file=sys.stderr
        )
        return 1
    print("DEPLOYMENT_ADMISSION_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
