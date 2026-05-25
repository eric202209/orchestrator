---
title: Test Scaffold Contract
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - tests
  - scaffold
  - pytest
  - unittest
priority: 8
confidence: 0.84
---

# Test Scaffold Contract

Use this when a task creates or changes tests. Keep it as retrieved knowledge so
language- and framework-specific test style does not become universal planning
runtime behavior.

- Inspect nearby tests first.
- Match existing imports, fixtures, factories, and domain constructors.
- Do not replace project objects with raw dictionaries unless existing tests
  already do that.
- Compile changed Python tests before or with the final suite run.
- Preserve existing assertions unless the task explicitly asks to replace them.
