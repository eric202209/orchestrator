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

Do not delete scripts from `scripts/evals/fixtures/` casually. Those fixtures are
reusable corpus data for future eval runs and several tests assume the eval
harness layout remains stable.
