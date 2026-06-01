# Missing Report Artifact Fixture

Seed workspace for the `missing_report_artifact` evaluation case.

The fixture starts with calculator tests that fail because `subtract` is
missing, and it intentionally omits the required `reports/repair-summary.md`
artifact. The expected behavior is to implement the small missing function and
create the report.

Suggested task prompt:

```text
Implement subtract(left, right) in src/report_artifact/calculator.py and create
reports/repair-summary.md with a concise summary of add, multiply, and subtract
behavior. Do not weaken tests. Verify with python3 -m pytest -q.
```

Verifier command:

```bash
python3 -m pytest -q
```

The seed fixture is expected to fail pytest and fail the eval scorer until the
small source change and required report artifact both exist.
