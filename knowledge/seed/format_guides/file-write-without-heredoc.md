---
title: "File Writing: Heredoc Banned — Use printf or python3"
type: format_guide
applies_to: [planning, validation]
tags: [heredoc, file-write, printf, python3, command-shape]
priority: 10
---

Heredoc syntax (`cat <<EOF`, `cat > file <<`, `<<'EOF'`) is unconditionally banned in all plan steps including repair output.

Approved alternatives:

Short content (under 300 chars total): use `printf`
```
printf 'line1\nline2\n' > path/to/file.txt
```

Longer content: use `python3 -c` with escaped string — keep the argument under 700 chars total:
```
python3 -c "open('src/main.py','w').write('def main():\n    pass\n')"
```

Multi-section writes: use multiple `printf >>` append steps, each under 700 chars:
```
printf 'import os\n' > app.py
printf 'def run():\n    pass\n' >> app.py
```

Never use `\` line continuation or `$'...'` quoting as a heredoc workaround — these are also fragile. Use double-quoted Python string in `python3 -c` instead.
