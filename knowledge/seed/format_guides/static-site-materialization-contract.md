---
title: Static Site Materialization Contract
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - static-site
  - typed-ops
  - materialization
  - expected-files
  - html
  - css
  - svg
priority: 8
confidence: 0.86
---

# Static Site Materialization Contract

Use this only for plain static-site tasks that explicitly ask to create or edit
HTML, CSS, SVG, or JS files.

Do:
- Materialize every new `expected_files` path with typed file operations:
  `mkdir` for parent directories, then `write_file`, `append_file`, or
  `replace_in_file` for the file.
- Keep content domain-specific to the user's task. Do not invent a generic
  template when the task asks for a branded page, theme, or subject.
- If a file already exists and the task asks to reuse it, inspect or edit that
  file instead of rewriting a scaffold.
- Put verification after materialization and check the exact paths produced by
  the file operations.

Avoid:
- Plans that only verify `expected_files` without creating or editing them.
- Shell choreography such as `echo >> css/style.css` when typed file operations
  are available.
- Project-specific defaults in orchestrator code. Examples belong in knowledge
  or task prompts, not runtime normalizers.

Minimal plan shape:

```json
[
  {
    "step_number": 1,
    "description": "Inspect current static-site files",
    "commands": ["python -c \"import pathlib; print('\\n'.join(str(p) for p in pathlib.Path('.').rglob('*') if p.is_file()))\""],
    "verification": "python -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('.').exists() else 1)\"",
    "rollback": null,
    "expected_files": []
  },
  {
    "step_number": 2,
    "description": "Create or edit requested static-site files",
    "commands": [],
    "verification": "python -c \"import pathlib,sys; paths=['index.html','css/style.css']; sys.exit(0 if all(pathlib.Path(p).is_file() for p in paths) else 1)\"",
    "rollback": "rm -f index.html css/style.css",
    "expected_files": ["index.html", "css/style.css"],
    "ops": [
      {"op": "mkdir", "path": "css"},
      {"op": "write_file", "path": "index.html", "content": "<!doctype html>..."},
      {"op": "write_file", "path": "css/style.css", "content": "body {...}"}
    ]
  }
]
```
