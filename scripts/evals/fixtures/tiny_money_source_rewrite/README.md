# Tiny Money Source Rewrite Fixture

This fixture measures whether planning repair generalizes to another bounded
existing-source rewrite with existing tests and no test creation.

Suggested task prompt:

```text
Fix the existing money formatter in src/tiny_money/money.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q.
```

The seed fixture is expected to fail pytest until `format_cents` renders integer
cents as dollars with two decimals, including negative values.
