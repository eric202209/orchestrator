---
title: "Plan Command Length and Line Count Limits"
type: format_guide
applies_to: [planning, validation]
tags: [command-length, too-many-lines, brittle-command, plan-shape]
priority: 9
---

Each command in a plan step must stay within these hard limits:

- Max command length: 900 characters per command
- Max lines per step: keep steps to 1–3 commands
- Max steps in a plan: 4–6 for most tasks

Validator subcodes triggered when limits are exceeded:
- `oversized_command_length` — command is too long
- `too_many_lines` — step has too many lines

To stay within limits:
- Never inline full file content as a shell argument
- For content over 200 chars, split into multiple append steps (`printf >> file`)
- For content over 400 chars, write via a short python3 -c with a compact string
- Break multi-file writes into separate steps, one file per step
- Prefer short setup commands (`npm init -y`, `pip install fastapi`) over inline dependency specification
