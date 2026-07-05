# Operations Guide

Operator-facing runbook: startup, shutdown, backup, restore, health checks,
and the WF-B browser-session workflow. For installation, see `SETUP.md`.

## Startup

Linux/Ubuntu (native, with OpenClaw):

```bash
./start.sh
```

Starts Redis, Qdrant, the FastAPI backend (uvicorn), Celery workers, and the
React dev server, in that order. If existing `uvicorn`/`vite`/`celery`
processes are detected, it interactively asks whether to stop and restart
them (non-interactive/CI runs leave them running and warn).

Windows/WSL2 (Docker, llama.cpp — no OpenClaw):

```bash
./wsl-start.sh
```

Windows/WSL2 (Docker, Ollama — no OpenClaw, compact laptop profile):

```bash
./wsl-start.sh --ollama
```

Use `--check` on either wsl variant to validate setup without starting
services.

## Shutdown

Native (`start.sh`): re-run `./start.sh` and answer `y` at the "Existing
processes detected" prompt, or manually:

```bash
pkill -f "uvicorn app.main:app"
pkill -f "celery"
pkill -f "vite"
```

Docker/WSL: `docker compose -f docker-compose.windows.yml down` (adjust
compose file to the profile in use).

WF-B browser-session container (separate from the Orchestrator runtime — see
"Browser-Session / WF-B" below):

```bash
docker compose -f docker-compose.browser-session.yml down
```

## Backup Surface

Back up these paths. Everything else is regenerable from source + these:

| Path | Contents | Notes |
|---|---|---|
| `orchestrator.db` | SQLite control state (sessions, tasks, plans, checkpoints, audit) | This database is the authoritative source of truth for all control state. Orchestration event journals under `.agent/events/` (see below) are diagnostic evidence, not control state — losing them does not corrupt orchestration behavior |
| `qdrant/data` | Vector index for knowledge retrieval | Rebuildable from `orchestrator.db` `KnowledgeItem` records via the ingest script, but back up to avoid a slow rebuild |
| `knowledge/` | Source knowledge documents | Ingest source |
| `.env` | Local configuration and secrets | Never commit; back up outside the repo |

**Never back up** (excluded on purpose):
- the WF-B browser-session Docker volume (`browser-session-profile`) —
  contains a live, authenticated ChatGPT session; treat it as durable
  credential material, not application data, and exclude it from any backup
  that leaves the machine
- `logs/`, `checkpoints/`, `run/`, `qdrant/bin`/`qdrant/snapshots` (runtime
  state, regenerable)
- `.agent/events/` journals under project workspaces (diagnostic evidence,
  not backup-critical — see "Evidence Collection" below)

## Restore

1. Stop all services (see Shutdown).
2. Restore `orchestrator.db`, `qdrant/data`, `knowledge/`, and `.env` from
   backup into the repository root.
3. Run `./start.sh` (or the appropriate `wsl-start.sh` variant) — it will
   detect the existing venv/frontend deps and skip re-provisioning them.
4. Run the post-install smoke checklist below to confirm the restore.

## Post-Install / Post-Restore Smoke Checklist

```bash
curl -s http://localhost:8080/api/v1/ops/health | python3 -m json.tool
```

Expect `overall_status` healthy/degraded per component (`db`, `redis`,
`qdrant`, `celery`). A missing or `unhealthy` component points at a service
that did not start — check `logs/backend.log`, `logs/worker.log`,
`logs/qdrant.log`.

```bash
curl -s http://localhost:8080/api/v1/ops/build-identity
```

Confirms which build/commit is actually running. The Docker image copies
`app/` at build time, so rebuild the `orchestrator` and `celery_worker`
images explicitly after code changes before trusting anything the running
system reports as proof of the latest behavior — a stale image will silently
keep running old code.

If mobile (ClawMobile) will connect, verify the mobile gateway key is
configured:

```bash
curl -s -H "Cookie: <dashboard session cookie>" \
  http://localhost:8080/api/v1/mobile-admin/connection-info
```

`api_key_configured` must be `true`, or mobile REST endpoints will return
`503`.

## Post-Upgrade Verification

After pulling new code or rebuilding a Docker image:

1. `./start.sh` (or the Docker equivalent) — rebuilds the venv/frontend deps
   if `requirements.txt`/`package.json` changed.
2. Confirm `ops/build-identity` reflects the new commit (Docker images copy
   `app/` at build time — rebuild `orchestrator` and `celery_worker` images
   explicitly if using Docker; a stale image will silently keep running old
   code).
3. Run the certification test pack before treating any evidence produced
   after the upgrade as trustworthy:

   ```bash
   PYTHONPATH=. venv/bin/python -m pytest \
     app/tests/test_candidate_audit.py app/tests/test_candidate_generation.py \
     app/tests/test_candidate_machine_profiles.py app/tests/test_candidate_operator_policy.py \
     app/tests/test_candidate_outcome.py app/tests/test_candidate_recovery_infrastructure.py \
     app/tests/test_candidate_registry_runtime.py app/tests/test_candidate_rollout.py \
     app/tests/test_candidate_selection_policy.py app/tests/test_candidate_selection_runtime.py \
     app/tests/test_candidate_validation.py app/tests/test_direct_ollama_compact_retry_candidate_prompt.py \
     app/tests/test_execution_recovery_delegation.py app/tests/test_execution_recovery_s2.py \
     app/tests/test_execution_recovery_s25.py app/tests/test_execution_recovery_s3.py \
     app/tests/test_execution_recovery_s4.py app/tests/test_execution_recovery_service.py \
     app/tests/test_phase10j_audit_api.py app/tests/test_plan_candidate.py \
     app/tests/test_planner_recovery_regressions.py app/tests/test_recovery_context.py \
     app/tests/test_recovery_inspection_report.py app/tests/test_recovery_lifecycle.py \
     app/tests/test_recovery_ops_summary.py app/tests/test_recovery_outcome.py \
     app/tests/test_recovery_policy.py app/tests/test_recovery_strategy_registry.py \
     app/tests/test_reflection_validation.py app/tests/test_slot_merge_audit.py \
     app/tests/test_slot_merge_validation.py app/tests/test_startup_config_validation.py \
     app/tests/test_validator_rule_telemetry.py app/tests/test_validator_runtime_evidence.py \
     -q
   ```

   This is the fast, targeted subset covering Candidate Recovery, Slot
   Merge, execution recovery, the recovery policy/strategy registry, and
   validator rule telemetry — the tests that rollout decisions actually
   depend on. It is a pre-check, not a replacement for the full suite
   (`PYTHONPATH=. venv/bin/python -m pytest app/tests/ -q`), which should
   still run at every release that touches `app/`.
4. If the upgrade touched `app/db_migrations.py`, confirm the new
   `_migration_0NN_*` function ran: check the `schema_migrations` table for
   the new version, or watch `logs/backend.log` startup output. Do not run
   `alembic upgrade head` — the Alembic directory in this repo is historical
   and not wired into startup; see `alembic/README`.

## Browser-Session / WF-B

The Planner Relay browser-session container provides a persistent, logged-in
Chromium instance that the Planner Relay script pastes prompts into and
reads responses from. It is separate infrastructure from the Orchestrator
runtime — if the relay script crashes, the browser and login session
survive.

```bash
docker compose -f docker-compose.browser-session.yml up -d
```

Health checks:

```bash
curl http://127.0.0.1:9222/json/version   # Chrome DevTools Protocol; must return JSON
```

`http://localhost:6080` must display the container desktop (noVNC). First
login to ChatGPT is manual, once, via noVNC — never automate it. If Chrome
reports a locked profile after an unclean restart, remove the
`SingletonLock`/`SingletonSocket`/`SingletonCookie` files from the persistent
profile volume before restarting the container.

**Security note:** the compose file currently publishes noVNC (6080) and CDP
(9223→9222) on all host interfaces, and the VNC server runs without a
password. CDP access is equivalent to full control of the authenticated
ChatGPT session in that browser. Do not expose this container's ports beyond
a trusted LAN or tailnet, and do not put it behind a public reverse proxy
until it is bound to loopback/tailnet-only interfaces.

Run the relay:

```bash
scripts/relay/run_planner_relay.sh
```

Always confirm `Send? [y/N]` manually — this gate is permanent by design,
not a temporary limitation. The relay has no conversation-target pinning
today: it pastes into whichever ChatGPT tab is currently active in that
browser, so confirm the correct conversation is focused before approving
Send.

## Database Concurrency / SQLite Locking

The control-plane database is SQLite (`orchestrator.db`), in `journal_mode=WAL`
(set on every connection via `app/database.py`'s pragma listener). Each
process — the `uvicorn` API and every Celery worker process — opens its own
SQLAlchemy connection pool (`pool_size=10, max_overflow=20` by default,
configurable via `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` in `.env`) onto the same
file; WAL lets readers proceed while a writer commits, and `busy_timeout=30000`
makes a connection wait up to 30s for a lock before raising, instead of
failing immediately.

**Known risk (Phase 18L-R):** under concurrent load, an unbounded Celery
worker pool (Celery defaults `--concurrency` to one prefork process per CPU
core) combined with SQLite's default `journal_mode=delete` caused the API to
become completely unresponsive — every connection blocked on file-level
locks until each process's pool (`size 5 + overflow 10 = 15`) was exhausted,
surfacing as `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5
overflow 10 reached, connection timed out`. Recovery required killing and
restarting the wedged `uvicorn` process.

**Phase 19A mitigations:**
- WAL mode (Phase 18N) removes the primary lock-contention path between
  concurrent readers and a writer.
- `start.sh` now caps Celery worker concurrency at 4 processes by default
  (override with `CELERY_WORKER_CONCURRENCY` in `.env`) instead of Celery's
  default of one process per CPU core — on a 20-core host this previously
  spawned ~20 worker processes, each holding its own pool against the same
  file for the full duration of whatever orchestration task it was running.
- Three Celery maintenance tasks (`process_github_webhook`,
  `scheduled_task_execution`, `cleanup_old_logs` in
  `app/tasks/maintenance.py`) only released their DB session on the success
  path; an exception before that point leaked the connection out of the
  pool for the rest of the process's life. Fixed to close deterministically
  in a `finally` block, matching the pattern already used elsewhere in that
  file.

**If the API wedges again** (health check hangs, backend log shows
`QueuePool limit ... reached, connection timed out`):

1. Check `GET /api/v1/ops/health` — `details.database_pool` reports this
   process's own pool state (`size`, `checked_out`, `overflow`,
   `checked_in`; each process has a separate pool, so this only reflects
   whichever process answers the request).
2. If the API itself is unresponsive to `/health`, a graceful `SIGTERM` may
   also hang (it did during the Phase 18L-R incident) — `SIGKILL` the
   `uvicorn` process and restart it (`pkill -9 -f "uvicorn app.main:app"`,
   then `./start.sh`). This does not corrupt `orchestrator.db`; WAL mode is
   crash-safe.
3. Confirm no orphaned worker processes remain (`ps aux | grep celery`);
   `pkill -f "celery -A app.celery_app worker"` if needed, then restart.
4. This is an operational mitigation for a known SQLite-under-load
   limitation, not a guarantee that pool exhaustion cannot recur under
   heavier concurrency than has been verified — see
   `docs/roadmap/done/phase19/phase19a-db-pool-sqlite-concurrency-hardening-report.md`.

## Evidence Collection

Validator and recovery telemetry evidence is written per-project-workspace
under `.agent/events/session_{session_id}_task_{task_id}.jsonl`. This is
diagnostic evidence, not control state — the database remains authoritative
for orchestration behavior — but it is the only record of fine-grained
validator/recovery decisions, and nothing currently enforces its retention.
To collect and summarize retained evidence:

```bash
python scripts/phase18e_collect_real_session_validator_evidence.py --db orchestrator.db
```

Run this after every evidence-gated validation batch (Candidate Recovery,
Slot Merge) before the source project workspace is archived or cleaned, and
keep a copy of any journal files a validation report cites — once a task
workspace is cleaned up, an unretained journal is gone.

## Repository Directory Hygiene

The working directory contains live runtime state that must never be
committed: `qdrant/`, `logs/`, `checkpoints/`, `run/`, `graphify-out/`,
`frontend/test-results/`, `.pytest_cache/`. These are gitignored; verify with
`git status` before any bulk `git add`.
