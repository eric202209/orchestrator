#!/usr/bin/env python3
"""Validate an orchestration plan JSON blob from stdin or a file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from app.services.orchestration.validator import (
        ValidatorService as _ValidatorService,
    )
except Exception:
    _ValidatorService = None


_TRANSIENT_EXPECTED_FILE_PARTS = {
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
}


def _read_plan_text(plan_file: str | None) -> str:
    if plan_file:
        return Path(plan_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def _lite_infer_phase(step: dict) -> str | None:
    text = " ".join(
        [
            str(step.get("description") or ""),
            str(step.get("verification") or ""),
            str(step.get("rollback") or ""),
        ]
        + [str(command or "") for command in step.get("commands", []) or []]
        + [str(path or "") for path in step.get("expected_files", []) or []]
    ).lower()
    has_frontend_markers = any(
        marker in text
        for marker in ("frontend", "react", "vite", "npm install", "src/main.tsx")
    )
    has_backend_markers = any(
        marker in text
        for marker in (
            "backend",
            "fastapi",
            "requirements.txt",
            "pip install",
            "app/main.py",
            "pytest",
        )
    )
    if any(
        marker in text
        for marker in (
            "wire api config",
            "proxy",
            "cors",
            "vite.config",
            "api/client",
            "localhost:8080",
            "localhost:3000",
            ".env",
        )
    ):
        return "wire_api_config"
    if has_frontend_markers and not any(
        marker in text for marker in ("eslint", "vitest", "smoke check", "dev-ready")
    ):
        return "create_frontend_skeleton"
    if has_backend_markers and not any(
        marker in text
        for marker in ("smoke check", "dev-ready", "health", "routes", "cors")
    ):
        return "create_backend_skeleton"
    if any(
        marker in text
        for marker in (
            "health",
            "smoke check",
            "dev-ready",
            "routes",
            "eslint",
            "vitest",
            "type-check",
            "build",
            "tsc --noemit",
        )
    ):
        return "verify_dev_startup"
    if has_frontend_markers:
        return "create_frontend_skeleton"
    if has_backend_markers:
        return "create_backend_skeleton"
    return None


def _coerce_optional_command_field(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        if not parts:
            return None
        return " && ".join(parts)
    rendered = str(value).strip()
    return rendered or None


def _normalize_lite_step(step: dict) -> dict:
    normalized = dict(step)
    verification = _coerce_optional_command_field(step.get("verification"))
    rollback = _coerce_optional_command_field(step.get("rollback"))
    normalized["verification"] = verification
    normalized["rollback"] = rollback

    expected_files: list[str] = []
    for raw_path in step.get("expected_files", []) or []:
        rendered = str(raw_path or "").strip()
        if not rendered:
            continue
        if any(part in _TRANSIENT_EXPECTED_FILE_PARTS for part in Path(rendered).parts):
            continue
        expected_files.append(rendered)
    normalized["expected_files"] = expected_files
    return normalized


def _lite_validate(
    plan: object,
    *,
    workflow_profile: str,
) -> dict:
    reasons: list[str] = []
    details: dict = {}

    if not isinstance(plan, list):
        return {
            "accepted": False,
            "status": "rejected",
            "profile": "implementation",
            "reasons": ["Plan payload must be a list of step objects"],
            "details": {"received_type": type(plan).__name__},
        }

    phase_order = {
        "create_frontend_skeleton": 0,
        "create_backend_skeleton": 1,
        "wire_api_config": 2,
        "verify_dev_startup": 3,
    }
    last_phase = -1
    violating_steps: list[int] = []

    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            reasons.append(f"Step {index} is not an object")
            continue
        step = _normalize_lite_step(step)
        commands = step.get("commands")
        if not isinstance(commands, list) or any(
            not isinstance(command, str) for command in commands
        ):
            reasons.append(f"Step {index} commands must be an array of strings")
        verification = step.get("verification")
        if verification is not None and not isinstance(verification, str):
            reasons.append(f"Step {index} verification must be a string or null")
        rollback = step.get("rollback")
        if rollback is not None and not isinstance(rollback, str):
            reasons.append(f"Step {index} rollback must be a string or null")

        command_texts = [str(command or "") for command in commands or []]
        all_text = "\n".join(
            command_texts
            + [str(verification or ""), str(rollback or "")]
            + [str(path or "") for path in step.get("expected_files", []) or []]
        )
        if re.search(r"(?<![\w./-])\.\.(?:/[A-Za-z0-9._@:+-]+)+(?:/)?", all_text):
            reasons.append(f"Step {index} uses parent-directory traversal")
        if any(token in all_text for token in ("nohup", "disown")) or re.search(
            r"(^|[\s])&($|[\s])", all_text
        ):
            reasons.append(f"Step {index} uses background-process commands")

        if workflow_profile == "fullstack_scaffold":
            phase = _lite_infer_phase(step)
            if phase:
                position = phase_order[phase]
                if position < last_phase:
                    violating_steps.append(int(step.get("step_number", index)))
                else:
                    last_phase = position

    if violating_steps:
        reasons.append(
            f"Plan violates required workflow phase order for {workflow_profile} (steps: {violating_steps[:5]})"
        )
        details["workflow_phase_violations"] = violating_steps

    details["normalized_plan"] = [
        _normalize_lite_step(step) if isinstance(step, dict) else step for step in plan
    ]

    accepted = not reasons
    return {
        "accepted": accepted,
        "status": "accepted" if accepted else "rejected",
        "profile": "implementation",
        "reasons": reasons,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate orchestration plan JSON against local validator rules."
    )
    parser.add_argument(
        "plan_file",
        nargs="?",
        help="Optional path to a JSON file. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--task-prompt",
        default="Set up frontend and backend in one workspace",
        help="Task prompt used for validation heuristics.",
    )
    parser.add_argument(
        "--execution-profile",
        default="full_lifecycle",
        help="Execution profile for validation heuristics.",
    )
    parser.add_argument(
        "--workflow-profile",
        default="fullstack_scaffold",
        help="Workflow profile for phase-order validation.",
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Workspace path used for file-existence checks.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional task title.",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional task description.",
    )
    args = parser.parse_args()

    raw_text = _read_plan_text(args.plan_file).strip()
    if not raw_text:
        print("No JSON input received.", file=sys.stderr)
        return 2

    try:
        plan = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        return 2

    if _ValidatorService is not None:
        verdict = _ValidatorService.validate_plan(
            plan,
            output_text=raw_text,
            task_prompt=args.task_prompt,
            execution_profile=args.execution_profile,
            project_dir=Path(args.project_dir).resolve(),
            title=args.title,
            description=args.description,
            workflow_profile=args.workflow_profile,
        )
        payload = {
            "accepted": verdict.accepted,
            "status": verdict.status,
            "profile": verdict.profile,
            "reasons": verdict.reasons,
            "details": verdict.details,
            "validator_mode": "full",
        }
        print(json.dumps(payload, indent=2))
        return 0 if verdict.accepted else 1

    payload = _lite_validate(plan, workflow_profile=args.workflow_profile)
    payload["validator_mode"] = "lite"
    print(json.dumps(payload, indent=2))
    return 0 if payload["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
