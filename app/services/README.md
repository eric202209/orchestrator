# Services Architecture

## Package Responsibilities

- `agents/`
  - Backend registry, runtime factory, and provider-specific adapters.
- `model_adaptation/`
  - Provider-neutral prompt envelopes and backend or model adaptation profiles.
- `orchestration/`
  - Planning, execution, validation, failure handling, persistence, and policy.
- `session/`
  - Session lifecycle, execution entrypoints, checkpoint inspection, and streaming.
- `workspace/`
  - System settings, isolation, checkpoints, and context preservation.
- `planning/`
  - Planner sessions, commit flow, and planning-specific runtime usage.

## Boundary Rules

- Keep vendor-neutral orchestration contracts out of provider-specific runtime implementations.
- Keep adaptation logic out of `agents/` unless it is strictly adapter bootstrap code.
- Treat `app/services/__init__.py` as a stable surface and avoid exporting experimental internals there.
