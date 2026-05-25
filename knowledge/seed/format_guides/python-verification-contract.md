---
title: Python Verification Contract
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - python
  - verification
  - pytest
  - unittest
priority: 8
confidence: 0.86
---

# Python Verification Contract

Use this for Python projects and Python test repair, not as unconditional
planning behavior for every project type.

- Prefer `python -m pytest tests/ -q` when the project has pytest signals.
- Use `python -m unittest discover -s tests` when the project is clearly
  unittest-based and has no pytest signal.
- Use `python -m py_compile <path>` for syntax-only checks on changed Python
  files when no test suite exists.
- For app import assertions, create a small test file with structured file
  operations instead of putting brittle import logic in inline `python -c`
  snippets.
- Verification commands must fail nonzero when the expected behavior is absent.
