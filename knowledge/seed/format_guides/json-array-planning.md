---
title: First-Pass JSON Array Planning
type: format_guide
applies_to:
  - planning
tags:
  - planning
  - json
  - local-model
priority: 96
---

For executable task planning, return only a bare JSON array of executable steps. The first non-whitespace character must be `[` and the final non-whitespace character must be `]`.

Each step should include `step_number`, `description`, `commands` or `ops`, `verification`, `rollback`, and `expected_files`. Use `ops` for file writes when possible, and keep command lists short and deterministic.

Do not include Markdown fences, prose, wrapper objects, comments, or analysis text outside the array.
