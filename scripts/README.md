# Scripts

Operational scripts kept here are project-specific helpers. Runtime startup is
handled by `../start.sh`; service logs are written directly to `../logs/`.

## Session And Replay Inspection

- `capture_replay_report.py` - capture semantic replay reports from event journals.
- `capture_task_evidence_bundle.py` - capture a stable per-TaskExecution evidence bundle.
- `phase6b_evidence_report.py` - collect session/task evidence across logs, replay, and endpoints.
- `inspect_session_state.py` - inspect one session in SQLite.
- `inspect_task_execution_attempts.py` - inspect task execution attempts.
- `inspect_event_journal.py` - inspect orchestration event journals.
- `inspect_runtime_logs.py` - inspect runtime log identity coverage.
- `inspect_checkpoints.py` - inspect checkpoint contents.
- `diagnose_planning_stuck.py` - diagnose planning/session stalls.
- `session_outcome_report.py` - summarize recent session outcomes.

## Planning And Knowledge

- `validate_plan_json.py` - validate planner JSON against the deterministic plan contract.
- `planning_floor_check.py` - run an OpenClaw planning-floor diagnostic.
- `phase8a_shadow_probe.py` - offline direct-vs-OpenClaw repair prompt probe
  for Phase 8A runtime boundary diagnostics.
- `ingest_knowledge.py` - ingest knowledge documents into SQLite and Qdrant.
  For the compact laptop Ollama Docker runtime, prefer
  `./wsl-start.sh --ollama --ingest-knowledge`, or:
  `docker compose -f docker-compose.windows.yml exec -T orchestrator python scripts/ingest_knowledge.py --source-dir /app --qdrant-url http://qdrant:6333`.

## Developer Utilities

- `format-python.sh` - format backend Python files with Black.
- `security_check.sh` - scan tracked source-like files for likely secret exposure.
- `orchestrator-mobile-api.sh` - call mobile API endpoints using local env credentials.
- `kill-all.sh` - force-kill local development processes.

Removed obsolete scripts: old `/tmp` log sync/status/cleanup helpers. Current
startup no longer writes logs to `/tmp`.
