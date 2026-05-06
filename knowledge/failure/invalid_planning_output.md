---
type: debug_case
title: Invalid Planning Output / JSON Parse Failure
applies_to: [failure]
tags: [planning, json, parse, format, output]
priority: 9
---

The LLM returned planning output that could not be parsed as valid JSON or structured steps. This breaks the planning phase before execution begins.

Error patterns that match this failure:
- "json.JSONDecodeError"
- "JSON parsing error detected"
- "invalid planning output"
- "Failed to parse plan"
- "plan steps could not be extracted"
- "unexpected character in json"
- "planning output is not valid json"
- "Expecting value: line"

Root cause: The LLM output did not conform to the required planning format. Common causes include:
- Model returned prose instead of a JSON array
- Response was truncated mid-structure
- Model used markdown fences around JSON that the parser does not strip
- Context window was too large and the model produced partial output

Recommended action: review_failure. Do not retry with the same prompt — it will likely fail the same way. Shorten the planning context, inject the planning format guide, or simplify the task description. Check the planning format guide knowledge item.
