#!/usr/bin/env bash
# P43-PRODUCT-22-C2-C3B: ONE controlled disposable live TaskExecution through the
# restricted Worker, the strict C2 Host Launcher, and a real Docker sandbox.
#
# Run on the GX10 Host as root, from a root-only reviewed clone checked out at
# the candidate commit (C1A rule: never from a tree ai-agent can modify):
#
#   export P43_C2_CANDIDATE=<40-hex commit>      # the reviewed, committed candidate
#   export P43_C2_REDIS_IMAGE=redis:<tag>@sha256:<64-hex>   # operator-supplied pin
#   export P43_C2_OPERATOR=<operator id>          # recorded in the candidate pin
#   bash scripts/maintenance/p43_c2_live_run.sh check
#   bash scripts/maintenance/p43_c2_live_run.sh setup --operator-approved
#   bash scripts/maintenance/p43_c2_live_run.sh boundary
#   bash scripts/maintenance/p43_c2_live_run.sh submit --operator-approved
#   bash scripts/maintenance/p43_c2_live_run.sh evidence
#   bash scripts/maintenance/p43_c2_live_run.sh cleanup --operator-approved
#
# Every resource it creates is named p43-c2-* / orchestrator-worker:c2c3b or
# lives under /srv/p43-c2. It never modifies p43-sandbox-bridge.service (C1B),
# ai-agent, the production database, Redis, or workspace. No automatic retry.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
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
AI_AGENT=ai-agent
# The Sandbox image the Launcher already pins; reused as the Worker base.
BASE_IMAGE=$(sed -n 's/^IMAGE = "\(.*\)"$/\1/p' "$REPO/app/services/sandbox_bridge/docker_executor.py")
FAILURES=0

ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
info() { printf 'INFO %s\n' "$*"; }
refuse() { echo "p43-c2-live-run: $*" >&2; exit 78; }
need_approval() { [[ "${1:-}" == --operator-approved ]] || refuse "requires --operator-approved"; }

require_host() {
    [[ $EUID -eq 0 ]] || refuse "must run as root on the Host"
    [[ ! -e /.dockerenv && ! -e /run/.containerenv ]] || refuse "must run on the Host, not in a container"
    [[ -z "$(git -C "$REPO" status --porcelain)" ]] || refuse "reviewed clone is not clean"
    [[ "$(git -C "$REPO" rev-parse HEAD)" == "${P43_C2_CANDIDATE:-}" ]] ||
        refuse "clone HEAD is not P43_C2_CANDIDATE"
    [[ "$(stat -c '%u' "$REPO")" == 0 ]] || refuse "reviewed clone is not root-owned"
}

require_inputs() {
    [[ "${P43_C2_CANDIDATE:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "P43_C2_CANDIDATE must be a 40-hex commit"
    [[ "${P43_C2_REDIS_IMAGE:-}" =~ ^[a-z0-9./_:-]+@sha256:[0-9a-f]{64}$ ]] ||
        refuse "P43_C2_REDIS_IMAGE must be digest-pinned (operator-supplied)"
    [[ "${P43_C2_OPERATOR:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}$ ]] || refuse "P43_C2_OPERATOR is required"
    [[ "$BASE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || refuse "pinned Sandbox base image not found"
}

instance_value() { sed -n "s/^$1=//p" "$ROOT/env/instance.env"; }

c1b_fingerprint() {
    printf '%s|%s|%s\n' \
        "$(systemctl show "$C1B_SERVICE" -p MainPID -p ActiveEnterTimestamp --value | tr '\n' ' ')" \
        "$(sha256sum /opt/p43-sandbox-bridge/MANIFEST.sha256 2>/dev/null | cut -d' ' -f1)" \
        "$(docker inspect "$AI_AGENT" --format '{{.Id}} {{.State.StartedAt}}' 2>/dev/null)"
}

# ---------------------------------------------------------------- check
cmd_check() {
    require_inputs
    require_host
    docker ps -a --format '{{.Names}}' | grep -Eq '^(p43-c2-|p43-sbx-c2-)' &&
        bad "a p43-c2-*/p43-sbx-c2-* container already exists" || ok "no p43-c2 containers"
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
    "$HOST_SCRIPT" c2-preflight || bad "c2-preflight"
    info "C1B/ai-agent fingerprint: $(c1b_fingerprint)"
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
        -v "$ROOT/pin/worker-pin.json:/var/lib/orchestrator-worker/identity/worker-pin.json:ro" \
        -v "$ROOT/workspace:/workspace/authoritative"
}

cmd_setup() {
    need_approval "${1:-}"
    cmd_check
    c1b_fingerprint >"$ROOT.c1b-before" 2>/dev/null || true
    local tree inst node pw c2gid interp
    tree=$(git -C "$REPO" rev-parse HEAD^{tree})
    inst=c2-c1-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex[:16])')
    node=orchestrator-worker-c2-c1@$inst

    install -d -o root -g root -m 0700 "$ROOT" "$ROOT/env" "$ROOT/pin" "$ROOT/redis"
    mv "$ROOT.c1b-before" "$ROOT/env/c1b-before"
    install -d -o "$WORKER_UID" -g "$WORKER_UID" -m 0700 \
        "$ROOT/db" "$ROOT/runtime" "$ROOT/checkpoints" "$ROOT/identity" "$ROOT/workspace"

    # Non-secret contract files (no duplicate keys: docker env precedence is moot).
    local template=$REPO/config/worker-c2-live.env.example
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

    # Strict C2 Launcher (separate user/key/state/socket; C1B untouched).
    "$HOST_SCRIPT" c2-install --operator-approved
    c2gid=$(getent group p43-sandbox-c2 | cut -d: -f3)
    systemctl start "$C2_SERVICE"
    for _ in $(seq 50); do [[ -S $C2_RUN/launcher.sock ]] && break; sleep 0.2; done
    [[ -S $C2_RUN/launcher.sock ]] || refuse "C2 Launcher socket did not appear"
    printf 'C2_GID=%s\nNODE=%s\nCANDIDATE_TREE=%s\n' "$c2gid" "$node" "$tree" >"$ROOT/env/run.vars"

    # Worker image from a git-archive release of exactly the candidate commit.
    bash "$REPO/scripts/maintenance/p43_c2_worker_release.sh" stage "$P43_C2_CANDIDATE" "$ROOT/context"
    docker build -q -f "$ROOT/context/Dockerfile.worker" \
        --build-arg "PYTHON_BASE_IMAGE=$BASE_IMAGE" \
        --build-arg "ORCHESTRATOR_GIT_SHA=$P43_C2_CANDIDATE" \
        -t "$IMAGE_TAG" "$ROOT/context" >"$ROOT/env/image.id"
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
    docker_run_image --network none \
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
    for _ in $(seq 60); do
        docker logs "$WORKER" 2>&1 | grep -q 'Worker boot identity manifest' && break; sleep 1
    done
    docker logs "$WORKER" 2>&1 | grep 'Worker boot identity manifest' || bad "no boot identity line"
    info "setup complete; run: $0 boundary"
}

# ---------------------------------------------------------------- boundary
expect() { # LABEL EXPECTED ACTUAL
    [[ "$2" == "$3" ]] && ok "$1" || bad "$1: expected '$2' got '$3'"
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
    mounts=$(docker inspect "$WORKER" --format '{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}}{{"\n"}}{{end}}' | sort)
    expected=$(printf '%s\n' \
        "$ROOT/db:/var/lib/orchestrator-worker/db:true" \
        "$ROOT/runtime:/var/lib/orchestrator-worker/runtime:true" \
        "$ROOT/checkpoints:/var/lib/orchestrator-worker/checkpoints:true" \
        "$ROOT/identity:/var/lib/orchestrator-worker/identity:true" \
        "$ROOT/pin/worker-pin.json:/var/lib/orchestrator-worker/identity/worker-pin.json:false" \
        "$ROOT/workspace:/workspace/authoritative:true" \
        "$C2_RUN:$C2_RUN:false" \
        "$C2_WORKER_KEY:/run/secrets/p43-sandbox-bridge-c2.key:false" | sort)
    expect "Worker effective mounts (exact)" "$expected" "$mounts"
    nets=$(docker inspect "$WORKER" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')
    expect "Worker networks" "$NET " "$nets"
    expect "Network internal" "true" "$(docker network inspect "$NET" --format '{{.Internal}}')"
    expect "Network members" "$REDIS $WORKER" \
        "$(docker network inspect "$NET" --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | sort | xargs)"
    for p in /var/run/docker.sock /run/docker.sock /run/secrets/p43-sandbox-bridge.key /run/p43-sandbox-bridge/launcher.sock; do
        docker exec "$WORKER" test ! -e "$p" && ok "absent in Worker: $p" || bad "visible in Worker: $p"
    done
    expect "C2 key in Worker" "$WORKER_UID 400" "$(docker exec "$WORKER" stat -c '%u %a' /run/secrets/p43-sandbox-bridge-c2.key)"
    expect "Worker launch line" "1" "$(docker exec "$WORKER" sh -c 'tr "\0" " " </proc/1/cmdline' | grep -c -- "--pool=solo --concurrency=1 --loglevel=INFO --hostname=$NODE")"
    docker logs "$WORKER" 2>&1 | grep -q 'Worker boot identity manifest created: .*status=IDENTITY_VERIFIED pin_status=PIN_MATCHED' &&
        ok "boot identity VERIFIED with PIN_MATCHED" || bad "boot identity not verified against the pin"
    local probe='import socket,sys
for host, port in (("model-provider", 11434), ("ai-gateway", 8000), ("ai-agent", 6379), ("1.1.1.1", 443)):
    try:
        socket.create_connection((host, port), 3).close(); print("REACHABLE", host); sys.exit(1)
    except OSError:
        pass
print("NO_EGRESS")'
    expect "No provider/production/egress reachability" "NO_EGRESS" "$(docker exec "$WORKER" python3 -c "$probe" 2>&1 | tail -1)"
    fmt='{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.HostConfig.CapDrop}} {{.Config.User}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}'
    expect "Redis container" "true false [ALL] $REDIS_UID:$REDIS_UID $NET" "$(docker inspect "$REDIS" --format "$fmt")"
    systemctl is-active --quiet "$C2_SERVICE" && ok "C2 Launcher active" || bad "C2 Launcher inactive"
    journalctl -u "$C2_SERVICE" -b --no-pager | grep -q 'startup .*allowed_uid=10001 max_active=1 require_authorization=True' &&
        ok "strict Launcher startup line" || bad "strict Launcher startup line missing"
    expect "C2 ledger empty before submission" '"ledger": {}' \
        "$(cd /opt/p43-sandbox-bridge-c2/lib && runuser -u p43-sandbox-c2 -- env P43_SANDBOX_BRIDGE_INSTANCE=c2 /usr/bin/python3 -B -E -s -m sandbox_bridge.host_service status c2 | grep -o '"ledger": {[^}]*}')"
    docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' |
        grep -Eq '^(/etc/p43-sandbox-bridge-c2|/var/lib/p43-sandbox-bridge-c2|/run/p43-sandbox-bridge-c2|/srv/p43-c2)(/|$)' &&
        bad "ai-agent mounts C2 material" || ok "ai-agent mounts no C2 material"
    docker exec "$AI_AGENT" sh -c "test ! -e $C2_RUN/launcher.sock && test ! -e $C2_WORKER_KEY && test ! -e $ROOT" &&
        ok "C2 socket/key/state not visible in ai-agent" || bad "C2 material visible in ai-agent"
    expect "C1B Launcher and ai-agent unchanged" "$(cat "$ROOT/env/c1b-before")" "$(c1b_fingerprint)"
    printf 'C2_BOUNDARY=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"
    if [[ $FAILURES -eq 0 ]]; then date -u +%s >"$ROOT/env/boundary.pass"; else rm -f "$ROOT/env/boundary.pass"; return 1; fi
}

# ---------------------------------------------------------------- submit
cmd_submit() {
    need_approval "${1:-}"
    require_host
    [[ -f $ROOT/env/boundary.pass ]] || refuse "boundary has not passed; run: $0 boundary"
    [[ ! -e $ROOT/env/submitted ]] || refuse "already submitted once; no retry"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$ROOT/env/submitted"
    # Same image, same DB; no Launcher key/socket; shares only the Worker PID
    # namespace, which the existing publish-side identity binding inspects.
    # shellcheck disable=SC2046
    docker_run_image --name p43-c2-submitter --network "$NET" --pid "container:$WORKER" \
        -w /opt/orchestrator --env-file "$ROOT/env/submitter.env" \
        --env-file "$ROOT/env/instance.env" --env-file "$ROOT/env/secrets.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter submit | tee "$ROOT/env/submit.json"
    # Observation needs only the database: no network, no broker credential.
    # shellcheck disable=SC2046
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/submitter.env" --env-file "$ROOT/env/instance.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter observe --deadline-seconds 600 |
        tee "$ROOT/env/observe.json"
}

# ---------------------------------------------------------------- evidence
cmd_evidence() {
    require_host
    local since; since=$(cat "$ROOT/env/submitted")
    install -d -m 0700 "$EVIDENCE"
    cp "$ROOT/env/submit.json" "$ROOT/env/observe.json" "$ROOT/env/image.id" "$ROOT/env/run.vars" \
        "$ROOT/pin/worker-pin.json" "$ROOT/pin/launcher-context.json" "$EVIDENCE/"
    sed -n 's/^\(WORKER_INSTANCE_ID\|CELERY_[A-Z_]*_URL\|CELERY_RESULT_BACKEND\)=/&/p' "$ROOT/env/instance.env" >"$EVIDENCE/instance.env"
    docker image inspect "$IMAGE_TAG" --format '{{.Id}} {{.Created}}' >"$EVIDENCE/image.txt"
    docker logs "$WORKER" 2>&1 | grep -E 'boot identity|C2_SANDBOX|Received task|Reusing stored plan|Step 1|completed successfully|succeeded in|ERROR|WARNING' \
        >"$EVIDENCE/worker.log" || true
    journalctl -u "$C2_SERVICE" -b --no-pager -o short-iso >"$EVIDENCE/launcher.journal" || true
    (cd /opt/p43-sandbox-bridge-c2/lib && runuser -u p43-sandbox-c2 -- /usr/bin/python3 -B -E -s -c '
import sqlite3
db = sqlite3.connect("file:/var/lib/p43-sandbox-bridge-c2/ledger/ledger.sqlite3?mode=ro", uri=True)
for row in db.execute("SELECT request_id, admitted, status FROM sandbox_requests ORDER BY admitted"):
    print(*row)') >"$EVIDENCE/ledger.txt" || true
    docker events --since "$since" --until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --filter type=container \
        --format '{{.TimeNano}} {{.Action}} {{.Actor.Attributes.name}} {{.Actor.Attributes.image}}' |
        grep ' p43-sbx-c2-' >"$EVIDENCE/docker-events.txt" || true
    # Disposable DB (no credentials: the submitter user has an unusable hash).
    /usr/bin/python3 -c 'import sqlite3, sys; sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).backup(sqlite3.connect(sys.argv[2]))' \
        "$ROOT/db/orchestrator.db" "$EVIDENCE/orchestrator.db"
    chmod 0600 "$EVIDENCE"/*
    info "evidence in $EVIDENCE (no credentials)"
}

# ---------------------------------------------------------------- cleanup
cmd_cleanup() {
    need_approval "${1:-}"
    [[ $EUID -eq 0 ]] || refuse "must run as root on the Host"
    docker rm -f p43-c2-submitter "$WORKER" >/dev/null 2>&1 || true
    systemctl stop "$C2_SERVICE" 2>/dev/null || true
    docker ps -a --format '{{.Names}}' | grep -E '^p43-sbx-c2-[0-9a-f]{32}$' | xargs -r docker rm -f || true
    docker rm -f "$REDIS" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker image rm "$IMAGE_TAG" >/dev/null 2>&1 || true
    "$HOST_SCRIPT" c2-rollback --operator-approved
    local before=""
    [[ -f $ROOT/env/c1b-before ]] && before=$(cat "$ROOT/env/c1b-before")
    rm -rf --one-file-system -- "$ROOT"
    docker ps -a --format '{{.Names}}' | grep -Eq '^(p43-c2-|p43-sbx-c2-)' && bad "p43-c2 containers remain" || ok "no p43-c2 containers remain"
    docker network inspect "$NET" >/dev/null 2>&1 && bad "network remains" || ok "network removed"
    systemctl is-active --quiet "$C2_SERVICE" && bad "C2 Launcher active" || ok "C2 Launcher stopped"
    [[ ! -e $C2_WORKER_KEY ]] && ok "C2 Worker key copy removed" || bad "C2 Worker key copy remains"
    [[ ! -e $ROOT ]] && ok "$ROOT removed (Redis credential and disposable DB gone)" || bad "$ROOT remains"
    [[ -z $before || "$before" == "$(c1b_fingerprint)" ]] && ok "C1B Launcher and ai-agent unchanged" ||
        bad "C1B/ai-agent fingerprint changed"
    printf 'C2_CLEANUP=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"
    [[ $FAILURES -eq 0 ]]
}

case "${1:-}" in
    check) cmd_check ;;
    setup) cmd_setup "${2:-}" ;;
    boundary) cmd_boundary ;;
    submit) cmd_submit "${2:-}" ;;
    evidence) cmd_evidence ;;
    cleanup) cmd_cleanup "${2:-}" ;;
    *) echo "usage: $0 {check|setup --operator-approved|boundary|submit --operator-approved|evidence|cleanup --operator-approved}" >&2; exit 64 ;;
esac
