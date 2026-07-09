# Maintenance Scripts

This folder contains operator-run diagnostics and historical validation runners.
Most files here are not part of normal CI/CD and should not be treated as app
entry points.

Keep these categories:

- Shared helpers, especially `_runner_common.py`.
- Reusable eval support, especially `score_orchestrator_eval_case.py`.
- Scripts imported by `app/tests/`.
- The latest live runner for a still-active validation track.
- Historical runners that still have direct unit tests.

It is safe to remove old one-off runner generations when:

- their result is already captured under `docs/roadmap/reports/` or
  `docs/roadmap/done/`;
- a later runner replaced them;
- no `app/tests/` module imports them;
- no active eval harness calls them.
- archived docs do not depend on the exact script path for reproducibility, or
  those docs are updated to mark the runner as retired.

Do not delete scripts from `scripts/evals/fixtures/` casually. Those fixtures are
reusable corpus data for future eval runs and several tests assume the eval
harness layout remains stable.

## Current Contents

- `_runner_common.py` - shared helper imports for runner scripts.
- `check_openai_compatible_endpoint.py` - reusable endpoint diagnostic.
- `planning_contract_report.py` - reusable report, imported by `app/tests/`.
- `reflection_replay.py` - reusable offline reflection-quality diagnostic.
- `score_orchestrator_eval_case.py` - reusable eval scoring, imported by `app/tests/`.
- `phase10k_p2_live_pilot_runner.py` - imported by `app/tests/test_phase10k_p2_live_pilot_runner.py`.
- `phase18f_seed_real_session_evidence.py`, `phase18i_machine_a_limited_validation.py` -
  Phase 18F/18I evidence-generation harnesses (see
  `docs/roadmap/done/workflow/` for their reports).
- `workspace_collision_audit.py` - Phase 23B read-only Project workspace
  collision audit; imported by `app/tests/`.

2026-07: removed 45 one-off T1/WorkingMemory confirmation and pilot runners
(`t1_*_runner.py`, `t1_*_driver*.py`, `wm_*_runner.py`, `wm_*_pilot*.py`,
`validate_incremental*.py`, `validate_repo_memory*.py`,
`hg_p2b_strict_validation_r4_runner.py`, `probe_incremental_output.py`,
`test_wm_off_runner_v3.py`) per the criteria above: each was a completed,
dated, project/task-ID-specific historical run with no `app/tests/`
reference and no active eval harness dependency; their findings remain in
`docs/roadmap/done/` and `docs/roadmap/reports/`.
