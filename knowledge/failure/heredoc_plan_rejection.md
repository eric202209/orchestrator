---
title: "Plan Rejected: Heredoc Commands"
type: failure_memory
applies_to: [planning, validation, failure]
tags: [heredoc, plan-validation, brittle-command, file-write]
priority: 10
failure_signature: "brittle heredoc-heavy"
---

Plan was rejected because one or more steps used heredoc syntax (`cat <<EOF`, `cat > file <<`, `<<'EOF'`).

Validator subcodes: `multiple_heredoc_across_plan`, `single_heredoc_across_plan`.

Root cause: Qwen defaults to heredoc for multi-line file writes. The orchestrator bans heredoc unconditionally in both initial planning and repair because it produces fragile shell escaping and interacts poorly with the workspace isolation layer.

Fix: replace all heredoc file writes with `printf` for short content, or `python3 -c "open('path','w').write('content')"` for longer content. Never use `cat <<EOF` or any heredoc variant. This applies inside repair output too — repair that reintroduces heredoc will be rejected again.

Do not suggest heredoc as a fallback for quote escaping. Use double quotes instead of single quotes to avoid `\'` issues.
