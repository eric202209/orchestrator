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
#   export P43_C2_U1_EVIDENCE=<dir holding p43-product-02/ and p43-product-18/>
#   ... submit --operator-approved reference ; ... evaluate reference
#   ... evaluate naive                       # frozen control, never submitted
#   ... submit --operator-approved probe
# The frozen material is copied by the committed submitter, byte-verified
# against the pinned frozen manifest. Only manifest, task, baseline and the
# reference control reach the submitter; the hidden suite is mounted only into
# the network-less evaluation container on the pinned pytest image.
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
AI_AGENT=ai-agent
# The Sandbox image the Launcher already pins; reused as the Worker base.
BASE_IMAGE=$(sed -n 's/^IMAGE = "\(.*\)"$/\1/p' "$REPO/app/services/sandbox_bridge/docker_executor.py")
SUBMITTER=$REPO/scripts/maintenance/p43_c2_trusted_submitter.py
PYTEST_IMAGE=""
FAILURES=0

ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
info() { printf 'INFO %s\n' "$*"; }
refuse() { echo "p43-c2-live-run: $*" >&2; exit 78; }
need_approval() { [[ "${1:-}" == --operator-approved ]] || refuse "requires --operator-approved"; }

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
    [[ "${P43_C2_CANDIDATE:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "P43_C2_CANDIDATE must be a 40-hex commit"
    [[ "${P43_C2_CANDIDATE_TREE:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "P43_C2_CANDIDATE_TREE must be a 40-hex tree"
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

    # Frozen U1 material (PRODUCT-23B), copied and verified by committed code.
    if [[ -n ${P43_C2_U1_EVIDENCE:-} ]]; then
        /usr/bin/python3 -I "$SUBMITTER" stage-material "$P43_C2_U1_EVIDENCE" "$ROOT/u1-submit" --parts submit
        /usr/bin/python3 -I "$SUBMITTER" stage-material "$P43_C2_U1_EVIDENCE" "$ROOT/u1-eval" --parts evaluation
        ok "U1 material staged and verified against the pinned frozen manifest"
    fi

    # Strict C2 Launcher (separate user/key/state/socket; C1B untouched).
    "$HOST_SCRIPT" c2-install --operator-approved
    c2gid=$(getent group p43-sandbox-c2 | cut -d: -f3)
    # Orphan recovery: a stopped Sandbox-named container on the pinned pytest
    # image must be removed by the strict Launcher's startup.
    local orphan
    orphan=p43-sbx-c2-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex)')
    docker create --pull=never --name "$orphan" --network none "$PYTEST_IMAGE" true >/dev/null
    systemctl start "$C2_SERVICE"
    for _ in $(seq 50); do [[ -S $C2_RUN/launcher.sock ]] && break; sleep 0.2; done
    [[ -S $C2_RUN/launcher.sock ]] || refuse "C2 Launcher socket did not appear"
    if docker inspect "$orphan" >/dev/null 2>&1; then
        refuse "Launcher startup did not remove the orphan $orphan"
    fi
    ok "Launcher startup removed the pytest-image orphan $orphan"
    printf 'C2_GID=%s\nNODE=%s\nCANDIDATE_TREE=%s\nPYTEST_IMAGE=%s\nORPHAN=%s\n' \
        "$c2gid" "$node" "$tree" "$PYTEST_IMAGE" "$orphan" >"$ROOT/env/run.vars"

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
    expect "Network members" "$REDIS $WORKER" \
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
    fmt='{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.HostConfig.CapDrop}} {{.Config.User}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}'
    expect "Redis container" "true false [ALL] $REDIS_UID:$REDIS_UID $NET" "$(docker inspect "$REDIS" --format "$fmt")"
    systemctl is-active --quiet "$C2_SERVICE" && ok "C2 Launcher active" || bad "C2 Launcher inactive"
    strict_launcher_started && ok "strict Launcher startup line" || bad "strict Launcher startup line missing"
    expect "C2 ledger empty before submission" '"ledger": {}' \
        "$(cd /opt/p43-sandbox-bridge-c2/lib && runuser -u p43-sandbox-c2 -- env P43_SANDBOX_BRIDGE_INSTANCE=c2 /usr/bin/python3 -B -E -s -m sandbox_bridge.host_service status c2 | grep -o '"ledger": {[^}]*}')"
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
    local control=${2:-exit-check} sfx material=()
    [[ $control =~ ^(exit-check|reference|probe)$ ]] || refuse "unknown control: $control"
    require_host
    [[ -f $ROOT/env/boundary.pass ]] || refuse "boundary has not passed; run: $0 boundary"
    sfx=$(control_suffix "$control")
    [[ ! -e $ROOT/env/submitted$sfx ]] || refuse "$control already submitted once; no retry"
    if [[ $control == reference ]]; then
        [[ -d $ROOT/u1-submit ]] || refuse "U1 material not staged (set P43_C2_U1_EVIDENCE before setup)"
        material=(-v "$ROOT/u1-submit:/run/p43-u1-material:ro")
    fi
    date -u +%Y-%m-%dT%H:%M:%SZ >"$ROOT/env/submitted$sfx"
    [[ -e $ROOT/env/submitted ]] || cp "$ROOT/env/submitted$sfx" "$ROOT/env/submitted"
    # Same image, same DB; no Launcher key/socket; shares only the Worker PID
    # namespace, which the existing publish-side identity binding inspects.
    # shellcheck disable=SC2046
    docker_run_image --name p43-c2-submitter --network "$NET" --pid "container:$WORKER" \
        -w /opt/orchestrator --env-file "$ROOT/env/submitter.env" \
        --env-file "$ROOT/env/instance.env" --env-file "$ROOT/env/secrets.env" \
        $(authority_mounts) "${material[@]}" --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter submit --control "$control" |
        tee "$ROOT/env/submit$sfx.json"
    # Observation needs only the database: no network, no broker credential.
    # shellcheck disable=SC2046
    docker_run_image --network none -w /opt/orchestrator \
        --env-file "$ROOT/env/submitter.env" --env-file "$ROOT/env/instance.env" \
        $(authority_mounts) --entrypoint python3 "$IMAGE_TAG" \
        -m scripts.maintenance.p43_c2_trusted_submitter observe --control "$control" \
        --deadline-seconds 600 | tee "$ROOT/env/observe$sfx.json"
}

# ---------------------------------------------------------------- evaluate
eval_container() { # independent evaluation: pinned pytest image, no network, read-only
    docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges:true --user=65534:65534 --memory=1g \
        --pids-limit=128 --tmpfs /tmp:rw,nosuid,nodev,size=256m -e HOME=/tmp -e TMPDIR=/tmp \
        -v "$ROOT/u1-eval:/eval:ro" -v "$1:/cs:ro" --entrypoint python3 "$PYTEST_IMAGE" -I "${@:2}"
}

cmd_evaluate() {
    local control=${1:-} out source cs observed=() u1=/eval/p43-product-18/u1
    [[ $control =~ ^(reference|naive)$ ]] || refuse "unknown evaluation control: ${control:-none}"
    require_host
    # shellcheck disable=SC1091
    source "$ROOT/env/run.vars"
    [[ -d $ROOT/u1-eval ]] || refuse "U1 material not staged"
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
    else
        source=$ROOT/u1-eval/p43-product-18/u1/controls/naive
    fi
    [[ -f $source/manifest.json && ! -L $source ]] || refuse "Change Set missing: $source"
    # A private, read-only copy: the Worker cannot change what is judged.
    cs=$out/change-set
    cp -R --no-preserve=ownership,mode "$source" "$cs"
    find "$cs" -type l | grep -q . && refuse "Change Set contains links"
    chmod -R a+rX,go-w "$cs"
    eval_container "$cs" /eval/p43-product-02/oracle.py "$u1/baseline" /cs \
        tests/test_rangekit_acceptance.py tests/test_ranges.py >"$out/oracle.json" || true
    eval_container "$cs" /eval/p43-product-18/tools/hidden_suite.py "$u1/baseline" /cs \
        "$u1/hidden" >"$out/hidden.json" || true
    /usr/bin/python3 -I "$SUBMITTER" verdict "$ROOT/u1-eval" "$cs" --control "$control" \
        --oracle "$out/oracle.json" --hidden "$out/hidden.json" "${observed[@]}" | tee "$out/verdict.json"
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

cmd_evidence() {
    require_host
    local since; since=$(cat "$ROOT/env/submitted")
    install -d -m 0700 "$EVIDENCE"
    cp "$ROOT"/env/submit*.json "$ROOT"/env/observe*.json "$ROOT/env/image.id" "$ROOT/env/run.vars" \
        "$ROOT/pin/worker-pin.json" "$ROOT/pin/launcher-context.json" "$EVIDENCE/"
    local verdict
    for verdict in "$ROOT"/eval/*/; do
        [[ -d $verdict ]] || continue
        for f in oracle hidden verdict; do
            [[ -f $verdict$f.json ]] && cp "$verdict$f.json" "$EVIDENCE/$(basename "$verdict").$f.json"
        done
    done
    sed -n 's/^\(WORKER_INSTANCE_ID\|CELERY_[A-Z_]*_URL\|CELERY_RESULT_BACKEND\)=/&/p' "$ROOT/env/instance.env" >"$EVIDENCE/instance.env"
    docker image inspect "$IMAGE_TAG" --format '{{.Id}} {{.Created}}' >"$EVIDENCE/image.txt"
    docker logs "$WORKER" 2>&1 | grep -E 'boot identity|C2_SANDBOX|Received task|Reusing stored plan|Step 1|completed successfully|succeeded in|ERROR|WARNING' \
        >"$EVIDENCE/worker.log" || true
    preserve_worker_boot_identity_diagnostics
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
    if ! preserve_worker_boot_identity_diagnostics; then
        bad "Worker boot identity diagnostics could not be preserved"
    fi
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

case "${1:-}" in
    check) cmd_check ;;
    setup) cmd_setup "${2:-}" ;;
    boundary) cmd_boundary ;;
    submit) cmd_submit "${2:-}" "${3:-}" ;;
    evaluate) cmd_evaluate "${2:-}" ;;
    evidence) cmd_evidence ;;
    cleanup) cmd_cleanup "${2:-}" ;;
    *) echo "usage: $0 {check|setup --operator-approved|boundary|submit --operator-approved [exit-check|reference|probe]|evaluate reference|naive|evidence|cleanup --operator-approved}" >&2; exit 64 ;;
esac
