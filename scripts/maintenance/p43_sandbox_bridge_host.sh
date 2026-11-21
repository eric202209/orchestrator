#!/usr/bin/env bash
# P43-PRODUCT-22-C1 one-time Host Launcher bootstrap. Run on the GX10 Host as root.
#   source-check                   read-only: this checkout must be a root-only copy ai-agent cannot write
#   preflight                      read-only checks; changes nothing
#   install --operator-approved    create service user, root-owned code copy, key (if absent),
#                                  state dirs, systemd unit; enable and start the service
#   verify                         after the Compose change + ai-agent recreation: socket identity,
#                                  ai-agent mounts, synthetic client check from inside ai-agent
#   rollback --operator-approved   stop/disable only this service; remove only its socket and unit
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
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SRC=$REPO/app/services/sandbox_bridge
MODULES=(__init__.py contract.py docker_executor.py host_service.py ipc_contract.py ledger.py staging.py transport.py)
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
  local files=("$script" "$unit") rel
  for m in "${MODULES[@]}"; do files+=("app/services/sandbox_bridge/$m"); done
  for rel in "${files[@]}"; do
    p=$REPO/$rel
    if [[ -L "$p" || ! -f "$p" ]]; then bad "source $rel missing or symlink"; continue; fi
    [[ "$(stat -c '%u' "$p")" == 0 && $(( 0$(stat -c '%a' "$p") & 022 )) -eq 0 ]] \
      || bad "source $rel not root-owned or group/other-writable ($(stat -c '%u:%g %a' "$p"))"
  done
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
      [[ "$s" == / || "$REPO" == "$s" || "$REPO" == "$s"/* ]] && bad "source $REPO is inside ai-agent mount $s"
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

case "${1:-}" in
  source-check) source_check; printf 'SOURCE_CHECK=%s failures=%d\n' "$([[ $FAILURES -eq 0 ]] && echo PASS || echo FAIL)" "$FAILURES"; [[ $FAILURES -eq 0 ]] ;;
  preflight) preflight ;;
  install) install_host "${2:-}" ;;
  verify) verify_host ;;
  rollback) rollback "${2:-}" ;;
  *) echo "usage: $0 {source-check|preflight|install --operator-approved|verify|rollback --operator-approved}" >&2; exit 64 ;;
esac
