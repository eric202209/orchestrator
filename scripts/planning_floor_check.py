#!/usr/bin/env python3
"""Run the minimal planner failure-floor check outside orchestration."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import SessionLocal
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.parsing import (
    extract_plan_steps,
    extract_structured_text,
)
from app.services.orchestration.validation.validator import ValidatorService


def _build_floor_prompt(task: str, project_dir: Path, prompt_profile: str) -> str:
    del project_dir
    prompt = f"""Return JSON array only. No markdown. No prose.

Task: {task}
Working directory: the current directory.

Return exactly one runnable step object with exactly these keys:
step_number, description, commands, verification, rollback, expected_files.

Rules:
1. step_number must be 1.
2. commands must be a non-empty JSON array of runnable shell strings.
3. verification must be one runnable shell string.
4. rollback must be null.
5. expected_files must be [].
6. Do not use cd, absolute paths, .., or ~.

Example shape:
[{{"step_number":1,"description":"Run the minimal command","commands":["echo hello world"],"verification":"python -c \\"print('hello world')\\"","rollback":null,"expected_files":[]}}]
"""
    return PlannerService.apply_prompt_profile(prompt, prompt_profile)


async def _run_command(cmd: list[str], *, cwd: Path | None) -> tuple[dict[str, Any], str, str]:
    started_at = time.monotonic()
    first_output_at: float | None = None
    last_output_at: float | None = None
    previous_output_at: float | None = None
    max_silent_gap: float | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd.resolve()) if cwd else None,
    )

    async def collect(stream: asyncio.StreamReader | None, chunks: list[str]) -> None:
        nonlocal first_output_at, last_output_at, previous_output_at, max_silent_gap
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            now = time.monotonic()
            if first_output_at is None:
                first_output_at = now
            if previous_output_at is not None:
                gap = now - previous_output_at
                max_silent_gap = gap if max_silent_gap is None else max(max_silent_gap, gap)
            previous_output_at = now
            last_output_at = now
            chunks.append(line.decode("utf-8", errors="replace").rstrip("\n"))

    await asyncio.gather(
        collect(process.stdout, stdout_chunks),
        collect(process.stderr, stderr_chunks),
    )
    return_code = await process.wait()
    duration = time.monotonic() - started_at

    stdout_text = "\n".join(stdout_chunks).strip()
    stderr_text = "\n".join(stderr_chunks).strip()
    diagnostics = {
        "duration_seconds": round(duration, 3),
        "first_output_after_seconds": (
            None if first_output_at is None else round(first_output_at - started_at, 3)
        ),
        "last_output_after_seconds": (
            None if last_output_at is None else round(last_output_at - started_at, 3)
        ),
        "max_silent_gap_seconds": (
            None if max_silent_gap is None else round(max_silent_gap, 3)
        ),
        "return_code": return_code,
        "stdout_chars": len(stdout_text),
        "stderr_chars": len(stderr_text),
        "stdout_lines": len([line for line in stdout_chunks if line]),
        "stderr_lines": len([line for line in stderr_chunks if line]),
    }
    diagnostics.update(OpenClawSessionService._channel_metadata(stdout_text, stderr_text))
    diagnostics["output_token_estimate"] = OpenClawSessionService._estimate_token_count(
        f"{stdout_text}\n{stderr_text}".strip()
    )
    return diagnostics, stdout_text, stderr_text


def _load_candidate_json(text: str) -> Any:
    structured = extract_structured_text(text)
    for candidate in (structured, text):
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _parse_plan(stdout_text: str, stderr_text: str) -> tuple[list[dict[str, Any]] | None, str]:
    candidates = [
        ("stdout", stdout_text),
        ("stderr_recovered", OpenClawSessionService._recover_json_like_output_from_stderr(stderr_text)),
        ("stderr", stderr_text),
        ("combined", f"{stdout_text}\n{stderr_text}".strip()),
    ]
    for source, text in candidates:
        parsed = _load_candidate_json(text)
        plan = extract_plan_steps(parsed)
        if plan is not None:
            return plan, source
    return None, "none"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Phase 6K minimal planner floor check without orchestration."
    )
    parser.add_argument("--task", default="echo hello world")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--source-brain", default="local")
    parser.add_argument("--session-prefix", default="planning-floor")
    parser.add_argument("--execution-profile", default="review_only")
    parser.add_argument("--prompt-profile", default="auto")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    db = SessionLocal()
    try:
        runtime = OpenClawSessionService(db, session_id=None, task_id=None, use_demo_mode=False)
        metadata = runtime.get_backend_metadata()
        prompt_profile = args.prompt_profile
        if prompt_profile == "auto":
            prompt_profile = PlannerService.select_prompt_profile(
                metadata.get("backend"),
                metadata.get("model_family"),
            )
        prompt = _build_floor_prompt(args.task, project_dir, prompt_profile)
        command = runtime.build_cli_agent_command(
            prompt,
            source_brain=args.source_brain,
            timeout_seconds=args.timeout,
            session_prefix=args.session_prefix,
        )
    finally:
        db.close()

    diagnostics, stdout_text, stderr_text = asyncio.run(_run_command(command, cwd=project_dir))
    plan, parse_source = _parse_plan(stdout_text, stderr_text)
    validation_payload: dict[str, Any]
    accepted = False
    if plan is None:
        validation_payload = {
            "accepted": False,
            "status": "parse_failed",
            "profile": None,
            "reasons": ["No valid runnable plan JSON could be parsed from stdout or stderr"],
            "details": {},
        }
    else:
        verdict = ValidatorService.validate_plan(
            plan,
            output_text=stdout_text or stderr_text,
            task_prompt=args.task,
            execution_profile=args.execution_profile,
            project_dir=project_dir,
        )
        accepted = bool(verdict.accepted)
        validation_payload = {
            "accepted": verdict.accepted,
            "status": verdict.status,
            "profile": verdict.verdict.profile,
            "reasons": verdict.reasons,
            "details": verdict.details,
        }

    payload = {
        "check": "planning_floor",
        "task": args.task,
        "project_dir": str(project_dir),
        "prompt_profile": prompt_profile,
        "backend_command": command[:2] + ["..."],
        "diagnostics": diagnostics,
        "parse_source": parse_source,
        "parsed_plan": plan,
        "validation": validation_payload,
        "raw_stdout": stdout_text,
        "raw_stderr": stderr_text,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
