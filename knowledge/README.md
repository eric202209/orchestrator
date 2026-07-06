# Knowledge Directory

Drop markdown or JSON files here to inject domain knowledge into the orchestrator's planning, validation, and failure-handling flows.

Files are ingested by `scripts/planning_and_knowledge/ingest_knowledge.py`. Re-run the script after adding or editing files.

---

## Markdown frontmatter format

Every markdown knowledge file must start with a YAML frontmatter block:

```markdown
---
title: "JSON Output Format Guide"
type: format_guide
applies_to:
  - planning
  - validation
tags:
  - json
  - output-format
priority: 10
# Optional fields:
tool_name: code_runner
failure_signature: "jsondecodeerror:planning"
project_scope: my-project
---

The body of the document goes here. Describe the format, rule, or example
in plain text or markdown. Keep it under 800 characters for best results.
```

### Required frontmatter fields

| Field | Description |
|-------|-------------|
| `title` | Short human-readable name |
| `type` | One of: `format_guide`, `tool_contract`, `debug_case`, `best_practice`, `failure_memory`, `system_doc`, `task_example` |
| `applies_to` | List of phases this item applies to: `planning`, `validation`, `failure`, or `all` |

### Optional frontmatter fields

| Field | Description |
|-------|-------------|
| `tags` | List of string tags for filtering |
| `priority` | Integer — higher = retrieved first (default: 0) |
| `tool_name` | Restrict to a specific tool name |
| `failure_signature` | Exact signature string for failure-memory matching |
| `project_scope` | Restrict to a specific project name |

---

## JSON format

Files ending in `.json` must be a single object with the same fields:

```json
{
  "title": "Auth Module Task Example",
  "knowledge_type": "task_example",
  "applies_to": ["planning"],
  "content": "When implementing auth modules, always include token refresh logic...",
  "tags": ["auth", "example"],
  "priority": 5
}
```
