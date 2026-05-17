---
title: Workspace-Safe Command Planning
type: format_guide
applies_to:
  - planning
tags:
  - workspace
  - commands
  - isolation
priority: 94
---

Plan commands for the current task workspace only. Use relative paths and verify only files that the task creates or updates.

Prefer simple commands such as `ls`, `cat <file>`, `test -f <file>`, or `python -c` checks that stay inside the workspace. Use `node -e` only when the workspace is already a Node project. Avoid broad repository scans, parent-directory writes, global cleanup, daemon management, or commands that assume another workload's files.

For file creation, prefer structured `ops` with `write_file` or `append_file` over complex shell heredocs.
