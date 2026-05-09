---
title: "Planning Repair: Output Wrapped in Markdown Fences"
type: failure_memory
applies_to: [planning, failure]
tags: [repair, json, markdown-fence, output-contract]
priority: 10
failure_signature: "repair_output_contract_violation"
---

Planning repair returned a JSON array wrapped in markdown code fences instead of a bare JSON array. The parser looks for the first character to be `[`. When the output starts with ` ```json ` or ` ``` `, it is classified as prose, not JSON, and terminates with `repair_output_contract_violation`.

Error patterns:
- "Planning repair returned prose instead of JSON array"
- "repair_output_contract_violation"
- "repair returned markdown-fenced JSON"

Root cause: Qwen wraps JSON in ` ```json\n[...]\n``` ` as a formatting habit from instruction-tuning.

Fix: repair output must be a bare JSON array starting with `[` and ending with `]`. No ` ``` ` fences. No introductory prose. No trailing explanation. The entire output must be the JSON array and nothing else.
