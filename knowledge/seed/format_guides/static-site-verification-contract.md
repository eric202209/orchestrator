---
title: Static Site Verification Contract
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - static-site
  - verification
  - html
  - css
  - svg
  - expected-files
priority: 8
confidence: 0.84
---

# Static Site Verification Contract

Use this for plain HTML/CSS/SVG tasks that ask to strengthen verification or
check content/link integrity.

Do:
- Inspect the existing file layout before choosing verification targets.
- Verify the stylesheet link from `index.html` to the real CSS path.
- Verify SVG linkage through the path that actually exists:
  - If HTML has an `<img>` or inline SVG, verify that exact HTML reference.
  - If CSS uses `background-image: url(...)`, verify the CSS `url(...)` points
    to an existing SVG file.
- Use concrete existing asset paths such as `images/flower-bg.svg`; do not invent
  placeholder names like `images/example.svg`, `images/logo.svg`, or
  `images/*.svg` inside content assertions.
- Keep `expected_files` to concrete files that must exist. Use glob patterns only
  when checking a family of already existing files and the runtime supports it.

Avoid:
- Requiring an `<img>` tag when the current design references the SVG from CSS.
- Checking that HTML literally contains `images/*.svg`.
- Treating verification-only tasks as a reason to recreate the site.

Example verification commands:

```json
[
  {
    "step_number": 1,
    "description": "Verify stylesheet link and CSS SVG background",
    "commands": [
      "python -c \"import pathlib,re,sys; html=pathlib.Path('index.html').read_text(); css=pathlib.Path('css/style.css').read_text(); ok='css/style.css' in html and re.search(r\\\"url\\\\(['\\\\\\\"]?\\\\.\\\\./images/flower-bg\\\\.svg['\\\\\\\"]?\\\\)\\\", css) and pathlib.Path('images/flower-bg.svg').is_file(); sys.exit(0 if ok else 1)\""
    ],
    "verification": "python -c \"import pathlib,re,sys; html=pathlib.Path('index.html').read_text(); css=pathlib.Path('css/style.css').read_text(); ok='css/style.css' in html and 'background-image' in css and pathlib.Path('images/flower-bg.svg').is_file(); sys.exit(0 if ok else 1)\"",
    "rollback": null,
    "expected_files": ["index.html", "css/style.css", "images/flower-bg.svg"]
  }
]
```
