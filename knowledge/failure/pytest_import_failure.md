---
title: "Completion Verification Failed: pytest ModuleNotFoundError"
type: failure_memory
applies_to: [validation, failure]
tags: [pytest, import, completion-verification, pythonpath, module-not-found]
priority: 9
failure_signature: "module_not_found"
---

Final completion verification ran `pytest` and failed with `ModuleNotFoundError: No module named 'X'` even though individual execution steps reported the tests as passing.

Error patterns:
- "ModuleNotFoundError: No module named"
- "ImportError: cannot import name"
- completion verification `pytest` fails after task_completion validation accepted
- `failure_class: "module_not_found"`

Root cause: The execution steps ran pytest with `PYTHONPATH=src` or `PYTHONPATH=libhidden` so the module was importable during execution. The final completion verification command ran bare `pytest` without the path prefix, so the module was not found.

Fix: structure the project so bare `pytest` works without custom PYTHONPATH. Put source files in the workspace root or in a proper package (`__init__.py` present). Avoid non-standard source layouts like `libhidden/`. If a custom path is needed, add a `conftest.py` at the root that adds the source directory to `sys.path`, so `pytest` finds modules regardless of cwd.
