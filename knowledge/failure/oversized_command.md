---
title: "Plan Rejected: Oversized Command"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [command-length, brittle-command, oversized, plan-validation]
priority: 9
failure_signature: "oversized_command_length"
---

One or more plan steps contained a command exceeding the 900-character limit. The validator subcode is `oversized_command_length` and includes the step number and actual length.

Error patterns:
- "oversized_command_length"
- "Plan contains brittle heredoc-heavy or malformed commands"
- command length 1000+ chars in a single step

Root cause: Qwen inlines large file content directly into `printf` or `python3 -c` arguments — full source code in a single shell argument. Even when heredoc is banned, oversized `printf` bodies hit this limit.

Fix: split large content into smaller writes. Keep each command under 700 characters. For files with more than ~200 characters of content, break into multiple `printf >> file` append steps, or write a short seed file and patch it with subsequent sed/python commands. Never inline an entire source file as a shell argument.
