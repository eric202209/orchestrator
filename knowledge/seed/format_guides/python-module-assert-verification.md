---
title: Python Module Assertion Verification
type: format_guide
applies_to:
  - execution
  - validation
tags:
  - python
  - verification
  - module-assertions
priority: 7
confidence: 0.78
---

# Python Module Assertion Verification

Use this for small Python projects where the task asks to verify behavior of a
specific module function. Keep module names project-specific and grounded in the
workspace; do not hardcode module names into orchestration runtime checks.

- Prefer real test commands when tests exist.
- If no test suite exists, a short `python -c` assertion can verify a named
  module function, but the module path must come from workspace evidence or the
  task prompt.
- Keep inline assertions simple and read-only. Do not write files, delete files,
  start network clients, or import unrelated packages.
- For exception behavior, prefer a tiny test file or a normalized
  `unittest.TestCase().assertRaises(...)` expression over one-line `try/except`
  shell snippets.
