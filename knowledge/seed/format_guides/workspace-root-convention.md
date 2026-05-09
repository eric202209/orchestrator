---
title: "Workspace Root: Never Create a Nested Project Folder"
type: format_guide
applies_to: [planning, validation]
tags: [workspace, nested-folder, project-root, command-shape]
priority: 10
---

The task workspace is already the project root. All files must be created relative to the current directory (`.`). Never create a new named subdirectory as the project root.

Banned patterns:
- `mkdir my-app && cd my-app`
- `npx create-react-app my-app` (creates `my-app/` subdirectory)
- `mkdir project-name && cd project-name && npm init`
- any step that changes into a newly created folder before doing the work

Required patterns:
- `npm init -y` (in current directory)
- `npx create-vite@latest . --template react` (note the `.`)
- `vite create . --template react`
- write all files to relative paths: `src/index.js`, `app.py`, `tests/test_main.py`

The validator subcode `nested_project_folder_command` fires when a step creates and changes into a subdirectory. This applies in both initial planning and repair output.

If a library's scaffold command does not support `.` as the target, use its init flags to write into the current directory, or manually create the files with `printf`/`python3 -c`.
