# Orchestration Package

This package holds the internal orchestration pipeline used by the worker.

## Module Map

- `planning_flow.py`
  - planning retries, minimal-prompt fallback, plan repair, plan validation
- `execution_loop.py`
  - the step-by-step execution/debug/revision state machine
- `completion_flow.py`
  - completion validation, baseline publish validation, final status/report handling
- `failure_flow.py`
  - top-level exception handling and error checkpoint behavior

- `execution_flow.py`
  - step assessment and timeout helpers
- `step_support.py`
  - step self-repair and execution-result coercion
- `workspace_guard.py`
  - workspace/path normalization and isolation enforcement
- `parsing.py`
  - plan extraction and structured text recovery helpers
- `task_rules.py`
  - task-intent classification and virtual merge gate rules
- `reporting.py`
  - task report payload/render helpers
- `policy.py`
  - shared orchestration thresholds and timeout caps
- `persistence.py`
  - checkpoint, validation, and live-log persistence helpers
- `app/services/agents/agent_backends.py`
  - backend registry and capability metadata for runtime selection
- `app/services/agents/agent_runtime.py`
  - runtime factory used by worker/session entrypoints to instantiate the configured backend
- `runtime.py`
  - workspace snapshot/state-manager/runtime support
- `telemetry.py`
  - structured phase-event recording for resume/debug observability
- `validator.py`
  - deterministic plan/step/completion validation
- `planner.py`
  - planner-specific fallback/repair prompt logic
- `executor.py`
  - tool-failure inspection helpers
- `types.py`
  - shared orchestration dataclasses, including `OrchestrationRunContext`

## Package Conventions

- Keep `worker.py` as the Celery entrypoint and coordinator, not the place for dense orchestration logic.
- Prefer adding new orchestration behavior to one of these modules instead of growing the worker again.
- Use `__init__.py` as the stable import surface for the worker and nearby orchestration callers.
- Pass shared runtime state through `OrchestrationRunContext` instead of expanding flow signatures one keyword at a time.
- Record major phase transitions with `telemetry.py` so checkpoint resumes can explain what happened before a failure or retry.
