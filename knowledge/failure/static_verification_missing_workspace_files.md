---
title: "Static Verification Plan References Missing Workspace Files"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [static-site, verification, missing-files, workspace-inventory]
priority: 9
failure_signature: "Verification/review plan references source files that do not exist in the current workspace"
---

A static-site verification or review plan was rejected because it referenced source files that are not present in the current workspace. Common bad examples are assuming `style.css`, `app.css`, `garden.svg`, or `logo.svg` when the accepted site actually uses nested paths such as `css/style.css` and `images/flower-bg.svg`.

Fix: repair the plan by grounding every command in the current workspace inventory. First inspect existing files, then verify the paths that actually exist. For verification-only tasks, do not create or rewrite source files just to satisfy the plan. Never repair this by creating conventional replacement files such as `styles.css`, `style.css`, `index.html`, `icon.svg`, or `garden.svg`, and do not rewrite existing app assets such as `index.html` or `css/style.css`; those are app mutations, not verification. Use content-aware commands such as `node -e` that read `index.html`, parse the real asset references, and assert those referenced files exist.
