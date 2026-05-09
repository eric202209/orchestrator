---
title: "Verification Commands: What the Validator Accepts"
type: format_guide
applies_to: [planning, validation]
tags: [verification, pytest, npm-test, weak-verification, plan-shape]
priority: 10
---

Every plan must include a verification step as its last step. The validator rejects plans with `weak_verification` or `missing_verification_command`.

Accepted strong verification commands by stack:

Python (any): `pytest` or `python -m pytest` or `python -m pytest tests/`
FastAPI: `python -m pytest` or `uvicorn app.main:app --host 0.0.0.0 --port 8000 &` then `curl -s http://localhost:8000/health`
Node/React: `npm run build` (confirms no build error) or `npm test`
Static HTML: `npm run build` (if build step exists) or `node -e "require('fs').existsSync('index.html') || process.exit(1)"`
CLI script: `python script.py --help` or `python -c "from module import func; assert func(2,3)==5"`

Commands that are always rejected as weak verification:
- `echo "done"` or `echo "success"`
- `ls` or `ls -la`
- `cat file`
- `true`
- any command that always exits 0 regardless of implementation correctness
