# Debug Import Error Repair Fixture

Seed workspace for the `debug_import_error_repair` evaluation case.

The fixture starts with a small src-layout package whose tests fail during
import because `import_repair.__init__` exposes a module path that does not
exist. The repair should fix the import/module path issue without weakening the
tests.

Suggested task prompt:

```text
Fix the failing Python test suite caused by an import or module path error
without weakening tests. Keep changes scoped to src/ and tests/. Verify with
python3 -m pytest -q.
```

Verifier command:

```bash
python3 -m pytest -q
```

The seed fixture is expected to fail with an import error. After a correct
repair, the greeting helper should import cleanly and the tests should pass.
