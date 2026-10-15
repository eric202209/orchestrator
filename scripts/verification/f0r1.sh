#!/usr/bin/env bash
# Phase 37 OPS1-B4B-DP3-F0-R1 — disposable deployment feasibility rehearsal.
#
# Subcommands:
#   plan       (default) print the rehearsal plan; no mutation.
#   stage DIR  HOST side. Build a disposable staging tree DIR from a Git commit
#              and an existing venv.            Requires F0R1_CONFIRM=stage.
#   preflight  CONTAINER side. Read-only isolation/environment gate (R2, R8).
#   rehearse   CONTAINER side. Preflight gate, then R1-R8 inside disposable
#              state only.                      Requires F0R1_CONFIRM=rehearse.
#   cleanup DIR HOST side. Remove a validated staging tree after its evidence
#              was archived.                    Requires F0R1_CONFIRM=cleanup.
#
# Result statuses: PASS, FAIL, BLOCKED, NOT_TESTED, INFO, RESIDUAL.
# NOT_TESTED and BLOCKED never count as PASS. RESIDUAL marks an operation that
# still runs with trusted Worker authority; it is never a sandbox claim.
# This script issues no F0 verdict and authorizes nothing.

set -Eeuo pipefail
umask 0022

readonly F0R1_VERSION="f0r1-v1"
readonly API_UID=61001
readonly WORKER_UID=61002
readonly SHARED_GID=61003
readonly REDIS_UID=61004
readonly OUTSIDER_UID=61005

# Container-side layout (fixed by the docker run command printed by `stage`).
readonly REL_A=/opt/orch/release   # read-only bind mount, staged without bytecode
readonly SCRIPT_DIR_IN=/opt/f0r1   # read-only bind mount holding this script
readonly STATE=/srv/orch           # disposable state bind mount
readonly EVIDENCE=/evidence        # disposable evidence bind mount
readonly REL_B=$STATE/release-b    # DAC-protected, precompiled copy of REL_A
readonly MARKER=.f0r1-disposable
readonly PY_REL=venv/bin/python
# CHOWN(0) SETGID(6) SETUID(7). Root only provisions state ownership and drops
# to rehearsal UIDs; it never reads or signals another UID's files/processes.
readonly ALLOWED_CAP_MASK=$((0xc1))
readonly NS_ISOLATED="net mnt pid ipc uts"
# Interpreter flags for any Python run as container root: isolated mode and no
# site import, so no venv .pth or project code executes before privilege drop.
readonly ROOT_PY_FLAGS="-I -S"

RESULTS_FILE=""
SUMMARY_DONE=0
declare -a STARTED_PIDS=()
declare -a APP_ENV=()

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

result() { # ID STATUS DETAIL
    local line
    line=$(printf '%s\t%s\t%s' "$1" "$2" "$3")
    printf '%s\n' "$line"
    if [[ -n "$RESULTS_FILE" ]]; then
        printf '%s\n' "$line" >>"$RESULTS_FILE"
    fi
}

note() { printf '[f0r1] %s\n' "$*" >&2; }

refuse() { # exit without mutation
    printf '[f0r1] REFUSED: %s\n' "$*" >&2
    exit 2
}

require_confirm() {
    [[ "${F0R1_CONFIRM:-}" == "$1" ]] ||
        refuse "subcommand '$1' mutates state; set F0R1_CONFIRM=$1 to proceed"
}

summary_and_exit() {
    local fail=0 blocked=0 nt=0
    if [[ -n "$RESULTS_FILE" && -f "$RESULTS_FILE" ]]; then
        fail=$(awk -F'\t' '$2=="FAIL"' "$RESULTS_FILE" | wc -l)
        blocked=$(awk -F'\t' '$2=="BLOCKED"' "$RESULTS_FILE" | wc -l)
        nt=$(awk -F'\t' '$2=="NOT_TESTED"' "$RESULTS_FILE" | wc -l)
    else
        fail=${PREFLIGHT_FAIL:-0}
        blocked=${PREFLIGHT_BLOCKED:-0}
    fi
    SUMMARY_DONE=1
    printf 'SUMMARY\tfail=%s\tblocked=%s\tnot_tested=%s\t(no F0 verdict issued)\n' \
        "$fail" "$blocked" "$nt"
    if ((fail > 0 || blocked > 0 || nt > 0)); then
        exit 1
    fi
    exit 0
}

# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

cmd_plan() {
    cat <<'EOF'
F0-R1 rehearsal plan (no action taken)

HOST (operator):
  1. Review the base image (see handoff "image review"); export F0R1_IMAGE.
  2. F0R1_CONFIRM=stage F0R1_REPO=<orchestrator checkout> F0R1_COMMIT=<sha> \
     [F0R1_VENV=<venv>] [F0R1_PROD_CONTAINER=<name|id>] \
     bash scripts/verification/f0r1.sh stage /tmp/orch-f0r1.<id>
     -> prints the exact docker run command.
  3. Run that docker run command (disposable container, --network none).
  4. As root: tar -C <dir> -czf <archive outside dir> evidence state/logs, then:
     F0R1_CONFIRM=cleanup F0R1_EVIDENCE_ARCHIVE=<archive> \
     bash scripts/verification/f0r1.sh cleanup /tmp/orch-f0r1.<id>
  stage and cleanup must run as root on the host (namespace references,
  0700 staging root, root-owned release).

CONTAINER (entrypoint): bash /opt/f0r1/f0r1.sh rehearse
  R2/R8 preflight gate (read-only) -> blocks on any isolation failure
  R1 protected release placement + write-denial probes + import probes
  R3 non-owner Worker UID/GID and read/write set
  R4 disposable SQLite WAL/SHM multi-UID (recipe R) incl. negative control
  R5 cache-free (A) vs precompiled (B) Worker startup timing/RSS
  R6 Worker boot identity + Beat launch with recorded configuration
  R7 residual trusted Worker authority (RESIDUAL rows, never PASS)
  R8 portability/environment record
EOF
}

# --------------------------------------------------------------------------
# stage (host side)
# --------------------------------------------------------------------------

validate_stage_dir_name() { # DIR -> validated absolute path on stdout
    local dir=$1 parent base
    [[ "$dir" == /* ]] || refuse "staging dir must be absolute: $dir"
    parent=$(dirname -- "$dir")
    base=$(basename -- "$dir")
    [[ "$parent" == /tmp || "$parent" == /var/tmp ]] ||
        refuse "staging dir parent must be /tmp or /var/tmp: $dir"
    [[ "$base" =~ ^orch-f0r1\.[A-Za-z0-9_-]{4,64}$ ]] ||
        refuse "staging dir name must match orch-f0r1.<id>: $base"
    printf '%s\n' "$dir"
}

# The staged release has no .git, so the Worker reads this file instead of
# Git: HEAD, tree, and the raw commit object binding them. The Worker
# recomputes the tree from the release contents. Integrity only; authenticity
# rests on the root-owned release and an external identity pin.
write_release_identity() { # REPO COMMIT TREE RELEASE_DIR
    local target=$4/.orchestrator-release-identity.json commit_b64
    [[ ! -e "$target" && ! -L "$target" ]] ||
        refuse "commit tree already contains $(basename -- "$target")"
    commit_b64=$(git -C "$1" cat-file commit "$2" | base64 -w0)
    printf '{"schema_version":1,"kind":"git-archive-release","candidate_head":"%s","candidate_tree":"%s","commit_object":"%s"}\n' \
        "$2" "$3" "$commit_b64" >"$target"
}

capture_ns() { # PREFIX PID -> KEY=VALUE lines
    local prefix=$1 pid=$2 ns value
    for ns in net mnt pid ipc uts user cgroup; do
        value=$(readlink "/proc/$pid/ns/$ns" 2>/dev/null || true)
        printf '%s_%s=%s\n' "$prefix" "$ns" "${value:-unavailable}"
    done
}

cmd_stage() {
    require_confirm stage
    [[ $# -eq 1 ]] || refuse "usage: stage /tmp/orch-f0r1.<id>"
    [[ ! -e /.dockerenv && ! -e /run/.containerenv ]] ||
        refuse "stage must run on the host, not inside a container"
    [[ "$(id -u)" -eq 0 ]] || refuse "stage must run as root (namespace references, ownership)"
    local dir repo commit venv tree run_id archive_sha
    dir=$(validate_stage_dir_name "$1")
    [[ ! -e "$dir" && ! -L "$dir" ]] || refuse "staging dir already exists: $dir"
    repo=${F0R1_REPO:?F0R1_REPO is required}
    commit=${F0R1_COMMIT:?F0R1_COMMIT is required}
    venv=${F0R1_VENV:-$repo/venv}
    git -C "$repo" cat-file -e "${commit}^{commit}" ||
        refuse "commit not found: $commit"
    commit=$(git -C "$repo" rev-parse "${commit}^{commit}")
    tree=$(git -C "$repo" rev-parse "${commit}^{tree}")
    [[ -f "$venv/pyvenv.cfg" && -L "$venv/bin/python" ]] ||
        refuse "venv missing pyvenv.cfg or bin/python symlink: $venv"
    if git -C "$repo" ls-tree -r --name-only "$commit" |
        grep -E '(^|/)(\.env|.*\.db|.*\.sqlite3?|__pycache__/.*|.*\.pyc)$' >/dev/null; then
        refuse "commit tree contains .env/db/bytecode entries; refusing to stage"
    fi

    run_id=$(basename -- "$dir")
    # mkdir (no -p) fails atomically if anything, including a symlink, exists.
    # 0700 keeps other host users out of container-created files (container
    # root is host root when the user namespace is shared).
    mkdir -m 0700 -- "$dir"
    mkdir -m 0755 -- "$dir/release" "$dir/state" "$dir/evidence" "$dir/f0r1"
    printf '%s\n' "$run_id" >"$dir/$MARKER"
    printf '%s\n' "$run_id" >"$dir/state/$MARKER"

    git -C "$repo" archive --format=tar "$commit" |
        tar -x --no-same-owner --no-same-permissions -C "$dir/release"
    archive_sha=$(git -C "$repo" archive --format=tar "$commit" | sha256sum | cut -d' ' -f1)
    write_release_identity "$repo" "$commit" "$tree" "$dir/release"
    tar -C "$(dirname -- "$venv")" --exclude='__pycache__' --exclude='*.pyc' \
        -cf - "$(basename -- "$venv")" |
        tar -x --no-same-owner -C "$dir/release"
    if [[ "$(basename -- "$venv")" != venv ]]; then
        mv -- "$dir/release/$(basename -- "$venv")" "$dir/release/venv"
    fi
    # Protected release model: root-owned, not group/other writable.
    chown -R 0:0 -- "$dir/release"
    chmod -R u=rwX,go=rX -- "$dir/release"
    install -m 0644 -- "${BASH_SOURCE[0]}" "$dir/f0r1/f0r1.sh"

    {
        printf 'f0r1_version=%s\nrun_id=%s\ncommit=%s\ntree=%s\n' \
            "$F0R1_VERSION" "$run_id" "$commit" "$tree"
        printf 'git_archive_sha256=%s\n' "$archive_sha"
        printf 'venv_source=%s\n' "$venv"
        printf 'venv_pyvenv_cfg_sha256=%s\n' "$(sha256sum <"$venv/pyvenv.cfg" | cut -d' ' -f1)"
        printf 'staged_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$dir/evidence/stage-identity.txt"
    (cd "$dir/release" && find . -type f ! -path './venv/*' -print0 | sort -z |
        xargs -0 sha256sum) >"$dir/evidence/release-source.sha256"
    (cd "$dir/release" && find ./venv -type f -print0 | sort -z |
        xargs -0 sha256sum) >"$dir/evidence/release-venv.sha256"

    # Reference namespace identities, captured read-only for the R2 comparison.
    {
        capture_ns HOST 1
        if [[ -n "${F0R1_PROD_CONTAINER:-}" ]] && command -v docker >/dev/null; then
            local prod_pid
            prod_pid=$(docker inspect -f '{{.State.Pid}}' "$F0R1_PROD_CONTAINER" 2>/dev/null || true)
            if [[ "$prod_pid" =~ ^[1-9][0-9]*$ ]]; then
                capture_ns PROD "$prod_pid"
            fi
        fi
    } >"$dir/evidence/reference-ns.env"

    cat <<EOF
Staged: $dir  (run_id=$run_id commit=$commit tree=$tree)
Review $dir/evidence/reference-ns.env: HOST_* and PROD_* values must not be
'unavailable' (capture as root), otherwise R2 will report BLOCKED.

Proposed (NOT executed) disposable container command:

docker run --rm --name "$run_id" --hostname f0r1 \\
  --user 0:0 --network none --ipc private \\
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \\
  --security-opt no-new-privileges=true \\
  --pids-limit 512 --memory 8g --memory-swap 8g --cpus 4 \\
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \\
  -v "$dir/release:$REL_A:ro" \\
  -v "$dir/f0r1:$SCRIPT_DIR_IN:ro" \\
  -v "$dir/state:$STATE:rw" \\
  -v "$dir/evidence:$EVIDENCE:rw" \\
  -e F0R1_CONFIRM=rehearse -e F0R1_RUN_ID="$run_id" \\
  --entrypoint /bin/bash "\${F0R1_IMAGE:?reviewed image required}" \\
  $SCRIPT_DIR_IN/f0r1.sh rehearse
EOF
}

# --------------------------------------------------------------------------
# cleanup (host side)
# --------------------------------------------------------------------------

validate_cleanup_target() { # DIR — every check that must hold right before rm
    local dir=$1 base
    [[ -d "$dir" && ! -L "$dir" ]] || refuse "not a real directory: $dir"
    [[ "$(realpath -e -- "$dir")" == "$dir" ]] || refuse "path is not canonical: $dir"
    [[ "$(stat -c '%u %a' -- "$dir")" == "$(id -u) 700" ]] ||
        refuse "staging dir must be owned by the invoking user with mode 0700: $dir"
    base=$(basename -- "$dir")
    [[ -f "$dir/$MARKER" && ! -L "$dir/$MARKER" &&
        "$(cat -- "$dir/$MARKER")" == "$base" ]] ||
        refuse "disposable marker missing, symlinked, or mismatched in $dir"
    if awk -v d="$dir" '$5 == d || index($5, d "/") == 1 { found=1 } END { exit !found }' \
        /proc/self/mountinfo; then
        refuse "a mount exists at or below $dir (is the container still running?)"
    fi
}

cmd_cleanup() {
    require_confirm cleanup
    [[ $# -eq 1 ]] || refuse "usage: cleanup /tmp/orch-f0r1.<id>"
    local dir archive
    dir=$(validate_stage_dir_name "$1")
    validate_cleanup_target "$dir"
    archive=${F0R1_EVIDENCE_ARCHIVE:-}
    [[ -n "$archive" && -f "$archive" && ! -L "$archive" && -s "$archive" ]] ||
        refuse "set F0R1_EVIDENCE_ARCHIVE to a non-empty regular-file archive of $dir/evidence"
    case "$(realpath -m -- "$archive")" in
        "$dir" | "$dir"/*) refuse "evidence archive must live outside $dir" ;;
    esac
    # Re-validate immediately before deletion. rm -rf never follows symlinks
    # inside the tree; --one-file-system stops at device boundaries, and the
    # mountinfo check above covers same-device bind mounts.
    validate_cleanup_target "$dir"
    rm -rf --one-file-system -- "$dir"
    note "removed $dir"
}

# --------------------------------------------------------------------------
# Preflight (container side, read-only)
# --------------------------------------------------------------------------

PREFLIGHT_FAIL=0
PREFLIGHT_BLOCKED=0

gate() { # ID STATUS DETAIL
    result "$1" "$2" "$3"
    case "$2" in
        FAIL) PREFLIGHT_FAIL=$((PREFLIGHT_FAIL + 1)) ;;
        BLOCKED) PREFLIGHT_BLOCKED=$((PREFLIGHT_BLOCKED + 1)) ;;
    esac
}

status_field() { awk -v k="$1:" '$1==k {print $2}' /proc/self/status; }

ref_ns() { # KEY -> value from reference-ns.env, parsed without sourcing
    local file=$EVIDENCE/reference-ns.env
    [[ -f "$file" ]] || return 0
    awk -F= -v k="$1" '$1==k {print $2; exit}' "$file"
}

check_location() {
    local mine
    mine=$(realpath -e -- "${BASH_SOURCE[0]}")
    if [[ "$mine" != "$SCRIPT_DIR_IN/f0r1.sh" ]]; then
        gate R2-LOC BLOCKED "script not running from $SCRIPT_DIR_IN (got $mine)"
        return
    fi
    if [[ -z "${F0R1_RUN_ID:-}" || ! -f "$STATE/$MARKER" ||
        "$(cat -- "$STATE/$MARKER")" != "$F0R1_RUN_ID" ]]; then
        gate R2-STATE BLOCKED "disposable marker absent or not matching F0R1_RUN_ID"
    else
        gate R2-STATE PASS "disposable state marker matches $F0R1_RUN_ID"
    fi
    local extra=""
    if [[ -d "$STATE" ]]; then
        extra=$(find "$STATE" -mindepth 1 -maxdepth 1 ! -name "$MARKER" -print -quit)
    fi
    if [[ -n "$extra" ]]; then
        gate R2-STATE-FRESH BLOCKED "disposable state is not fresh: $extra"
    else
        gate R2-STATE-FRESH PASS "disposable state contains only the marker"
    fi
    [[ -d "$REL_A" ]] || gate R1-REL BLOCKED "release mount $REL_A absent"
    [[ -d "$EVIDENCE" ]] || gate R2-EVID BLOCKED "evidence mount $EVIDENCE absent"
}

check_live_deployment() {
    local pid1 hits=""
    pid1=$(tr '\0\n' '  ' </proc/1/cmdline)
    if [[ "$pid1" != *"$SCRIPT_DIR_IN/f0r1.sh"* ]]; then
        gate R2-PID1 BLOCKED "PID 1 is not this rehearsal: $pid1"
    else
        gate R2-PID1 PASS "PID 1 is the rehearsal entrypoint (private PID namespace)"
    fi
    local p cmd
    for p in /proc/[0-9]*; do
        cmd=$(tr '\0\n' '  ' <"$p/cmdline" 2>/dev/null || true)
        case "$cmd" in
            *redis-server* | *celery* | *uvicorn* | *openclaw*) hits+="${p#/proc/}:${cmd:0:60};" ;;
        esac
    done
    if [[ -n "$hits" ]]; then
        gate R2-LIVE BLOCKED "service processes already present: $hits"
    else
        gate R2-LIVE PASS "no redis/celery/uvicorn/openclaw processes visible"
    fi
}

check_paths_absent() {
    local path present=""
    for path in /root/.openclaw /root/.orchestrator /root/.ssh /var/run/docker.sock \
        /run/docker.sock /run/containerd/containerd.sock /run/podman/podman.sock; do
        if [[ -e "$path" || -L "$path" ]]; then present+="$path "; fi
    done
    if [[ -n "$present" ]]; then
        gate R2-HOSTFS BLOCKED "production/host paths visible: $present"
    else
        gate R2-HOSTFS PASS "no production config, credential, or runtime-socket paths"
    fi
}

check_mounts() {
    local unexpected="" mp opts line
    while read -r line; do
        mp=$(awk '{print $5}' <<<"$line")
        opts=$(awk '{print $6}' <<<"$line")
        case "$mp" in
            / | /proc | /proc/* | /dev | /dev/* | /sys | /sys/* | /tmp | \
                /etc/hosts | /etc/hostname | /etc/resolv.conf | \
                "$REL_A" | "$SCRIPT_DIR_IN" | "$STATE" | "$EVIDENCE") ;;
            *) unexpected+="$mp " ;;
        esac
        case "$mp" in
            "$REL_A" | "$SCRIPT_DIR_IN")
                if [[ ",$opts," == *",ro,"* ]]; then
                    gate "R1-MOUNT-${mp##*/}" PASS "$mp mounted read-only"
                else
                    gate "R1-MOUNT-${mp##*/}" BLOCKED "$mp is not mounted read-only"
                fi
                ;;
            /)
                if [[ ",$opts," == *",ro,"* ]]; then
                    gate R2-ROOTFS PASS "container root filesystem is read-only"
                else
                    gate R2-ROOTFS BLOCKED "container root filesystem is writable"
                fi
                ;;
        esac
    done </proc/self/mountinfo
    if [[ -n "$unexpected" ]]; then
        gate R2-MOUNTS BLOCKED "unexpected mounts: $unexpected"
    else
        gate R2-MOUNTS PASS "only the expected disposable mounts are present"
    fi
}

check_env_names() {
    local bad
    bad=$(env | cut -d= -f1 |
        grep -E '(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|OPENCLAW|AWS_|GITHUB_|ANTHROPIC|OPENAI)' |
        grep -v '^F0R1_' | tr '\n' ' ' || true)
    if [[ -n "$bad" ]]; then
        gate R2-ENV BLOCKED "secret-like environment variable names present: $bad"
    else
        gate R2-ENV PASS "no secret-like environment variable names inherited"
    fi
}

check_caps() {
    local eff bnd amb nnp seccomp aa
    eff=$((16#$(status_field CapEff)))
    bnd=$((16#$(status_field CapBnd)))
    amb=$((16#$(status_field CapAmb)))
    nnp=$(status_field NoNewPrivs)
    seccomp=$(status_field Seccomp)
    if ((eff & ~ALLOWED_CAP_MASK || bnd & ~ALLOWED_CAP_MASK || amb != 0)); then
        gate R2-CAPS BLOCKED "capabilities exceed allowlist: eff=$(printf %x $eff) bnd=$(printf %x $bnd) amb=$(printf %x $amb)"
    else
        gate R2-CAPS PASS "eff=$(printf %x $eff) bnd=$(printf %x $bnd) within CHOWN,SETGID,SETUID"
    fi
    [[ "$nnp" == 1 ]] && gate R2-NNP PASS "NoNewPrivs=1" ||
        gate R2-NNP BLOCKED "NoNewPrivs=$nnp"
    [[ "$seccomp" == 2 ]] && gate R2-SECCOMP PASS "seccomp filter mode" ||
        gate R2-SECCOMP BLOCKED "Seccomp=$seccomp (filter mode required)"
    aa=$(cat /proc/self/attr/apparmor/current 2>/dev/null ||
        cat /proc/self/attr/current 2>/dev/null || true)
    if [[ -z "$aa" ]]; then
        gate R2-LSM INFO "no AppArmor/LSM label readable"
    elif [[ "$aa" == unconfined* ]]; then
        gate R2-LSM BLOCKED "LSM label is unconfined"
    else
        gate R2-LSM PASS "LSM label: $aa"
    fi
}

check_network() {
    local ifaces routes listen
    ifaces=$(awk -F: 'NR>2 {gsub(/ /,"",$1); print $1}' /proc/net/dev | tr '\n' ' ')
    routes=$(awk 'NR>1' /proc/net/route | wc -l)
    listen=$(awk 'NR>1 && $4=="0A"' /proc/net/tcp /proc/net/tcp6 2>/dev/null | wc -l)
    if [[ "$ifaces" == "lo " && "$routes" -eq 0 ]]; then
        gate R2-NET PASS "interfaces: lo only; no routes"
    else
        gate R2-NET BLOCKED "interfaces: $ifaces routes=$routes"
    fi
    if [[ "$listen" -eq 0 ]]; then
        gate R2-LISTEN PASS "no listening TCP sockets before rehearsal"
    else
        gate R2-LISTEN BLOCKED "$listen listening TCP sockets visible (shared netns?)"
    fi
}

check_namespaces() {
    local ns mine host prod
    for ns in $NS_ISOLATED; do
        mine=$(readlink "/proc/self/ns/$ns")
        host=$(ref_ns "HOST_$ns")
        prod=$(ref_ns "PROD_$ns")
        if [[ ! "$host" =~ ^$ns:\[[0-9]+\]$ ]]; then
            gate "R2-NS-$ns" BLOCKED "no valid host reference for $ns (${host:-absent}); cannot demonstrate"
        elif [[ ! "$prod" =~ ^$ns:\[[0-9]+\]$ ]]; then
            gate "R2-NS-$ns" BLOCKED "no valid production reference for $ns (${prod:-absent})"
        elif [[ "$host" == "$prod" && "$ns" != uts ]]; then
            # The production container has private net/mnt/pid/ipc; equality
            # means a reference was captured from the wrong context.
            gate "R2-NS-$ns" BLOCKED "host and production references are identical ($host); recapture"
        elif [[ "$mine" == "$host" || "$mine" == "$prod" ]]; then
            gate "R2-NS-$ns" BLOCKED "$mine shared with host or production container"
        else
            gate "R2-NS-$ns" PASS "$mine differs from host $host and production $prod"
        fi
    done
    # User namespace: separate PID/mount/network namespaces say nothing about
    # UID isolation. An identity map means container UID 0 is host UID 0.
    local uid_map
    uid_map=$(tr -s ' ' </proc/self/uid_map | sed 's/^ //' | tr '\n' ';')
    mine=$(readlink /proc/self/ns/user)
    host=$(ref_ns HOST_user)
    if [[ "$uid_map" == "0 0 4294967295;" ]]; then
        gate R2-NS-user RESIDUAL "identity uid_map ($mine, host reference ${host:-absent}): container root has host-root authority over mounted paths; no user-namespace isolation"
    elif [[ -n "$host" && "$mine" == "$host" ]]; then
        gate R2-NS-user RESIDUAL "user namespace $mine shared with host"
    else
        gate R2-NS-user INFO "user namespace $mine differs from host ${host:-absent}; uid_map $uid_map (remapping not independently verified)"
    fi
    gate R2-UIDMAP INFO "uid_map: $uid_map gid_map: $(tr -s ' ' </proc/self/gid_map | sed 's/^ //' | tr '\n' ';')"
}

check_tools_and_image() {
    local tool missing=""
    for tool in setpriv timeout sha256sum find cp chown chmod install redis-server awk \
        stat realpath sort xargs seq grep sed; do
        command -v "$tool" >/dev/null || missing+="$tool "
    done
    if [[ -n "$missing" ]]; then
        gate R8-TOOLS BLOCKED "missing tools in image: $missing"
    else
        gate R8-TOOLS PASS "required tools present"
    fi
    local target
    target=$(readlink -f -- "$REL_A/$PY_REL" 2>/dev/null || true)
    if [[ -n "$target" && -x "$target" ]]; then
        gate R8-PYTHON PASS "venv interpreter resolves to $target"
    else
        gate R8-PYTHON BLOCKED "venv interpreter target missing in image: ${target:-unresolved}"
    fi
    local cached=""
    if [[ -d "$REL_A" ]]; then
        cached=$(find "$REL_A" \( -name __pycache__ -o -name '*.pyc' \) -print -quit)
    fi
    if [[ ! -d "$REL_A" ]]; then
        gate R5-PRECOND BLOCKED "release A absent"
    elif [[ -n "$cached" ]]; then
        gate R5-PRECOND BLOCKED "release A contains bytecode: $cached"
    else
        gate R5-PRECOND PASS "release A contains no bytecode caches"
    fi
    local uid used=""
    for uid in $API_UID $WORKER_UID $REDIS_UID $OUTSIDER_UID; do
        if awk -v u="$uid" '$1=="Uid:" && ($2==u || $3==u) {f=1} END {exit !f}' \
            /proc/[0-9]*/status 2>/dev/null; then
            used+="$uid "
        fi
    done
    if [[ -n "$used" ]]; then
        gate R3-UIDS BLOCKED "rehearsal UIDs already in use: $used"
    else
        gate R3-UIDS PASS "UIDs $API_UID/$WORKER_UID/$REDIS_UID/$OUTSIDER_UID unused"
    fi
}

run_preflight() {
    check_location
    check_live_deployment
    check_paths_absent
    check_mounts
    check_env_names
    check_caps
    check_network
    check_namespaces
    check_tools_and_image
}

cmd_preflight() {
    run_preflight
    summary_and_exit
}

# --------------------------------------------------------------------------
# Rehearsal helpers (container side)
# --------------------------------------------------------------------------

build_app_env() { # RELEASE
    local rel=$1 secret
    secret=$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)
    APP_ENV=(
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        "LANG=C.UTF-8"
        "PYTHONPATH=$rel"
        "PYTHONSAFEPATH=1"
        "PYTHONNOUSERSITE=1"
        "PYTHONDONTWRITEBYTECODE=1"
        "DATABASE_URL=sqlite:///$STATE/db/orchestrator.db"
        "CELERY_BROKER_URL=redis://127.0.0.1:6379/0"
        "CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1"
        "RUNTIME_ROOT=$STATE/runtime"
        "WORKER_SOURCE_ROOT=$rel"
        "CHECKPOINT_DIR=$STATE/checkpoints"
        "CANDIDATE_CONTENT_DIR=$STATE/candidate-content"
        "WORKSPACE_ROOT=$STATE/projects"
        "HOST_WORKSPACE_ROOT=$STATE/projects"
        "SECRET_KEY=$secret"
        "ENABLE_TEST_RUNTIME_BACKENDS=true"
        "AGENT_BACKEND=stub_success"
        "PLANNING_BACKEND="
        "EXECUTION_BACKEND="
        "DEBUG_REPAIR_BACKEND="
        "REPAIR_BACKEND="
        "COMPLETION_REPAIR_BACKEND="
    )
}

record_launch() { # NAME UID ARGV...
    local name=$1 uid=$2
    shift 2
    {
        printf 'uid=%s gid=%s groups=%s\n' "$uid" "$uid" "$SHARED_GID"
        printf 'cwd=%s\n' "$STATE/cwd"
        printf 'argv='
        printf '%q ' "$@"
        printf '\n'
        printf '%s\n' "${APP_ENV[@]}" | sed -E 's/^(SECRET_KEY)=.*/\1=<redacted-disposable>/'
    } >"$EVIDENCE/launch/$name.txt"
}

as_uid() { # UID HOME CMD... (foreground, app environment, empty cwd)
    local uid=$1 home=$2
    shift 2
    (cd "$STATE/cwd" && exec setpriv --reuid="$uid" --regid="$uid" --groups="$SHARED_GID" \
        -- env -i "${APP_ENV[@]}" "HOME=$home" "$@")
}

start_bg() { # NAME UID HOME CMD... -> sets BG_PID
    local name=$1 uid=$2 home=$3
    shift 3
    record_launch "$name" "$uid" "$@"
    (cd "$STATE/cwd" && exec setpriv --reuid="$uid" --regid="$uid" --groups="$SHARED_GID" \
        -- env -i "${APP_ENV[@]}" "HOME=$home" "$@") \
        >"$STATE/logs/$name.log" 2>&1 &
    BG_PID=$!
    STARTED_PIDS+=("$BG_PID")
}

proc_alive() { # PID — running and not a zombie
    [[ -r "/proc/$1/status" ]] &&
        ! awk '$1=="State:" && $2=="Z" {z=1} END {exit !z}' "/proc/$1/status" 2>/dev/null
}

signal_as_owner() { # SIG PID — root has no CAP_KILL; signal as the owning UID
    local sig=$1 pid=$2 uid
    uid=$(awk '$1=="Uid:" {print $2}' "/proc/$pid/status" 2>/dev/null || true)
    [[ "$uid" =~ ^[0-9]+$ ]] || return 0
    if [[ "$uid" -eq 0 ]]; then
        kill -"$sig" "$pid" 2>/dev/null || true
    else
        setpriv --reuid="$uid" --regid="$uid" --clear-groups -- \
            /bin/bash -c 'kill -"$1" "$2"' _ "$sig" "$pid" 2>/dev/null || true
    fi
}

stop_pid() { # PID — signals only a direct child of this script, never a group
    local pid=$1 i
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" -ne 1 && "$pid" -ne $$ ]] || return 0
    [[ -d "/proc/$pid" ]] || return 0
    [[ "$(awk '$1=="PPid:" {print $2}' "/proc/$pid/status" 2>/dev/null)" == "$$" ]] || return 0
    if proc_alive "$pid"; then signal_as_owner TERM "$pid"; fi
    for i in $(seq 1 30); do
        proc_alive "$pid" || break
        sleep 1
    done
    if proc_alive "$pid"; then
        signal_as_owner KILL "$pid"
        for i in $(seq 1 10); do
            proc_alive "$pid" || break
            sleep 1
        done
    fi
    if proc_alive "$pid"; then
        result R0-STOP FAIL "PID $pid survived TERM/KILL; container teardown will reap it"
        return 0
    fi
    wait "$pid" 2>/dev/null || true
}

stop_all() {
    local pid
    for pid in "${STARTED_PIDS[@]}"; do stop_pid "$pid"; done
}

wait_for_log() { # LOGFILE REGEX TIMEOUT PID -> 0 when matched
    local log=$1 regex=$2 limit=$3 pid=$4 waited=0
    while ((waited < limit * 10)); do
        if grep -Eq -- "$regex" "$log" 2>/dev/null; then
            proc_alive "$pid"
            return
        fi
        proc_alive "$pid" || return 1
        sleep 0.1
        waited=$((waited + 1))
    done
    return 1
}

tcp_exchange() { # PORT REQUEST -> first response line (empty if unreachable)
    (
        exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 0
        printf '%b' "$2" >&3
        line=""
        IFS= read -r -t 3 line <&3 || true
        printf '%s' "${line%$'\r'}"
    ) 2>/dev/null || true
}

proc_creds() { # PID -> "Uid=.. Gid=.. Groups=.. CapEff=.. NoNewPrivs=.."
    awk '$1=="Uid:"{u=$2} $1=="Gid:"{g=$2} $1=="Groups:"{$1="";gr=$0}
         $1=="CapEff:"{c=$2} $1=="NoNewPrivs:"{n=$2}
         END {printf "Uid=%s Gid=%s Groups=%s CapEff=%s NoNewPrivs=%s", u, g, gr, c, n}' \
        "/proc/$1/status"
}

# --------------------------------------------------------------------------
# Rehearsal steps
# --------------------------------------------------------------------------

provision_state() {
    install -d -m 0755 -o 0 -g 0 "$STATE/cwd" "$STATE/logs" "$EVIDENCE/launch"
    install -d -m 2770 -o 0 -g "$SHARED_GID" "$STATE/db" "$STATE/runtime" \
        "$STATE/checkpoints" "$STATE/candidate-content" "$STATE/projects" "$STATE/beat"
    install -d -m 0700 -o "$REDIS_UID" -g "$REDIS_UID" "$STATE/redis"
    install -d -m 0700 -o "$API_UID" -g "$API_UID" "$STATE/home-api"
    install -d -m 0700 -o "$WORKER_UID" -g "$WORKER_UID" "$STATE/home-worker"
    install -d -m 0755 -o 0 -g 0 "$STATE/pycache-empty"
    # U5 recipe R (a): provisioning pre-creates the DB file 0660 before any opener.
    install -m 0660 -o "$API_UID" -g "$SHARED_GID" /dev/null "$STATE/db/orchestrator.db"
    result R4-PROVISION INFO "db dir 2770 root:$SHARED_GID; db 0660 $API_UID:$SHARED_GID"

    # Release B: root-owned DAC-protected copy with precompiled bytecode.
    cp -a -- "$REL_A" "$REL_B"
    chown -R 0:0 -- "$REL_B"
    chmod -R u=rwX,go=rX -- "$REL_B"
    # Root compiles (parses) only; -I -S keeps venv .pth code from executing.
    # shellcheck disable=SC2086
    if (cd "$STATE/cwd" && env -i PATH=/usr/bin:/bin "$REL_B/$PY_REL" $ROOT_PY_FLAGS \
        -m compileall -q -j 0 --invalidation-mode checked-hash "$REL_B/app" "$REL_B/venv/lib" \
        >"$STATE/logs/compileall.log" 2>&1); then
        result R5-B-PREP PASS "release B precompiled (root-owned, 0755/0644)"
    else
        # Some third-party files never compile; B startup below is the real check.
        result R5-B-PREP INFO "compileall reported per-file errors; see compileall.log"
    fi
    chmod -R u=rwX,go=rX -- "$REL_B"
    (cd "$REL_B" && find . -type f -print0 | sort -z | xargs -0 sha256sum) \
        >"$EVIDENCE/release-b.sha256"
}

r1_write_probes() { # LABEL RELEASE EXPECTED_ERROR
    # Each probe is a single mkdir or file create as the Worker UID. It passes
    # only when it fails with the expected reason: A is protected by the
    # read-only mount (EROFS), B by root ownership/DAC (EACCES).
    local label=$1 rel=$2 expected=$3 target op out ok=0 total=0
    for target in "$rel/__f0r1_probe" "$rel/app/__f0r1_probe" \
        "$rel/app/__pycache__" "$rel/venv/lib/__f0r1_probe" "$rel/run"; do
        total=$((total + 1))
        if [[ "$target" == */__f0r1_probe ]]; then op='exec 2>&1; : >"$1"'; else op='mkdir -- "$1" 2>&1'; fi
        [[ -d "$target" ]] && { target=$target/__f0r1_probe; op='exec 2>&1; : >"$1"'; }
        if out=$(as_uid "$WORKER_UID" "$STATE/home-worker" /bin/sh -c "$op" _ "$target"); then
            result "R1-WRITE-$label" FAIL "Worker UID created $target"
        elif [[ -e "$target" ]]; then
            result "R1-WRITE-$label" FAIL "$target exists after denied write"
        elif [[ "$out" != *"$expected"* ]]; then
            result "R1-WRITE-$label" FAIL "$target denied for an unexpected reason: ${out:0:120}"
        else
            ok=$((ok + 1))
        fi
    done
    if ((ok == total)); then
        result "R1-WRITE-$label" PASS "$total/$total Worker write/mkdir probes denied ($expected) in $rel"
    fi
}

r1_positive_control() { # the Worker UID can write where the contract says it may
    local target=$STATE/runtime/__f0r1_control
    if as_uid "$WORKER_UID" "$STATE/home-worker" /bin/sh -c ': >"$1" && rm -f -- "$1"' \
        _ "$target" >/dev/null 2>&1; then
        result R1-CONTROL PASS "Worker UID can write the shared runtime dir (denials are not a broken UID setup)"
    else
        result R1-CONTROL FAIL "Worker UID cannot write $STATE/runtime; negative probes are inconclusive"
    fi
}

r1_import_probes() { # release A, cache-free (B3-01..04 equivalents)
    local probe code
    build_app_env "$REL_A"
    APP_ENV+=("PYTHONPYCACHEPREFIX=$STATE/pycache-empty")
    for probe in "app.config" "app.celery_app" "app.config,app.celery_app" \
        "app.celery_app,app.config" "app.tasks.worker"; do
        code="import importlib, sys
for name in '$probe'.split(','):
    module = importlib.import_module(name)
    assert module.__file__.startswith('$REL_A/'), module.__file__
assert sys.flags.dont_write_bytecode == 1
assert sys.pycache_prefix == '$STATE/pycache-empty', sys.pycache_prefix
print('F0R1_IMPORT_OK')"
        if as_uid "$WORKER_UID" "$STATE/home-worker" "$REL_A/$PY_REL" -c "$code" \
            >"$STATE/logs/import-${probe//,/_}.log" 2>&1 &&
            grep -qx F0R1_IMPORT_OK "$STATE/logs/import-${probe//,/_}.log"; then
            result "R1-IMPORT" PASS "import-only (not runtime startup) $probe from $REL_A as UID $WORKER_UID, cache-free flags confirmed"
        else
            result "R1-IMPORT" FAIL "import $probe failed; see logs/import-${probe//,/_}.log"
        fi
    done
}

start_redis() {
    build_app_env "$REL_A"
    start_bg redis "$REDIS_UID" "$STATE/redis" redis-server --bind 127.0.0.1 --port 6379 \
        --protected-mode yes --dir "$STATE/redis" --save "" --appendonly no --daemonize no
    REDIS_PID=$BG_PID
    local i
    for i in $(seq 1 50); do
        if [[ "$(tcp_exchange 6379 'PING\r\n')" == "+PONG" ]]; then
            result R5-BROKER PASS "disposable redis on 127.0.0.1:6379 (UID $REDIS_UID)"
            return 0
        fi
        sleep 0.2
    done
    result R5-BROKER FAIL "disposable redis did not answer PING"
    return 1
}

r4_api_start() {
    build_app_env "$REL_A"
    start_bg api "$API_UID" "$STATE/home-api" "$REL_A/$PY_REL" -m uvicorn app.main:app \
        --host 127.0.0.1 --port 8000 --no-access-log
    API_PID=$BG_PID
    local i status=""
    for i in $(seq 1 120); do
        status=$(tcp_exchange 8000 'GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n')
        [[ "$status" == HTTP/* ]] && break
        [[ -d "/proc/$API_PID" ]] || break
        sleep 1
    done
    if [[ "$status" == "HTTP/"*" 200"* ]]; then
        result R4-API PASS "API started as UID $API_UID; /health: $status"
    else
        result R4-API FAIL "API not healthy (${status:-no response}); see logs/api.log"
    fi
}

r4_sqlite_probes() {
    local db=$STATE/db/orchestrator.db f
    for f in "$db" "$db-wal" "$db-shm"; do
        if [[ -e "$f" ]]; then
            result R4-FILES INFO "$(stat -c '%n %u:%g %a' -- "$f")"
        else
            result R4-FILES INFO "$f absent"
        fi
    done
    local holder writer
    holder="import sqlite3, time
c = sqlite3.connect('$db', timeout=30)
c.execute('CREATE TABLE IF NOT EXISTS f0r1_probe (uid INTEGER, ts REAL)')
c.commit()
c.execute('BEGIN')
c.execute('SELECT count(*) FROM f0r1_probe').fetchone()
time.sleep(10)
c.execute('COMMIT')
c.execute('INSERT INTO f0r1_probe VALUES (?, ?)', ($API_UID, time.time()))
c.commit()
print(c.execute('PRAGMA journal_mode').fetchone()[0])"
    writer="import os, sqlite3, time
c = sqlite3.connect('$db', timeout=30)
c.execute('INSERT INTO f0r1_probe VALUES (?, ?)', (os.getuid(), time.time()))
c.commit()
print(c.execute('PRAGMA integrity_check').fetchone()[0])"
    build_app_env "$REL_A"
    as_uid "$API_UID" "$STATE/home-api" "$REL_A/$PY_REL" -c "$holder" \
        >"$STATE/logs/r4-holder.log" 2>&1 &
    local holder_pid=$!
    STARTED_PIDS+=("$holder_pid")
    sleep 1
    if as_uid "$WORKER_UID" "$STATE/home-worker" "$REL_A/$PY_REL" -c "$writer" \
        >"$STATE/logs/r4-worker.log" 2>&1 && grep -qx ok "$STATE/logs/r4-worker.log"; then
        result R4-WORKER-WRITE PASS "Worker UID wrote while API UID held a read transaction"
    else
        result R4-WORKER-WRITE FAIL "see logs/r4-worker.log"
    fi
    # Recipe R: while the holder is open, WAL/SHM exist with the shared group
    # (setgid dir) and the DB file mode (0660).
    local aux_bad="" aux
    for aux in "$db-wal" "$db-shm"; do
        if [[ ! -e "$aux" ]]; then
            aux_bad+="$aux absent; "
        elif [[ "$(stat -c '%g %a' -- "$aux")" != "$SHARED_GID 660" ]]; then
            aux_bad+="$(stat -c '%n %u:%g %a' -- "$aux"); "
        fi
    done
    if [[ -z "$aux_bad" ]]; then
        result R4-AUX PASS "WAL/SHM present with gid $SHARED_GID mode 0660 during concurrent access"
    else
        result R4-AUX FAIL "$aux_bad"
    fi
    if wait "$holder_pid" && grep -qx wal "$STATE/logs/r4-holder.log"; then
        result R4-API-WRITE PASS "API UID wrote after Worker write; journal_mode=wal"
    else
        result R4-API-WRITE FAIL "see logs/r4-holder.log"
    fi
    if (cd "$STATE/cwd" && setpriv --reuid="$OUTSIDER_UID" --regid="$OUTSIDER_UID" \
        --clear-groups -- env -i PATH=/usr/bin:/bin "$REL_A/$PY_REL" -c "$writer" \
        >"$STATE/logs/r4-outsider.log" 2>&1); then
        result R4-NEGATIVE FAIL "UID $OUTSIDER_UID without group $SHARED_GID wrote the DB"
    elif grep -qE 'sqlite3\.OperationalError: (unable to open database file|attempt to write a readonly database)' \
        "$STATE/logs/r4-outsider.log"; then
        result R4-NEGATIVE PASS "UID $OUTSIDER_UID without group $SHARED_GID denied by SQLite open/permission"
    else
        result R4-NEGATIVE FAIL "outsider failed for an unexpected reason; see logs/r4-outsider.log"
    fi
    for f in "$db" "$db-wal" "$db-shm"; do
        [[ -e "$f" ]] && result R4-FILES INFO "after: $(stat -c '%n %u:%g %a' -- "$f")"
    done
}

r5_worker_run() { # LABEL RELEASE
    local label=$1 rel=$2 t0 t1 pid log manifest
    build_app_env "$rel"
    if [[ "$label" == A ]]; then
        APP_ENV+=("PYTHONPYCACHEPREFIX=$STATE/pycache-empty")
    fi
    t0=$(date +%s.%N)
    start_bg "worker-$label" "$WORKER_UID" "$STATE/home-worker" "$rel/$PY_REL" -m celery \
        -A app.celery_app worker --loglevel=INFO --concurrency=1 \
        --hostname="f0r1-$label@localhost"
    pid=$BG_PID
    log=$STATE/logs/worker-$label.log
    if wait_for_log "$log" 'f0r1-'"$label"'@localhost ready' 240 "$pid"; then
        t1=$(date +%s.%N)
        result "R5-START-$label" PASS "ready in $(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.2f", b-a}')s; VmHWM=$(awk '$1=="VmHWM:" {print $2 $3}' "/proc/$pid/status")"
        local creds
        creds=$(proc_creds "$pid")
        if [[ "$creds" == "Uid=$WORKER_UID Gid=$WORKER_UID Groups= $SHARED_GID CapEff=0000000000000000 NoNewPrivs=1" ]]; then
            result "R3-CREDS-$label" PASS "$creds"
        else
            result "R3-CREDS-$label" FAIL "$creds"
        fi
        # Broker round trip: "ready" in the log alone is not full startup.
        if as_uid "$WORKER_UID" "$STATE/home-worker" timeout 90 "$rel/$PY_REL" -m celery \
            -A app.celery_app inspect ping --destination="f0r1-$label@localhost" --timeout=10 \
            >"$STATE/logs/ping-$label.log" 2>&1 && grep -q 'pong' "$STATE/logs/ping-$label.log"; then
            result "R5-PING-$label" PASS "Worker answered a broker ping via disposable redis"
        else
            result "R5-PING-$label" FAIL "no pong; see logs/ping-$label.log"
        fi
        # The identity directory/file are 0700/0600 Worker-owned; read them as
        # the Worker (root has no DAC_OVERRIDE). Root only redirects stdout.
        local reader
        reader="import json, pathlib, sys
start = pathlib.Path(sys.argv[1]).stat().st_mtime
found = sorted(p for p in pathlib.Path(sys.argv[2]).glob('worker-*.json') if p.stat().st_mtime >= start)
assert len(found) == 1, found
data = json.loads(found[0].read_text())
assert data['candidate_source_root'] == sys.argv[3], data['candidate_source_root']
print(json.dumps(data, indent=2, sort_keys=True))"
        if grep -q 'Worker boot identity manifest created' "$log" &&
            as_uid "$WORKER_UID" "$STATE/home-worker" "$rel/$PY_REL" -I -S -c "$reader" \
                "$STATE/logs/worker-$label.log.start" "$STATE/runtime/worker-identities" "$rel" \
                >"$EVIDENCE/identity-$label.json" 2>"$STATE/logs/identity-$label.err"; then
            result "R6-IDENTITY-$label" PASS "one boot identity under RUNTIME_ROOT; candidate_source_root $rel"
        else
            result "R6-IDENTITY-$label" FAIL "boot identity absent, ambiguous, or wrong source root; see logs/identity-$label.err and worker-$label.log"
        fi
    else
        result "R5-START-$label" FAIL "Worker not ready within 240s or exited; see logs/worker-$label.log"
        result "R3-CREDS-$label" NOT_TESTED "Worker did not reach ready"
        result "R5-PING-$label" NOT_TESTED "Worker did not reach ready"
        result "R6-IDENTITY-$label" NOT_TESTED "Worker did not reach ready"
    fi
    stop_pid "$pid"
}

r6_beat() {
    local pid log
    build_app_env "$REL_A"
    APP_ENV+=("PYTHONPYCACHEPREFIX=$STATE/pycache-empty")
    start_bg beat "$WORKER_UID" "$STATE/home-worker" "$REL_A/$PY_REL" -m celery \
        -A app.celery_app beat --loglevel=INFO --schedule="$STATE/beat/celerybeat-schedule" \
        --pidfile=
    pid=$BG_PID
    log=$STATE/logs/beat.log
    if wait_for_log "$log" 'beat: Starting' 120 "$pid"; then
        sleep 10
        if proc_alive "$pid"; then
            result R6-BEAT PASS "Beat running as UID $WORKER_UID from $REL_A for 10s"
        else
            result R6-BEAT FAIL "Beat exited after start; see logs/beat.log"
        fi
    else
        result R6-BEAT FAIL "Beat did not start; see logs/beat.log"
    fi
    stop_pid "$pid"
}

r1_integrity() {
    if (cd "$REL_A" && sha256sum --quiet -c "$EVIDENCE/release-source.sha256" &&
        sha256sum --quiet -c "$EVIDENCE/release-venv.sha256") \
        >"$STATE/logs/release-a-verify.log" 2>&1; then
        result R1-INTEGRITY-A PASS "release A source and venv match the staged manifests"
    else
        result R1-INTEGRITY-A FAIL "release A changed or manifest unreadable; see logs/release-a-verify.log"
    fi
    if (cd "$REL_B" && sha256sum --quiet -c "$EVIDENCE/release-b.sha256" \
        >"$STATE/logs/release-b-verify.log" 2>&1); then
        result R1-INTEGRITY-B PASS "release B unchanged after rehearsal"
    else
        result R1-INTEGRITY-B FAIL "release B changed; see logs/release-b-verify.log"
    fi
    # Run A is cache-free because PYTHONPYCACHEPREFIX points at an empty,
    # root-owned directory (Python then ignores every __pycache__, stdlib
    # included) and bytecode writing is disabled. Confirm both held.
    local cached prefix_entries flags_ok
    cached=$(find "$REL_A" \( -name __pycache__ -o -name '*.pyc' \) -print -quit)
    prefix_entries=$(find "$STATE/pycache-empty" -mindepth 1 -print -quit)
    flags_ok=$(grep -c "PYTHONPYCACHEPREFIX=$STATE/pycache-empty" "$EVIDENCE/launch/worker-A.txt" 2>/dev/null || true)
    if [[ -z "$cached" && -z "$prefix_entries" && "$flags_ok" == 1 ]] &&
        awk -F'\t' '$1=="R1-IMPORT" && $2!="PASS" {bad=1} $1=="R1-IMPORT" {n++} END {exit bad || n!=5}' \
            "$RESULTS_FILE"; then
        result R5-CACHEFREE-A PASS "empty pycache prefix and no bytecode in release A; flags confirmed in-process by R1-IMPORT"
    else
        result R5-CACHEFREE-A FAIL "cache-free conditions not all met (release=${cached:-none} prefix=${prefix_entries:-empty} launch_flag=$flags_ok)"
    fi
}

r3_write_set() {
    # Run find as each UID: it sees exactly what that UID can traverse, and
    # root (no DAC_OVERRIDE) could not enter the 0700 homes anyway.
    local uid home errors
    for uid in $WORKER_UID $API_UID; do
        home=$STATE/home-worker
        [[ "$uid" -eq "$API_UID" ]] && home=$STATE/home-api
        as_uid "$uid" "$home" find "$STATE" /tmp -xdev -uid "$uid" -printf '%u:%g %m %p\n' \
            2>"$STATE/logs/write-set-$uid.err" | sort >"$EVIDENCE/write-set-$uid.txt" || true
        errors=$(grep -vc 'Permission denied' "$STATE/logs/write-set-$uid.err" || true)
        result R3-WRITESET INFO "UID $uid owns $(wc -l <"$EVIDENCE/write-set-$uid.txt") reachable paths (write-set-$uid.txt; unreadable dirs listed in write-set-$uid.err; other errors: $errors)"
    done
    local out
    if out=$(as_uid "$WORKER_UID" "$STATE/home-worker" /bin/sh -c 'ls -- "$1" 2>&1' _ "$STATE/home-api"); then
        result R3-SEPARATION FAIL "Worker UID can read the API home"
    elif [[ "$out" == *"Permission denied"* ]]; then
        result R3-SEPARATION PASS "Worker UID cannot read the API home (EACCES)"
    else
        result R3-SEPARATION FAIL "API home check failed for an unexpected reason: ${out:0:120}"
    fi
}

r7_residual() {
    grep -rlE 'subprocess\.(run|Popen|check_output|check_call|call)\(' "$REL_A/app" \
        --include='*.py' | grep -v '/tests/' | sed "s|^$REL_A/||" | sort \
        >"$EVIDENCE/r7-subprocess-sites.txt" || true
    result R7-SUBPROCESS RESIDUAL "$(wc -l <"$EVIDENCE/r7-subprocess-sites.txt") app modules spawn subprocesses with Worker UID authority (r7-subprocess-sites.txt)"
    result R7-VERIFY RESIDUAL "verification/candidate commands (execute_verification_command, candidate_checks._run_command) run as the Worker UID; not sandboxed"
    result R7-DB RESIDUAL "Worker UID has read/write on the shared DB and runtime state (R4/R3)"
    result R7-BROKER RESIDUAL "broker has no authentication; any process in the Worker netns can reach it"
    result R7-ROOT-PROVISION RESIDUAL "container root (CHOWN/SETUID/SETGID) provisions state and launches every UID; trusted orchestration, not sandboxed"
    local userns_note
    userns_note=$(awk -F'\t' '$1=="R2-NS-user" {print $3}' "$RESULTS_FILE")
    result R7-USERNS RESIDUAL "${userns_note:-user namespace not recorded}"
}

r8_portability() {
    result R8-HOST INFO "kernel=$(uname -r) arch=$(uname -m) os=$(. /etc/os-release && printf '%s' "$PRETTY_NAME")"
    # shellcheck disable=SC2086
    result R8-VERSIONS INFO "python=$("$REL_A/$PY_REL" $ROOT_PY_FLAGS -V 2>&1) sqlite=$("$REL_A/$PY_REL" $ROOT_PY_FLAGS -c 'import sqlite3; print(sqlite3.sqlite_version)' 2>&1) redis=$(redis-server --version | awk '{print $3}')"
    result R8-GENERIC INFO "generic Linux: numeric UIDs/shared GID, setgid 2770 state dirs, 0660 DB pre-provisioning, read-only release, env-configured roots"
    result R8-HOST-SPECIFIC INFO "host/runtime-specific: Docker seccomp/AppArmor profile, user-namespace mapping, aarch64 wheels in venv, image python path"
}

ABORT_LINE=""

on_rehearse_exit() {
    local status=$?
    stop_all
    if ((SUMMARY_DONE == 0)); then
        # set -e stopped the rehearsal early: never leave a silent partial run.
        result R0-ABORT FAIL "rehearsal aborted (status $status, line ${ABORT_LINE:-unknown}); later checks did not run"
        cp -- "$STATE"/logs/*.log "$EVIDENCE/" 2>/dev/null || true
        printf 'SUMMARY\taborted\t(no F0 verdict issued)\n'
        exit 1
    fi
}

cmd_rehearse() {
    require_confirm rehearse
    if [[ -d "$EVIDENCE" && ! -L "$EVIDENCE" && ! -e "$EVIDENCE/results.tsv" ]]; then
        RESULTS_FILE=$EVIDENCE/results.tsv
        : >"$RESULTS_FILE"
    fi
    run_preflight
    if ((PREFLIGHT_FAIL > 0 || PREFLIGHT_BLOCKED > 0)) || [[ -z "$RESULTS_FILE" ]]; then
        note "preflight blocked; no rehearsal mutation performed"
        summary_and_exit
    fi
    note "preflight passed; recording results to $RESULTS_FILE"
    trap on_rehearse_exit EXIT
    trap 'ABORT_LINE=$LINENO' ERR

    provision_state
    build_app_env "$REL_A"
    r1_positive_control
    r1_write_probes A "$REL_A" "Read-only file system"
    r1_write_probes B "$REL_B" "Permission denied"
    r1_import_probes
    if start_redis; then
        r4_api_start
        r4_sqlite_probes
        stop_pid "$API_PID"
        touch "$STATE/logs/worker-A.log.start"
        r5_worker_run A "$REL_A"
        touch "$STATE/logs/worker-B.log.start"
        r5_worker_run B "$REL_B"
        r6_beat
    else
        result R4-API NOT_TESTED "broker unavailable"
        result R5-START-A NOT_TESTED "broker unavailable"
        result R5-START-B NOT_TESTED "broker unavailable"
        result R6-BEAT NOT_TESTED "broker unavailable"
    fi
    r3_write_set
    r1_integrity
    r7_residual
    r8_portability
    stop_all
    cp -- "$STATE"/logs/*.log "$EVIDENCE/" 2>/dev/null || true
    summary_and_exit
}

main() {
    local cmd=${1:-plan}
    [[ $# -gt 0 ]] && shift
    case "$cmd" in
        plan) cmd_plan ;;
        stage) cmd_stage "$@" ;;
        preflight) cmd_preflight ;;
        rehearse) cmd_rehearse ;;
        cleanup) cmd_cleanup "$@" ;;
        *) refuse "unknown subcommand: $cmd (plan|stage|preflight|rehearse|cleanup)" ;;
    esac
}

main "$@"
