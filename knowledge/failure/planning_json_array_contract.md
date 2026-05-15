---
title: Planning Must Return A JSON Array
type: failure_memory
applies_to:
  - planning
  - failure
tags:
  - planning
  - json
  - local-model
failure_signature: debug_parse_error
priority: 95
---

When planning with local or instruction-sensitive models, first-pass success improves when the model is constrained to return a bare JSON array as the first non-whitespace output.

Avoid Markdown fences, wrapper objects, prose before the array, or partial single-step summaries. Each step should include stable `step_number`, `description`, `commands` or `ops`, `verification`, `rollback`, and `expected_files`.

If a repair pass is needed after malformed planning output, ask for only the corrected JSON array and preserve workspace-safe paths from the original task.
