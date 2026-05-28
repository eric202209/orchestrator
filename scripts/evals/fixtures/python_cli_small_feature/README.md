# Python CLI Small Feature Fixture

Seed workspace for the `python_cli_small_feature` evaluation case.

The fixture starts as a tiny Python package with one intentionally missing CLI
feature: `--uppercase`. The orchestrator task should add that feature under
`src/` and update tests under `tests/`.

Suggested task prompt:

```text
Add the --uppercase option to this small Python CLI. When the flag is present,
the CLI should uppercase the message before printing it. Keep changes scoped to
src/ and tests/. Verify with python3 -m pytest -q.
```

Use by copying this directory to a clean project workspace, then run a normal
orchestrator task against that workspace through the production queue path
(`queue_task_for_session(...)` or the backend API that queues a task). Do not
launch eval runs with `execute_orchestration_task.run(...)` unless the harness
has already marked the session running and active; otherwise the execution loop
will stop before step 1 with `cancelled/session_pending`.

After completion, score it with:

```bash
python3 scripts/score_orchestrator_eval_case.py \
  --manifest scripts/evals/orchestrator-eval-v1-manifest.json \
  --case-id python_cli_small_feature \
  --project-dir /path/to/copied/workspace \
  --session-id <session_id> \
  --task-id <task_id> \
  --output docs/roadmap/reports/evals/orchestrator-eval-v1-smoke-YYYY-MM-DD.json
```

The seed fixture itself is expected to fail until the feature is implemented.
After the orchestrator completes correctly, `python3 -m pytest -q` should pass.
