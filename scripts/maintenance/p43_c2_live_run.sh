#!/usr/bin/env bash
# P43-PRODUCT-22-C2-C3B: ONE controlled disposable live TaskExecution through the
# restricted Worker, the strict C2 Host Launcher, and a real Docker sandbox.
#
# Run on the GX10 Host as root, from the protected git-archive source staged
# by p43_c2_worker_release.sh (never from an ai-agent-writable checkout):
#
#   export P43_C2_CANDIDATE=<40-hex commit>      # reviewed candidate commit
#   export P43_C2_CANDIDATE_TREE=<40-hex tree>   # independently reviewed tree
#   export P43_C2_REDIS_IMAGE=redis:<tag>@sha256:<64-hex>   # operator-supplied pin
#   export P43_C2_OPERATOR=<operator id>          # recorded in the candidate pin
#   bash scripts/maintenance/p43_c2_live_run.sh check
#   bash scripts/maintenance/p43_c2_live_run.sh setup --operator-approved
#   bash scripts/maintenance/p43_c2_live_run.sh boundary
#   bash scripts/maintenance/p43_c2_live_run.sh submit --operator-approved
#   bash scripts/maintenance/p43_c2_live_run.sh evidence
#   bash scripts/maintenance/p43_c2_live_run.sh cleanup --operator-approved
#
# P43-PRODUCT-23B trusted controls (one setup serves all; each runs once):
#   export P43_C2_CASE=U1                         # default; U1 or U2 only
#   export P43_C2_EVIDENCE=<dir holding p43-product-02/ and p43-product-18/>
#   # P43_C2_U1_EVIDENCE remains accepted as the U1 compatibility alias.
#   ... submit --operator-approved reference ; ... evaluate reference
#   ... evaluate naive                       # frozen control, never submitted
#   ... submit --operator-approved probe
# The frozen material is copied by the committed submitter, byte-verified
# against the pinned frozen manifest. Only manifest, task, baseline and the
# reference control reach the submitter; the hidden suite is mounted only into
# the network-less evaluation container on the pinned pytest image.
#
# P43-PRODUCT-23C provider-enabled mode (one real-provider model window):
#   export P43_C2_PROVIDER_UPSTREAM=http://<host>:<port>   # the one upstream
#   export P43_C2_PROVIDER_MODEL=<served model id>         # the one model
#   export P43_C2_PROVIDER_NETWORK=<docker network>        # relay egress only
#   bash scripts/maintenance/p43_c2_live_run.sh run-model-window --operator-approved
# runs check -> setup -> boundary -> submit model -> evaluate model ->
# evidence -> cleanup under that single approval. setup then starts the
# disposable relay p43-c2-relay (alias model-provider on the internal network,
# plus P43_C2_PROVIDER_NETWORK); the Worker stays on the internal network
# only. The relay forwards at most 6 completions and none outside the
# 20-minute window armed immediately before the model submission. Provider
# mode admits only the model control; provider-free mode never admits it.
#
# The persistent C2 ledger is never reset: setup snapshots it before Launcher
# startup and every later stage verifies against that baseline by request ID
# (ledger-*.json in the evidence separates historical and current-window rows).
#
# Every resource it creates is named p43-c2-* / orchestrator-worker:c2c3b or
# lives under /srv/p43-c2. It never modifies p43-sandbox-bridge.service (C1B),
# ai-agent, the production database, Redis, or workspace. No automatic retry.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
HOST_SCRIPT=$REPO/scripts/maintenance/p43_sandbox_bridge_host.sh
ROOT=/srv/p43-c2
EVIDENCE=/root/p43-c2-evidence
NET=p43-c2-net
REDIS=p43-c2-redis
WORKER=p43-c2-worker
IMAGE_TAG=orchestrator-worker:c2c3b
QUEUE=orchestrator-c2-c1-test
WORKER_UID=10001
REDIS_UID=10002
C2_SERVICE=p43-sandbox-bridge-c2.service
C1B_SERVICE=p43-sandbox-bridge.service
C2_RUN=/run/p43-sandbox-bridge-c2
C2_WORKER_KEY=/etc/p43-sandbox-bridge-c2/worker.key
C2_LEDGER=/var/lib/p43-sandbox-bridge-c2/ledger/ledger.sqlite3
AI_AGENT=ai-agent
# The Sandbox image the Launcher already pins; reused as the Worker base.
BASE_IMAGE=$(sed -n 's/^IMAGE = "\(.*\)"$/\1/p' "$REPO/app/services/sandbox_bridge/docker_executor.py")
SUBMITTER=$REPO/scripts/maintenance/p43_c2_trusted_submitter.py
PYTEST_IMAGE=""
FAILURES=0
# P43-PRODUCT-23C provider-enabled mode.
RELAY=p43-c2-relay
RELAY_UID=10003
RELAY_PORT=8000
RELAY_SCRIPT=/opt/orchestrator/scripts/maintenance/p43_c2_provider_relay.py
PROVIDER_DELTA=$REPO/config/worker-c2-provider.env.example
MODEL_WINDOW_SECONDS=1200
PROVIDER_FACTS=""

ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
info() { printf 'INFO %s\n' "$*"; }
refuse() { echo "p43-c2-live-run: $*" >&2; exit 78; }
need_approval() { [[ "${1:-}" == --operator-approved ]] || refuse "requires --operator-approved"; }

selected_case() {
    if [[ -f $ROOT/env/run.vars ]]; then
        sed -n 's/^CASE_ID=//p' "$ROOT/env/run.vars"
    else
        printf '%s\n' "${P43_C2_CASE:-U1}"
    fi
}

case_slug() {
    case "${1:-$(selected_case)}" in
        U1) printf 'u1\n' ;;
        U2) printf 'u2\n' ;;
        *) refuse "unknown P43_C2_CASE: ${1:-$(selected_case)}" ;;
    esac
}

case_evidence_root() {
    printf '%s\n' "${P43_C2_EVIDENCE:-${P43_C2_U1_EVIDENCE:-}}"
}

case_info_json() {
    /usr/bin/python3 -I "$SUBMITTER" case-info --case "${1:-$(selected_case)}"
}

case_material_root() {
    case_info_json "${1:-$(selected_case)}" |
        /usr/bin/python3 -I -c 'import json, sys; print(json.load(sys.stdin)["material_root"])'
}

wait_for_worker_identity() {
    local worker_name="$1" logs_path="$2" startup_seconds="${3:-60}"
    local worker_status="" identity_ready=0
    for _ in $(seq "$startup_seconds"); do
        worker_status=$(docker inspect --format '{{.State.Status}}' "$worker_name" 2>/dev/null || true)
        docker logs "$worker_name" >"$logs_path" 2>&1 || true
        if [[ $worker_status == exited || $worker_status == dead ]]; then
            info "Worker exited during startup (status=${worker_status:-unavailable}); retained diagnostics:"
            cat "$logs_path" >&2
            refuse "Worker exited during startup"
        fi
        if [[ $worker_status == created ]]; then
            sleep 1
            continue
        fi
        if [[ $worker_status != running ]]; then
            info "Worker startup state is unavailable or unexpected (status=${worker_status:-unavailable}); retained diagnostics:"
            cat "$logs_path" >&2
            refuse "Worker startup state could not be verified"
        fi
        if grep -Fq 'status=IDENTITY_VERIFIED pin_status=PIN_MATCHED' "$logs_path"; then
            identity_ready=1
            break
        fi
        sleep 1
    done
    if [[ $identity_ready -ne 1 ]]; then
        info "Worker did not establish controlled identity within $startup_seconds seconds; retained diagnostics:"
        cat "$logs_path" >&2
        refuse "required controlled Worker identity status was not established"
    fi
    info "Worker startup identity: $(grep -F 'Worker boot identity manifest created:' "$logs_path" | tail -n 1)"
}

require_host() {
    [[ $EUID -eq 0 ]] || refuse "must run as root on the Host"
    [[ ! -e /.dockerenv && ! -e /run/.containerenv ]] || refuse "must run on the Host, not in a container"
    [[ "$(stat -c '%u' "$REPO")" == 0 ]] || refuse "protected source release is not root-owned"
    [[ ! -e "$REPO/.git" && ! -L "$REPO/.git" &&
       -f "$REPO/.orchestrator-release-identity.json" &&
       -f "$REPO/release-manifest.json" ]] ||
        refuse "source is not a protected git-archive release"
    /usr/bin/python3 -I - "$REPO" "$P43_C2_CANDIDATE" "$P43_C2_CANDIDATE_TREE" <<'PY' ||
import base64, hashlib, json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
head, tree = sys.argv[2:4]
manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
marker = json.loads((root / ".orchestrator-release-identity.json").read_text(encoding="utf-8"))
commit = base64.b64decode(marker["commit_object"], validate=True)
actual = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
if (not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(r"[0-9a-f]{40}", tree) or
        manifest.get("kind") != "p43-host-source" or manifest.get("candidate_head") != head or
        manifest.get("candidate_tree") != tree or marker.get("candidate_head") != head or
        marker.get("candidate_tree") != tree or actual != head or
        not commit.startswith(f"tree {tree}\n".encode())):
    raise SystemExit("candidate identity/tree mismatch")
PY
        refuse "protected candidate identity/tree verification failed"
    CANDIDATE_TREE=$P43_C2_CANDIDATE_TREE
}

require_inputs() {
    local case_id=${P43_C2_CASE:-U1}
    case_info_json "$case_id" >/dev/null || refuse "unsupported or mismatched evaluation case: $case_id"
    [[ "${P43_C2_CANDIDATE:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "P43_C2_CANDIDATE must be a 40-hex commit"
    [[ "${P43_C2_CANDIDATE_TREE:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "P43_C2_CANDIDATE_TREE must be a 40-hex tree"
    [[ "${P43_C2_REDIS_IMAGE:-}" =~ ^[a-z0-9./_:-]+@sha256:[0-9a-f]{64}$ ]] ||
        refuse "P43_C2_REDIS_IMAGE must be digest-pinned (operator-supplied)"
    [[ "${P43_C2_OPERATOR:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}$ ]] || refuse "P43_C2_OPERATOR is required"
    [[ "$BASE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || refuse "pinned Sandbox base image not found"
}

provider_mode_requested() {
    [[ -n ${P43_C2_PROVIDER_UPSTREAM:-} ]]
}

require_provider_inputs() {
    [[ "${P43_C2_PROVIDER_UPSTREAM:-}" =~ ^http://[A-Za-z0-9][A-Za-z0-9.-]{0,252}:[0-9]{1,5}$ ]] ||
        refuse "P43_C2_PROVIDER_UPSTREAM must be http://host:port (no path, query or credentials)"
    [[ "${P43_C2_PROVIDER_MODEL:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$ ]] ||
        refuse "P43_C2_PROVIDER_MODEL is required"
    [[ "${P43_C2_PROVIDER_NETWORK:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] ||
        refuse "P43_C2_PROVIDER_NETWORK is required"
    [[ $P43_C2_PROVIDER_NETWORK != "$NET" ]] || refuse "P43_C2_PROVIDER_NETWORK must not be $NET"
    [[ -f $PROVIDER_DELTA ]] || refuse "provider env delta missing: $PROVIDER_DELTA"
}

# Recorded by setup; later stages never re-read the provider environment.
provider_mode() {
    [[ -f $ROOT/env/run.vars ]] && grep -qx 'PROVIDER_MODE=1' "$ROOT/env/run.vars"
}

declared_context() { sed -n 's/^EXECUTION_CONTEXT_TOKENS=\([0-9]*\)$/\1/p' "$PROVIDER_DELTA"; }

# Read-only provider facts (GET /v1/models only; no inference) from a one-shot
# unprivileged container on the provider network. Never through the Worker.
provider_facts() {
    docker run --rm --pull=never --network "$P43_C2_PROVIDER_NETWORK" --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges:true --user 65534:65534 --memory=128m --pids-limit=32 \
        --entrypoint python3 "$BASE_IMAGE" -I -c '
import http.client, json, socket, sys, urllib.parse
upstream, model, declared = sys.argv[1], sys.argv[2], int(sys.argv[3])
parts = urllib.parse.urlsplit(upstream)
facts = {"upstream": upstream, "approved_model": model, "declared_context_tokens": declared}
try:
    facts["upstream_ip"] = socket.gethostbyname(parts.hostname)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    connection.request("GET", "/v1/models")  # http.client never follows redirects
    response = connection.getresponse()
    facts["status"] = response.status
    if response.status != 200:
        raise RuntimeError("model list status %d" % response.status)
    listed = json.loads(response.read(1048576)).get("data") or []
except Exception as exc:
    facts["error"] = type(exc).__name__ + ": " + str(exc)[:200]
    print(json.dumps(facts, sort_keys=True)); sys.exit(1)
facts["served_models"] = [{k: e.get(k) for k in ("id", "root", "owned_by", "max_model_len")} for e in listed]
match = [e for e in listed if e.get("id") == model]
facts["approved_model_served"] = len(match) == 1
limit = match[0].get("max_model_len") if match else None
facts["provider_context_tokens"] = limit if isinstance(limit, int) else None
facts["context_verified"] = isinstance(limit, int) and limit >= declared
print(json.dumps(facts, sort_keys=True))
sys.exit(0 if facts["approved_model_served"] and facts["context_verified"] else 1)
' "$P43_C2_PROVIDER_UPSTREAM" "$P43_C2_PROVIDER_MODEL" "$(declared_context)"
}

instance_value() { sed -n "s/^$1=//p" "$ROOT/env/instance.env"; }

c1b_fingerprint() {
    printf '%s|%s|%s\n' \
        "$(systemctl show "$C1B_SERVICE" -p MainPID -p ActiveEnterTimestamp --value | tr '\n' ' ')" \
        "$(sha256sum /opt/p43-sandbox-bridge/MANIFEST.sha256 2>/dev/null | cut -d' ' -f1)" \
        "$(docker inspect "$AI_AGENT" --format '{{.Id}} {{.State.StartedAt}}' 2>/dev/null)"
}

# The C2 ledger persists across windows (replay protection), so a window is
# judged against a baseline taken before its Launcher starts, by request
# identity: historical rows must survive unchanged and stay tombstoned, and a
# new row is valid only once this window has submitted a control.
ledger_snapshot() { # read-only JSON snapshot, as the service user (owner of the WAL files)
    [[ ! -L $C2_LEDGER ]] || return 1
    if [[ ! -e $C2_LEDGER ]]; then # fresh Host: the Launcher creates the ledger
        printf '{"bloom_buckets": 0, "present": false, "requests": [], "taken_at": %s}\n' "$(date +%s)"
        return 0
    fi
    (cd / && runuser -u p43-sandbox-c2 -- /usr/bin/python3 -I - "$C2_LEDGER") <<'PY'
import hashlib, json, sqlite3, sys, time

BITS = 1 << 18  # RequestLedger.BITS; positions as RequestLedger.admit (pinned by tests)
taken_at = time.time()
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10)
try:
    db.execute("BEGIN")
    rows = db.execute(
        "SELECT request_id, admitted, status FROM sandbox_requests ORDER BY admitted, request_id"
    ).fetchall()
    bloom = {b: bits for b, bits in db.execute("SELECT bucket, bits FROM replay_bloom") if len(bits) == BITS // 8}
finally:
    db.close()


def tombstoned(request_id):
    digest = hashlib.sha256(request_id.encode()).digest()
    bits = bloom.get(digest[0])
    positions = [int.from_bytes(digest[i : i + 4], "big") % BITS for i in (1, 5, 9, 13)]
    return bits is not None and all(bits[p // 8] & (1 << (p % 8)) for p in positions)


requests = [
    {"request_id": r, "admitted": a, "status": s, "tombstoned": tombstoned(r)} for r, a, s in rows
]
print(json.dumps({"present": True, "taken_at": taken_at, "bloom_buckets": len(bloom), "requests": requests}, sort_keys=True))
PY
}

ledger_verify() { # BASELINE CURRENT [WINDOW_START_EPOCH]; attribution JSON, exit 1 on any violation
    /usr/bin/python3 -I - "$@" <<'PY'
import json, sys


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


baseline, current = load(sys.argv[1]), load(sys.argv[2])
window_start = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
before = {r["request_id"]: r for r in baseline["requests"]}
now = {r["request_id"]: r for r in current["requests"]}
violations = []
if not current["present"] or current["bloom_buckets"] != 256:
    violations.append("ledger_absent_or_replay_bloom_incomplete")
historical = []
for rid, old in before.items():
    if old["admitted"] > baseline["taken_at"]:
        violations.append(f"baseline_row_after_snapshot:{rid}")
    row = now.get(rid)
    if row is None:
        violations.append(f"historical_removed:{rid}")
        continue
    # Launcher startup reclassifies an uncertain ACCEPTED row; nothing else may change.
    lifecycle = (old["status"], row["status"]) == ("ACCEPTED", "INTERNAL_ERROR")
    if row["admitted"] != old["admitted"] or (row["status"] != old["status"] and not lifecycle):
        violations.append(f"historical_modified:{rid}")
    if not row["tombstoned"]:
        violations.append(f"historical_tombstone_cleared:{rid}")
    historical.append({"request_id": rid, "admitted": old["admitted"],
                       "status_before": old["status"], "status_now": row["status"]})
window = []
for rid, row in now.items():
    if rid in before:
        continue
    if window_start is None:
        violations.append(f"unexpected_admission_before_submission:{rid}")
    elif row["admitted"] < window_start or row["admitted"] <= baseline["taken_at"]:
        violations.append(f"unattributed_admission:{rid}")
    if not row["tombstoned"]:
        violations.append(f"window_request_not_tombstoned:{rid}")
    window.append({"request_id": rid, "admitted": row["admitted"], "status": row["status"]})
report = dict(
    verdict="FAIL" if violations else "PASS", violations=violations,
    baseline_taken_at=baseline["taken_at"], window_start=window_start,
    historical=historical, current_window=window,
)
print(json.dumps(report, indent=2, sort_keys=True))
sys.exit(1 if violations else 0)
PY
}

ledger_check() { # NAME [WINDOW_START_EPOCH]: snapshot now, verify against the baseline
    local name=$1; shift
    ledger_snapshot >"$ROOT/env/ledger-$name.snapshot.json" || return 1
    ledger_verify "$ROOT/env/ledger-baseline.json" "$ROOT/env/ledger-$name.snapshot.json" "$@" \
        >"$ROOT/env/ledger-$name.json"
}

window_start() { # epoch of this window's first submission; empty before it
    [[ -f $ROOT/env/submitted ]] || return 0
    date -u -d "$(cat "$ROOT/env/submitted")" +%s
}

check_no_c2_containers() { # ABSENT_LABEL PRESENT_LABEL; failed inspection is never absence
    local names
    if ! names=$(docker ps -a --format '{{.Names}}'); then
        bad "container inspection failed"
        return 0
    fi
    grep -Eq '^(p43-c2-|p43-sbx-c2-)' <<<"$names" && bad "$2" || ok "$1"
}

# ---------------------------------------------------------------- check
cmd_check() {
    require_inputs
    require_host
    check_no_c2_containers "no p43-c2 containers" "a p43-c2-*/p43-sbx-c2-* container already exists"
    docker network inspect "$NET" >/dev/null 2>&1 && bad "network $NET exists" || ok "network $NET absent"
    docker image inspect "$IMAGE_TAG" >/dev/null 2>&1 && bad "image tag $IMAGE_TAG exists" || ok "image tag absent"
    [[ -e $ROOT || -e $EVIDENCE ]] && bad "$ROOT or $EVIDENCE exists" || ok "$ROOT and $EVIDENCE absent"
    systemctl is-active --quiet "$C2_SERVICE" && bad "$C2_SERVICE active" || ok "$C2_SERVICE inactive"
    docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 && ok "pinned base present: $BASE_IMAGE" ||
        bad "pinned base missing: $BASE_IMAGE"
    docker image inspect "$P43_C2_REDIS_IMAGE" >/dev/null 2>&1 && ok "Redis image present" ||
        info "Redis image not local; setup pulls exactly $P43_C2_REDIS_IMAGE"
    getent passwd "$REDIS_UID" >/dev/null && info "Host UID $REDIS_UID: $(getent passwd "$REDIS_UID" | cut -d: -f1)" ||
        ok "no Host account owns UID $REDIS_UID"
    local preflight_out
    preflight_out=$("$HOST_SCRIPT" c2-preflight) || bad "c2-preflight"
    printf '%s\n' "$preflight_out"
    # Execution-ready stage: the pinned pytest image must be present (no pull).
    PYTEST_IMAGE=$(sed -n 's/^C2_PYTEST_IMAGE=\(sha256:[0-9a-f]\{64\}\)$/\1/p' <<<"$preflight_out")
    [[ -n $PYTEST_IMAGE ]] && ok "pytest image execution-ready: $PYTEST_IMAGE" ||
        bad "pytest image not execution-ready (unpinned, absent or mismatched)"
    info "C1B/ai-agent fingerprint: $(c1b_fingerprint)"
    if provider_mode_requested; then
        require_provider_inputs
        [[ -n $(case_evidence_root) ]] || bad "provider mode requires P43_C2_EVIDENCE (or legacy P43_C2_U1_EVIDENCE)"
        docker network inspect "$P43_C2_PROVIDER_NETWORK" >/dev/null 2>&1 &&
            ok "provider network present: $P43_C2_PROVIDER_NETWORK" || bad "provider network absent"
        if PROVIDER_FACTS=$(provider_facts); then
            ok "provider serves $P43_C2_PROVIDER_MODEL with context >= $(declared_context) (read-only model list)"
        else
            bad "provider facts not verified: $PROVIDER_FACTS"
        fi
        info "provider facts: $PROVIDER_FACTS"
    fi
    printf 'C2_LIVE_CHECK=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo READY || echo BLOCKED)" "$FAILURES"
    [[ $FAILURES -eq 0 ]]
}

# ---------------------------------------------------------------- setup
docker_run_image() { # restricted one-shot container from the built image
    docker run --rm --user "$WORKER_UID:$WORKER_UID" --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=16m \
        --memory=512m --pids-limit=64 "$@"
}

authority_mounts() {
    printf '%s\n' \
        -v "$ROOT/db:/var/lib/orchestrator-worker/db" \
        -v "$ROOT/runtime:/var/lib/orchestrator-worker/runtime" \
        -v "$ROOT/checkpoints:/var/lib/orchestrator-worker/checkpoints" \
        -v "$ROOT/identity:/var/lib/orchestrator-worker/identity" \
        -v "$ROOT/pin/worker-pin.json:/run/secrets/p43-sandbox-bridge-c2-worker-pin.json:ro" \
        -v "$ROOT/workspace:/workspace/authoritative"
}

# Disposable provider relay (23C): the Worker image, non-root, read-only, no
# capabilities, no Docker socket, no Worker/Sandbox/C1B/evaluation material.
# Internal network alias model-provider; only it joins the provider network.
start_relay() {
    install -d -o "$RELAY_UID" -g "$RELAY_UID" -m 0700 "$ROOT/relay"
    install -d -o root -g root -m 0755 "$ROOT/relay-ctl"
    docker run -d --name "$RELAY" --network "$NET" --network-alias model-provider \
        --user "$RELAY_UID:$RELAY_UID" --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=8m \
        --memory=256m --memory-swap=256m --pids-limit=64 --cpus=0.5 --restart=no \
        -e P43_RELAY_UPSTREAM="$P43_C2_PROVIDER_UPSTREAM" -e P43_RELAY_MODEL="$P43_C2_PROVIDER_MODEL" \
        -e P43_RELAY_STATE_DIR=/var/lib/p43-relay -e P43_RELAY_DEADLINE_FILE=/run/p43-relay-ctl/deadline \
        -e P43_RELAY_PORT="$RELAY_PORT" \
        -v "$ROOT/relay:/var/lib/p43-relay" -v "$ROOT/relay-ctl:/run/p43-relay-ctl:ro" \
        -e P43_RELAY_BIND=self --entrypoint python3 "$IMAGE_TAG" -I "$RELAY_SCRIPT" >/dev/null
    # The listener binds its internal-network address first; only then does
    # the relay gain the provider network, on which it never listens.
    local started=0
    for _ in $(seq 50); do # no `| grep -q` (SIGPIPE under pipefail)
        grep -q '^p43-c2-relay listening ' <<<"$(docker logs "$RELAY" 2>&1)" && { started=1; break; }
        sleep 0.2
    done
    [[ $started -eq 1 ]] || refuse "relay did not start: $(docker logs "$RELAY" 2>&1 | tail -n 5)"
    docker network connect "$P43_C2_PROVIDER_NETWORK" "$RELAY"
    ok "relay started (alias model-provider) for $P43_C2_PROVIDER_MODEL at $P43_C2_PROVIDER_UPSTREAM"
}

cmd_setup() {
    need_approval "${1:-}"
    cmd_check
    c1b_fingerprint >"$ROOT.c1b-before" 2>/dev/null || true
    local tree inst node pw c2gid interp
    tree=$CANDIDATE_TREE
    inst=c2-c1-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex[:16])')
    node=orchestrator-worker-c2-c1@$inst

    install -d -o root -g root -m 0700 "$ROOT" "$ROOT/env" "$ROOT/pin" "$ROOT/redis"
    mv "$ROOT.c1b-before" "$ROOT/env/c1b-before"
    install -d -o "$WORKER_UID" -g "$WORKER_UID" -m 0700 \
        "$ROOT/db" "$ROOT/runtime" "$ROOT/checkpoints" "$ROOT/identity" "$ROOT/workspace"

    # Non-secret contract files (no duplicate keys: docker env precedence is moot).
    local template=$REPO/config/worker-c2-live.env.example
    if provider_mode_requested; then
        # Provider mode: the template with every delta key replaced (23C).
        local keys
        keys=$(sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$PROVIDER_DELTA" | paste -sd'|')
        { grep -Ev "^($keys)=" "$template"
          grep -E '^[A-Z][A-Z0-9_]*=' "$PROVIDER_DELTA" | sed "s|@MODEL@|$P43_C2_PROVIDER_MODEL|g"
        } >"$ROOT/env/contract.env"
        template=$ROOT/env/contract.env
        printf '%s\n' "$PROVIDER_FACTS" >"$ROOT/env/provider-facts.json"
    fi
    install -m 0600 "$template" "$ROOT/env/worker.env"
    sed -e '/^SANDBOX_BRIDGE_/d' \
        -e "s|^CONTROLLED_WORKER_NODE_IDENTITY=.*|CONTROLLED_WORKER_NODE_IDENTITY=$node|" \
        "$template" >"$ROOT/env/submitter.env"
    sed -e "s|^CONTROLLED_WORKER_NODE_IDENTITY=.*|CONTROLLED_WORKER_NODE_IDENTITY=$node|" \
        "$template" >"$ROOT/env/pin.env"
    printf 'WORKER_INSTANCE_ID=%s\nCELERY_BROKER_URL=redis://%s:6379/9\nCELERY_RESULT_BACKEND=redis://%s:6379/10\n' \
        "$inst" "$REDIS" "$REDIS" >"$ROOT/env/instance.env"
    chmod 0600 "$ROOT/env/"*.env

    # Disposable Redis credential: root-only env file + Redis-owned config.
    pw=$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(24))')
    (umask 077 && printf 'CELERY_BROKER_PASSWORD=%s\n' "$pw" >"$ROOT/env/secrets.env")
    (umask 077 && printf 'requirepass %s\nprotected-mode yes\nsave ""\nappendonly no\n' "$pw" >"$ROOT/redis/redis.conf")
    chown "$REDIS_UID:$REDIS_UID" "$ROOT/redis/redis.conf" && chmod 0400 "$ROOT/redis/redis.conf"
    unset pw

    # Isolated network and Redis.
    docker network create --internal "$NET" >/dev/null
    docker pull "$P43_C2_REDIS_IMAGE" >/dev/null
    docker run -d --name "$REDIS" --network "$NET" --user "$REDIS_UID:$REDIS_UID" \
        --read-only --cap-drop=ALL --security-opt=no-new-privileges:true \
        --tmpfs /data:rw,nosuid,nodev,size=64m --memory=256m --pids-limit=64 \
        -v "$ROOT/redis/redis.conf:/etc/redis.conf:ro" \
        "$P43_C2_REDIS_IMAGE" redis-server /etc/redis.conf >/dev/null
    ok "Redis started on internal $NET"

    # Selected frozen material, copied and verified by committed code before
    # any submitter mount is created. The model mount never receives controls,
    # hidden, case or oracle material.
    local case_id=${P43_C2_CASE:-U1} case_slug_value evidence case_info manifest_sha
    case_slug_value=$(case_slug "$case_id")
    evidence=$(case_evidence_root)
    case_info=$(case_info_json "$case_id")
    manifest_sha=$(/usr/bin/python3 -I -c 'import json, sys; print(json.load(sys.stdin)["manifest_sha256"])' <<<"$case_info")
    printf '%s\n' "$case_info" >"$ROOT/env/case-binding.json"
    if [[ -n $evidence ]]; then
        if provider_mode_requested; then
            # Model mount: manifest, task and baseline only (no reference control).
            /usr/bin/python3 -I "$SUBMITTER" stage-material "$evidence" "$ROOT/${case_slug_value}-model" --parts model --case "$case_id"
            /usr/bin/python3 -I "$SUBMITTER" stage-material "$evidence" "$ROOT/${case_slug_value}-eval" --parts evaluation --case "$case_id"
        else
            /usr/bin/python3 -I "$SUBMITTER" stage-material "$evidence" "$ROOT/${case_slug_value}-submit" --parts submit --case "$case_id"
            /usr/bin/python3 -I "$SUBMITTER" stage-material "$evidence" "$ROOT/${case_slug_value}-eval" --parts evaluation --case "$case_id"
        fi
        ok "$case_id material staged and verified against the pinned frozen manifest $manifest_sha"
    fi

    # Strict C2 Launcher (separate user/key/state/socket; C1B untouched).
    "$HOST_SCRIPT" c2-install --operator-approved
    c2gid=$(getent group p43-sandbox-c2 | cut -d: -f3)
    # Orphan recovery: a stopped Sandbox-named container on the pinned pytest
    # image must be removed by the strict Launcher's startup.
    local orphan
    orphan=p43-sbx-c2-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex)')
    docker create --pull=never --name "$orphan" --network none "$PYTEST_IMAGE" true >/dev/null
    # Ledger baseline: only the Launcher admits, so rows present while it is
    # inactive are historical (retained by design; never reset or rewritten).
    systemctl is-active --quiet "$C2_SERVICE" && refuse "C2 Launcher active before the ledger baseline"
    ledger_snapshot >"$ROOT/env/ledger-baseline.json" || refuse "C2 ledger baseline could not be read"
    info "C2 ledger baseline: $(/usr/bin/python3 -I -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["requests"]))' \
        "$ROOT/env/ledger-baseline.json") historical request(s)"
    systemctl start "$C2_SERVICE"
    for _ in $(seq 50); do [[ -S $C2_RUN/launcher.sock ]] && break; sleep 0.2; done
    [[ -S $C2_RUN/launcher.sock ]] || refuse "C2 Launcher socket did not appear"
    if docker inspect "$orphan" >/dev/null 2>&1; then
        refuse "Launcher startup did not remove the orphan $orphan"
    fi
    ok "Launcher startup removed the pytest-image orphan $orphan"
    printf 'C2_GID=%s\nNODE=%s\nCANDIDATE_TREE=%s\nPYTEST_IMAGE=%s\nORPHAN=%s\nCASE_ID=%s\nCASE_MANIFEST_SHA256=%s\n' \
        "$c2gid" "$node" "$tree" "$PYTEST_IMAGE" "$orphan" "$case_id" "$manifest_sha" >"$ROOT/env/run.vars"
    if provider_mode_requested; then
        printf 'PROVIDER_MODE=1\nPROVIDER_MODEL=%s\nPROVIDER_NETWORK=%s\nUPSTREAM_IP=%s\nUPSTREAM_PORT=%s\n' \
            "$P43_C2_PROVIDER_MODEL" "$P43_C2_PROVIDER_NETWORK" \
            "$(/usr/bin/python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["upstream_ip"])' "$ROOT/env/provider-facts.json")" \
            "${P43_C2_PROVIDER_UPSTREAM##*:}" >>"$ROOT/env/run.vars"
    fi

    # The protected Host source is also the exact Worker image build context.
    docker build -q -f "$REPO/Dockerfile.worker" \
        --build-arg "PYTHON_BASE_IMAGE=$BASE_IMAGE" \
        --build-arg "ORCHESTRATOR_GIT_SHA=$P43_C2_CANDIDATE" \
        -t "$IMAGE_TAG" "$REPO" >"$ROOT/env/image.id"
    ok "built $IMAGE_TAG $(cat "$ROOT/env/image.id")"

    # Candidate pin for the release inside the image (existing worksheet).
    interp=$(docker run --rm --entrypoint /bin/sh "$IMAGE_TAG" -c 'head -n1 /usr/local/bin/celery' | sed 's/^#!//')
    /usr/bin/python3 - "$interp" "$node" >"$ROOT/pin/launcher-context.json" <<'PY'
import json, sys
interp, node = sys.argv[1:]
print(json.dumps({
    "context_schema_version": 1, "launcher": "celery-worker",
    "interpreter": interp, "script": "/usr/local/bin/celery",
    "args": ["-A", "app.celery_app", "worker", "--queues=orchestrator-c2-c1-test",
             "--pool=solo", "--concurrency=1", "--loglevel=INFO", f"--hostname={node}"],
    "cwd": "/opt/orchestrator", "worker_node_identity": node}))
PY
    chmod 0444 "$ROOT/pin/launcher-context.json"
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/pin.env" --env-file "$ROOT/env/instance.env" \
        --env-file "$ROOT/env/secrets.env" \
        -v "$ROOT/pin/launcher-context.json:/launcher-context.json:ro" \
        --entrypoint python3 "$IMAGE_TAG" -m scripts.maintenance.worker_pin_worksheet \
        --source-root /opt/orchestrator --expected-head "$P43_C2_CANDIDATE" \
        --expected-tree "$tree" --launcher-context /launcher-context.json \
        >"$ROOT/pin/worksheet.json"
    docker_run_image -i --network none \
        --entrypoint python3 "$IMAGE_TAG" -c '
import datetime as dt, json, sys
sheet = json.load(sys.stdin)
assert sheet["validation"]["result"] == "VALID", sheet["validation"]
now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
stamp = lambda t: t.isoformat().replace("+00:00", "Z")
pin = dict(sheet["proposed_pin_body"])
pin["approval"] = {"operator_identity": sys.argv[1],
                   "approval_id": "p43-c2-c3b-" + now.strftime("%Y%m%dT%H%M%SZ"),
                   "approved_at": stamp(now), "expires_at": stamp(now + dt.timedelta(hours=4))}
print(json.dumps(pin, sort_keys=True))
' "$P43_C2_OPERATOR" <"$ROOT/pin/worksheet.json" >"$ROOT/pin/worker-pin.json"
    chmod 0444 "$ROOT/pin/worker-pin.json"
    ok "candidate pin $(sha256sum "$ROOT/pin/worker-pin.json" | cut -c1-16) for $P43_C2_CANDIDATE"

    # Fresh disposable schema via the trusted submitter (no Launcher material).
    # shellcheck disable=SC2046
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/submitter.env" --env-file "$ROOT/env/instance.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter init

    if provider_mode_requested; then
        start_relay
    fi

    # Restricted Worker.
    # shellcheck disable=SC2046
    docker run -d --name "$WORKER" --network "$NET" \
        --user "$WORKER_UID:$WORKER_UID" --group-add "$c2gid" \
        --read-only --cap-drop=ALL --security-opt=no-new-privileges:true \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m --pids-limit=256 \
        --memory=1g --memory-swap=1g --cpus=1 \
        --env-file "$ROOT/env/worker.env" --env-file "$ROOT/env/instance.env" \
        --env-file "$ROOT/env/secrets.env" -e WORKER_ALLOW_DISPOSABLE_START=1 \
        $(authority_mounts) \
        -v "$C2_RUN:$C2_RUN:ro" \
        -v "$C2_WORKER_KEY:/run/secrets/p43-sandbox-bridge-c2.key:ro" \
        "$IMAGE_TAG" >/dev/null
    local worker_logs="$ROOT/env/worker-startup.log"
    wait_for_worker_identity "$WORKER" "$worker_logs" 60
    info "setup complete; run: $0 boundary"
}

# ---------------------------------------------------------------- boundary
expect() { # LABEL EXPECTED ACTUAL
    [[ "$2" == "$3" ]] && ok "$1" || bad "$1: expected '$2' got '$3'"
}

strict_launcher_started() { # current invocation only; no `| grep -q` (SIGPIPE under pipefail)
    local inv
    inv=$(systemctl show -p InvocationID --value "$C2_SERVICE") || return 1
    [[ -n "$inv" ]] || return 1
    grep -q 'startup .*allowed_uid=10001 max_active=1 require_authorization=True' \
        <<<"$(journalctl _SYSTEMD_INVOCATION_ID="$inv" --no-pager -o cat)"
}

canonical_mounts() {
    sed '/^$/d' | sort
}

# 23C provider mode: the relay is the Worker's only route to a model, and it
# is not a proxy. Uses only the read-only model list; zero completions.
relay_boundary() {
    local fmt expected probe direct
    fmt='{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}} {{len .HostConfig.Devices}} {{.HostConfig.RestartPolicy.Name}}'
    expect "Relay HostConfig" "true false [ALL] [no-new-privileges:true] $RELAY_UID:$RELAY_UID 0 no" \
        "$(docker inspect "$RELAY" --format "$fmt")"
    expect "Relay mounts (exact)" "$(printf '%s\n' "$ROOT/relay:/var/lib/p43-relay:true" \
        "$ROOT/relay-ctl:/run/p43-relay-ctl:false" | sort)" \
        "$(docker inspect "$RELAY" --format '{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}}{{"\n"}}{{end}}' | canonical_mounts)"
    expect "Relay networks" "$(printf '%s\n' "$NET" "$PROVIDER_NETWORK" | sort | xargs)" \
        "$(docker inspect "$RELAY" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | sort | xargs)"
    expect "Relay alias on $NET" "True" \
        "$(docker inspect "$RELAY" --format '{{json .NetworkSettings.Networks}}' | /usr/bin/python3 -I -c \
            'import json, sys; print("model-provider" in (json.load(sys.stdin)[sys.argv[1]].get("Aliases") or []))' "$NET")"
    local internal_ip provider_ip
    internal_ip=$(docker inspect "$RELAY" --format "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}")
    provider_ip=$(docker inspect "$RELAY" --format "{{(index .NetworkSettings.Networks \"$PROVIDER_NETWORK\").IPAddress}}")
    expect "Relay listens on its internal address only" "1" \
        "$(grep -c "^p43-c2-relay listening bind=$internal_ip port=$RELAY_PORT " <<<"$(docker logs "$RELAY" 2>&1)")"
    expect "Relay unreachable from the provider network" "REFUSED" \
        "$(docker run --rm --pull=never --network "$PROVIDER_NETWORK" --read-only --cap-drop=ALL \
            --security-opt=no-new-privileges:true --user 65534:65534 --memory=64m --entrypoint python3 \
            "$BASE_IMAGE" -I -c 'import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close(); print("REACHABLE")
except OSError:
    print("REFUSED")' "$provider_ip" "$RELAY_PORT" 2>&1 | tail -1)"
    expect "Discovery search executable for the non-root Worker (B2)" "uid=$WORKER_UID /usr/bin/rg" \
        "$(docker exec "$WORKER" sh -c 'echo "uid=$(id -u) $(command -v rg)"' 2>&1)"
    expect "Sandbox network disabled (pinned executor argv)" "2" \
        "$(grep -c -- '"--network=none",' "$REPO/app/services/sandbox_bridge/docker_executor.py")"
    direct='import socket, sys
port = int(sys.argv[2])
for host, p in ((sys.argv[1], port), ("ai-gateway", port), ("ai-agent", 8080), ("172.17.0.1", 8080),
                ("172.17.0.1", 6379), ("172.17.0.1", port), ("8.8.8.8", 53)):
    try:
        socket.create_connection((host, p), 3).close(); print("REACHABLE", host, p); sys.exit(1)
    except OSError:
        pass
print("NO_DIRECT_ROUTE")'
    expect "Worker cannot reach upstream/production/external directly" "NO_DIRECT_ROUTE" \
        "$(docker exec "$WORKER" python3 -c "$direct" "$UPSTREAM_IP" "$UPSTREAM_PORT" 2>&1 | tail -1)"
    probe='import http.client, json, socket, sys
model = sys.argv[1]
def call(method, path, body=None):
    c = http.client.HTTPConnection("model-provider", 8000, timeout=15)
    c.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
    r = c.getresponse(); data = r.read(); c.close(); return r.status, data
def raw(line):
    s = socket.create_connection(("model-provider", 8000), 5); s.sendall(line); data = s.recv(4096); s.close()
    return int(data.split(b" ", 2)[1])
chat = lambda m: json.dumps({"model": m, "messages": [{"role": "user", "content": "boundary"}]}).encode()
status, data = call("GET", "/v1/models")
print(json.dumps({
    "models": [status, [e.get("id") for e in json.loads(data).get("data", [])] if status == 200 else None],
    "other_path": call("GET", "/v1/models/other")[0],
    "legacy_completions": call("POST", "/v1/completions", b"{}")[0],
    "delete": call("DELETE", "/v1/models")[0],
    "absolute_form": raw(b"GET http://1.1.1.1/ HTTP/1.1\r\nHost: 1.1.1.1\r\n\r\n"),
    "connect": raw(b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n"),
    "other_model": call("POST", "/v1/chat/completions", chat(model + "-other"))[0],
    "unarmed_completion": call("POST", "/v1/chat/completions", chat(model))[0],
}, sort_keys=True))'
    expect "Relay allowlist from the Worker (no completion forwarded)" \
        "{\"absolute_form\": 404, \"connect\": 405, \"delete\": 405, \"legacy_completions\": 404, \"models\": [200, [\"$PROVIDER_MODEL\"]], \"other_model\": 403, \"other_path\": 404, \"unarmed_completion\": 503}" \
        "$(docker exec "$WORKER" python3 -c "$probe" "$PROVIDER_MODEL" 2>&1 | tail -1)"
    expect "Relay ledger: no completion admitted or forwarded before arming" "0 0" \
        "$(relay_counts "$ROOT/relay" | /usr/bin/python3 -I -c 'import json, sys; c = json.load(sys.stdin); print(c["completions_admitted"], c["completions_forwarded"])')"
    [[ ! -e $ROOT/relay-ctl/deadline ]] && ok "evaluation window not armed" || bad "deadline already armed"
}

relay_counts() { # STATE_DIR: bounded summary of the relay ledger (no bodies, no headers)
    /usr/bin/python3 -I - "$1/ledger.jsonl" <<'PY'
import collections, json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
completions = [e for e in events if e.get("event") == "completion"]
refused = collections.Counter(e["reason"] for e in events if e.get("event") == "refused")
print(json.dumps({
    "started": next((e for e in events if e.get("event") == "relay_started"), None),
    "completions_admitted": sum(e.get("event") == "completion_admitted" for e in events),
    "completions_forwarded": len(completions),
    "completions_completed": sum(e.get("outcome") == "completed" for e in completions),
    "completion_outcomes": dict(collections.Counter(e.get("outcome") for e in completions)),
    "refused": dict(refused),
    "model_lists": sum(e.get("event") == "model_list" for e in events),
    "requests": [{k: e.get(k) for k in ("sequence", "completion_index", "at", "duration_seconds",
                  "timeout_seconds", "outcome", "http_status", "upstream_status", "response_model",
                  "usage", "request_sha256", "client_delivered")} for e in completions],
}, indent=2, sort_keys=True))
PY
}

cmd_boundary() {
    require_host
    # shellcheck disable=SC1091
    source "$ROOT/env/run.vars"
    local fmt mounts expected nets
    expect "Worker identity" "uid=$WORKER_UID gid=$WORKER_UID groups=$WORKER_UID,$C2_GID" \
        "$(docker exec "$WORKER" id | sed 's/([^)]*)//g')"
    expect "CapEff/CapPrm/CapBnd zero, NoNewPrivs" "0000000000000000 0000000000000000 0000000000000000 1" \
        "$(docker exec "$WORKER" awk '/^(CapEff|CapPrm|CapBnd|NoNewPrivs):/{printf "%s ", $2}' /proc/1/status | sed 's/ $//')"
    fmt='{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{len .HostConfig.Devices}} pid=[{{.HostConfig.PidMode}}]'
    expect "Worker HostConfig" "true false [ALL] [no-new-privileges:true] 1073741824 1000000000 256 0 pid=[]" \
        "$(docker inspect "$WORKER" --format "$fmt")"
    mounts=$(docker inspect "$WORKER" --format '{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}}{{"\n"}}{{end}}' | canonical_mounts)
    expected=$(printf '%s\n' \
        "$ROOT/db:/var/lib/orchestrator-worker/db:true" \
        "$ROOT/runtime:/var/lib/orchestrator-worker/runtime:true" \
        "$ROOT/checkpoints:/var/lib/orchestrator-worker/checkpoints:true" \
        "$ROOT/identity:/var/lib/orchestrator-worker/identity:true" \
        "$ROOT/pin/worker-pin.json:/run/secrets/p43-sandbox-bridge-c2-worker-pin.json:false" \
        "$ROOT/workspace:/workspace/authoritative:true" \
        "$C2_RUN:$C2_RUN:false" \
        "$C2_WORKER_KEY:/run/secrets/p43-sandbox-bridge-c2.key:false" | sort)
    expect "Worker effective mounts (exact)" "$expected" "$mounts"
    nets=$(docker inspect "$WORKER" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')
    expect "Worker networks" "$NET " "$nets"
    expect "Network internal" "true" "$(docker network inspect "$NET" --format '{{.Internal}}')"
    local members="$REDIS $WORKER"
    provider_mode && members=$(printf '%s\n' "$REDIS" "$RELAY" "$WORKER" | sort | xargs)
    expect "Network members" "$members" \
        "$(docker network inspect "$NET" --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | sort | xargs)"
    for p in /var/run/docker.sock /run/docker.sock /run/secrets/p43-sandbox-bridge.key /run/p43-sandbox-bridge/launcher.sock; do
        docker exec "$WORKER" test ! -e "$p" && ok "absent in Worker: $p" || bad "visible in Worker: $p"
    done
    expect "C2 key in Worker" "$WORKER_UID 400" "$(docker exec "$WORKER" stat -c '%u %a' /run/secrets/p43-sandbox-bridge-c2.key)"
    expect "Worker launch line" "1" "$(docker exec "$WORKER" sh -c 'tr "\0" " " </proc/1/cmdline' | grep -c -- "--pool=solo --concurrency=1 --loglevel=INFO --hostname=$NODE")"
    grep -q 'Worker boot identity manifest created: .*status=IDENTITY_VERIFIED pin_status=PIN_MATCHED' <<<"$(docker logs "$WORKER" 2>&1)" &&
        ok "boot identity VERIFIED with PIN_MATCHED" || bad "boot identity not verified against the pin"
    local probe='import socket,sys
for host, port in (("model-provider", 11434), ("ai-gateway", 8000), ("ai-agent", 6379), ("1.1.1.1", 443)):
    try:
        socket.create_connection((host, port), 3).close(); print("REACHABLE", host); sys.exit(1)
    except OSError:
        pass
print("NO_EGRESS")'
    expect "No provider/production/egress reachability" "NO_EGRESS" "$(docker exec "$WORKER" python3 -c "$probe" 2>&1 | tail -1)"
    provider_mode && relay_boundary
    fmt='{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.HostConfig.CapDrop}} {{.Config.User}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}'
    expect "Redis container" "true false [ALL] $REDIS_UID:$REDIS_UID $NET" "$(docker inspect "$REDIS" --format "$fmt")"
    systemctl is-active --quiet "$C2_SERVICE" && ok "C2 Launcher active" || bad "C2 Launcher inactive"
    strict_launcher_started && ok "strict Launcher startup line" || bad "strict Launcher startup line missing"
    ledger_check boundary &&
        ok "C2 ledger: historical requests preserved and tombstoned; no admission before submission" ||
        bad "C2 ledger baseline check: $(cat "$ROOT/env/ledger-boundary.json" 2>/dev/null || echo unreadable)"
    docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' |
        grep -Eq '^(/etc/p43-sandbox-bridge-c2|/var/lib/p43-sandbox-bridge-c2|/run/p43-sandbox-bridge-c2|/srv/p43-c2)(/|$)' &&
        bad "ai-agent mounts C2 material" || ok "ai-agent mounts no C2 material"
    docker exec "$AI_AGENT" sh -c "test ! -e $C2_RUN/launcher.sock && test ! -e $C2_WORKER_KEY && test ! -e $ROOT" &&
        ok "C2 socket/key/state not visible in ai-agent" || bad "C2 material visible in ai-agent"
    expect "C1B Launcher and ai-agent unchanged" "$(cat "$ROOT/env/c1b-before")" "$(c1b_fingerprint)"
    # Direct Docker negative control (not the Launcher path): an absent image
    # with --pull=never fails and records no pull event.
    local t0 absent=sha256:0000000000000000000000000000000000000000000000000000000000000000
    t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if docker run --rm --pull=never --network=none "$absent" true >/dev/null 2>&1; then
        bad "direct Docker: absent image ran"
    else ok "direct Docker: absent image fails with --pull=never"; fi
    expect "direct Docker: no pull event" "" \
        "$(docker events --since "$t0" --until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --filter type=image --filter event=pull --format '{{.Actor.ID}}')"
    printf 'C2_BOUNDARY=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"
    if [[ $FAILURES -eq 0 ]]; then date -u +%s >"$ROOT/env/boundary.pass"; else rm -f "$ROOT/env/boundary.pass"; return 1; fi
}

# ---------------------------------------------------------------- submit
control_suffix() { # exit-check keeps the PRODUCT-22 file names
    [[ $1 == exit-check ]] && echo "" || echo ".$1"
}

cmd_submit() {
    need_approval "${1:-}"
    local control=${2:-exit-check} sfx material=() case_id case_slug_value material_root
    case_id=$(selected_case)
    case_slug_value=$(case_slug "$case_id")
    material_root=$(case_material_root "$case_id")
    [[ $control =~ ^(exit-check|reference|probe|model)$ ]] || refuse "unknown control: $control"
    require_host
    [[ -f $ROOT/env/boundary.pass ]] || refuse "boundary has not passed; run: $0 boundary"
    if provider_mode; then
        [[ $control == model ]] || refuse "provider mode admits only the model control"
    else
        [[ $control != model ]] || refuse "the model control requires provider mode (set up with P43_C2_PROVIDER_*)"
    fi
    sfx=$(control_suffix "$control")
    [[ ! -e $ROOT/env/submitted$sfx ]] || refuse "$control already submitted once; no retry"
    if [[ $control == reference ]]; then
        [[ -d $ROOT/${case_slug_value}-submit ]] || refuse "$case_id material not staged (set P43_C2_EVIDENCE before setup)"
        material=(-v "$ROOT/${case_slug_value}-submit:$material_root:ro")
    elif [[ $control == model ]]; then
        [[ -d $ROOT/${case_slug_value}-model ]] || refuse "$case_id model material not staged"
        material=(-v "$ROOT/${case_slug_value}-model:$material_root:ro")
    fi
    # Before the first submission no new row may exist; later ones may only
    # follow this window's first submission.
    ledger_check "pre-submit$sfx" "$(window_start)" ||
        refuse "C2 ledger check failed before $control: $(cat "$ROOT/env/ledger-pre-submit$sfx.json" 2>/dev/null || echo unreadable)"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$ROOT/env/submitted$sfx"
    [[ -e $ROOT/env/submitted ]] || cp "$ROOT/env/submitted$sfx" "$ROOT/env/submitted"
    local deadline=""
    if [[ $control == model ]]; then
        # The one authoritative evaluation deadline, armed before submission.
        # Write-once; the relay reads it once and refuses completions after it.
        deadline=$(($(date +%s) + MODEL_WINDOW_SECONDS))
        (set -o noclobber && printf '%s\n' "$deadline" >"$ROOT/relay-ctl/deadline") ||
            refuse "evaluation deadline already armed; no second window"
        chmod 0444 "$ROOT/relay-ctl/deadline"
        info "evaluation window armed: deadline $(date -u -d "@$deadline" +%Y-%m-%dT%H:%M:%SZ)"
    fi
    # Same image, same DB; no Launcher key/socket; shares only the Worker PID
    # namespace, which the existing publish-side identity binding inspects.
    # shellcheck disable=SC2046
    docker_run_image --name p43-c2-submitter --network "$NET" --pid "container:$WORKER" \
        -w /opt/orchestrator --env-file "$ROOT/env/submitter.env" \
        --env-file "$ROOT/env/instance.env" --env-file "$ROOT/env/secrets.env" \
        $(authority_mounts) "${material[@]}" --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter submit --control "$control" --case "$case_id" |
        tee "$ROOT/env/submit$sfx.json"
    if [[ $control == model ]]; then
        observe_model "$deadline"
        return
    fi
    # Observation needs only the database: no network, no broker credential.
    # shellcheck disable=SC2046
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/submitter.env" --env-file "$ROOT/env/instance.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter observe --control "$control" \
        --deadline-seconds 600 | tee "$ROOT/env/observe$sfx.json"
}

observe_once() { # OUT DEADLINE_SECONDS; exit 0 terminal, 2 nonterminal at its deadline
    local case_id; case_id=$(selected_case)
    # shellcheck disable=SC2046
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/submitter.env" --env-file "$ROOT/env/instance.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter observe --control model \
        --deadline-seconds "$2" --case "$case_id" >"$1"
}

# Observe until terminal or the evaluation deadline. A Task still nonterminal
# at the deadline is an evaluation timeout, not a product FAILED: the Worker
# is stopped (Celery warm shutdown, then SIGKILL) and the state it actually
# left is observed again. Nothing is rewritten to make the record complete.
observe_model() {
    local deadline=$1 remaining rc=0
    remaining=$((deadline - $(date +%s)))
    ((remaining > 0)) || remaining=0
    observe_once "$ROOT/env/observe.model.json" "$remaining" || rc=$?
    cat "$ROOT/env/observe.model.json"
    if [[ $rc -eq 0 ]]; then
        printf 'terminal\n' >"$ROOT/env/model-window.outcome"
        ok "model TaskExecution reached a terminal state within the evaluation window"
        return 0
    fi
    if [[ $rc -ne 2 ]]; then
        # Not a model outcome: stop the Worker so nothing changes unobserved.
        printf 'observation_failed\n' >"$ROOT/env/model-window.outcome"
        docker stop --time 30 "$WORKER" >/dev/null 2>&1 || true
        bad "model observation failed (rc=$rc); Worker stopped"
        return 1
    fi
    printf 'evaluation_deadline_exhausted\n' >"$ROOT/env/model-window.outcome"
    info "evaluation deadline reached with the Task nonterminal; stopping the disposable Worker"
    docker stop --time 30 "$WORKER" >/dev/null 2>&1 || true
    docker logs "$WORKER" >"$ROOT/env/worker-at-deadline.log" 2>&1 || true
    rc=0
    observe_once "$ROOT/env/observe.model.after-stop.json" 0 || rc=$?
    cat "$ROOT/env/observe.model.after-stop.json"
    info "post-stop observation recorded (rc=$rc); classification: evaluation_deadline_exhausted"
    return 0
}

# ---------------------------------------------------------------- evaluate
eval_container() { # independent evaluation: pinned pytest image, no network, read-only
    local evaluation_root=$1 change_set=$2
    shift 2
    docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges:true --user=65534:65534 --memory=1g \
        --pids-limit=128 --tmpfs /tmp:rw,nosuid,nodev,size=256m -e HOME=/tmp -e TMPDIR=/tmp \
        -v "$evaluation_root:/eval:ro" -v "$change_set:/cs:ro" --entrypoint python3 "$PYTEST_IMAGE" -I "$@"
}

cmd_evaluate() {
    local control=${1:-} out source cs observed=() case_id case_slug_value evaluation_root case_dir
    local -a evaluation_paths
    [[ $control =~ ^(reference|naive|naive-symptom|naive-partial|model)$ ]] || refuse "unknown evaluation control: ${control:-none}"
    require_host
    # shellcheck disable=SC1091
    source "$ROOT/env/run.vars"
    case_id=$(selected_case)
    case_slug_value=$(case_slug "$case_id")
    evaluation_root=$ROOT/${case_slug_value}-eval
    case_dir=/eval/p43-product-18/$case_slug_value
    [[ -d $evaluation_root ]] || refuse "$case_id material not staged"
    out=$ROOT/eval/$control
    [[ ! -e $out ]] || refuse "$control already evaluated"
    install -d -m 0700 "$ROOT/eval" && install -d -m 0755 "$out"
    if [[ $control == reference ]]; then
        [[ -f $ROOT/env/observe.reference.json ]] || refuse "reference not observed"
        # Container path -> Host path of the captured Change Set.
        source=$(/usr/bin/python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["change_set"]["directory"])' \
            "$ROOT/env/observe.reference.json")
        [[ $source == /var/lib/orchestrator-worker/runtime/* ]] || refuse "unexpected Change Set path: $source"
        source=$ROOT/runtime/${source#/var/lib/orchestrator-worker/runtime/}
        observed=(--observed "$ROOT/env/observe.reference.json")
    elif [[ $control == model ]]; then
        provider_mode || refuse "model evaluation requires provider mode"
        local record=$ROOT/env/observe.model.json
        [[ -f $ROOT/env/observe.model.after-stop.json ]] && record=$ROOT/env/observe.model.after-stop.json
        [[ -f $record ]] || refuse "model not observed"
        # Supplementary contamination evidence from the recorded requests.
        /usr/bin/python3 -I "$SUBMITTER" contamination "$evaluation_root" "$ROOT/relay" --case "$case_id" >"$out/contamination.json" ||
            bad "contamination scan failed"
        # The actual held Change Set, whatever the outcome; never a substitute.
        source=$(/usr/bin/python3 -I -c 'import json, sys; cs = json.load(open(sys.argv[1])).get("change_set"); print(cs["directory"] if cs else "")' \
            "$record")
        if [[ -z $source ]]; then
            printf '{"control": "model", "verdict": "NO_CHANGE_SET"}\n' | tee "$out/verdict.json"
            return 0
        fi
        [[ $source == /var/lib/orchestrator-worker/runtime/* ]] || refuse "unexpected Change Set path: $source"
        source=$ROOT/runtime/${source#/var/lib/orchestrator-worker/runtime/}
        observed=(--observed "$record")
    else
        source=$evaluation_root/p43-product-18/$case_slug_value/controls/naive
    fi
    [[ -f $source/manifest.json && ! -L $source ]] || refuse "Change Set missing: $source"
    # A private, read-only copy: the Worker cannot change what is judged.
    cs=$out/change-set
    cp -R --no-preserve=ownership,mode "$source" "$cs"
    find "$cs" -type l | grep -q . && refuse "Change Set contains links"
    chmod -R a+rX,go-w "$cs"
    mapfile -t evaluation_paths < <(/usr/bin/python3 -I - "$evaluation_root/p43-product-18/$case_slug_value/manifest.json" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["acceptance"])
print(*manifest["existing_tests"], sep="\n")
PY
    )
    [[ ${#evaluation_paths[@]} -ge 1 && -n ${evaluation_paths[0]} ]] || refuse "case evaluation paths unavailable"
    eval_container "$evaluation_root" "$cs" /eval/p43-product-02/oracle.py \
        "$case_dir/baseline" /cs "${evaluation_paths[@]}" >"$out/oracle.json" || true
    eval_container "$evaluation_root" "$cs" /eval/p43-product-18/tools/hidden_suite.py \
        "$case_dir/baseline" /cs "$case_dir/hidden" >"$out/hidden.json" || true
    /usr/bin/python3 -I "$SUBMITTER" verdict "$evaluation_root" "$cs" --control "$control" \
        --case "$case_id" --oracle "$out/oracle.json" --hidden "$out/hidden.json" "${observed[@]}" | tee "$out/verdict.json"
}

# ---------------------------------------------------------------- evidence
preserve_worker_boot_identity_diagnostics() {
    local source="$ROOT/runtime/worker-identities" owner
    if [[ ! -d "$source" || -L "$source" ]]; then
        info "no Worker boot identity directory to preserve"
        return 0
    fi
    if [[ -e "$EVIDENCE" || -L "$EVIDENCE" ]]; then
        [[ -d "$EVIDENCE" && ! -L "$EVIDENCE" ]] || return 1
        owner=$(stat -c '%u' "$EVIDENCE")
        [[ "$owner" == 0 || "$owner" == "$EUID" ]] || return 1
    else
        install -d -m 0700 "$EVIDENCE"
    fi
    chmod 0700 "$EVIDENCE"
    /usr/bin/python3 -I - "$source" "$EVIDENCE" <<'PY' || return $?
import json, pathlib, re, sys

source, destination = map(pathlib.Path, sys.argv[1:])
allowed = (
    "schema_version", "candidate_head", "candidate_tree", "candidate_source_root",
    "source_manifest_digest", "boot_timestamp", "worker_pid", "worker_start_ticks",
    "worker_executable", "worker_cwd", "worker_argv", "worker_node_identity",
    "configuration_fingerprint", "configuration_schema_version",
    "configuration_policy_id", "identity_status", "record_id", "candidate_pin_status",
    "candidate_pin_identity", "identity_status_reasons",
)
saved = 0
for path in sorted(source.glob("worker-*.json")):
    if path.is_symlink() or not path.is_file():
        continue
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        record = manifest.get("record_id")
        if not isinstance(record, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", record):
            continue
        summary = {key: manifest[key] for key in allowed if key in manifest}
        if not {"record_id", "identity_status", "candidate_pin_status", "identity_status_reasons"} <= summary.keys():
            continue
        target = destination / f"worker-boot-identity-{record}.json"
        with target.open("x", encoding="utf-8") as handle:
            json.dump(summary, handle, sort_keys=True)
            handle.write("\n")
        target.chmod(0o600)
        saved += 1
    except FileExistsError:
        continue
print(f"WORKER_BOOT_IDENTITY_DIAGNOSTICS={saved}")
PY
}

export_model_change_set() {
    # Export the authoritative disposable Change Set before cleanup. A
    # reconstructed candidate is never an input to this path.
    local observation="$ROOT/env/observe.model.json"
    [[ -f $ROOT/env/observe.model.after-stop.json ]] && \
        observation="$ROOT/env/observe.model.after-stop.json"
    /usr/bin/python3 -I - "$ROOT" "$EVIDENCE" "$observation" \
        "$ROOT/db/orchestrator.db" <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tempfile

root, evidence, observation_path, database = map(Path, sys.argv[1:])
runtime_raw = root / "runtime"
if runtime_raw.is_symlink() or not runtime_raw.is_dir():
    raise SystemExit("disposable runtime root is missing or is a symlink")
runtime = runtime_raw.resolve()
if evidence.is_symlink() or not evidence.is_dir():
    raise SystemExit("protected evidence root is missing or is a symlink")
evidence = evidence.resolve()
case_binding_path = root / "env" / "case-binding.json"
if case_binding_path.is_symlink() or not case_binding_path.is_file():
    raise SystemExit("selected case binding is missing")
case_binding = json.loads(case_binding_path.read_text(encoding="utf-8"))
case_id = case_binding.get("id")
case_manifest_sha256 = case_binding.get("manifest_sha256")
if case_id not in {"U1", "U2"} or not isinstance(case_manifest_sha256, str):
    raise SystemExit("selected case binding is invalid")
expected_case_manifests = dict(
    U1="26965b02fe87e35de753267d0b0ee5741c0e555e1e019180a913224543b91f86",
    U2="314142190e698daf19b1b2f3e8d7e64629c9c7644953cb22442458569ee1bd3d",
)
if case_manifest_sha256 != expected_case_manifests[case_id]:
    raise SystemExit("selected case manifest identity mismatch")
export_parent = evidence / "change-sets"
exported = export_parent / "model"


class ExportError(RuntimeError):
    pass


def fail(message):
    raise ExportError(message)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid JSON: {path}: {exc}")


def json_list(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        fail(f"{label} must be a list of strings")
    return value


def safe_relative(value, label):
    if not isinstance(value, str) or not value or value.startswith("/"):
        fail(f"unsafe {label}: {value!r}")
    if "\\" in value:
        fail(f"unsafe {label}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        fail(f"unsafe {label}: {value!r}")
    if path.as_posix() != value:
        fail(f"non-canonical {label}: {value!r}")
    return value


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_files(directory):
    if directory.is_symlink() or not directory.is_dir():
        fail(f"Change Set directory is not a real directory: {directory}")
    for child in directory.iterdir():
        if child.is_symlink():
            fail(f"Change Set contains symlink: {child}")
        if child.name not in {"manifest.json", "files"}:
            fail(f"unexpected Change Set entry: {child.name}")
    manifest = directory / "manifest.json"
    files_root = directory / "files"
    if manifest.is_symlink() or not manifest.is_file():
        fail("Change Set manifest is missing or is not a regular file")
    if files_root.is_symlink() or not files_root.is_dir():
        fail("Change Set files root is missing or is not a real directory")
    found = {}
    for current, directories, files in os.walk(files_root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                fail(f"Change Set contains symlink: {child}")
        for name in files:
            child = current_path / name
            if child.is_symlink() or not child.is_file():
                fail(f"Change Set contains non-regular file: {child}")
            relative = child.relative_to(files_root).as_posix()
            safe_relative(relative, "artifact path")
            found[relative] = child
    return manifest, found


def fsync_file(path):
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_json(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    fsync_file(path)


try:
    observed = read_json(observation_path)
    status = str(observed.get("task_execution_status") or "").lower()
    execution_id = observed.get("task_execution_id")
    if not isinstance(execution_id, int):
        fail("terminal observation has no integer task_execution_id")
    if observed.get("terminal") is not True:
        fail("Change Set export requires a terminal observation")
    if (
        observed.get("case_id") != case_id
        or observed.get("case_manifest_sha256") != case_manifest_sha256
    ):
        fail("terminal observation case identity mismatch")

    database_connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    database_connection.row_factory = sqlite3.Row
    execution = database_connection.execute(
        """
        SELECT id, session_id, task_id, attempt_number, status,
               dispatch_lease_fence
        FROM task_executions WHERE id = ?
        """,
        (execution_id,),
    ).fetchone()
    change_set = database_connection.execute(
        """
        SELECT project_id, task_id, session_id, task_execution_id,
               added_files, modified_files, deleted_files, disposition,
               review_decision, disposition_metadata, status
        FROM task_execution_change_sets WHERE task_execution_id = ?
        """,
        (execution_id,),
    ).fetchone()
    if execution is None:
        fail(f"TaskExecution {execution_id} is absent from authoritative DB")

    identity = {
        "task_execution_id": int(execution["id"]),
        "task_id": int(execution["task_id"]),
        "session_id": int(execution["session_id"]),
        "attempt_number": int(execution["attempt_number"]),
        "dispatch_lease_fence": execution["dispatch_lease_fence"],
    }
    if status != str(execution["status"] or "").lower():
        fail("terminal observation status differs from authoritative DB")
    for key, expected in identity.items():
        if key not in observed or observed[key] != expected:
            fail(f"terminal observation identity mismatch: {key}")

    observed_change = observed.get("change_set")
    if observed_change is None:
        if change_set is not None:
            fail("authoritative Change Set exists but terminal observation omitted it")
        if status != "done":
            write_private_json(
                evidence / "model-change-set-export.json",
                {
                    "schema": "openclaw.p43.model-change-set-export.v1",
                    "status": "not_applicable",
                    "reason": "unsuccessful_task_no_captured_change_set",
                    "case_id": case_id,
                    "case_manifest_sha256": case_manifest_sha256,
                    **identity,
                },
            )
            database_connection.close()
            raise SystemExit(0)
        fail("DONE TaskExecution has no observed captured Change Set")
    if change_set is None:
        fail("terminal observation has a Change Set but authoritative DB row is absent")
    if not isinstance(observed_change, dict):
        fail("terminal observation Change Set is not an object")

    def db_paths(name):
        try:
            return json_list(json.loads(change_set[name] or "[]"), f"DB {name}")
        except (TypeError, json.JSONDecodeError) as exc:
            fail(f"invalid DB {name}: {exc}")

    added = db_paths("added_files")
    modified = db_paths("modified_files")
    deleted = db_paths("deleted_files")
    for label, values in (("added path", added), ("modified path", modified), ("deleted path", deleted)):
        for value in values:
            safe_relative(value, label)
        if len(values) != len(set(values)):
            fail(f"duplicate {label}")
    changed = set(added) | set(modified)
    if set(added) & set(modified):
        fail("DB Change Set overlaps added and modified paths")
    for name, expected in (("task_id", int(change_set["task_id"])), ("session_id", int(change_set["session_id"]))):
        if expected != identity[name]:
            fail(f"authoritative Change Set identity mismatch: {name}")
    if int(change_set["task_execution_id"]) != execution_id:
        fail("authoritative Change Set execution identity mismatch")
    for key, expected in (("added_files", added), ("modified_files", modified), ("deleted_files", deleted)):
        actual = json_list(observed_change.get(key), f"observed {key}")
        if set(actual) != set(expected) or len(actual) != len(expected):
            fail(f"terminal observation differs from authoritative {key}")
    directory = observed_change.get("directory")
    prefix = "/var/lib/orchestrator-worker/runtime/"
    if not isinstance(directory, str) or not directory.startswith(prefix):
        fail("Change Set source is outside the expected disposable artifact root")
    suffix = directory[len(prefix):]
    if not suffix or any(part in {"", ".", ".."} for part in PurePosixPath(suffix).parts):
        fail("unsafe Change Set source path")
    source_cursor = runtime
    for part in PurePosixPath(suffix).parts:
        source_cursor /= part
        if source_cursor.is_symlink():
            fail("Change Set source contains a symlink path component")
    source = runtime / Path(*PurePosixPath(suffix).parts)
    try:
        source.resolve().relative_to(runtime)
    except ValueError:
        fail("Change Set source resolves outside the disposable runtime root")
    if observed_change.get("artifact_present") is not True:
        fail("terminal observation does not confirm a captured artifact")

    source_manifest_path, source_files = artifact_files(source)
    source_manifest_bytes = source_manifest_path.read_bytes()
    source_manifest = read_json(source_manifest_path)
    if source_manifest.get("schema") != "openclaw.task_execution_change_set_artifact.v1":
        fail("unexpected Change Set manifest schema")
    if source_manifest.get("task_execution_id") != execution_id:
        fail("Change Set manifest execution identity mismatch")
    if source_manifest.get("project_id") != int(change_set["project_id"]):
        fail("Change Set manifest project identity mismatch")
    if source_manifest.get("task_id") != identity["task_id"]:
        fail("Change Set manifest task identity mismatch")
    copied = json_list(source_manifest.get("copied_files"), "manifest copied_files")
    manifest_deleted = json_list(source_manifest.get("deleted_files"), "manifest deleted_files")
    for value in copied + manifest_deleted:
        safe_relative(value, "manifest path")
    if len(copied) != len(set(copied)) or len(manifest_deleted) != len(set(manifest_deleted)):
        fail("manifest contains duplicate paths")
    if set(copied) != changed or set(manifest_deleted) != set(deleted):
        fail("manifest scope differs from authoritative Change Set")
    if set(source_files) != set(copied):
        fail("Change Set on-disk files differ from manifest")

    export_parent.mkdir(parents=True, exist_ok=True)
    if export_parent.is_symlink():
        fail("protected Change Set export directory is a symlink")
    os.chmod(export_parent, 0o700)
    if exported.exists() or exported.is_symlink():
        fail(f"refusing to overwrite existing export: {exported}")
    temporary = Path(tempfile.mkdtemp(prefix=".model-change-set-", dir=export_parent))
    os.chmod(temporary, 0o700)
    try:
        shutil.copyfile(source_manifest_path, temporary / "manifest.json")
        os.chmod(temporary / "manifest.json", 0o600)
        files_destination_root = temporary / "files"
        files_destination_root.mkdir(parents=True, exist_ok=True)
        os.chmod(files_destination_root, 0o700)
        for relative, source_file in source_files.items():
            destination = temporary / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(destination.parent, 0o700)
            shutil.copyfile(source_file, destination)
            os.chmod(destination, 0o600)
        copied_hashes = []
        for relative in sorted(source_files):
            source_file = source_files[relative]
            exported_file = temporary / "files" / relative
            source_digest = sha256(source_file)
            exported_digest = sha256(exported_file)
            if source_digest != exported_digest or source_file.stat().st_size != exported_file.stat().st_size:
                fail(f"source/export byte mismatch: {relative}")
            copied_hashes.append(
                {"path": relative, "bytes": source_file.stat().st_size, "sha256": source_digest}
            )
        if sha256(source_manifest_path) != sha256(temporary / "manifest.json"):
            fail("source/export manifest byte mismatch")
        write_private_json(
            temporary / "export-provenance.json",
            {
                "schema": "openclaw.p43.model-change-set-export.v1",
                "status": "complete",
                "case_id": case_id,
                "case_manifest_sha256": case_manifest_sha256,
                "source_directory": str(source),
                "source_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
                "exported_manifest_sha256": sha256(temporary / "manifest.json"),
                "files": copied_hashes,
                "task_id": identity["task_id"],
                "session_id": identity["session_id"],
                "task_execution_id": identity["task_execution_id"],
                "attempt_number": identity["attempt_number"],
                "dispatch_lease_fence": identity["dispatch_lease_fence"],
                "project_id": int(change_set["project_id"]),
                "change_set_disposition": change_set["disposition"],
                "change_set_status": change_set["status"],
                "review_decision": json.loads(change_set["review_decision"] or "null"),
                "disposition_metadata": json.loads(change_set["disposition_metadata"] or "null"),
            },
        )
        fsync_file(temporary / "manifest.json")
        fsync_directory(temporary / "files")
        fsync_directory(temporary)
        os.rename(temporary, exported)
        temporary = None
        fsync_directory(export_parent)
        if not (exported / "export-provenance.json").is_file():
            fail("atomic export boundary is incomplete")
        print(json.dumps({"status": "complete", "directory": str(exported), **identity}, sort_keys=True))
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
    database_connection.close()
except SystemExit:
    raise
except Exception as exc:
    print(f"Change Set export refused: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

cmd_evidence() {
    require_host
    local since; since=$(cat "$ROOT/env/submitted")
    install -d -m 0700 "$EVIDENCE"
    local export_rc=0
    export_model_change_set || export_rc=$?
    cp "$ROOT"/env/submit*.json "$ROOT"/env/observe*.json "$ROOT/env/image.id" "$ROOT/env/run.vars" "$ROOT/env/case-binding.json" \
        "$ROOT/pin/worker-pin.json" "$ROOT/pin/launcher-context.json" "$EVIDENCE/"
    local verdict
    for verdict in "$ROOT"/eval/*/; do
        [[ -d $verdict ]] || continue
        for f in oracle hidden verdict contamination; do
            [[ -f $verdict$f.json ]] && cp "$verdict$f.json" "$EVIDENCE/$(basename "$verdict").$f.json"
        done
    done
    sed -n 's/^\(WORKER_INSTANCE_ID\|CELERY_[A-Z_]*_URL\|CELERY_RESULT_BACKEND\)=/&/p' "$ROOT/env/instance.env" >"$EVIDENCE/instance.env"
    docker image inspect "$IMAGE_TAG" --format '{{.Id}} {{.Created}}' >"$EVIDENCE/image.txt"
    docker logs "$WORKER" 2>&1 | grep -E 'boot identity|C2_SANDBOX|Received task|Reusing stored plan|Step 1|completed successfully|succeeded in|ERROR|WARNING' \
        >"$EVIDENCE/worker.log" || true
    preserve_worker_boot_identity_diagnostics
    provider_mode && preserve_provider_evidence
    journalctl -u "$C2_SERVICE" -b --no-pager -o short-iso >"$EVIDENCE/launcher.journal" || true
    (cd /opt/p43-sandbox-bridge-c2/lib && runuser -u p43-sandbox-c2 -- /usr/bin/python3 -B -E -s -c '
import sqlite3
db = sqlite3.connect("file:/var/lib/p43-sandbox-bridge-c2/ledger/ledger.sqlite3?mode=ro", uri=True)
for row in db.execute("SELECT request_id, admitted, status FROM sandbox_requests ORDER BY admitted"):
    print(*row)') >"$EVIDENCE/ledger.txt" || true
    # Historical vs current-window attribution (ledger.txt is the raw table).
    local ledger_rc=0
    ledger_check final "$(window_start)" || ledger_rc=1
    preserve_ledger_attribution
    docker events --since "$since" --until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --filter type=container \
        --format '{{.TimeNano}} {{.Action}} {{.Actor.Attributes.name}} {{.Actor.Attributes.image}}' |
        grep ' p43-sbx-c2-' >"$EVIDENCE/docker-events.txt" || true
    # Disposable DB (no credentials: the submitter user has an unusable hash).
    /usr/bin/python3 -c 'import sqlite3, sys; sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).backup(sqlite3.connect(sys.argv[2]))' \
        "$ROOT/db/orchestrator.db" "$EVIDENCE/orchestrator.db"
    find "$EVIDENCE" -maxdepth 1 -type f -exec chmod 0600 {} +
    info "evidence in $EVIDENCE (no credentials)"
    [[ $export_rc -eq 0 ]] || {
        info "captured Change Set export failed; disposable root retained"
        return 1
    }
    [[ $ledger_rc -eq 0 ]] || { info "C2 ledger attribution failed: see $EVIDENCE/ledger-final.json"; return 1; }
}

# 23C: provider/relay evidence. Root-only (0700/0600), outside every Worker
# mount; bodies are bounded by the relay (6 x 2 MiB each way); no headers or
# credentials are recorded anywhere.
preserve_provider_evidence() {
    local f
    for f in provider-facts.json contract.env model-window.outcome worker-at-deadline.log; do
        [[ -f $ROOT/env/$f ]] && cp "$ROOT/env/$f" "$EVIDENCE/"
    done
    [[ -f $ROOT/relay-ctl/deadline ]] && cp "$ROOT/relay-ctl/deadline" "$EVIDENCE/evaluation-deadline"
    cp "$ROOT/relay/ledger.jsonl" "$EVIDENCE/relay-ledger.jsonl"
    relay_counts "$ROOT/relay" >"$EVIDENCE/relay-summary.json"
    install -d -m 0700 "$EVIDENCE/relay-bodies"
    cp "$ROOT"/relay/bodies/* "$EVIDENCE/relay-bodies/" 2>/dev/null || true
    chmod 0600 "$EVIDENCE"/relay-bodies/* 2>/dev/null || true
    docker logs "$RELAY" >"$EVIDENCE/relay.log" 2>&1 || true
    docker logs "$WORKER" 2>&1 | grep -E 'RETRY|HTTP Request|Discovery|Planning|Debug Repair|Completion|TaskExecution|FAILED|terminal' \
        >"$EVIDENCE/worker-provider.log" || true
    info "relay: $(/usr/bin/python3 -I -c 'import json, sys; c = json.load(open(sys.argv[1])); print("admitted=%d forwarded=%d completed=%d refused=%s model_lists=%d" % (c["completions_admitted"], c["completions_forwarded"], c["completions_completed"], c["refused"], c["model_lists"]))' "$EVIDENCE/relay-summary.json")"
}

preserve_ledger_attribution() { # copy ledger-*.json from the disposable root; never overwrite
    local f
    [[ -d $ROOT/env ]] || return 0
    [[ ! -L $EVIDENCE ]] || return 1
    [[ -d $EVIDENCE ]] || install -d -m 0700 "$EVIDENCE"
    for f in "$ROOT"/env/ledger-*.json; do
        [[ -f $f && ! -L $f && ! -e $EVIDENCE/$(basename "$f") ]] && cp -- "$f" "$EVIDENCE/"
    done
    chmod 0600 "$EVIDENCE"/ledger-*.json 2>/dev/null || true
}

# ---------------------------------------------------------------- cleanup
cmd_cleanup() {
    need_approval "${1:-}"
    [[ $EUID -eq 0 ]] || refuse "must run as root on the Host"
    if ! preserve_worker_boot_identity_diagnostics; then
        bad "Worker boot identity diagnostics could not be preserved"
    fi
    docker rm -f p43-c2-submitter "$WORKER" "$RELAY" >/dev/null 2>&1 || true
    systemctl stop "$C2_SERVICE" 2>/dev/null || true
    docker ps -a --format '{{.Names}}' | grep -E '^p43-sbx-c2-[0-9a-f]{32}$' | xargs -r docker rm -f || true
    docker rm -f "$REDIS" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker image rm "$IMAGE_TAG" >/dev/null 2>&1 || true
    "$HOST_SCRIPT" c2-rollback --operator-approved
    # The persistent ledger is never removed here; verify it survived the window.
    if [[ -f $ROOT/env/ledger-baseline.json ]]; then
        ledger_check cleanup "$(window_start)" &&
            ok "historical C2 ledger preserved; current-window requests attributed" ||
            bad "C2 ledger after cleanup: $(cat "$ROOT/env/ledger-cleanup.json" 2>/dev/null || echo unreadable)"
        preserve_ledger_attribution || bad "C2 ledger attribution could not be preserved"
    else
        info "no C2 ledger baseline (setup did not reach Launcher startup)"
    fi
    local before=""
    [[ -f $ROOT/env/c1b-before ]] && before=$(cat "$ROOT/env/c1b-before")
    rm -rf --one-file-system -- "$ROOT"
    check_no_c2_containers "no p43-c2 containers remain" "p43-c2 containers remain"
    docker network inspect "$NET" >/dev/null 2>&1 && bad "network remains" || ok "network removed"
    systemctl is-active --quiet "$C2_SERVICE" && bad "C2 Launcher active" || ok "C2 Launcher stopped"
    [[ ! -e $C2_WORKER_KEY ]] && ok "C2 Worker key copy removed" || bad "C2 Worker key copy remains"
    [[ ! -e $ROOT ]] && ok "$ROOT removed (Redis credential and disposable DB gone)" || bad "$ROOT remains"
    [[ -z $before || "$before" == "$(c1b_fingerprint)" ]] && ok "C1B Launcher and ai-agent unchanged" ||
        bad "C1B/ai-agent fingerprint changed"
    printf 'C2_CLEANUP=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"
    [[ $FAILURES -eq 0 ]]
}

# ---------------------------------------------------------------- 23C window
# One approval covers the whole controlled window. Each stage is its own
# process, so a refusal ends that stage only; once the model is submitted,
# evaluation, evidence and cleanup always run. No stage is retried and the
# model is submitted at most once. Evidence precedes cleanup and cleanup runs
# once: after success, after a stage failure, and after an INT/TERM/HUP
# (handled once the running stage returns). The Worker and the relay are
# stopped first, so the recorded state is final.
cmd_run_model_window() {
    need_approval "${1:-}"
    provider_mode_requested || refuse "run-model-window requires P43_C2_PROVIDER_UPSTREAM/MODEL/NETWORK"
    require_provider_inputs
    WINDOW_SELF=("$BASH" "$REPO/scripts/maintenance/p43_c2_live_run.sh")
    WINDOW_RC=0
    WINDOW_STATUS=""
    trap model_window_interrupted INT TERM HUP
    if ! "${WINDOW_SELF[@]}" setup --operator-approved; then
        WINDOW_STATUS=SETUP_FAILED
    elif ! "${WINDOW_SELF[@]}" boundary; then
        WINDOW_STATUS=BOUNDARY_FAILED
    else
        "${WINDOW_SELF[@]}" submit --operator-approved model || WINDOW_RC=1
        "${WINDOW_SELF[@]}" evaluate model || WINDOW_RC=1
    fi
    model_window_finish
}

model_window_interrupted() {
    WINDOW_STATUS=INTERRUPTED
    info "interrupted; stopping the window, then evidence and cleanup"
    model_window_finish
    exit 130
}

model_window_finish() {
    trap '' INT TERM HUP
    set +e
    [[ -z $WINDOW_STATUS ]] || WINDOW_RC=1
    docker stop --time 30 p43-c2-submitter "$WORKER" "$RELAY" >/dev/null 2>&1
    if [[ -f $ROOT/env/submitted ]]; then
        if [[ ! -f $ROOT/env/model-window.outcome ]]; then
            if [[ $WINDOW_STATUS == INTERRUPTED ]]; then
                printf 'interrupted\n' >"$ROOT/env/model-window.outcome"
            else
                printf 'submission_incomplete\n' >"$ROOT/env/model-window.outcome"
            fi
        fi
        if [[ ! -f $ROOT/env/observe.model.after-stop.json ]] &&
            ! grep -qx terminal "$ROOT/env/model-window.outcome"; then
            observe_once "$ROOT/env/observe.model.after-stop.json" 0
        fi
        if ! "${WINDOW_SELF[@]}" evidence; then
            info "evidence failed; Worker and relay stopped, $ROOT retained; inspect, then run: cleanup --operator-approved"
            printf 'C2_MODEL_WINDOW=EVIDENCE_FAILED_STATE_RETAINED\n'
            WINDOW_RC=1
            return 1
        fi
    else
        model_window_presubmission_capture
    fi
    "${WINDOW_SELF[@]}" cleanup --operator-approved || WINDOW_RC=1
    if [[ -z $WINDOW_STATUS ]]; then
        WINDOW_STATUS=COMPLETE
        [[ $WINDOW_RC -eq 0 ]] || WINDOW_STATUS=COMPLETE_WITH_FAILURES
    fi
    printf 'C2_MODEL_WINDOW=%s\n' "$WINDOW_STATUS"
    return "$WINDOW_RC"
}

# Before any submission no completion can be admitted (window unarmed); keep
# the diagnostics that cleanup would otherwise remove.
model_window_presubmission_capture() {
    [[ ! -L $EVIDENCE ]] || return 1
    install -d -m 0700 "$EVIDENCE"
    local f
    for f in run.vars provider-facts.json contract.env worker-startup.log boundary.pass; do
        [[ -f $ROOT/env/$f && ! -L $ROOT/env/$f ]] && cp -- "$ROOT/env/$f" "$EVIDENCE/presubmission-$f"
    done
    [[ -f $ROOT/relay/ledger.jsonl ]] && cp -- "$ROOT/relay/ledger.jsonl" "$EVIDENCE/presubmission-relay-ledger.jsonl"
    docker logs "$WORKER" >"$EVIDENCE/presubmission-worker.log" 2>&1
    docker logs "$RELAY" >"$EVIDENCE/presubmission-relay.log" 2>&1
    chmod 0600 "$EVIDENCE"/presubmission-* 2>/dev/null
    info "pre-submission diagnostics in $EVIDENCE"
    return 0
}

case "${1:-}" in
    check) cmd_check ;;
    setup) cmd_setup "${2:-}" ;;
    boundary) cmd_boundary ;;
    submit) cmd_submit "${2:-}" "${3:-}" ;;
    evaluate) cmd_evaluate "${2:-}" ;;
    evidence) cmd_evidence ;;
    cleanup) cmd_cleanup "${2:-}" ;;
    run-model-window) cmd_run_model_window "${2:-}" ;;
    *) echo "usage: $0 {check|setup --operator-approved|boundary|submit --operator-approved [exit-check|reference|probe|model]|evaluate reference|naive|model|evidence|cleanup --operator-approved|run-model-window --operator-approved}" >&2; exit 64 ;;
esac
