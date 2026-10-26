"""Benchmark CLI.

    venv/bin/python scripts/benchmark/cli.py evaluate-history \
        --evidence-root docs/roadmap/reports/evidence \
        --output .dev-runtime/benchmark/history-results.json
    venv/bin/python scripts/benchmark/cli.py validate-cases
    venv/bin/python scripts/benchmark/cli.py run --case workflow-a \
        --profile isolated-development --max-attempts 2 --dry-run

Live campaign (Phase 40D-2), against a running isolated development runtime:

    venv/bin/python scripts/benchmark/cli.py campaign-init \
        --campaign phase40d2-c1 --cases workflow-a,workflow-b,workflow-c
    venv/bin/python scripts/benchmark/cli.py run --case workflow-a \
        --profile isolated-development --max-attempts 2 --live --campaign phase40d2-c1
    venv/bin/python scripts/benchmark/cli.py campaign-metrics --campaign phase40d2-c1
    venv/bin/python scripts/benchmark/cli.py campaign-close --campaign phase40d2-c1 --stop-runtime
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import socket
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark import live, runner  # noqa: E402
from scripts.benchmark.evidence_loader import (  # noqa: E402
    CASES_DIR,
    REGISTRY_PATH,
    EvidenceRoots,
    HistoryLoader,
    load_cases,
    load_registry,
)
from scripts.benchmark.metrics import grouped_metrics  # noqa: E402
from scripts.benchmark.schema import validate_case  # noqa: E402

RESULT_SCHEMA_VERSION = "benchmark.history_result.v1"
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 4
EXIT_LIVE_STOPPED = 5
LIVE_OUTPUT_ROOT = REPO_ROOT / "docs/roadmap/reports/evidence/phase40d2"


class NetworkDisabledError(RuntimeError):
    pass


@contextlib.contextmanager
def network_disabled() -> Iterator[None]:
    """Fail any socket connection attempted during offline evaluation."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise NetworkDisabledError(
            "network access is disabled during offline historical evaluation"
        )

    original_connect = socket.socket.connect
    original_create = socket.create_connection
    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create  # type: ignore[assignment]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_history(
    evidence_root: Path, runtime_root: Path, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    registry = load_registry()
    cases = load_cases()
    with network_disabled():
        result = HistoryLoader(
            EvidenceRoots(repo_root, evidence_root, runtime_root), registry, cases
        ).load()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "inputs": {
            "registry": {
                "path": str(REGISTRY_PATH.relative_to(repo_root)),
                "sha256": _sha256(REGISTRY_PATH),
            },
            "cases": {
                path.name: _sha256(path) for path in sorted(CASES_DIR.glob("*.json"))
            },
            "evidence_root": str(evidence_root),
            "runtime_root": str(runtime_root),
        },
        "case_validation": result.case_errors,
        "cases": cases,
        "attempts": result.attempts,
        "metrics": grouped_metrics(result.attempts),
        "inventory": result.inventory,
        "conflicts": result.conflicts,
    }


def _protected_output(output: Path, repo_root: Path) -> str | None:
    return live.protected_output_problem(output, repo_root)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 40D autonomous engineering benchmark"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    history = commands.add_parser(
        "evaluate-history", help="offline, read-only historical evaluation"
    )
    history.add_argument(
        "--evidence-root",
        type=Path,
        default=REPO_ROOT / "docs/roadmap/reports/evidence",
    )
    history.add_argument(
        "--runtime-root", type=Path, default=REPO_ROOT / ".dev-runtime/phase40b-r1"
    )
    history.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".dev-runtime/benchmark/history-results.json",
    )
    history.add_argument("--overwrite", action="store_true")

    commands.add_parser("validate-cases", help="validate canonical case definitions")

    run = commands.add_parser("run", help="controlled benchmark run")
    run.add_argument("--case", required=True)
    run.add_argument("--profile", default="isolated-development")
    run.add_argument("--max-attempts", type=int, default=2)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    run.add_argument("--campaign", help="live: campaign created by campaign-init")
    run.add_argument(
        "--resume-campaign",
        help="live: repeat the campaign id to confirm resuming a case with existing attempts",
    )
    run.add_argument("--output-root", type=Path, default=LIVE_OUTPUT_ROOT)

    init = commands.add_parser(
        "campaign-init",
        help="pin source, runtime, model, and cases for a live campaign",
    )
    init.add_argument("--campaign", required=True)
    init.add_argument("--cases", required=True, help="comma-separated case ids")
    init.add_argument("--max-attempts", type=int, default=2)
    init.add_argument("--output-root", type=Path, default=LIVE_OUTPUT_ROOT)

    for name, text in (
        ("campaign-metrics", "deterministic metrics over a campaign's attempt records"),
        ("campaign-close", "record cleanup; optionally stop owned runtime processes"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--campaign", required=True)
        sub.add_argument("--output-root", type=Path, default=LIVE_OUTPUT_ROOT)
        if name == "campaign-close":
            sub.add_argument("--stop-runtime", action="store_true")

    args = parser.parse_args(argv)

    if args.command in {"campaign-init", "campaign-metrics", "campaign-close"}:
        ctx = live.LiveContext(
            repo_root=REPO_ROOT, output_root=args.output_root, cases=load_cases()
        )
        if args.command == "campaign-init":
            outcome = live.init_campaign(
                ctx, args.campaign, args.cases.split(","), args.max_attempts
            )
            code = (
                EXIT_OK
                if outcome["status"] == live.STATUS_CAMPAIGN_CREATED
                else EXIT_INVALID
            )
        elif args.command == "campaign-metrics":
            outcome = live.campaign_metrics(ctx, args.campaign)
            _write_json(ctx.campaign_dir(args.campaign) / "metrics.json", outcome)
            code = EXIT_OK
        else:
            outcome = live.close_campaign(
                ctx, args.campaign, stop_runtime=args.stop_runtime
            )
            code = EXIT_OK
        print(json.dumps(outcome, indent=2, sort_keys=True, default=str))
        return code

    if args.command == "validate-cases":
        errors = {
            case_id: validate_case(case)
            for case_id, case in sorted(load_cases().items())
        }
        print(json.dumps(errors, indent=2, sort_keys=True))
        return EXIT_OK if not any(errors.values()) else EXIT_INVALID

    if args.command == "evaluate-history":
        problem = _protected_output(args.output, REPO_ROOT)
        if problem:
            print(problem, file=sys.stderr)
            return EXIT_INVALID
        if args.output.exists() and not args.overwrite:
            print(f"output exists: {args.output} (pass --overwrite)", file=sys.stderr)
            return EXIT_INVALID
        result = evaluate_history(
            args.evidence_root.resolve(), args.runtime_root.resolve()
        )
        _write_json(args.output, result)
        overview = result["metrics"]["cross_phase_overview"]["metrics"]
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "attempted": overview["slots"]["attempted"],
                    "not_attempted": overview["slots"]["not_attempted"],
                    "independently_accepted": sum(
                        1
                        for attempt in result["attempts"]
                        if attempt["reliability"]["independently_accepted"] is True
                    ),
                    "conflicts": len(result["conflicts"]),
                    "case_validation_errors": sum(
                        len(errors) for errors in result["case_validation"].values()
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK

    config = runner.build_run_config(args.case, args.max_attempts, args.live)
    config["profile"] = args.profile
    outcome = (
        runner.run_live(
            config,
            repo_root=REPO_ROOT,
            campaign_id=args.campaign,
            output_root=args.output_root,
            resume_campaign=args.resume_campaign,
        )
        if args.live
        else runner.dry_run(config, repo_root=REPO_ROOT)
    )
    print(json.dumps(outcome, indent=2, sort_keys=True, default=str))
    return {
        runner.STATUS_DRY_RUN_PREPARED: EXIT_OK,
        runner.STATUS_DRY_RUN_BLOCKED: EXIT_BLOCKED,
        live.STATUS_CASE_COMPLETE: EXIT_OK,
        live.STATUS_BUDGET_EXHAUSTED: EXIT_OK,
        live.STATUS_PREFLIGHT_FAILED: EXIT_LIVE_STOPPED,
        live.STATUS_CAMPAIGN_TERMINATED: EXIT_LIVE_STOPPED,
        live.STATUS_RESUME_CONFIRMATION_REQUIRED: EXIT_LIVE_STOPPED,
        live.STATUS_BUSY: EXIT_LIVE_STOPPED,
    }.get(outcome["status"], EXIT_INVALID)


if __name__ == "__main__":
    raise SystemExit(main())
