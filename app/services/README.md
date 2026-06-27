# Services Architecture

## Package Responsibilities

- `agents/`
  - Backend registry, runtime factory, and provider-specific adapters.
- `auth/`
  - Authentication rate limiting and user/project authorization helpers.
- `human_guidance/`
  - Human Guidance storage, activation, selection, conflict checks, and plan/post-write validation.
- `integrations/`
  - External service adapters such as GitHub.
- `model_adaptation/`
  - Provider-neutral prompt envelopes and backend or model adaptation profiles.
- `observability/`
  - Health payloads, runtime build identity, log streaming, and streaming health telemetry.
- `orchestration/`
  - Planning, execution, validation, failure handling, persistence, and policy.
- `permissions/`
  - Permission approval flow helpers used by session and agent execution.
- `project/`
  - Project-facing indexing, naming, and state-summary helpers.
- `session/`
  - Session lifecycle, execution entrypoints, checkpoint inspection, and streaming.
- `tasks/`
  - Task persistence, task execution records, workspace promotion coordination, and task tool tracking.
- `workspace/`
  - System settings, isolation, checkpoints, context preservation, and workspace file locking.
- `planning/`
  - Planner sessions, commit flow, and planning-specific runtime usage.

## Boundary Rules

- Keep vendor-neutral orchestration contracts out of provider-specific runtime implementations.
- Keep adaptation logic out of `agents/` unless it is strictly adapter bootstrap code.
- Treat `app/services/__init__.py` as a stable surface and avoid exporting experimental internals there.
- Keep root-level `app/services/*.py` modules as compatibility shims only when older import paths still exist.
