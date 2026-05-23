---
title: "Static Site File Materialization"
type: format_guide
applies_to: [planning, validation]
tags: [static-site, file-write, directories, css, svg]
priority: 96
---

For static-site tasks that create files under folders such as `css/`, `images/`,
`assets/`, or `js/`, materialize parent directories and files before appending
or verifying content.

Prefer structured operations:
- `write_file` for `index.html`, `css/style.css`, and SVG assets.
- Include the complete initial file content in the `write_file` operation.

If using shell commands instead of structured operations:
- Run `mkdir -p css images` before writing nested files.
- Use `printf ... > css/style.css` for initial content.
- Use `>> css/style.css` only after the file and parent directory already exist.

Never plan a verification command for `css/style.css` or `images/*.svg` before
the step that creates that exact file. Do not append to a nested path as the
first materialization action.
