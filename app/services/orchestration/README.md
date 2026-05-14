# Orchestration Package

This package holds the internal orchestration pipeline used by the worker.

## Module Map

- `planning/`
  - planning-stage helpers and prompt repair logic
- `execution/`
  - execution-time helpers, runtime support, and step self-repair
- `validation/`
  - deterministic validation, parsing, and workspace guardrails
- `events/`
  - event vocabularies, phase telemetry, and derived observability payloads
- `phases/`
  - phase state machines for planning, execution, completion, and failure
- `context/`
  - prompt/context assembly and HITL sentinel helpers
- `diagnostics/`
  - debug feedback, evidence capsules, diff capsules, and stuck-run diagnostics
- `operations/`
  - shared typed operation/file-op contracts
- `reporting/`
  - task reports, replay, policy simulation, and decision timeline projections
- `state/`
  - checkpoint, validation, live-log persistence, and session-state helpers

- `phases/planning_flow.py`
  - planning retries, minimal-prompt fallback, plan repair, plan validation
- `phases/execution_loop.py`
  - the step-by-step execution/debug/revision state machine
- `phases/completion_flow.py`
  - completion validation, baseline publish validation, final status/report handling
- `phases/completion_repair_capsule.py`
  - bounded completion-repair prompt capsule rendering
- `phases/failure_flow.py`
  - top-level exception handling and error checkpoint behavior

- `execution/execution_flow.py`
  - step assessment and timeout helpers
- `execution/step_support.py`
  - step self-repair and execution-result coercion
- `validation/workspace_guard.py`
  - workspace/path normalization and isolation enforcement
- `validation/parsing.py`
  - plan extraction and structured text recovery helpers
- `operations/file_ops_contract.py`
  - typed file operation shape and rendering helpers
- `context/assembly.py`
  - planning, execution, and completion-repair prompt context assembly
- `diagnostics/debug_feedback.py`
  - runtime failure classification and bounded debug repair prompts
- `diagnostics/evidence_capsule.py`
  - deterministic workspace evidence collection
- `reporting/task_report.py`
  - task report payload/render helpers
- `state/persistence.py`
  - checkpoint, validation, and live-log persistence helpers
- `task_rules.py`
  - task-intent classification and virtual merge gate rules
- `policy.py`
  - shared orchestration thresholds and timeout caps
- `app/services/agents/agent_backends.py`
  - backend registry and capability metadata for runtime selection
- `app/services/agents/agent_runtime.py`
  - runtime factory used by worker/session entrypoints to instantiate the configured backend
- `execution/runtime.py`
  - workspace snapshot/state-manager/runtime support
- `events/telemetry.py`
  - structured phase-event recording for resume/debug observability
- `validation/validator.py`
  - deterministic plan/step/completion validation
- `planning/planner.py`
  - planner-specific fallback/repair prompt logic
- `execution/executor.py`
  - tool-failure inspection helpers
- `types.py`
  - shared orchestration dataclasses, including `OrchestrationRunContext`

## Package Conventions

- Keep `worker.py` as the Celery entrypoint and coordinator, not the place for dense orchestration logic.
- Prefer adding new orchestration behavior to one of these modules instead of growing the worker again.
- Use `__init__.py` as the stable import surface for the worker and nearby orchestration callers.
- Pass shared runtime state through `OrchestrationRunContext` instead of expanding flow signatures one keyword at a time.
- Record major phase transitions with `events/telemetry.py` so checkpoint resumes can explain what happened before a failure or retry.
