---
title: "Plan Rejected: Nested Project Folder Creation"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [workspace, nested-folder, plan-validation, project-root]
priority: 9
failure_signature: "nested_project_folder_command"
---

Plan was rejected because steps created a new subdirectory and changed into it as the project root (e.g., `mkdir my-app && cd my-app`). The validator subcode is `nested_project_folder_command`.

Error patterns:
- "nested_project_folder_command"
- "Plan generates deliverable inside a new nested project folder"
- "planning_validation_failed_after_repair"

Root cause: Qwen treats the task workspace as a blank slate and tries to scaffold a new project by creating a named subdirectory — the same instinct as `npx create-react-app my-app`. The orchestrator's workspace is already the project root.

Fix: all commands must target the existing workspace root (`.`). Never create a new named project subdirectory. Use `npm init -y` or `vite create . --template react` in the current directory, not `mkdir app-name && cd app-name`. Write all files relative to the task workspace root.
