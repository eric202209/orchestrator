---
type: format_guide
title: Shell-Safe Command Format Guide
applies_to: [planning, validation]
tags: [shell, commands, planning, validation]
priority: 20
---

Commands in planning output must be shell-safe and valid.

Avoid complex nested quoting in one-line commands.

Do not use `python3 -c "..."` when the code contains f-strings, nested quotes, JSON strings, or semicolons.

Treat inline Python as a shell-safety risk first, not a convenience shortcut.

Do not use heredoc syntax in planning output or repair output. The planner
validator rejects heredoc shapes such as `cat > file <<EOF`, `<<'PY'`, and
looped heredocs because they are brittle in the workspace isolation layer.

If the code is longer than a few lines, write it to a file and run the file.

For planning output and verification commands:
- prefer short `printf` writes, package-manager/editor-friendly commands, or
  script-file execution for Python snippets
- avoid nested shell quoting inside `python -c`
- reject commands that rely on partially escaped quotes or unfinished string literals

Never emit commands with unmatched quotes, unfinished parentheses, or partially escaped strings.
