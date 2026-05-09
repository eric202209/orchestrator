---
title: "Completion Validation False Positive: .venv JS Files"
type: failure_memory
applies_to: [validation, failure]
tags: [mixed-stack, venv, completion-validation, python, node]
priority: 8
failure_signature: "mixed Python and Node"
---

Completion validation incorrectly rejected a Python-only workspace as a mixed Python/Node stack because JavaScript files exist inside `.venv/lib/python3.12/site-packages/` vendor paths (e.g., `pip/_vendor/urllib3/contrib/emscripten/emscripten_fetch_worker.js`).

Error patterns:
- "Workspace mixes Python and Node/JS artifacts even though the accepted plan targets a single python stack"
- completion validation rejected after all pytest steps passed

Root cause: The workspace consistency validator counted `.js` files inside `.venv/`, `venv/`, or `site-packages/` vendor directories as Node implementation evidence. These are Python dependency vendored files, not project Node artifacts.

This is a validator scoping bug, not a planning or execution error. The task execution itself was correct — pytest passed and all steps completed. The validator over-counted dependency paths.

Operator action: if you see this failure after all steps passed and pytest was green, the workspace is likely correct. Inspect `.venv/` contents before assuming a real mixed-stack issue.
