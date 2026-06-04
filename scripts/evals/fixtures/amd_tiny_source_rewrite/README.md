# AMD Tiny Source Rewrite Fixture

This fixture is intentionally smaller than the normal Task 1 eval cases. It
exists to measure whether an AMD-local model lane can perform one bounded source
rewrite when tests and source already exist.

Suggested task prompt:

```text
Fix the existing string formatter in src/amd_tiny/formatting.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q.
```

The seed fixture is expected to fail pytest until `format_label` trims
surrounding whitespace and returns title-cased words joined by single spaces.
