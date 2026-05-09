---
title: "Plan Rejected: Weak or Missing Verification"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [verification, weak-verification, plan-validation, pytest, npm-test]
priority: 9
failure_signature: "weak_verification"
---

Plan was rejected because verification steps used commands that do not actually check correctness: `echo "done"`, `ls`, `cat file`, or no verification step at all. Subcodes: `weak_verification`, `missing_verification_command`.

Error patterns:
- "weak_verification_steps"
- "missing_verification_command"
- "Plan is missing verification commands for implementation-heavy work"

Root cause: Qwen uses `echo` or `ls` as a placeholder verification step, or omits verification entirely.

Fix for Python projects: use `pytest` or `python -m pytest` or `python -c "import module; assert ..."`.
Fix for Node/React: use `npm run build`, `npm test`, or `node -e "require('./dist/index.js')"`.
Fix for FastAPI: use `python -m pytest` or `curl -s http://localhost:8000/health`.
Fix for static: use `npm run build` to confirm no build errors.
The verification step must be the last step and must fail if the implementation is broken.
