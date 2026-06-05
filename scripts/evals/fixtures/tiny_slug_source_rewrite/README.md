# Tiny Slug Source Rewrite Fixture

This fixture measures whether planning repair generalizes to one bounded
existing-source rewrite with existing tests and no test creation.

Suggested task prompt:

```text
Fix the existing slug formatter in src/tiny_slug/slug.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q.
```

The seed fixture is expected to fail pytest until `slugify` trims whitespace,
lowercases text, removes punctuation separators, collapses repeated separators,
and joins words with single hyphens.
