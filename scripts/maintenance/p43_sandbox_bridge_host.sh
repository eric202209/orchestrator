#!/usr/bin/env bash
# P43-PRODUCT-22-C1 one-time Host Launcher bootstrap. Run on the GX10 Host as root.
#   source-check                   read-only: this checkout must be a root-only copy ai-agent cannot write
#   preflight                      read-only checks; changes nothing
#   install --operator-approved    create service user, root-owned code copy, key (if absent),
#                                  state dirs, systemd unit; enable and start the service
#   verify                         after the Compose change + ai-agent recreation: socket identity,
#                                  ai-agent mounts, synthetic client check from inside ai-agent
#   rollback --operator-approved   stop/disable only this service; remove only its socket and unit
#   c2-preflight                   read-only checks for the isolated strict C2 Launcher
#   c2-install --operator-approved create the separate C2 user, code copy, keys, and unit; never
#                                  enables or starts it and never touches the C1B service
#   c2-rollback --operator-approved stop the C2 unit; remove only its unit and Worker key copy
# Never prints key contents. Never runs broad Docker cleanup. Never edits Compose.
set -Eeuo pipefail
umask 077

SERVICE=p43-sandbox-bridge.service
SVC_USER=p43-sandbox
OPT=/opt/p43-sandbox-bridge
KEY_DIR=/etc/p43-sandbox-bridge
KEY=$KEY_DIR/bridge.key
STATE=/var/lib/p43-sandbox-bridge
SOCK=$STATE/socket/launcher.sock
UNIT_DST=/etc/systemd/system/$SERVICE
AI_AGENT=ai-agent
AI_AGENT_REPO=${AI_AGENT_REPO:-/root/.openclaw/workspace/vault/orchestrator}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
SRC=$REPO/app/services/sandbox_bridge
MODULES=(__init__.py authorization.py contract.py docker_executor.py host_service.py ipc_contract.py ledger.py staging.py transport.py)
# C2-C3A isolated strict instance: shares nothing with the C1B service above.
C2_SERVICE=p43-sandbox-bridge-c2.service
C2_USER=p43-sandbox-c2
C2_OPT=/opt/p43-sandbox-bridge-c2
C2_KEY_DIR=/etc/p43-sandbox-bridge-c2
C2_KEY=$C2_KEY_DIR/bridge.key
C2_WORKER_KEY=$C2_KEY_DIR/worker.key
C2_WORKER_UID=10001
C2_STATE=/var/lib/p43-sandbox-bridge-c2
C2_RUN=/run/p43-sandbox-bridge-c2
C2_UNIT_DST=/etc/systemd/system/$C2_SERVICE
IMAGE=$(sed -n 's/^IMAGE = "\(.*\)"$/\1/p' "$SRC/docker_executor.py")
FAILURES=0

ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
info() { printf 'INFO %s\n' "$*"; }
need_approval() { [[ "${1:-}" == --operator-approved ]] || { echo "refusing: requires --operator-approved" >&2; exit 64; }; }

# C1A: everything run as Host root must come from a copy ai-agent cannot modify. ai-agent is UID 0
# with no userns remap, so any Host path bind-mounted into it is ai-agent-writable. Printed hashes
# from a mutable tree prove nothing; the operator reviews this copy itself (see the C1A addendum).
# This check guards against running from the wrong tree; it is not a trust anchor on its own.
source_check() {
  local p sources s unit=scripts/maintenance/$SERVICE script=scripts/maintenance/p43_sandbox_bridge_host.sh
  local files=("$script" "$unit" "scripts/maintenance/$C2_SERVICE") rel
  for m in "${MODULES[@]}"; do files+=("app/services/sandbox_bridge/$m"); done
  for rel in "${files[@]}"; do
    p=$REPO/$rel
    if [[ -L "$p" || ! -f "$p" ]]; then bad "source $rel missing or symlink"; continue; fi
    [[ "$(stat -c '%u' "$p")" == 0 && $(( 0$(stat -c '%a' "$p") & 022 )) -eq 0 ]] \
      || bad "source $rel not root-owned or group/other-writable ($(stat -c '%u:%g %a' "$p"))"
  done
  if [[ -n "${P43_C2_CANDIDATE:-}" ]]; then
    if [[ -e "$REPO/.git" || -L "$REPO/.git" || ! -f "$REPO/.orchestrator-release-identity.json" ]]; then
      bad "C2 source is not a protected git-archive release"
    elif ! /usr/bin/python3 -I - "$REPO" "$P43_C2_CANDIDATE" "${P43_C2_CANDIDATE_TREE:-}" <<'PY'
import base64, hashlib, json, os, pathlib, re, stat, sys
root = pathlib.Path(sys.argv[1])
expected_head, expected_tree = sys.argv[2:4]
def git_oid(kind, data):
    return hashlib.sha1(kind + b" " + str(len(data)).encode() + b"\0" + data).digest()
if not re.fullmatch(r"[0-9a-f]{40}", expected_head) or not re.fullmatch(r"[0-9a-f]{40}", expected_tree):
    raise SystemExit("candidate identity inputs invalid")
manifest_path = root / "release-manifest.json"
marker_path = root / ".orchestrator-release-identity.json"
for metadata_path in (manifest_path, marker_path):
    info = metadata_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o222:
        raise SystemExit("release identity metadata file unsafe")
raw_manifest = manifest_path.read_bytes()
manifest = json.loads(raw_manifest.decode("utf-8"))
canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
if raw_manifest != canonical:
    raise SystemExit("release manifest is not canonical")
if (set(manifest) != {"schema_version", "kind", "candidate_head", "candidate_tree", "files"} or
        manifest["schema_version"] != 1 or manifest["kind"] != "p43-host-source" or
        manifest["candidate_head"] != expected_head or manifest["candidate_tree"] != expected_tree or
        not isinstance(manifest["files"], list)):
    raise SystemExit("release manifest identity mismatch")
expected = {}
last_path = None
for entry in manifest["files"]:
    if not isinstance(entry, dict) or set(entry) != {"path", "mode", "blob_sha1"}:
        raise SystemExit("release manifest entry malformed")
    path, mode, blob = entry["path"], entry["mode"], entry["blob_sha1"]
    pure = pathlib.PurePosixPath(path) if isinstance(path, str) else None
    if (pure is None or "\\" in path or pure.is_absolute() or not pure.parts or
            pure.as_posix() != path or any(part in {"", ".", ".."} for part in pure.parts) or
            mode not in {"100644", "100755"} or not re.fullmatch(r"[0-9a-f]{40}", blob) or
            path in expected or (last_path is not None and path <= last_path)):
        raise SystemExit("release manifest path or file metadata invalid")
    expected[path] = (mode, blob)
    last_path = path
marker = json.loads(marker_path.read_text(encoding="utf-8"))
commit = base64.b64decode(marker["commit_object"], validate=True)
if (set(marker) != {"schema_version", "kind", "candidate_head", "candidate_tree", "commit_object"} or
        marker["schema_version"] != 1 or marker["kind"] != "git-archive-release" or
        marker["candidate_head"] != expected_head or marker["candidate_tree"] != expected_tree or
        git_oid(b"commit", commit).hex() != expected_head or
        not commit.startswith(f"tree {expected_tree}\n".encode())):
    raise SystemExit("release identity marker mismatch")
actual_files, actual_dirs = set(), set()
for current, dirs, files in os.walk(root, followlinks=False):
    base = pathlib.Path(current)
    for name in dirs + files:
        item = base / name
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222 or
                (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise SystemExit("release ownership, link, or permissions unsafe")
        rel = item.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            actual_dirs.add(rel)
        elif stat.S_ISREG(info.st_mode):
            if rel not in {"release-manifest.json", ".orchestrator-release-identity.json"}:
                actual_files.add(rel)
        else:
            raise SystemExit("release has an unmanifested entry")
if actual_files != set(expected):
    raise SystemExit("release file inventory mismatch")
expected_dirs = set()
for path in expected:
    parent = pathlib.PurePosixPath(path).parent
    while str(parent) != ".":
        expected_dirs.add(str(parent))
        parent = parent.parent
if actual_dirs != expected_dirs:
    raise SystemExit("release directory inventory mismatch")
for path, (mode, blob) in expected.items():
    item = root / pathlib.PurePosixPath(path)
    info = item.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or bool(info.st_mode & stat.S_IXUSR) != (mode == "100755"):
        raise SystemExit(f"release file mode/type mismatch: {path}")
    digest = hashlib.sha1(f"blob {info.st_size}\0".encode())
    with item.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != blob:
        raise SystemExit(f"release file content mismatch: {path}")
def tree_oid(directory, rel=""):
    entries = []
    for child in directory.iterdir():
        child_rel = f"{rel}/{child.name}".lstrip("/")
        if child_rel in {"release-manifest.json", ".orchestrator-release-identity.json"}:
            continue
        info = child.lstat()
        name = os.fsencode(child.name)
        if stat.S_ISDIR(info.st_mode):
            entries.append((name + b"/", b"40000 " + name + b"\0" + tree_oid(child, child_rel)))
        else:
            mode, blob = expected[child_rel]
            entries.append((name, mode.encode() + b" " + name + b"\0" + bytes.fromhex(blob)))
    entries.sort(key=lambda entry: entry[0])
    return git_oid(b"tree", b"".join(entry for _, entry in entries))
if tree_oid(root).hex() != expected_tree:
    raise SystemExit("release content does not reproduce expected Git tree")
print(f"CANDIDATE_HEAD={expected_head} CANDIDATE_TREE={expected_tree}")
PY
    then
      bad "protected C2 candidate verification failed"
    else
      ok "protected C2 candidate identity, tree, ownership, and immutability verified"
    fi
  fi
  p=$REPO/app/services/sandbox_bridge
  while :; do # every directory from the module dir up to / (sticky root-owned dirs such as /tmp allowed)
    if [[ -L "$p" ]] || ! { [[ "$(stat -c '%u' "$p")" == 0 ]] && { (( (0$(stat -c '%a' "$p") & 022) == 0 )) || [[ -k "$p" ]]; }; }; then
      bad "source dir $p not root-owned/non-writable ($(stat -c '%u:%g %a' "$p"))"
    fi
    [[ "$p" == / ]] && break
    p=$(dirname "$p")
  done
  if ! sources=$(docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null); then
    bad "ai-agent mounts unavailable: cannot prove the source is outside them"
  else
    while IFS= read -r s; do
      [[ -z "$s" ]] && continue
      s=$(readlink -m "$s")
      [[ "$s" == / || "$REPO" == "$s" || "$REPO" == "$s"/* || "$s" == "$REPO"/* ]] &&
        bad "source $REPO overlaps ai-agent mount $s"
    done <<<"$sources"
  fi
  [[ $FAILURES -eq 0 ]] && ok "source $REPO is root-only and outside ai-agent mounts"
  return 0
}

preflight() {
  source_check
  [[ $EUID -eq 0 ]] && ok "running as root" || bad "must run as root on the Host"
  [[ "$(ps -p 1 -o comm= | xargs)" == systemd ]] && ok "systemd is PID 1" || bad "systemd is not PID 1"
  local sdv; sdv=$(systemctl --version | awk 'NR==1{print $2}')
  [[ "$sdv" =~ ^[0-9]+$ && $sdv -ge 250 ]] && ok "systemd $sdv (LoadCredential)" || bad "systemd >= 250 required (got $sdv)"
  systemctl is-active --quiet docker.service && ok "docker.service active" || bad "docker.service not active"
  getent group docker >/dev/null && ok "docker group exists (gid $(getent group docker | cut -d: -f3))" || bad "docker group missing"
  [[ -n "$IMAGE" ]] && docker image inspect "$IMAGE" >/dev/null 2>&1 && ok "pinned image present locally" || bad "pinned image missing: $IMAGE"
  /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' && ok "/usr/bin/python3 $(/usr/bin/python3 -c 'import platform;print(platform.python_version())')" || bad "/usr/bin/python3 >= 3.10 required"
  # Each Docker-derived check must fail closed when Docker itself cannot answer.
  local secopts agent mounts
  if ! secopts=$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null); then bad "docker info unavailable"
  elif grep -q userns <<<"$secopts"; then bad "daemon userns-remap enabled: allowed UID 0 assumption invalid"
  else ok "no daemon userns-remap (container UID 0 == Host UID 0)"; fi
  agent=$(docker inspect "$AI_AGENT" --format '{{.State.Running}}|{{.Config.User}}|{{.HostConfig.UsernsMode}}|{{.HostConfig.Privileged}}' 2>/dev/null || true)
  [[ "$agent" == "true||"*"|false" ]] && ok "ai-agent running as root, unprivileged, no per-container userns ($agent)" || bad "unexpected ai-agent identity: '$agent'"
  if ! mounts=$(docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null); then bad "ai-agent mounts unavailable"
  elif grep -Eq '(^| )/(var/)?run/docker\.sock( |$)' <<<"$mounts"; then bad "ai-agent already mounts the Docker socket"
  else ok "ai-agent has no Docker socket mount"; fi
  for m in "${MODULES[@]}"; do [[ -f "$SRC/$m" && ! -L "$SRC/$m" ]] || bad "missing source module $m"; done
  (cd "$SRC" && sha256sum "${MODULES[@]}") | sed 's/^/INFO source /'
  if command -v systemd-analyze >/dev/null; then
    systemd-analyze verify "$REPO/scripts/maintenance/$SERVICE" 2>&1 | grep -v -e "User=p43-sandbox" -e "not executable" | sed 's/^/INFO systemd-analyze: /' || true
  fi
  # Existing state is reported, never changed.
  id "$SVC_USER" >/dev/null 2>&1 && info "service user exists: $(id "$SVC_USER")" || info "service user absent (install creates it)"
  [[ -e "$UNIT_DST" ]] && info "unit already installed" || info "unit absent"
  [[ -e "$KEY" ]] && info "key present: $(stat -c '%U:%G %a %s bytes' "$KEY")" || info "key absent (install generates it)"
  [[ -e "$STATE" ]] && info "state dir present: $(stat -c '%U:%G %a' "$STATE")" || info "state dir absent"
  printf 'PREFLIGHT=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo READY || echo BLOCKED)" "$FAILURES"
  [[ $FAILURES -eq 0 ]]
}

private_dir() { # path owner
  [[ -L "$1" ]] && { echo "refusing symlink: $1" >&2; exit 1; }
  install -d -o "$2" -g "$2" -m 0700 "$1"
}

install_host() {
  need_approval "${1:-}"
  preflight
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
  fi
  # Docker access is granted only to the unit via SupplementaryGroups, never permanently to the account.
  id -nG "$SVC_USER" | tr ' ' '\n' | grep -qx docker && { echo "refusing: $SVC_USER is a permanent docker group member" >&2; exit 1; }

  # Root-owned code copy: ai-agent can write the repo, so the service must never run code from it.
  install -d -o root -g root -m 0755 "$OPT" "$OPT/lib" "$OPT/lib/sandbox_bridge"
  for m in "${MODULES[@]}"; do install -o root -g root -m 0644 "$SRC/$m" "$OPT/lib/sandbox_bridge/$m"; done
  (cd "$OPT/lib/sandbox_bridge" && sha256sum "${MODULES[@]}") > "$OPT/MANIFEST.sha256"
  chmod 0644 "$OPT/MANIFEST.sha256"
  (cd "$SRC" && sha256sum -c --quiet "$OPT/MANIFEST.sha256") || { echo "installed code does not match source" >&2; exit 1; }

  # Key: generated once; an existing key is validated and never rotated or printed.
  install -d -o root -g root -m 0700 "$KEY_DIR"
  if [[ ! -e "$KEY" ]]; then
    local tmp; tmp=$(mktemp "$KEY_DIR/.bridge.key.XXXXXX")
    /usr/bin/python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(32) + "\n")' > "$tmp"
    chmod 0600 "$tmp"
    mv -n "$tmp" "$KEY"; rm -f "$tmp"
  fi
  [[ -f "$KEY" && ! -L "$KEY" && "$(stat -c '%u:%g %a' "$KEY")" == "0:0 600" ]] || { echo "unsafe key file $KEY" >&2; exit 1; }
  [[ "$(tr -d '\n' < "$KEY" | wc -c)" -ge 64 ]] || { echo "key too short" >&2; exit 1; }

  # Persistent (not /run) so the ai-agent bind mount survives Launcher restarts and reboots.
  private_dir "$STATE" "$SVC_USER"
  for d in socket ledger staging; do private_dir "$STATE/$d" "$SVC_USER"; done

  install -o root -g root -m 0644 "$REPO/scripts/maintenance/$SERVICE" "$UNIT_DST"
  systemctl daemon-reload
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  for _ in $(seq 1 50); do [[ -S "$SOCK" ]] && break; sleep 0.2; done
  systemctl is-active --quiet "$SERVICE" && [[ -S "$SOCK" ]] || { journalctl -u "$SERVICE" -n 30 --no-pager; exit 1; }
  ok "service active; socket $(stat -c '%u:%g %a' "$SOCK")"
  echo "NEXT: apply scripts/maintenance/p43-sandbox-bridge.compose-ai-agent.yml to ai-agent, recreate ai-agent, then run: $0 verify"
}

verify_host() {
  local run t0 out
  run=$(date -u +%Y%m%dT%H%M%SZ)-$$
  out=$REPO/docs/roadmap/reports/evidence/p43-product-22-c1/$run
  install -d -m 2775 "$out"
  t0=$(date +%s)
  {
    systemctl is-active --quiet "$SERVICE" && ok "service active" || bad "service inactive"
    [[ "$(stat -c '%U:%G %a' "$STATE/socket")" == "$SVC_USER:$SVC_USER 700" ]] && ok "socket dir $(stat -c '%u:%g %a' "$STATE/socket")" || bad "socket dir identity"
    [[ -S "$SOCK" && "$(stat -c '%U %a' "$SOCK")" == "$SVC_USER 660" ]] && ok "socket $(stat -c '%u:%g %a' "$SOCK")" || bad "socket identity"
    [[ "$(ls -A "$STATE/socket")" == launcher.sock ]] && ok "socket dir holds only launcher.sock" || bad "unexpected entries in socket dir"
    local mounts residual
    mounts=$(docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}|{{.RW}}{{"\n"}}{{end}}') || bad "ai-agent inspect unavailable"
    grep -qx "$STATE/socket|/run/p43-sandbox-bridge|false" <<<"$mounts" && ok "ai-agent socket-dir mount read-only" || bad "ai-agent socket-dir mount missing/rw"
    grep -qx "$KEY|/run/secrets/p43-sandbox-bridge.key|false" <<<"$mounts" && ok "ai-agent key mount read-only" || bad "ai-agent key mount missing/rw"
    grep -Eq '^(/var)?/run/docker\.sock\|' <<<"$mounts" && bad "Docker socket mounted into ai-agent" || ok "no Docker socket in ai-agent"
    awk -F'|' -v s="$STATE" -v k="$KEY" '(index($1, s) == 1 && $1 != s "/socket") || (index($1, "/etc/p43-sandbox-bridge") == 1 && $1 != k) || index($1, "/opt/p43-sandbox-bridge") == 1' <<<"$mounts" \
      | grep -q . && bad "unintended bridge Host mount" || ok "no ledger/staging/state-root/code mounts"
    docker exec -w "$AI_AGENT_REPO/app/services" "$AI_AGENT" /usr/bin/python3 -B -E -s -m sandbox_bridge.host_service client-check > "$out/client-check.json" \
      && ok "client-check $(cat "$out/client-check.json")" || bad "client-check $(cat "$out/client-check.json")"
    sleep 1
    docker events --since "$t0" --until "$(date +%s)" --filter type=container --format '{{.Action}} {{.Actor.Attributes.name}} {{.Actor.Attributes.image}}' \
      | grep ' p43-sbx-' > "$out/docker-events.txt" || true
    grep -q '^create p43-sbx-' "$out/docker-events.txt" && ok "real sandbox container created" || bad "no sandbox container create event"
    grep -q '^destroy p43-sbx-' "$out/docker-events.txt" && ok "sandbox container destroyed" || bad "no sandbox destroy event"
    grep -v " $IMAGE\$" "$out/docker-events.txt" | grep -q . && bad "sandbox used unexpected image" || ok "pinned image only"
    residual=$(docker ps -aq --filter name=^/p43-sbx-) && [[ -z "$residual" ]] && ok "no residual sandbox containers" || bad "residual sandbox containers or docker unavailable"
    [[ -z "$(ls -A "$STATE/staging")" ]] && ok "staging empty" || bad "staging not empty"
    runuser -u "$SVC_USER" -- /usr/bin/python3 -B -E -s -c 'import sys; sys.path.insert(0, "/opt/p43-sandbox-bridge/lib"); from sandbox_bridge.host_service import main; sys.exit(main(["status"]))' \
      | sed 's/^/INFO status /'
    journalctl -u "$SERVICE" --since "@$t0" --no-pager -o cat > "$out/journal.txt" || true
    (cd "$OPT/lib/sandbox_bridge" && sha256sum -c --quiet "$OPT/MANIFEST.sha256") && ok "installed code matches manifest" || bad "installed code drift"
    printf 'C1_VERIFY=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"
  } 2>&1 | tee "$out/verify.txt"
  chmod 0664 "$out"/*
  grep -q '^C1_VERIFY=PASS' "$out/verify.txt"
}

rollback() {
  need_approval "${1:-}"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  [[ -S "$SOCK" && ! -L "$SOCK" ]] && rm -f -- "$SOCK"
  [[ -f "$UNIT_DST" ]] && rm -f -- "$UNIT_DST"
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE" 2>/dev/null || true
  ok "service stopped/disabled; unit and socket removed"
  info "preserved: $STATE (ledger, staging, socket dir), $KEY, $OPT, user $SVC_USER"
  info "keep $STATE/socket and $KEY while ai-agent Compose still references them (create_host_path=false)"
  info "to restore the previous ai-agent mounts: remove the two p43-sandbox-bridge entries from Compose and recreate ai-agent"
}

c2_preflight() {
  preflight || true
  local mounts
  if ! mounts=$(docker inspect "$AI_AGENT" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null); then
    bad "ai-agent mounts unavailable: cannot prove the C2 key/socket stay outside the model environment"
  elif grep -Eq "^($C2_KEY_DIR|$C2_STATE|$C2_RUN)(/|\$)" <<<"$mounts"; then
    bad "ai-agent mounts C2 Launcher material"
  else ok "ai-agent mounts no C2 key, state, or socket path"; fi
  getent passwd "$C2_WORKER_UID" >/dev/null && info "Host account owns UID $C2_WORKER_UID: $(getent passwd "$C2_WORKER_UID" | cut -d: -f1) (cannot traverse $C2_RUN unless in group $C2_USER)" || info "no Host account owns UID $C2_WORKER_UID"
  systemctl is-active --quiet "$C2_SERVICE" && bad "C2 Launcher already active" || ok "C2 Launcher not active"
  [[ -e "$C2_UNIT_DST" ]] && info "C2 unit already installed" || info "C2 unit absent"
  [[ -e "$C2_KEY" ]] && info "C2 key present: $(stat -c '%U:%G %a' "$C2_KEY")" || info "C2 key absent (c2-install generates it)"
  printf 'C2_PREFLIGHT=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo READY || echo BLOCKED)" "$FAILURES"
  [[ $FAILURES -eq 0 ]]
}

c2_install() {
  need_approval "${1:-}"
  c2_preflight
  if ! id "$C2_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$C2_USER"
  fi
  id -nG "$C2_USER" | tr ' ' '\n' | grep -qx docker && { echo "refusing: $C2_USER is a permanent docker group member" >&2; exit 1; }
  install -d -o root -g root -m 0755 "$C2_OPT" "$C2_OPT/lib" "$C2_OPT/lib/sandbox_bridge"
  for m in "${MODULES[@]}"; do install -o root -g root -m 0644 "$SRC/$m" "$C2_OPT/lib/sandbox_bridge/$m"; done
  (cd "$C2_OPT/lib/sandbox_bridge" && sha256sum "${MODULES[@]}") > "$C2_OPT/MANIFEST.sha256"
  chmod 0644 "$C2_OPT/MANIFEST.sha256"
  (cd "$SRC" && sha256sum -c --quiet "$C2_OPT/MANIFEST.sha256") || { echo "installed C2 code does not match source" >&2; exit 1; }
  # Dedicated key: never the C1B key. The Launcher reads it via LoadCredential; the
  # Worker gets an owner-only copy for its UID (load_key requires owner == reader).
  install -d -o root -g root -m 0700 "$C2_KEY_DIR"
  if [[ ! -e "$C2_KEY" ]]; then
    local tmp; tmp=$(mktemp "$C2_KEY_DIR/.bridge.key.XXXXXX")
    /usr/bin/python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(32) + "\n")' > "$tmp"
    chmod 0600 "$tmp"
    mv -n "$tmp" "$C2_KEY"; rm -f "$tmp"
  fi
  [[ -f "$C2_KEY" && ! -L "$C2_KEY" && "$(stat -c '%u:%g %a' "$C2_KEY")" == "0:0 600" ]] || { echo "unsafe key file $C2_KEY" >&2; exit 1; }
  cmp -s "$C2_KEY" "$KEY" && { echo "refusing: C2 key equals the C1B key" >&2; exit 1; }
  install -o "$C2_WORKER_UID" -g "$C2_WORKER_UID" -m 0400 "$C2_KEY" "$C2_WORKER_KEY"
  install -o root -g root -m 0644 "$REPO/scripts/maintenance/$C2_SERVICE" "$C2_UNIT_DST"
  systemctl daemon-reload
  ok "C2 unit installed (not enabled, not started); Worker key $(stat -c '%u:%g %a' "$C2_WORKER_KEY")"
  info "C2 socket group gid: $(getent group "$C2_USER" | cut -d: -f3) (pass to the Worker as --group-add)"
}

c2_rollback() {
  need_approval "${1:-}"
  systemctl stop "$C2_SERVICE" 2>/dev/null || true
  [[ -f "$C2_UNIT_DST" ]] && rm -f -- "$C2_UNIT_DST"
  [[ -f "$C2_WORKER_KEY" && ! -L "$C2_WORKER_KEY" ]] && rm -f -- "$C2_WORKER_KEY"
  systemctl daemon-reload
  systemctl reset-failed "$C2_SERVICE" 2>/dev/null || true
  ok "C2 Launcher stopped; unit and Worker key copy removed; $C2_RUN is removed by systemd"
  info "preserved for evidence: $C2_STATE (ledger, staging), $C2_KEY, $C2_OPT, user $C2_USER"
  info "C1B service $SERVICE was not touched"
}

case "${1:-}" in
  source-check) source_check; printf 'SOURCE_CHECK=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"; [[ $FAILURES -eq 0 ]] ;;
  preflight) preflight ;;
  install) install_host "${2:-}" ;;
  verify) verify_host ;;
  rollback) rollback "${2:-}" ;;
  c2-preflight) c2_preflight ;;
  c2-install) c2_install "${2:-}" ;;
  c2-rollback) c2_rollback "${2:-}" ;;
  *) echo "usage: $0 {source-check|preflight|install --operator-approved|verify|rollback --operator-approved|c2-preflight|c2-install --operator-approved|c2-rollback --operator-approved}" >&2; exit 64 ;;
esac
