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
   `alembic upgrade head` — Alembic is not used by this repo; the legacy
   Alembic migration tree was removed in Phase 20C.

## Browser-Session / WF-B / WF-C / WF-D

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

**Security (WF-C hardened):** noVNC (6080) and CDP (9223→9222) now bind to
`127.0.0.1` on the host by default — set via `NOVNC_BIND_HOST` /
`CDP_BIND_HOST` in `.env` (see `.env.example`), both default `127.0.0.1`.
Neither port has its own authentication (the VNC server still runs without a
password; CDP has none by design), so both are equivalent to full control of
the authenticated ChatGPT session — loopback binding is the actual security
boundary. To reach them from another machine, use an SSH tunnel:

```bash
ssh -L 6080:localhost:6080 -L 9222:localhost:9222 user@host
```

Only widen `NOVNC_BIND_HOST`/`CDP_BIND_HOST` to `0.0.0.0` if you understand
and accept exposing full session control to that interface, and never on a
machine reachable from an untrusted network.

**Conversation pinning (WF-C):** set `RELAY_EXPECTED_CONVERSATION_URL` in
`.env` to the exact ChatGPT conversation URL the relay should send into. If
set, the relay verifies the browser's current tab matches before pasting or
sending, and aborts with a clear message on any mismatch — it never switches
tabs itself. Leave it blank only when deliberately bootstrapping a new
conversation.

**Preflight (WF-C):** `scripts/relay/check_relay.sh` checks the container,
noVNC, CDP, expected conversation, login state, relay directories, selectors
file, and file writability, printing PASS/FAIL with diagnostics.
`run_planner_relay.sh` runs it automatically and refuses to start the relay
on FAIL; run it standalone any time to diagnose without sending anything.

**Metrics (WF-C):** every relay run appends one JSON line per event to
`relay/metrics.jsonl` (start, completion with elapsed time and outcome —
success, selector failure, URL mismatch, login expired, browser unavailable,
operator/relay cancelled, or unexpected error). Append-only, no database;
inspect with `tail -f relay/metrics.jsonl` or `jq`.

Run the relay:

```bash
scripts/relay/run_planner_relay.sh
```

Always confirm `Send? [y/N]` manually — this gate is permanent by design,
not a temporary limitation.

**Workflow Recovery (WF-D):** the relay is still stateless with respect to
Orchestrator and remains outside `app/`, but it now writes minimal browser-run
metadata under `relay/` so an operator can recover from interruption without
manual archaeology:

- `relay/state.json` records the current relay phase (`waiting_send`,
  `waiting_response`, or `extracting_response`) plus timestamp and URL.
- `relay/session_snapshot.json` records the conversation URL, title, assistant
  message count, and timestamp before Send and after response extraction.
- `relay/relay.lock` prevents concurrent relay executions.

Resume procedure:

```bash
scripts/relay/run_planner_relay.sh --resume
```

Resume never sends automatically. If the saved phase is `waiting_send`, the
operator still gets the normal `Send? [y/N]` gate. If the saved phase is
`waiting_response` or `extracting_response`, the relay asks only whether to
continue waiting/extracting the current assistant response; it does not click
Send.

**Replay Package:** create a deterministic local bundle for bug reports,
workflow debugging, or future regression tests:

```bash
scripts/relay/run_planner_relay.sh --bundle
```

The archive is written to `relay/replay/replay-YYYYMMDD-HHMMSS.zip` and
contains `input.md`, `output.md`, `relay.log`, `metrics.jsonl`,
`session_snapshot.json`, and `state.json` when present. There is no cloud
upload.

**Lock Recovery:** a second relay instance refuses to run and prints the PID
and timestamp from `relay/relay.lock`. Normal exits and `SIGINT`/`SIGTERM`
remove the lock automatically. If a machine crash leaves a lock behind, rerun
the relay normally; it removes stale locks when the PID no longer exists or
when the lock is older than `RELAY_LOCK_STALE_AFTER_S` (default 21600 seconds).
Only delete `relay/relay.lock` manually after confirming the recorded PID is
not an active relay process.

**Relay Permission Recovery:** if `relay/metrics.jsonl` (or another relay
runtime file) stops being writable — typically after a run as a different
user (e.g. root) leaves files owned outside the operator's group — fix it by
restoring the group-writable/setgid pattern rather than loosening
permissions to world-writable:

```bash
chgrp -R ubuntu relay/
chmod 2775 relay/
chmod 664 relay/metrics.jsonl relay/relay.log relay/output.md \
  relay/session_snapshot.json 2>/dev/null
```

`chmod 2775` sets the setgid bit on `relay/` so new files created by any
future run automatically inherit group `ubuntu`, preventing the ownership
drift from recurring. Confirm with `stat -c "%A %U:%G %n" relay relay/*` —
`relay/` should show `drwxrwsr-x` and runtime files should show
`rw-rw-r--`, both owned or grouped `ubuntu`. Never leave relay runtime files
group- or world-unwritable, and never resolve this by running the relay as
root.

**Failure Classification:** relay failures are standardized in both
`relay/relay.log` and `relay/metrics.jsonl` with `failure_reason` /
`failure_category`:

- `browser_unavailable`
- `login_expired`
- `url_mismatch`
- `selector_failure`
- `operator_cancelled`
- `relay_cancelled`
- `response_timeout`
- `browser_reload`
- `conversation_changed`
- `unexpected_error`

Selector failures print a concise diagnostic block for the input selector,
send button, streaming indicator, and assistant response. Stack traces are
suppressed by default; pass `--verbose` to include expanded exception details.

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
python scripts/session_and_replay/phase18e_collect_real_session_validator_evidence.py --db orchestrator.db
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
