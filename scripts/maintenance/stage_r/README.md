# Stage R Gate 1 and Gate 2 tooling

These scripts prepare checks for one later, operator-approved pinned Stage R
run. They do not start services or authorize a dogfood task. The Gate 1 and
Gate 2 checkers' Git, `/proc`, SQLite, Redis, pin, worksheet, and manifest
operations are reads. Those checkers never call `celery inspect`; standalone
`celery inspect` commands remain prohibited for the read-only gate because
Celery publishes control and reply traffic through Redis. A narrow,
separately approved exception is documented below for
`task_snapshot_adapter.py` only. Without a fresh, complete task snapshot, the
task-state check is `INCONCLUSIVE`.

The Gate checkers print JSON with stable check IDs and reason codes. Check status is
`PASS`, `FAIL`, or `INCONCLUSIVE`. Overall status precedence is FAIL, then
INCONCLUSIVE, then PASS. Exit codes are 0, 1, and 2 respectively. Reports
include digest values and changed field names, never pin bodies, approval
values, Redis credentials, task payloads, or protected-state digest values.

Both tools accept only the Orchestrator checkout containing the tooling as
`--source-root`; preflight also accepts only that checkout's `venv`. ProductRoot
is never traversed. Its identity is supplied only as a digest field in the
protected-state input files.

## Operator prerequisites

Before Gate 1, the operator must have:

- A reviewed, clean, committed source SHA and tree SHA approved for this run.
- A separately approved restart window and a verified current container ID.
- A verified host-side backup of `/root/.orchestrator/runtime` as described
  below. A missing or unverified backup blocks Gate 1.
- An approved venv/Python baseline, operator-provided protected-state digest
  maps (including `product_root_identity_sha256`), and a fresh task-state
  snapshot.
- An approved worksheet and a production-schema candidate pin for that exact
  source, configuration, launcher context, and worker node identity.
- The exact database path, Redis URL, runtime root, and pin path for the
  current deployment. Do not infer host Docker settings from container-local
  observations.

The tools do not read ProductRoot. The operator supplies its approved and
observed identity digests. Do not pass a ProductRoot path to either tool.

## Bounded Celery task snapshot exception

`task_snapshot_adapter.py` collects `active`, `reserved`, and `scheduled`
through Celery 5.6.3's `app.control.inspect(timeout=5.0)` API. It makes each
call once; it does not retry. Celery publishes remote-control messages and
worker replies through Redis. This is read-only with respect to application
task state and reservations, but it is **not** broker-write-free. The normal
`celery inspect` prohibition above remains in effect; only this adapter has a
narrow exception, and it may run only after the operator separately approves
those Redis control/reply writes. The required
`--approve-redis-control-reply-writes` argument records that approval at the
invocation boundary. It does not authorize a restart, Gate 1, Gate 2, or
dogfood.

Before invocation, obtain every `--expected-worker-node` value from the
approved worksheet/pin for this exact runtime. Supply a
`--broker-topology-evidence-ref` that identifies operator evidence establishing
that the expected node set is the complete set of workers sharing the broker.
The adapter does not inspect the broker topology. Its complete local `/proc`
census must match the expected worker masters before and after collection, and
every Celery reply must have the exact expected worker set. Local process
inspection cannot detect a remote or off-boundary worker sharing the broker;
without external broker-topology evidence, the adapter returns
`INCONCLUSIVE` and writes no snapshot.

The snapshot uses the existing parser schema only:
`snapshot_schema_version: 1`, timezone-aware UTC `captured_at`, and list fields
`active`, `reserved`, and `scheduled`. The parser accepts ages from five
seconds in the future through 120 seconds old; use the capture within 120
seconds. Nonempty rows contain only a worker node and SHA-256 digests of the
task ID and task name. Arguments, keyword arguments, ETA, priority, and raw
task payloads are not persisted. Active inspection uses
Celery's `safe=True`; reserved and scheduled replies can still carry raw task
fields in memory and are sanitized before output.

Use a new output directory on a filesystem that supports Linux atomic
no-replace directory rename. The adapter creates the directory with mode
`0700`, both files with mode `0600`, and publishes the complete directory
atomically. It refuses an existing output path, symlink, incomplete census,
missing/extra/duplicate worker reply, malformed response, timeout, missing
category, or collection over 20 seconds. Failure prints only a stable
sanitized reason, exits 2, emits no snapshot, and does not retry. Keep the
snapshot and its separate evidence note together under the operator's
restricted evidence retention policy. The note records call timing/coverage,
the topology reference and its limitations, process census, collection
window, non-atomicity, and snapshot SHA-256. The preflight parser does not
validate that note.

The three calls are not atomic. Tasks can transition between categories;
schedule the capture inside an approved short no-submission window. Use the
bounded command below only after the explicit operator approval described
above. Set `EXPECTED_NODE` from the approved worksheet/pin,
`TOPOLOGY_REF` to the non-secret evidence reference, and `EVIDENCE` to an
operator-controlled directory. No automatic retry is performed.

```bash
set -euo pipefail
: "${EXPECTED_NODE:?set from the approved worksheet/pin}"
: "${TOPOLOGY_REF:?set to the non-secret topology evidence reference}"
: "${EVIDENCE:?set to the operator-controlled evidence directory}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$EVIDENCE/task-snapshot-$STAMP"
timeout --signal=TERM --kill-after=2s 25s \
  "$PWD/venv/bin/python3" scripts/maintenance/stage_r/task_snapshot_adapter.py \
  --expected-worker-node "$EXPECTED_NODE" \
  --broker-topology-evidence-ref "$TOPOLOGY_REF" \
  --approve-redis-control-reply-writes \
  --output-dir "$OUT"
```

The emitted Stage R input is `$OUT/task-snapshot.json`; retain
`$OUT/task-snapshot-evidence.json` beside it. The approval flag is an explicit
operator acknowledgement, not a grant of any restart or gate authorization.

## Required host-side evidence backup

The Phase 38/39 certification evidence and worker identity manifests are kept
under `/root/.orchestrator/runtime`. Preserve the whole directory outside the
container before any restart or migration. This procedure is for a later
operator-approved gate; it was not run while preparing these tools.

Run these commands on the Docker host. Set `CONTAINER_ID` only after checking
that it identifies the intended current container. `BACKUP_BASE` must be a
host directory on storage outside the container and with enough free space.

```bash
set -euo pipefail
CONTAINER_ID='<verified-current-container-id>'
BACKUP_BASE='/path/on/host/orchestrator-runtime-backups'
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_BASE/runtime-$STAMP"

test -d "$BACKUP_BASE"
test -w "$BACKUP_BASE"
test ! -e "$DEST"
mkdir -m 0700 "$DEST"
docker cp -a "$CONTAINER_ID:/root/.orchestrator/runtime" "$DEST/runtime"
test -d "$DEST/runtime"
test "$(find "$DEST/runtime" -type f | wc -l)" -gt 0

cd "$DEST"
LC_ALL=C find runtime -type f -printf '%P\n' | LC_ALL=C sort > FILES.txt
LC_ALL=C find runtime -type l -printf '%P -> %l\n' | LC_ALL=C sort > SYMLINKS.txt
find runtime -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum > SHA256SUMS
sha256sum -c SHA256SUMS
find runtime -type f -printf '%P\n' | LC_ALL=C sort | cmp - FILES.txt
sha256sum FILES.txt SYMLINKS.txt SHA256SUMS
```

Record the destination, regular-file count, `SHA256SUMS` digest, and the
successful inventory and `sha256sum -c` checks in an operator-owned
`backup-verification.json`. It must follow this shape:

```json
{
  "schema_version": 1,
  "backup_status": "VERIFIED",
  "source_path": "/root/.orchestrator/runtime",
  "destination_path": "/host/backup/runtime-YYYYMMDDTHHMMSSZ",
  "outside_container": true,
  "file_count": 1,
  "inventory_verified": true,
  "manifest_verified": true,
  "sha256_manifest_digest": "<64 lowercase hex characters>"
}
```

The sample count and digest are placeholders, not evidence of a completed
backup. Re-run the file inventory and checksum verification before relying on
the copy. Keep the original runtime untouched. For a restore, stop the stack
under separate approval, preserve the current runtime as another recovery
copy, restore into a staging location first, and verify its inventory and
hashes before an operator chooses whether to restore it. Never restore into
ProductRoot or overwrite runtime evidence while services are active.

## Pre-restart quiescence checks

Prepare JSON files outside ProductRoot:

- `runtime-baseline.json`: `{"runtime": {"venv_inventory_sha256": "…",
  "python_identity": {…}}}` copied from the approved baseline check. To
  capture a first candidate, run the checker with `--venv` and no baseline,
  review its `current_runtime` output offline, then place that object under a
  `runtime` key in this file. That capture is inconclusive until separately
  approved; it is not a Gate 1 pass.
- `task-snapshot.json`: exact schema and collection requirements described in
  [Bounded Celery task snapshot exception](#bounded-celery-task-snapshot-exception).
  Each task array must be empty for quiescence. The parser accepts up to 120
  seconds of age and five seconds in the future.
- `protected-approved.json` and `protected-observed.json`: matching maps from
  stable field names to 64-character SHA-256 digests. Include
  `product_root_identity_sha256`; the checker compares supplied values only.
- `backup-verification.json`: the host-verified backup receipt above, copied
  into the gate evidence area.

From the checkout, run the checker with the reviewed identities and exact
runtime paths. The Redis URL is read from `STAGE_R_REDIS_URL`; it is never
printed. Supply every configured queue name (repeat `--queue-name` once per
queue) and the approved pin UID, GID, and mode explicitly. Prior runtime
observations are not defaults for a later run.

```bash
export STAGE_R_REDIS_URL='redis://localhost:6379/0'
"$PWD/venv/bin/python3" scripts/maintenance/stage_r/preflight.py \
  --source-root "$PWD" \
  --approved-head "$APPROVED_HEAD" \
  --approved-tree "$APPROVED_TREE" \
  --runtime-baseline "$EVIDENCE/runtime-baseline.json" \
  --venv "$PWD/venv" \
  --queue-name "$QUEUE_NAME_1" \
  --task-snapshot "$EVIDENCE/task-snapshot.json" \
  --database "$DATABASE_PATH" \
  --runtime-root /root/.orchestrator/runtime \
  --pin "$PIN_PATH" \
  --pin-owner-uid "$PIN_OWNER_UID" \
  --pin-owner-gid "$PIN_OWNER_GID" \
  --pin-mode "$PIN_MODE_OCTAL" \
  --protected-approved "$EVIDENCE/protected-approved.json" \
  --protected-observed "$EVIDENCE/protected-observed.json" \
  --backup-verification "$EVIDENCE/backup-verification.json"
```

The tool checks Git HEAD/tree and cleanliness, agent/OpenClaw process presence,
duplicate worker/Beat masters, Redis queue length and backend lease keys,
active/reserved/scheduled task evidence, active sessions through SQLite
read-only mode, venv/Python drift, protected digests, pin metadata/schema, and
backup verification. It does not treat unavailable evidence as empty. Stop on
any FAIL or INCONCLUSIVE result. Archive `logs/*.log` to the host before
startup because `start.sh` truncates them.

## Log archival

Before `start.sh` can run, archive the current logs from the host and verify
the copy. Use a new empty host destination:

```bash
LOG_DEST="$BACKUP_BASE/logs-$STAMP"
test ! -e "$LOG_DEST"
mkdir -m 0700 "$LOG_DEST"
docker cp -a "$CONTAINER_ID:/root/.openclaw/workspace/vault/orchestrator/logs/." "$LOG_DEST/"
find "$LOG_DEST" -maxdepth 1 -type f -print
find "$LOG_DEST" -maxdepth 1 -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum > "$LOG_DEST/SHA256SUMS"
(cd "$LOG_DEST" && sha256sum -c SHA256SUMS)
```

If the log copy is empty, incomplete, or cannot be verified, stop and resolve
that before startup.

## Approved pin preparation and worksheet derivation

Derive the worksheet from the reviewed checkout using
`scripts/maintenance/worker_pin_worksheet.py` and the explicit
`start.sh:start_workers` launcher context. The helper outputs an
`NOT_APPROVED` worksheet; it does not create an approved pin. The operator
must approve the source/configuration fields and create a separate candidate
pin accepted by the production `load_candidate_identity_pin` parser.

Derive the worksheet twice in the same launch environment and compare the
files byte-for-byte. Record both SHA-256 values. Do not print or paste the
candidate pin body into logs. Install the approved pin under a new unique path
for this approval, with the UID, GID, and mode specified by the current
operator-approved pin policy and accepted by the production parser. The
previously observed root/`0600` metadata is not assumed to remain current. If
the new path already exists, stop; do not remove or silently replace a stale
pin. Keep prior pins as evidence.

Provision the unique file into the existing pin directory only after approval;
these host-side commands are examples for the later gate, not actions taken in
this mission:

```bash
PIN_PATH="/root/.orchestrator/worker-pin/candidate-pin-$APPROVAL_ID.json"
test -f "$APPROVED_PIN_FILE"
docker exec "$CONTAINER_ID" test -d /root/.orchestrator/worker-pin
docker exec "$CONTAINER_ID" test ! -e "$PIN_PATH"
docker cp "$APPROVED_PIN_FILE" "$CONTAINER_ID:$PIN_PATH"
docker exec "$CONTAINER_ID" chown "$PIN_OWNER_UID:$PIN_OWNER_GID" "$PIN_PATH"
docker exec "$CONTAINER_ID" chmod "$PIN_MODE_OCTAL" "$PIN_PATH"
docker exec "$CONTAINER_ID" stat -c '%u:%g %a' "$PIN_PATH"
```

Confirm the final stat output matches the approved UID, GID, and mode. If the
directory or target state differs, stop for operator review; do not create a
replacement path or remove old files automatically.

The worksheet context must name the actual worker interpreter, the
`venv/bin/celery` script, worker arguments (including the effective
`--concurrency=N`), checkout working directory, launcher label, and expected
`celery@<node>` identity. The pin and worksheet must describe the same
environment that `start.sh` will launch. Use the committed helper twice and
redirect output to files so the pin projection is not printed.

Build the context in the shell that will launch the stack. The `.env` lookup
prints only the worker concurrency value; it does not print other settings:

```bash
PROJECT_ROOT="$(cd -P "$PWD" && pwd -P)"
cd "$PROJECT_ROOT"
WORKER_SCRIPT="$PROJECT_ROOT/venv/bin/celery"
WORKER_INTERPRETER="$(sed -n '1s/^#!//' "$WORKER_SCRIPT")"
WORKER_CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-4}"
if [ -r .env ]; then
  FILE_WORKER_CONCURRENCY="$(awk -F= '
    {
      line=$0; sub(/\r$/, "", line); eq=index(line, "="); if (!eq) next
      key=substr(line, 1, eq-1); gsub(/^[ \t]+|[ \t]+$/, "", key)
      if (key == "CELERY_WORKER_CONCURRENCY") {
        value=substr(line, eq+1); gsub(/^[ \t]+|[ \t]+$/, "", value)
      }
    }
    END { print value }
  ' .env)"
  [ -z "$FILE_WORKER_CONCURRENCY" ] || WORKER_CONCURRENCY="$FILE_WORKER_CONCURRENCY"
fi
[[ "$WORKER_CONCURRENCY" =~ ^[1-9][0-9]{0,3}$ ]]
test -x "$WORKER_INTERPRETER"
WORKER_NODE_ID="celery@$(hostname)"
export PROJECT_ROOT WORKER_SCRIPT WORKER_INTERPRETER WORKER_CONCURRENCY WORKER_NODE_ID
umask 077
"$PROJECT_ROOT/venv/bin/python3" - "$CONTEXT" <<'PY'
import json
import os
import sys

value = {
    "context_schema_version": 1,
    "launcher": "start.sh:start_workers",
    "interpreter": os.environ["WORKER_INTERPRETER"],
    "script": os.environ["WORKER_SCRIPT"],
    "args": [
        "-A",
        "app.celery_app",
        "worker",
        f"--concurrency={os.environ['WORKER_CONCURRENCY']}",
        "--loglevel=info",
    ],
    "cwd": os.environ["PROJECT_ROOT"],
    "worker_node_identity": os.environ["WORKER_NODE_ID"],
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY
```

```bash
DERIVATION="$EVIDENCE/worksheet-derivation.json"
CONTEXT="$EVIDENCE/launcher-context.json"
"$PWD/venv/bin/python3" -m scripts.maintenance.worker_pin_worksheet \
  --source-root "$PWD" \
  --expected-head "$APPROVED_HEAD" \
  --expected-tree "$APPROVED_TREE" \
  --launcher-context "$CONTEXT" > "$DERIVATION.first.json"
"$PWD/venv/bin/python3" -m scripts.maintenance.worker_pin_worksheet \
  --source-root "$PWD" \
  --expected-head "$APPROVED_HEAD" \
  --expected-tree "$APPROVED_TREE" \
  --launcher-context "$CONTEXT" > "$DERIVATION.second.json"
cmp -s "$DERIVATION.first.json" "$DERIVATION.second.json"
sha256sum "$DERIVATION.first.json" "$DERIVATION.second.json"
cp "$DERIVATION.first.json" "$DERIVATION"
```

The operator edits a separate approved worksheet copy and authors the pin
from its `proposed_pin_body`, adding the production approval fields only to
the pin. Keep the raw `NOT_APPROVED` derivation so the post-restart
same-shell derivation can be compared byte-for-byte against it.

## Docker restart implications and post-restart Redis check

**A separately approved host-side `docker restart` is required.** It
terminates existing Codex and Claude sessions and every process in the
container. PID 1 then starts Redis and the OpenClaw gateway; it does not start
the API, worker, Beat, Qdrant, or frontend. Redis may restore queued messages
from its RDB file. Never purge Redis automatically.

After checking the container ID and receiving that separate approval, the
host operator issues:

```bash
docker restart "$CONTAINER_ID"
docker exec -it "$CONTAINER_ID" bash
```

The restart closes the existing interactive container shells as well. Use the
new shell only after the host command returns and the container identity is
reconfirmed.

After the approved restart, open one fresh interactive shell in the verified
container, export the new approved pin path there, and repeat the preflight
using a fresh task snapshot and post-restart Redis observation. The queue must
still be zero and every check must be PASS before startup. If the queue is
nonempty or unavailable, stop. Do not start the worker and do not purge.
Use the evidence files from an operator-controlled path that is verified to
be available in the container; keep the runtime backup itself on the host.

## Same-shell environment propagation and full-stack startup

In that same post-restart shell:

1. `cd` to the reviewed checkout and export
   `WORKER_CANDIDATE_PIN_PATH` with the new unique approved pin path.
2. Re-derive the worksheet with the helper from this same shell and launcher
   context. Compare it byte-for-byte with the saved raw `NOT_APPROVED`
   derivation. Stop on any difference. Do not generate a new approval inside
   this shell.
3. Archive logs if the host-side archival step has not already been verified.
4. Run `./start.sh` once from the stopped topology. It starts Qdrant, API,
   worker, Beat, frontend, and health checks in its existing sequence; PID 1
   has already started Redis and OpenClaw gateway. Watch the one startup
   result. Do not automatically retry a failed start or identity check.

The shell setup and final launch look like this; repeat the full post-restart
preflight command from the previous section before the worksheet comparison:

```bash
cd /root/.openclaw/workspace/vault/orchestrator
PIN_PATH="/root/.orchestrator/worker-pin/candidate-pin-$APPROVAL_ID.json"
test -f "$PIN_PATH"
if grep -Eq '^[[:space:]]*WORKER_CANDIDATE_PIN_PATH[[:space:]]*=' .env; then
  echo 'WORKER_CANDIDATE_PIN_PATH in .env needs operator review' >&2
  exit 1
fi
export WORKER_CANDIDATE_PIN_PATH="$PIN_PATH"
# Recreate CONTEXT here using the same-shell block from the worksheet section.
"$PWD/venv/bin/python3" -m scripts.maintenance.worker_pin_worksheet \
  --source-root "$PWD" \
  --expected-head "$APPROVED_HEAD" \
  --expected-tree "$APPROVED_TREE" \
  --launcher-context "$CONTEXT" > "$EVIDENCE/worksheet-post-restart.json"
cmp -s "$DERIVATION" "$EVIDENCE/worksheet-post-restart.json"
./start.sh
```

Before that command, verify the existing venv, frontend `node_modules`, and
database are present and healthy. `start.sh` creates the venv and runs
`pip install -r requirements.txt` if its interpreter is absent; it runs
`pnpm install` if `frontend/node_modules` is absent; and it initializes the
database if `orchestrator.db` is absent. Its relocated-venv repair may rewrite
entrypoint scripts. If any such bootstrap or repair would be needed, stop for
separate operator review before allowing package, database, or venv changes.

`start.sh` truncates its logs and checks for existing Uvicorn, Vite, and
Celery processes. A non-interactive start refuses a mixed-version topology.
Resolve any unexpected process state under separate approval; do not use a
second start as a recovery action.

## Gate 2 identity verification

List candidates by name, then select exactly one boot manifest for the worker
master started in this boot:

```bash
find /root/.orchestrator/runtime/worker-identities \
  -maxdepth 1 -type f -name 'worker-*.json' -print
```

Use its process identity, boot ID, PID, and start ticks to disambiguate it from
historical manifests. If the manifest is missing or selection is ambiguous,
stop. Run:

```bash
"$PWD/venv/bin/python3" scripts/maintenance/stage_r/gate2_check.py \
  --worksheet "$APPROVED_WORKSHEET" \
  --pin "$PIN_PATH" \
  --manifest "$BOOT_MANIFEST" \
  --source-root "$PWD" \
  --runtime-root /root/.orchestrator/runtime
```

Gate 2 requires `IDENTITY_VERIFIED` and `PIN_MATCHED`, a valid approved
worksheet, current source identity, matching configuration fingerprint,
launcher and worker node identity, matching invocation projection fields,
and a live process identity matching the boot record. The output reports exact
field names that differ without printing their values. `IDENTITY_INCONCLUSIVE`,
`PIN_ABSENT`, `MISMATCH`, malformed evidence, missing process evidence, or any
field difference blocks Gate 2. Do not regenerate a pin or retry in-session.

## Bounded dogfood authorization boundary

Gate 2 PASS does not authorize a task. A separate operator approval is
required before one bounded non-promoting dogfood task. Use only the approved
disposable stdlib project/task and workflow template, one admission and one
run. Do not widen workload, enable promotion, or retry after a failure. This
tooling mission did not run Gate 1, Gate 2, or dogfood.

## Post-run evidence verification

After the separately approved run, collect the worker identity manifest,
invocation projection, verification/evaluator/ChangeSet evidence, archived
logs, cleanup state, and new protected-state and venv digests. Compare the
protected-state digest maps and runtime baseline again. Confirm that the
invocation is `CORRELATED`, promotion remained held, the disposable workspace
was cleaned up, and no protected digest or venv identity changed. Preserve the
new evidence bundle outside the container before any later restart or
migration. Do not modify historical Phase 38/39 evidence.

## Failure handling and rollback

- Any FAIL or INCONCLUSIVE result stops progression. Preserve output and logs.
- Do not automatically purge Redis, retry startup, regenerate/install a pin,
  or remove stale pins.
- On Gate 1 startup failure, follow the operator-approved recovery plan. A
  restart without a pin is a separate action and requires fresh approval.
- On Gate 2 failure, leave the stack fail-closed or use a separately approved
  unpinned restart. Preserve the mismatching manifest and pin evidence.
- Restore only from the verified host backup under a separate approved
  recovery action. Verify the restored file inventory and SHA-256 manifest;
  retain both the failed runtime and backup copy.
- Never restore, traverse, or mutate ProductRoot as part of this runbook.
