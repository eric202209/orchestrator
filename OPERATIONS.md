# Operations Guide

Operator-facing runbook: startup, shutdown, backup, restore, health checks,
and the WF-B browser-session workflow. For installation, see `SETUP.md`.

## Startup

Run native services from the development checkout:

```bash
./start.sh
```

The native launcher starts Redis, local Qdrant when `qdrant/bin/qdrant` is
present, FastAPI, one Celery worker, Celery Beat, and Vite. Without the local
Qdrant executable, the app can start with knowledge retrieval degraded. The
launcher checks its own saved process identities first. If verified processes
from this checkout are running, an interactive terminal offers to stop and
restart them. Declining cancels startup. A non-interactive start refuses to
replace a running topology and prints `./stop_all.sh` as the next step.

A saved identity that cannot be verified causes startup to refuse rather than
offer a restart. A process without a matching record, or one owned by another
checkout, is never adopted or signaled by this launcher. An occupied service
port causes startup to fail without signaling its owner. Celery Worker and
Beat do not reserve service ports, so confirm their owner through the owning
checkout or service supervisor before starting another topology; the current
checkout cannot safely restart an unverified process.

WSL2 profiles run the API, Worker, Redis, and Qdrant through Compose:

```bash
./wsl-start.sh --ollama --backend-only  # Windows-host Ollama
./wsl-start.sh --backend-only           # Windows llama.cpp
```

Both helpers also accept `--check` for validation without starting services.
The llama.cpp helper starts its configured Windows `llama-server.exe` when it
cannot reach the server. The Ollama helper checks the Windows Ollama endpoint.

## Native Process Identity and Shutdown

Use the checkout's own stop helper:

```bash
./stop_all.sh
```

It stops only services with identity records under this checkout's `run/`
directory. Before sending SIGTERM, the helper checks the recorded PID start
time, executable, full argument list, working directory, and canonical source
root. It rechecks identity after opening a Linux pidfd, which protects against
PID reuse. It waits up to 30 seconds by default and does not escalate to
SIGKILL. `PROJECT_PROCESS_STOP_TIMEOUT` changes the wait limit.

Services without identity records are skipped; legacy PID files without a
matching identity record cause a fail-closed error. A record that points to a
live process with invalid, stale, unreadable, or foreign-checkout identity is
also rejected without signaling. A record for a process that is confirmed gone
is retired without a signal. A process that does not exit after SIGTERM causes
a failure; there is no forced-kill fallback. The sweep may already have
stopped earlier verified services before it encounters an identity error. Do
not remove identity records or signal a PID by command-line pattern as a
workaround.

For read-only inspection of one service identity, run this from the checkout:

```bash
python3 scripts/maintenance/project_process.py status \
  --root "$PWD" --service worker
```

Use `backend`, `worker`, `beat`, `frontend`, `qdrant`, or `redis` as the
service. A process from another checkout is not owned by this checkout's
helper. If a separate development checkout owns it, use that checkout's own
procedure; production has its own approved lifecycle.

The native launcher also checks its service ports and does not signal a
process merely because it occupies one. Defaults are Redis `6379`, Qdrant
`6333`, API `8080` (`BACKEND_PORT` can change it), and Vite `3000`. Inspect a
conflict read-only with `ss -ltnp`; determine its checkout or supervisor
before deciding what to do. An unverified owner remains untouched.

## WSL2 and Docker Lifecycle

Run Compose commands from the intended checkout and use the matching file:

```bash
docker compose -f docker-compose.windows.yml ps
docker compose -f docker-compose.windows.yml logs --tail=100
docker compose -f docker-compose.windows.yml down
```

`down` stops this Compose project and preserves its host-bound SQLite database
and named Qdrant volume. Adding `-v` deletes the Qdrant volume. Compose does
not stop the Windows Ollama or llama.cpp process, nor a Vite server running in
WSL. For predictable frontend shutdown, start Orchestrator with
`--backend-only`/`--no-frontend` and run Vite in a foreground terminal; stop
that terminal with Ctrl+C. The WSL helpers' optional detached Vite process is
not recorded with the native checkout identity format, so do not signal a PID
based only on `/tmp/frontend.pid` or `logs/frontend.pid`.

A Docker port conflict causes Compose startup to fail; it does not terminate
the host process holding the port. Published ports are `8080`, `6379`, and
`6333`; optional Vite uses `3000`. The model services use Windows-host ports
`11434` (Ollama) and `8001` (llama.cpp). `wsl --shutdown` stops every running
WSL distribution and can interrupt other projects; it is not the normal
Orchestrator shutdown command.

## Development and Production Separation

Use this checkout for development. The existing GX10 production checkout is
`/root/.openclaw/workspace/vault/orchestrator-production`, with its separate
runtime root at `/root/.openclaw/workspace/vault/orchestrator-production-runtime`.
These paths describe the existing deployment only; new installations choose
their own layout. Do not run development startup or shutdown commands against
production, and do not infer a universal production deployment procedure from
these development helpers.

`scripts/maintenance/dogfood_deploy.sh` is a specialized native dogfood
routine. It refuses the exact production checkout, requires a clean committed
worktree, stops only verified processes from its own checkout, starts the
native stack non-interactively, and runs its admission check. It is not a
production release workflow and does not apply to the WSL Docker profiles.

The process manager's OS identity records say which local process belongs to a
checkout; they are not the protected Worker execution attestation. The
one-shot execution gate defaults to disabled. The real isolated OCI
Executor is not implemented or production-ready. For the existing GX10
runtime, Worker identity remains `IDENTITY_INCONCLUSIVE / PIN_ABSENT` and
one-shot execution remains disabled.

## Backup Surface and Consistency

Choose a backup destination outside the live checkout and runtime, preferably
on a separate device or backup service. Keep `.env` and any workspace data
that must be recoverable in access-controlled storage. Do not commit secrets.

| Data | Recommended capture |
|---|---|
| `orchestrator.db` | SQLite Online Backup API while running, or a cold copy after all writers are stopped |
| Qdrant collections | Create and download Qdrant snapshots; keep the snapshot files outside live Qdrant storage |
| `knowledge/` | Copy source documents if they are not otherwise versioned |
| `.env` | Encrypted or otherwise access-controlled copy; never place it in a public backup |
| Project workspaces | Back up separately when they contain user work that must be recovered |
| `.agent/events/` journals | Include only when the deployment's evidence-retention policy requires them |

Do not include the Planner Relay browser-session profile in routine application
backups. It contains a logged-in browser session and must be handled as
credential material if an operator explicitly needs to migrate it.

Do not copy only `orchestrator.db` while the API or Worker is writing in WAL
mode. Use SQLite's Online Backup API for a live database. For a cold file copy,
stop native services with this checkout's `./stop_all.sh`, or stop only the
matching WSL Compose project with `docker compose -f docker-compose.windows.yml down`;
use the production deployment's own approved lifecycle for production. Capture
the cold copy only after shutdown completes cleanly, including any present
`-wal` and `-shm` sidecars as one set. Never delete live sidecars to make a
copy appear clean. Verify every captured database with `PRAGMA integrity_check`
(`ok`) and an empty `PRAGMA foreign_key_check` result.

SQLite and Qdrant do not share an atomic snapshot boundary. If the backup must
represent one consistent application point, quiesce ingestion and other writes
while capturing both systems and record the capture times.

### Qdrant Snapshots

The local and WSL deployments are single-node Qdrant instances. List the
collections, create one snapshot per collection, then download each snapshot
using the name returned by Qdrant:

```bash
curl -fsS http://127.0.0.1:6333/collections | python3 -m json.tool
BACKUP_DIR=/path/to/backup
mkdir -p "$BACKUP_DIR"
COLLECTION='<collection-name>'
curl -fsS -X POST \
  "http://127.0.0.1:6333/collections/$COLLECTION/snapshots" \
  | python3 -m json.tool
SNAPSHOT='<snapshot-name-returned-above>'
curl -fsS \
  "http://127.0.0.1:6333/collections/$COLLECTION/snapshots/$SNAPSHOT" \
  -o "$BACKUP_DIR/$SNAPSHOT"
sha256sum "$BACKUP_DIR/$SNAPSHOT"
```

Repeat for every collection. Add the configured Qdrant authentication header
if the deployment enables API authentication. Collection snapshots omit
aliases. Test restoration in a separate Qdrant instance before relying on a
snapshot; Qdrant documents restore compatibility for the same minor version
or the next minor version in its [snapshot guide](https://qdrant.tech/documentation/operations/snapshots/).

### Existing Dogfood Backup Helper

`scripts/maintenance/dogfood_backup.py` is a manual dogfood helper, not a
scheduled or general-purpose deployment backup. It uses SQLite's online
backup API and archives the registered project event journals; it does not
capture Qdrant, `.env`, or whole project workspaces. Its default backup root
is `/root/.openclaw/workspace/vault/backups/orchestrator`, a deployment-specific
path. The supported `--db`, `--backup-root`, and `--retention-days` options
allow explicit paths, and its retention step prunes older runs. Review those
values before using it for an existing dogfood deployment.

```bash
PYTHONPATH=. venv/bin/python scripts/maintenance/dogfood_backup.py \
  --db /path/to/orchestrator.db \
  --backup-root /path/to/backup-root \
  --retention-days 14
```

The matching `dogfood_restore.py` helper restores into a required scratch
`--target-dir` and verifies SQLite integrity and journal archive completeness.
It is a drill tool; never point it at the live runtime. Set `--backup-root`
explicitly when the run is not under the helper's default path.

### A2 Backup Preflight and GX10 Defaults

`scripts/maintenance/backup_preflight.py` is a read-only dry-run. It measures
candidate paths and can probe SQLite/Redis state; it never copies data or
performs a restore. For the existing GX10 deployment, its default production
root is `/root/.openclaw/workspace/vault/orchestrator-production` and its
default runtime root is
`/root/.openclaw/workspace/vault/orchestrator-production-runtime`. Those are
not required paths for new deployments.

The verified explicit options are `--production-root`,
`--production-runtime-root`, `--destination`, `--restore-target`, and
`--probe`. For a different deployment, supply both roots explicitly:

```bash
venv/bin/python scripts/maintenance/backup_preflight.py \
  --production-root /path/to/production-checkout \
  --production-runtime-root /path/to/production-runtime \
  --destination /path/to/new-backup \
  --restore-target /path/to/empty-restore-drill \
  --probe
```

Review its path, size, device, SQLite, and queue findings before any separately
authorized capture or restore procedure. The preflight itself is not that
procedure.

## Restore and Restore Verification

Treat restore as a deployment-specific maintenance operation with a selected
root, downtime window, verified backup, and rollback copy. Do not restore into
a running database or Qdrant store, and do not use the development checkout as
an ambiguous restore target.

Restore a SQLite Online Backup API output as the database file only after all
connections to the target have stopped. If using a cold copy instead, preserve
the database and sidecars from that same capture together. Restore Qdrant
snapshots into a separate instance first, using a compatible Qdrant version;
verify collection names, point counts, payload/vector access, and any required
aliases before planning a live replacement. Restore `knowledge/`, workspace
content, and `.env` from their own verified copies as needed.

Before considering the restore usable, run `PRAGMA integrity_check`, require
an empty `PRAGMA foreign_key_check`, confirm migrations on startup, check
`/health` and authenticated `/api/v1/ops/health`, then verify representative
application records and Qdrant collections. A2 only checks proposed paths; it
does not automate these steps or authorize a production restore.

## Post-Install Smoke Check

The unauthenticated endpoint gives a quick API check:

```bash
curl -fsS http://127.0.0.1:8080/health
```

`/api/v1/ops/health` and `/api/v1/ops/build-identity` require an admin bearer
token. `/api/v1/ops/health` reports database, Redis, Qdrant, Celery, and
maintenance status; public `/health` reports API, database, Redis, and backend
checks plus this API process's database-pool details. Check the matching native
logs under `logs/`; for WSL use `docker compose -f docker-compose.windows.yml logs`.

For mobile connectivity, use the dashboard session cookie to check
`/api/v1/mobile-admin/connection-info`; `api_key_configured` must be `true`
for mobile REST endpoints to be available.

## Post-Upgrade Checks

First identify the checkout and its intended runtime. For native Linux,
`start.sh` uses source from that checkout but only creates a missing virtual
environment or frontend dependency directory; it does not reinstall updated
dependencies. If manifests changed, update them explicitly, then stop and
restart the same checkout with `./stop_all.sh` and `./start.sh`.

For WSL Docker, rebuild and recreate only the selected Compose project. The
Ollama launcher supports:

```bash
./wsl-start.sh --ollama --build --force-recreate --no-frontend
```

The default llama.cpp launcher does not accept `--build`; use Compose from the
intended WSL checkout:

```bash
docker compose -f docker-compose.windows.yml build
docker compose -f docker-compose.windows.yml up -d --force-recreate
```

Check `/health`, authenticated `/api/v1/ops/health`, and build identity after
startup. Review the release's focused validation results before relying on
new behavior. If Python source changed in Docker, verify that the rebuilt
containers use the expected commit; if `stale_container_check` is `unknown`,
the image lacks enough metadata to prove freshness. Schema changes use the
application's startup migration runner; Alembic is not used by this repo.

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

**Security (WF-C hardened):** noVNC (6080) and the host CDP port (9222) bind
to `127.0.0.1` by default via `NOVNC_BIND_HOST` / `CDP_BIND_HOST` in `.env`
(see `.env.example`). Compose maps host 9222 to the container relay on 9223;
that relay forwards to Chrome's container port 9222. Both bind hosts default
to `127.0.0.1`.
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

SQLite runs in WAL mode with a 30-second busy timeout. WAL allows readers to
continue while a writer commits, but SQLite still serializes writers. The API
process and each Celery worker process have separate SQLAlchemy pools; defaults
are `DB_POOL_SIZE=10` and `DB_MAX_OVERFLOW=20`. The native launcher starts the
Worker with concurrency 4 by default (`CELERY_WORKER_CONCURRENCY` can lower or
raise that value). The WSL Compose Worker is fixed at concurrency 1.

If requests slow down or database operations time out:

1. Check `http://127.0.0.1:8080/health`. Its `details.database_pool` values
   describe only the API process that answered; each Worker has its own pool.
   The authenticated `/api/v1/ops/health` reports component availability but
   does not report pool counters.
2. Inspect `logs/backend.log`, `logs/worker.log`, and `logs/beat.log` for native
   startup or task errors. For WSL, inspect service logs and state with
   `docker compose -f docker-compose.windows.yml logs` and
   `docker compose -f docker-compose.windows.yml ps`.
3. Avoid increasing task concurrency as a first response. For native installs,
   lower `CELERY_WORKER_CONCURRENCY` in `.env` if the host or SQLite file cannot
   sustain the current level. The Compose Worker remains at 1 unless the
   deployment's Compose configuration is deliberately changed.
4. If a native restart is needed, first allow active work to finish where
   possible, then run `./stop_all.sh` from that checkout. If a task needs a
   longer graceful shutdown window, set
   `PROJECT_PROCESS_STOP_TIMEOUT=600 ./stop_all.sh`. The helper uses SIGTERM
   and never sends SIGKILL; a timeout leaves the process identity available for
   inspection and returns an error.
5. If process identity validation fails, stop and preserve the error details.
   Do not use process-name-wide termination, forced killing, or a PID copied
   from `ps` as a workaround.
   Inspect the checkout root, saved identity, start time, executable, argv, and
   working directory; if ownership remains ambiguous, leave the process
   untouched and use its actual service supervisor or operator.
6. For WSL, use Compose from the matching checkout. `docker compose ... down`
   performs the container lifecycle for that Compose project; it does not
   signal host services or another checkout. Never remove live SQLite WAL or
   SHM sidecars to clear a lock.

This is operational guidance, not a guarantee that any level of concurrent
SQLite writes will succeed. Quiesce writes and use a verified backup before a
restore or database repair.

## Evidence Collection

Validator and recovery telemetry evidence is written per-project-workspace
under `.agent/events/session_{session_id}_task_{task_id}.jsonl`. This is
diagnostic evidence, not control state — the database remains authoritative
for orchestration behavior — but it is the only record of fine-grained
validator/recovery decisions, and nothing currently enforces its retention.
To collect and summarize retained evidence:

```bash
PYTHONPATH=. venv/bin/python scripts/session_and_replay/phase18e_collect_real_session_validator_evidence.py --db orchestrator.db
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
