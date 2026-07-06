# Orchestrator Eval Manifests

This folder holds lightweight orchestrator evaluation manifests.

2026-07: removed 18 one-off completed eval runners (`run_e*_post_e*_validation.py`,
`run_arm_b_*.py`, `analyze_phase12i_debug_budget.py`,
`run_recovery_opportunity_analysis.py`, `run_recovery_productionization_report.py`,
`run_recovery_validation_s4.py`) — each was a dated, slice-specific historical
run with no `app/tests/` reference and no dependency from any remaining
script; their findings remain in `docs/roadmap/done/` and
`docs/roadmap/reports/`. `run_orchestrator_eval_slice.py`,
`check_phase12c_gates.py`, and `analyze_phase12e_debug_repair_failures.py`
remain — all three are imported by `app/tests/`.

The v1 manifest is intentionally not a runner. It defines deterministic cases
for backend resilience and controlled eval expansion:

- `python_cli_small_feature`
- `medium_cli_multi_file_feature`
- `debug_import_error_repair`
- `checkpoint_resume_mid_task`
- `stale_replace_repair`
- `missing_report_artifact`
- `fake_verification_artifact_guard`

## Running a Case

Launch benchmark tasks through the normal production queue path. The supported
manual procedure is:

1. Copy the fixture into a clean project workspace.
2. Create the `Project`, `Session`, `Task`, and `SessionTask` database rows.
3. Call `app.services.session.session_runtime_service.queue_task_for_session(...)`
   or use the backend API endpoint that queues a task.
4. Score the completed workspace with `scripts/maintenance/score_orchestrator_eval_case.py`.

Do not call `execute_orchestration_task.run(...)` directly for eval runs. Direct
worker invocation bypasses queue setup, including `mark_session_running(...)`.
If a direct worker harness is used for a non-eval diagnostic, it must first set
`session.status = "running"` and `session.is_active = true`; otherwise the
execution loop will correctly stop at the first step boundary with
`cancelled/session_pending`.

Score an already-run task with:

```bash
python3 scripts/maintenance/score_orchestrator_eval_case.py \
  --manifest scripts/evals/orchestrator-eval-v1-manifest.json \
  --case-id debug_import_error_repair \
  --project-dir /path/to/project \
  --session-id 123 \
  --task-id 456 \
  --python venv/bin/python \
  --output docs/roadmap/reports/evals/example-report.json
```

The scorer reads existing `.agent/events/*.jsonl` files, state snapshot
JSONL, workspace files, and verifier command output. It does not submit tasks
or modify orchestration behavior. Verifier commands that start with `python` or
`python3` are run with the active scorer interpreter by default; pass `--python`
or `--venv-python` to choose a specific interpreter.

## Path Observability

Scorer reports include a `path_observability` section derived only from the
event journal and state snapshots. Use it to distinguish a failed eval that
missed the intended orchestration path from one that reached the path and failed
inside it.

Key fields:

- `planning_reached`: planning phase evidence was observed.
- `execution_reached`: execution phase, step, or execution-status evidence was
  observed.
- `step_started_count`: number of `step_started` events.
- `debug_repair_reached`: debug feedback or repair events were observed.
- `bounded_execution_debug_repair_used`: bounded execution debug repair
  metadata was observed. This is the preferred architecture-named field.
- `phase7f_used`: compatibility alias for historical reports.
- `diff_scoped_debug_repair_used`: diff-scoped debug repair metadata was
  observed. This is the preferred architecture-named field.
- `phase7g_used`: compatibility alias for historical reports.
- `repair_rejected_count`: number of rejected repair events.
- `checkpoint_loaded`: checkpoint-load evidence was observed.
- `intended_path_observed`: case-aware path check. For baseline implementation
  cases this means execution was reached; for debug-repair cases this means
  debug repair was reached; for checkpoint cases this means checkpoint load was
  observed.
- `primary_failure_phase`: best-effort failure classification such as
  `planning_validation`, `execution`, `debug_repair`, `checkpoint_resume`,
  `verifier`, or `unknown`.

For example, if a debug repair prompt changes but `python_cli_small_feature`
reports `execution_reached=false` and `primary_failure_phase=planning_validation`,
that run did not exercise the debug repair path.

## Minimal API Runner

`scripts/evals/run_orchestrator_eval_slice.py` runs one of the existing first
slice cases through the normal HTTP API queue path, then invokes the scorer:

```bash
ORCHESTRATOR_API_TOKEN=<token> \
venv/bin/python scripts/evals/run_orchestrator_eval_slice.py \
  --case-id python_cli_small_feature \
  --api-base-url http://127.0.0.1:8080/api/v1
```

Run the API runner from the repo virtualenv when available. The runner passes
the same virtualenv Python to `scripts/maintenance/score_orchestrator_eval_case.py`, so
manifest verifier commands like `python3 -m pytest -q` run with the interpreter
that has the eval dependencies installed. If `./venv/bin/python` exists, it is
also the runner's default `--python/--venv-python` value; otherwise the default
is the interpreter used to launch the runner.

Supported case IDs are only:

- `python_cli_small_feature`
- `medium_cli_multi_file_feature`
- `debug_import_error_repair`
- `checkpoint_resume_mid_task`
- `stale_replace_repair`
- `missing_report_artifact`
- `fake_verification_artifact_guard`

The runner copies `scripts/evals/fixtures/<case-id>` into a fresh workspace
under `/home/eric/projects`, creates project/session/task records through the
API, queues the task via `/sessions/{session_id}/tasks/{task_id}/run`, waits for
a terminal session state, and writes the JSON score report under
`docs/roadmap/reports/evals/`.

Run the same case repeatedly when single-run evidence is too noisy:

```bash
ORCHESTRATOR_API_TOKEN=<token> \
venv/bin/python scripts/evals/run_orchestrator_eval_slice.py \
  --case-id python_cli_small_feature \
  --api-base-url http://127.0.0.1:8080/api/v1 \
  --repeat 3
```

`--repeat` defaults to `1`. When repeated runs are requested, each individual
run still gets a normal scorer report, and the runner also writes one aggregate
report per case under `docs/roadmap/reports/evals/`.

Aggregate reports include:

- run context: `git_sha`, `model`, `backend`, `runtime_profile`, and
  `repeat_seed` when available
- `clean_success_count` and `clean_success_rate`
- `primary_failure_phase_distribution`
- `stable_primary_failure_phase`, true when at least 80% of runs share the same
  primary failure phase
- `path_observed_count` and `intended_path_observed_count`
- execution and debug-repair reached counts/rates
- debug-repair usage counts/rates, emitted with architecture names:
  - `bounded_execution_debug_repair_used_count`
  - `diff_scoped_debug_repair_used_count`
  - `bounded_execution_debug_repair_exercised_rate`
  - `diff_scoped_debug_repair_exercised_rate`

Historical aggregate reports may contain old compatibility keys such as
`phase7f_used_count`, `phase7g_used_count`, `phase7f_exercised_rate`, and
`phase7g_exercised_rate`. New aggregate reports no longer write those old
aggregate keys; old per-run report fields remain readable as input fallback.
- `most_common_blocker`
- `score_readiness_summary`, including terminal-event observation,
  event-journal stabilization, and journal paths used for scoring readiness
- individual `run_report_paths`

Use repeated runs to decide whether the next fix is a stable path bug or model
variance. Do not add new benchmark cases or medium/large project runs for this
first-slice stability loop.

## Phase 12C Gate Simulation

`scripts/evals/check_phase12c_gates.py` evaluates aggregate reports against the
Phase 12C-A simulation policy in `scripts/evals/phase12c-gate-policy.json`.
This command does not launch sessions, call model APIs, or make CI fail when a
simulated gate misses its threshold.

Example:

```bash
venv/bin/python scripts/evals/check_phase12c_gates.py \
  --summary-output docs/roadmap/reports/evals/phase12c-gate-simulation-summary.json \
  docs/roadmap/reports/evals/orchestrator-eval-v1-debug-import-error-repair-queue-20260601-010627-aggregate.json \
  docs/roadmap/reports/evals/orchestrator-eval-v1-missing-report-artifact-queue-20260601-005610-aggregate.json \
  docs/roadmap/reports/evals/orchestrator-eval-v1-fake-verification-artifact-guard-queue-20260601-010941-aggregate.json \
  docs/roadmap/reports/evals/orchestrator-eval-v1-stale-replace-repair-queue-20260601-005610-aggregate.json
```

The summary separates `simulated_hard_gate`, `simulated_negative_gate`, and
`diagnostic_only` cases. Phase 12C-A emits `would_pass` and `would_fail`, but
threshold misses remain advisory. Parse or schema errors still exit non-zero
because CI must be able to trust the generated artifact.

Phase 12C-B stability fields are included in the same summary. Use
`--min-evidence-sets` to control how many aggregate reports per case are needed
before a repeated pass is reported as an optional warning candidate:

```bash
venv/bin/python scripts/evals/check_phase12c_gates.py \
  --min-evidence-sets 3 \
  --summary-output docs/roadmap/reports/evals/phase12c-gate-simulation-summary.json \
  <aggregate-report> ...
```

The stability section remains warning-only. It can report
`optional_warning_candidate=true` or `flaky=true`, but it always keeps
`promotion_ready=false` and `blocking=false`. Real blocking gates require a
separate Phase 12C-C promotion decision.

For task creation, the runner prefers fixture-specific prompt text over the
manifest's generic `operator_prompt`. It first checks
`scripts/evals/fixtures/<case-id>/task_prompt.txt`, then `prompt.txt`, then the
first fenced block after `Suggested task prompt:` in the fixture `README.md`.
If none of those exist, it falls back to the manifest prompt. Inspect selected
prompts without creating API records with:

```bash
venv/bin/python scripts/evals/run_orchestrator_eval_slice.py \
  --cases python_cli_small_feature medium_cli_multi_file_feature debug_import_error_repair checkpoint_resume_mid_task stale_replace_repair missing_report_artifact fake_verification_artifact_guard \
  --print-prompts
```
