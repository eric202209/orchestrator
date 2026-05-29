# Medium CLI Multi-File Feature Fixture

Seed workspace for the `medium_cli_multi_file_feature` evaluation case.

The fixture starts as a small Python CLI with task parsing, storage helpers, and
output formatting split across modules. One feature is intentionally missing:
the `summary` command.

Suggested task prompt:

```text
Add the summary command to this Python CLI. The command should print a compact
summary of the current task list as "3 tasks, 2 complete". Keep the change
scoped to the existing src/ and tests/ files. The feature should use the
existing TaskStore and formatting module instead of hard-coding the output in
the CLI. Verify with python3 -m pytest -q.
```

This is a constrained medium fixture. It is meant to test multi-file planning,
not configuration architecture. A correct implementation should coordinate the
parser/dispatcher, store behavior, and formatter without inventing new package
paths.

The seed fixture itself is expected to fail until the feature is implemented.
After the orchestrator completes correctly, `python3 -m pytest -q` should pass.
