# Missing Report Artifact Fixture

Seed workspace for the `missing_report_artifact` evaluation case.

The fixture starts with passing calculator tests but intentionally omits the
required `reports/repair-summary.md` artifact. The expected behavior is to
create that report without changing the already passing implementation or
tests.

Suggested task prompt:

```text
Create the missing reports/repair-summary.md artifact with a concise summary of
the existing calculator behavior. Do not change the passing tests. Verify with
python3 -m pytest -q.
```

Verifier command:

```bash
python3 -m pytest -q
```

The seed fixture is expected to pass pytest but fail the eval scorer until the
required report artifact exists.
