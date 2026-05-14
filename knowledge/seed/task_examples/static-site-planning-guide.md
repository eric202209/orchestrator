---
title: Static Site Task Planning Guide
type: task_example
applies_to:
  - planning
  - validation
tags:
  - static-site
  - html
  - css
  - svg
  - verification
priority: 8
---

For small static HTML/CSS/SVG projects, keep plans generic and workspace-aware:

- Inspect the current workspace before deciding whether a file should be created or edited.
- Use structured file operations (`write_file`, `replace_in_file`, `append_file`) for file mutations instead of shell heredocs.
- Do not create verification-only source files unless the task explicitly asks for them. If the task asks to strengthen verification, prefer verification commands that check existing task outputs.
- Avoid command-only steps when the step's purpose is to mutate files. The file mutation should be visible as an operation, with a verification command proving the result.
- Verification should be content-aware. Prefer short `node -e` checks for static web assets: read the relevant HTML/CSS/SVG files, assert expected links/selectors/text/assets exist, and exit nonzero on failure.
- For existing-workspace edits, target existing files unless the task clearly names a new output path. Do not introduce unrelated scripts, reports, or helper files just to satisfy verification.
- Rollback should match the operation: delete newly created files, or restore/replace edited files only when a safe previous state is known.

Example planning shape:

1. Edit or create the requested HTML file with a structured file operation.
2. Edit or create the requested CSS/SVG asset with a structured file operation.
3. Verify with a short command that reads the files and checks links, asset references, and required visible content.
