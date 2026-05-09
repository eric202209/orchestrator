---
title: "Planning Repair: Output Is Prose Not JSON"
type: failure_memory
applies_to: [planning, failure]
tags: [repair, prose, json, output-contract]
priority: 9
failure_signature: "repair_returned_prose"
---

Planning repair returned natural language prose instead of a JSON array. The orchestrator checks that the first non-whitespace character of repair output is `[`. If it is not, the output is classified as prose and the repair attempt terminates without retry.

Error patterns:
- "repair_returned_prose"
- "Planning repair returned prose instead of JSON array"

Root cause: Qwen responded with an explanation or summary of the plan changes instead of the JSON plan array itself.

Fix: repair output must start immediately with `[` — the opening bracket of the JSON array of steps. Do not include any text before the array. Do not say "Here is the revised plan" or similar. Output only the bare JSON array.
