---
title: Workspace Commands Stay Inside Task Root
type: tool_contract
applies_to:
  - planning
  - validation
tags:
  - workspace
  - commands
  - isolation
failure_signature: project_mutation_lock_conflict
priority: 88
---

Agent tasks should use commands and file operations that stay inside the assigned project or task workspace. Prefer relative paths, explicit expected files, and simple verification commands that read only the files created by the current task.

Do not plan broad repository scans, parent-directory writes, global cleanup, or commands that mutate a shared project root unless the task is explicitly a mutation-lock probe.

When multiple workloads are queued, isolate each workload into its own disposable project unless the test intentionally exercises project mutation lock contention.
