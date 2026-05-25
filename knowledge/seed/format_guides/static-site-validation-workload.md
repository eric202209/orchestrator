---
title: Static Site Validation Workload
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - static-site
  - validation
  - html
  - css
  - svg
priority: 9
confidence: 0.82
---

# Static Site Validation Workload

Use this for validation-only static HTML/CSS/SVG tasks. This knowledge replaces
hardcoded planner fallback code for benchmark-shaped static sites.

- Treat validation-only tasks as read-only. Do not create, rewrite, append,
  replace, or delete app source assets.
- Inspect the current workspace before choosing file paths.
- Use existing files such as `index.html`, `css/style.css`, and real SVG paths
  only when they are present in the workspace or explicitly named by the task.
- Do not invent conventional paths such as `styles.css`, `style.css`,
  `app.css`, `logo.svg`, or `icon.svg`.
- Prefer short file/content assertions that prove links, asset references, and
  requested visible text exist.
- Keep `expected_files` empty for pure validation steps unless the task
  explicitly asks to create a verification helper file.
