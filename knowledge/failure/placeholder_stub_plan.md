---
title: "Plan Rejected: Placeholder or Stub Implementation"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [placeholder, stub, todo, plan-validation]
priority: 8
failure_signature: "placeholder_intent"
---

Plan was classified as containing placeholder or stub implementations. This is reclassified as `repairable` — repair will be triggered and the rejection hint will include the stub subcode.

Error patterns:
- "Plan appears to generate placeholder or stub implementations"
- "placeholder_intent"
- steps that write `TODO`, `pass`, `raise NotImplementedError`, or empty function bodies

Root cause: Qwen scaffolds the structure and leaves stubs instead of implementing the logic. Common when the task has multiple files — it fills in the shape but defers the content.

Fix: implement actual logic in each file. No `TODO` comments, no `pass` in non-abstract methods, no `raise NotImplementedError`. If the task is to create a utility function, write the function body. If it is a React component, render actual JSX. Stubs that do not satisfy the task will cause repair to fire.
