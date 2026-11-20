#!/usr/bin/env bash
# Operator-controlled P43-CAPABILITY-04B Ollama residency preflight.
# This script is deliberately inert unless an explicit operation is supplied.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

readonly ROOT=/run/p43-capability-04b
readonly OWNER_LOCK=/run/lock/p43-capability-04b.owner.lock
readonly ROLLBACK_LOCK=/run/lock/p43-capability-04b.rollback.lock
readonly UNIT=ollama
readonly MODEL=qwen3-coder:30b
readonly UNIT_FILE=/etc/systemd/system/ollama.service
readonly OVERRIDE_FILE=/etc/systemd/system/ollama.service.d/override.conf
readonly CONTEXT=32768
readonly WINDOW_SECONDS=900
readonly RESTORE_BUDGET_SECONDS=300
readonly WATCHDOG_DELAY_SECONDS=$((WINDOW_SECONDS - RESTORE_BUDGET_SECONDS))
readonly OBSERVE_SECONDS=180
readonly GIB_KIB=1048576

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPECTED_HOST=""
VLLM_BASE=""
VLLM_PID=""
VLLM_ENGINE_PID=""
RUN_ID=""
WATCHDOG_UNIT=""
INTERNAL_WATCHDOG=0
STOP_REQUESTED=0

usage() {
  cat <<'EOF'
Usage:
  p43-capability-04b-host-preflight.sh check --expected-host HOST --vllm-base-url URL --vllm-server-pid PID --vllm-enginecore-pid PID
  p43-capability-04b-host-preflight.sh run --expected-host HOST --vllm-base-url URL --vllm-server-pid PID --vllm-enginecore-pid PID
  p43-capability-04b-host-preflight.sh status
  p43-capability-04b-host-preflight.sh rollback

`run` is never the default and requires an interactive exact-phrase confirmation.
The watchdog uses a private noninteractive rollback entry point.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
log() { logger -t p43-capability-04b -- "$*" 2>/dev/null || true; }

parse_args() {
  local op="${1:-}"
  [[ -n "$op" ]] || { usage; return 2; }
  shift
  if [[ "$op" == _rollback-child ]]; then
    [[ "${P43_INTERNAL_ROLLBACK:-}" == 1 && $# == 2 ]] || die 'internal rollback entry point is not callable directly'
    OP="$op" RUN_ID="$1" CHILD_MODE="$2"
    return 0
  fi
  case "$op" in check|run|status|rollback|_rollback-child) ;; *) usage; return 2 ;; esac
  OP="$op"
  while (($#)); do
    case "$1" in
      --expected-host) (($# >= 2)) || die 'missing --expected-host value'; EXPECTED_HOST="$2"; shift 2 ;;
      --vllm-base-url) (($# >= 2)) || die 'missing --vllm-base-url value'; VLLM_BASE="${2%/}"; shift 2 ;;
      --vllm-pid) (($# >= 2)) || die 'missing --vllm-pid value'; VLLM_PID="$2"; shift 2 ;;
      --vllm-server-pid) (($# >= 2)) || die 'missing --vllm-server-pid value'; VLLM_PID="$2"; shift 2 ;;
      --vllm-enginecore-pid) (($# >= 2)) || die 'missing --vllm-enginecore-pid value'; VLLM_ENGINE_PID="$2"; shift 2 ;;
      --watchdog) (($# >= 2)) || die 'missing watchdog run id'; INTERNAL_WATCHDOG=1; RUN_ID="$2"; shift 2 ;;
      --watchdog-unit) (($# >= 2)) || die 'missing watchdog unit'; WATCHDOG_UNIT="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  if [[ "$OP" == check || "$OP" == run ]]; then
    [[ -n "$EXPECTED_HOST" ]] || die '--expected-host is required'
    [[ -n "$VLLM_BASE" ]] || die '--vllm-base-url is required'
    [[ "$VLLM_PID" =~ ^[1-9][0-9]*$ ]] || die '--vllm-pid must be a positive host PID'
    [[ "$VLLM_ENGINE_PID" =~ ^[1-9][0-9]*$ ]] || die '--vllm-enginecore-pid must be a positive host PID'
  fi
  if [[ -n "$VLLM_BASE" ]]; then
    [[ "$VLLM_BASE" =~ ^https?://[^/[:space:]]+(:[0-9]+)?(/[^[:space:]]*)?$ ]] || die 'vLLM base URL must be an http(s) URL without credentials or query'
    [[ "$VLLM_BASE" != *'@'* && "$VLLM_BASE" != *'?'* && "$VLLM_BASE" != *'#'* ]] || die 'vLLM URL credentials/query/fragment are not allowed'
  fi
}

require_root() { ((EUID == 0)) || die 'run this operation from a root-capable GX10 host shell'; }

require_commands() {
  local c
  for c in awk bash cat chmod chown curl date dirname flock grep head hostname install journalctl logger mktemp mv ollama ps readlink rm sha256sum sleep stat systemctl systemd-detect-virt systemd-run timeout tr; do
    command -v "$c" >/dev/null 2>&1 || die "required command unavailable: $c"
  done
  command -v python3 >/dev/null 2>&1 || die 'python3 is required for JSON and environment parsing'
}

check_trusted_script() {
  local uid mode mode_num
  uid="$(stat -c %u -- "$SCRIPT_PATH")"
  mode="$(stat -c %a -- "$SCRIPT_PATH")"
  [[ "$uid" == 0 ]] || die 'install the script as root-owned before using run or rollback'
  mode_num=$((8#$mode))
  (( (mode_num & 0022) == 0 )) || die 'script must not be group- or world-writable; install a protected copy'
}

check_host_identity() {
  local actual container vm product board vendor dmi
  actual="$(hostnamectl --static 2>/dev/null || hostname)"
  [[ "$actual" == "$EXPECTED_HOST" ]] || die "hostname mismatch: expected '$EXPECTED_HOST', got '$actual'"
  container="$(systemd-detect-virt --container 2>/dev/null || true)"
  [[ -z "$container" || "$container" == none ]] || die "container detected ('$container'); run directly on GX10 host"
  vm="$(systemd-detect-virt --vm 2>/dev/null || true)"
  [[ -z "$vm" || "$vm" == none ]] || die "virtual machine detected ('$vm'); refusing non-host execution"
  [[ -S /run/systemd/private ]] || die 'host systemd private socket unavailable'
  [[ "$(ps -p 1 -o comm= | tr -d '[:space:]')" == systemd ]] || die 'PID 1 is not systemd; refusing non-host execution'
  vendor="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)"
  product="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
  board="$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)"
  dmi="${product} ${board}"
  [[ "$vendor" =~ ASUS ]] || die "DMI manufacturer is not ASUS ('$vendor')"
  [[ "$dmi" =~ [Gg][Xx]10|[Aa]scent ]] || die "DMI does not identify a GX10/Ascent host ('$dmi')"
  note "Host identity: $actual; DMI: $dmi"
}

systemd_value() { systemctl show "$UNIT" -p "$1" --value; }

env_value() {
  local key="$1" envline
  envline="$(systemd_value Environment)"
  printf '%s' "$envline" | python3 -c 'import shlex,sys; k=sys.argv[1]; vals=shlex.split(sys.stdin.read()); found=[x.split("=",1)[1] for x in vals if x.startswith(k+"=")]; print(found[-1] if found else "")' "$key"
}

live_env_value() {
  local pid="$1" key="$2"
  tr '\0' '\n' <"/proc/$pid/environ" | awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); value=$0; found=1} END {if(found) print value}'
}

check_mem_baseline() {
  local avail swap_total swap_free
  read -r avail swap_total swap_free < <(awk '/^MemAvailable:/ {a=$2} /^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print a,t,f}' /proc/meminfo)
  [[ "$avail" =~ ^[0-9]+$ && "$swap_total" =~ ^[0-9]+$ && "$swap_free" =~ ^[0-9]+$ ]] || die 'cannot read host memory/swap values'
  (( avail >= 40 * GIB_KIB )) || die "MemAvailable below 40 GiB: $((avail / GIB_KIB)) GiB"
  (( swap_total == swap_free )) || die 'swap is already in use'
  note "MemAvailable: $((avail / GIB_KIB)) GiB; swap: unused"
}

memory_snapshot() {
  awk '/^MemAvailable:|^SwapTotal:|^SwapFree:/ {print $1, $2}' /proc/meminfo
  awk '$1=="pswpin" || $1=="pswpout" || $1=="oom_kill" {print $1"="$2}' /proc/vmstat
  awk '/^full / {print "psi_full_avg10="$2; print "psi_full_total=" $NF}' /proc/pressure/memory
}

proc_rss_kib() {
  local pid="$1"
  awk '/^VmRSS:/ {print $2; found=1} END {if(!found) exit 1}' "/proc/$pid/status"
}

proc_start_ticks() {
  local pid="$1" stat rest
  stat="$(cat "/proc/$pid/stat")" || return 1
  rest="${stat##*) }"
  awk '{print $20}' <<<"$rest"
}

cgroup_events_path() {
  local pid="$1" cg
  cg="$(awk -F: '$1=="0" {print $3}' "/proc/$pid/cgroup" 2>/dev/null || true)"
  [[ -n "$cg" ]] || return 1
  printf '/sys/fs/cgroup%s/memory.events\n' "$cg"
}

cgroup_memory_path() {
  local pid="$1" cg
  cg="$(awk -F: '$1=="0" {print $3}' "/proc/$pid/cgroup" 2>/dev/null || true)"
  [[ -n "$cg" ]] || return 1
  printf '/sys/fs/cgroup%s/memory.current\n' "$cg"
}

proc_cgroup_path() {
  awk -F: '$1=="0" {print $3; found=1} END {if(!found) exit 1}' "/proc/$1/cgroup"
}

check_vllm_process_contract() {
  local server_cgroup engine_cgroup server_ticks engine_ticks comm
  [[ -r "/proc/$VLLM_PID/cmdline" && -r "/proc/$VLLM_ENGINE_PID/cmdline" ]] || die 'vLLM Server or EngineCore PID is unreadable'
  server_ticks="$(proc_start_ticks "$VLLM_PID")" || die 'cannot identify vLLM Server process start time'
  engine_ticks="$(proc_start_ticks "$VLLM_ENGINE_PID")" || die 'cannot identify vLLM EngineCore process start time'
  server_cgroup="$(proc_cgroup_path "$VLLM_PID")" || die 'cannot identify vLLM Server cgroup'
  engine_cgroup="$(proc_cgroup_path "$VLLM_ENGINE_PID")" || die 'cannot identify vLLM EngineCore cgroup'
  [[ "$engine_cgroup" == "$server_cgroup"/* || "$engine_cgroup" == "$server_cgroup" ]] || die 'vLLM Server cgroup does not contain EngineCore; aggregate cgroup telemetry is not attributable'
  comm="$(tr '\0' ' ' <"/proc/$VLLM_PID/cmdline")"
  [[ "$comm" == *vllm* || "$comm" == *api_server* ]] || die 'supplied Server PID command line does not identify vLLM API Server'
  comm="$(tr '\0' ' ' <"/proc/$VLLM_ENGINE_PID/cmdline")"
  [[ "$comm" == *EngineCore* || "$comm" == *engine_core* ]] || die 'supplied EngineCore PID command line does not identify vLLM EngineCore'
  VLLM_SERVER_START_TICKS="$server_ticks"
  VLLM_ENGINE_START_TICKS="$engine_ticks"
  VLLM_CGROUP="/sys/fs/cgroup$server_cgroup"
  [[ -r "$VLLM_CGROUP/memory.current" && -r "$VLLM_CGROUP/memory.events" ]] || die 'vLLM Server cgroup does not expose aggregate memory telemetry'
}

check_resource_telemetry() {
  local psi_total psi_avg swap_in swap_out oomkill ollama_pid ollama_events vllm_events f
  psi_total="$(awk '/^full / {for(i=1;i<=NF;i++) if($i~/^total=/){sub("total=", "", $i); print $i}}' /proc/pressure/memory)"
  psi_avg="$(awk '/^full / {for(i=1;i<=NF;i++) if($i~/^avg10=/){sub("avg10=", "", $i); print $i}}' /proc/pressure/memory)"
  [[ "$psi_total" =~ ^[0-9]+$ && "$psi_avg" =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'memory PSI full telemetry is missing or malformed'
  read -r swap_in swap_out oomkill < <(awk '$1=="pswpin" {a=$2} $1=="pswpout" {b=$2} $1=="oom_kill" {c=$2} END {print a,b,c}' /proc/vmstat)
  [[ "$swap_in" =~ ^[0-9]+$ && "$swap_out" =~ ^[0-9]+$ && "$oomkill" =~ ^[0-9]+$ ]] || die 'host swap/OOM counters are unavailable'
  ollama_pid="$(systemd_value MainPID)"
  ollama_events="$(cgroup_events_path "$ollama_pid")"
  vllm_events="$VLLM_CGROUP/memory.events"
  for f in "$ollama_events" "$vllm_events"; do
    [[ -r "$f" ]] || die "cgroup OOM counters unavailable: $f"
    awk '$1=="oom" {o=1} $1=="oom_kill" {k=1} END {exit(o&&k?0:1)}' "$f" || die "cgroup OOM counters malformed: $f"
  done
  for f in "$(cgroup_memory_path "$ollama_pid")" "$VLLM_CGROUP/memory.current"; do
    [[ -r "$f" ]] && [[ "$(cat "$f")" =~ ^[0-9]+$ ]] || die "cgroup allocation telemetry unavailable: $f"
  done
  journalctl -k -n 1 -o cat --no-pager >/dev/null 2>&1 || die 'kernel journal telemetry is unavailable'
}

check_vllm() {
  local models metrics
  curl -fsS --connect-timeout 2 --max-time 4 -o /dev/null "$VLLM_BASE/health" || { log 'vLLM health endpoint failed'; return 1; }
  models="$(curl -fsS --connect-timeout 2 --max-time 4 "$VLLM_BASE/v1/models")" || { log 'vLLM models endpoint failed'; return 1; }
  metrics="$(curl -fsS --connect-timeout 2 --max-time 4 "$VLLM_BASE/metrics")" || { log 'vLLM metrics endpoint failed'; return 1; }
  python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); ids=[x.get("id") for x in d.get("data",[])]; sys.exit(0 if "qwen-local" in ids else 1)' <<<"$models" || { log 'vLLM does not list expected qwen-local model'; return 1; }
  python3 -c 'import re,sys; s=sys.stdin.read(); vals={};
for n in ("vllm:num_requests_running","vllm:num_requests_waiting"):
 m=re.findall(r"^"+re.escape(n)+r"(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$",s,re.M)
 if len(m)!=1: sys.exit(2)
 vals[n]=float(m[0])
sys.exit(0 if all(v==0 for v in vals.values()) else 1)' <<<"$metrics" || { log 'vLLM metrics are missing, nonzero, or malformed; require idle 0/0'; return 1; }
  [[ -r "/proc/$VLLM_PID/status" ]] || { log 'supplied vLLM Server PID is not readable'; return 1; }
  proc_rss_kib "$VLLM_PID" >/dev/null || { log 'cannot attribute vLLM process RSS'; return 1; }
}

check_ollama_baseline() {
  local active pid host ctx psjson count
  active="$(systemd_value ActiveState)"
  [[ "$active" == active ]] || die 'Ollama service is not active'
  pid="$(systemd_value MainPID)"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]] || die 'Ollama MainPID is missing or unreadable'
  host="$(live_env_value "$pid" OLLAMA_HOST)"
  ctx="$(live_env_value "$pid" OLLAMA_CONTEXT_LENGTH)"
  [[ "$host" == 0.0.0.0 ]] || die 'live Ollama OLLAMA_HOST is not 0.0.0.0'
  [[ -z "$ctx" ]] || die 'live Ollama already has OLLAMA_CONTEXT_LENGTH set'
  [[ -z "$(env_value OLLAMA_CONTEXT_LENGTH)" ]] || die 'systemd effective Environment already sets OLLAMA_CONTEXT_LENGTH'
  [[ "$(env_value OLLAMA_HOST)" == 0.0.0.0 ]] || die 'effective systemd OLLAMA_HOST is not 0.0.0.0'
  [[ -z "$(systemd_value UnsetEnvironment)" || "$(systemd_value UnsetEnvironment)" != *OLLAMA_CONTEXT_LENGTH* ]] || die 'UnsetEnvironment conflicts with temporary context setting'
  local env_files
  env_files="$(systemd_value EnvironmentFiles)"
  [[ -z "$env_files" || "$env_files" == - ]] || die 'EnvironmentFiles are present; cannot prove context precedence safely'
  [[ -f "$UNIT_FILE" && -f "$OVERRIDE_FILE" ]] || die 'documented persistent Ollama unit/override paths are missing'
  [[ "$(systemd_value FragmentPath)" == "$UNIT_FILE" ]] || die 'active Ollama FragmentPath differs from documented baseline'
  [[ "$(systemd_value DropInPaths)" == *"$OVERRIDE_FILE"* ]] || die 'persistent override is not listed in DropInPaths'
  [[ "$(systemd_value DropInPaths)" != *p43-capability-04b* ]] || die 'another P43 temporary drop-in already exists'
  psjson="$(curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:11434/api/ps)" || die 'Ollama /api/ps failed'
  count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("models",[])))' <<<"$psjson")" || die 'Ollama /api/ps is malformed'
  [[ "$count" == 0 ]] || die 'Ollama already has resident models'
  timeout 10s ollama ps | awk 'NR>1 && NF {n++} END {exit(n==0 ? 0 : 1)}' || die 'ollama ps is not empty or timed out'
  timeout 5s curl -fsS --connect-timeout 2 http://127.0.0.1:11434/api/version >/dev/null || die 'Ollama API health check failed'
}

baseline_checks() {
  require_root
  require_commands
  [[ -n "$EXPECTED_HOST" && -n "$VLLM_BASE" && -n "$VLLM_PID" ]] || die 'host, vLLM URL, and vLLM PID are required'
  check_host_identity
  check_mem_baseline
  check_ollama_baseline
  check_vllm || die 'vLLM health/idle/PID prerequisites failed'
  check_vllm_process_contract
  check_resource_telemetry
  local events
  events="$(cgroup_events_path "$(systemd_value MainPID)")" || die 'cannot identify Ollama cgroup OOM counters'
  [[ -r "$events" && -r "$(cgroup_memory_path "$(systemd_value MainPID)")" && -r /proc/pressure/memory ]] || die 'host PSI or Ollama cgroup memory/OOM telemetry unavailable'
  [[ -r "$VLLM_CGROUP/memory.events" && -r "$VLLM_CGROUP/memory.current" ]] || die 'vLLM aggregate cgroup memory/OOM telemetry unavailable'
  systemd-run --help 2>&1 | grep -q -- '--on-unit-inactive' || die 'systemd-run lacks recurring watchdog timer support'
  note 'Read-only prerequisites: PASS'
}

read_active_run() {
  [[ -f "$ROOT/active" ]] || return 1
  IFS= read -r RUN_ID <"$ROOT/active" || return 1
  [[ "$RUN_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || die 'active run marker is malformed'
}

read_status_run() {
  if [[ -f "$ROOT/active" ]]; then
    IFS= read -r RUN_ID <"$ROOT/active" || return 1
  elif [[ -f "$ROOT/last" ]]; then
    IFS= read -r RUN_ID <"$ROOT/last" || return 1
  else
    return 1
  fi
  [[ "$RUN_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || die 'run marker is malformed'
}

workdir_for() { printf '%s/%s\n' "$ROOT" "$1"; }
state_file_for() { printf '%s/state\n' "$(workdir_for "$1")"; }
kv() { awk -F= -v k="$2" '$1==k {sub(/^[^=]*=/, ""); v=$0; found=1} END {if(found) print v}' "$1"; }

set_state() {
  local key="$1" value="$2" file="$3" tmp
  [[ "$key" =~ ^[A-Z0-9_]+$ && "$value" != *$'\n'* ]] || die 'unsafe state value'
  exec 7>>"${file}.lock"
  flock -x 7
  tmp="$(mktemp "${file}.XXXXXX")"
  awk -F= -v k="$key" '$1!=k {print}' "$file" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$file"
  flock -u 7
  exec 7>&-
}

get_state() { kv "$(state_file_for "$1")" "$2"; }

transition_state() {
  local expected="$1" next_state="$2" file="$3" tmp current
  exec 7>>"${file}.lock"
  flock -x 7
  current="$(kv "$file" STATE)"
  if [[ "$current" != "$expected" ]]; then
    flock -u 7
    exec 7>&-
    return 1
  fi
  tmp="$(mktemp "${file}.XXXXXX")"
  awk -F= '$1!="STATE" {print}' "$file" >"$tmp"
  printf 'STATE=%s\n' "$next_state" >>"$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$file"
  flock -u 7
  exec 7>&-
}

verify_active_owner() {
  local id="$1" work state
  work="$(workdir_for "$id")"
  state="$(state_file_for "$id")"
  [[ -d "$work" && -f "$state" && -f "$ROOT/active" ]] || die 'run state is missing; do not infer restoration'
  [[ "$(stat -c %u "$work")" == 0 && "$(stat -c %a "$work")" == 700 ]] || die 'run directory ownership/mode mismatch'
  [[ "$(cat "$ROOT/active")" == "$id" ]] || die 'run does not own active marker'
  [[ "$(get_state "$id" RUN_ID)" == "$id" ]] || die 'run state ownership mismatch'
}

save_baseline() {
  local work="$1" pid="$2" vpid="$3" vstart rss oom_events oom_kill
  pid="$(systemd_value MainPID)"
  vstart="$VLLM_SERVER_START_TICKS"
  rss="$(proc_rss_kib "$pid")" || die 'cannot capture Ollama process RSS'
  oom_events="$(cgroup_events_path "$pid")"
  oom_kill="$(awk '$1=="oom_kill" {print $2}' /proc/vmstat)"
  {
    printf 'RUN_ID=%s\n' "$RUN_ID"
    printf 'EXPECTED_HOST=%s\n' "$EXPECTED_HOST"
    printf 'VLLM_BASE=%s\n' "$VLLM_BASE"
    printf 'VLLM_PID=%s\n' "$vpid"
    printf 'VLLM_START_TICKS=%s\n' "$vstart"
    printf 'VLLM_ENGINE_PID=%s\n' "$VLLM_ENGINE_PID"
    printf 'VLLM_ENGINE_START_TICKS=%s\n' "$VLLM_ENGINE_START_TICKS"
    printf 'VLLM_CGROUP=%s\n' "$VLLM_CGROUP"
    printf 'VLLM_CGROUP_EVENTS=%s\n' "$VLLM_CGROUP/memory.events"
    printf 'VLLM_CGROUP_MEMORY=%s\n' "$VLLM_CGROUP/memory.current"
    printf 'OLLAMA_PID=%s\n' "$pid"
    printf 'OLLAMA_CGROUP_EVENTS=%s\n' "$oom_events"
    printf 'OLLAMA_CGROUP_MEMORY=%s\n' "$(cgroup_memory_path "$pid")"
    printf 'OLLAMA_RSS_BASELINE_KIB=%s\n' "$rss"
    printf 'OLLAMA_CGROUP_MEMORY_BASELINE=%s\n' "$(cat "$(cgroup_memory_path "$pid")")"
    printf 'VLLM_CGROUP_MEMORY_BASELINE=%s\n' "$(cat "$VLLM_CGROUP/memory.current")"
    printf 'UNIT_SHA=%s\n' "$(sha256sum "$UNIT_FILE" | awk '{print $1}')"
    printf 'OVERRIDE_SHA=%s\n' "$(sha256sum "$OVERRIDE_FILE" | awk '{print $1}')"
    printf 'FRAGMENT_PATH=%s\n' "$(systemd_value FragmentPath)"
    printf 'DROPIN_PATHS=%s\n' "$(systemd_value DropInPaths)"
    printf 'OLLAMA_HOST=0.0.0.0\n'
    printf 'CONTEXT=%s\n' "$CONTEXT"
    printf 'START_EPOCH=%s\n' "$(date +%s)"
    printf 'START_UTC=%s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    printf 'VM_OOM_KILL_BASELINE=%s\n' "$oom_kill"
    printf 'OLLAMA_CGROUP_EVENTS_BASELINE=%s\n' "$(awk '$1=="oom" || $1=="oom_kill" {printf "%s:%s ",$1,$2}' "$oom_events")"
    printf 'VLLM_CGROUP_EVENTS_BASELINE=%s\n' "$(awk '$1=="oom" || $1=="oom_kill" {printf "%s:%s ",$1,$2}' "$VLLM_CGROUP/memory.events")"
  } >"$work/state"
  chmod 0600 "$work/state"
  printf '%s\n' "$RUN_ID" >"$ROOT/active"
  chmod 0600 "$ROOT/active"
  memory_snapshot >"$work/memory.before"
  chmod 0600 "$work/memory.before"
}

confirm_run() {
  local expected="RUN P43-CAPABILITY-04B $EXPECTED_HOST"
  [[ -t 0 ]] || die 'run requires an interactive terminal for explicit confirmation'
  printf 'This will create a runtime drop-in, arm rollback, and restart Ollama once.\nType exactly: %s\n> ' "$expected" >&2
  local answer
  IFS= read -r answer
  [[ "$answer" == "$expected" ]] || die 'operator confirmation did not match'
}

arm_timer() {
  local id="$1" work="$2" delay="$3" suffix="$4" url="$5" host="$6" vpid="$7" unit
  unit="p43-cap-04b-${id//[^a-zA-Z0-9]/-}-${suffix}"
  printf '%s\n' "$unit" >>"$work/watchdog-units"
  chmod 0600 "$work/watchdog-units"
  systemd-run --quiet --unit="$unit" --on-active="$delay" --on-unit-inactive=1m \
    --timer-property=AccuracySec=1s --timer-property=RemainAfterElapse=no \
    --property=RuntimeMaxSec=300s --property=TimeoutStopSec=5s --property=KillMode=control-group \
    --setenv="P43_WATCHDOG_UNIT=$unit.service" \
    "$work/rollback-exec.sh" rollback --watchdog "$id" --watchdog-unit "$unit.service" >/dev/null
  systemctl is-active --quiet "$unit.timer" || die 'rollback watchdog timer did not become active'
  local next
  next="$(systemctl show "$unit.timer" -p NextElapseUSecMonotonic --value)"
  [[ -n "$next" && "$next" != 0 ]] || die 'rollback watchdog deadline cannot be verified'
  [[ "$(systemctl show "$unit.timer" -p OnActiveUSec --value)" == 10min ]] || die 'initial watchdog deadline is not the required 10-minute bound'
  [[ "$(systemctl show "$unit.timer" -p OnUnitInactiveUSec --value)" == 1min ]] || die 'watchdog retry interval is not one minute'
  note "Watchdog armed: $unit.timer ($delay)"
}

cancel_watchdogs_after_restore() {
  local work="$1" unit
  [[ -f "$work/watchdog-units" ]] || return 0
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    systemctl stop "$unit.timer" >/dev/null 2>&1 || true
  done <"$work/watchdog-units"
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    if systemctl is-active --quiet "$unit.timer"; then return 1; fi
  done <"$work/watchdog-units"
}

restore_checks() {
  local id="$1" work="$2" pid host ctx psjson count vstart unit_sha override_sha
  unit_sha="$(sha256sum "$UNIT_FILE" | awk '{print $1}')"
  override_sha="$(sha256sum "$OVERRIDE_FILE" | awk '{print $1}')"
  [[ "$unit_sha" == "$(get_state "$id" UNIT_SHA)" ]] || { log 'persistent unit hash mismatch'; return 1; }
  [[ "$override_sha" == "$(get_state "$id" OVERRIDE_SHA)" ]] || { log 'persistent override hash mismatch'; return 1; }
  [[ "$(systemd_value FragmentPath)" == "$(get_state "$id" FRAGMENT_PATH)" ]] || { log 'FragmentPath mismatch after restore'; return 1; }
  [[ "$(systemd_value DropInPaths)" == "$(get_state "$id" DROPIN_PATHS)" ]] || { log 'DropInPaths mismatch after restore'; return 1; }
  [[ -z "$(env_value OLLAMA_CONTEXT_LENGTH)" ]] || { log 'effective context override remains'; return 1; }
  [[ "$(env_value OLLAMA_HOST)" == 0.0.0.0 ]] || { log 'effective OLLAMA_HOST differs'; return 1; }
  [[ "$(systemd_value ActiveState)" == active ]] || { log 'Ollama service inactive'; return 1; }
  pid="$(systemd_value MainPID)"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]] || { log 'Ollama PID unavailable'; return 1; }
  [[ "$(live_env_value "$pid" OLLAMA_HOST)" == 0.0.0.0 ]] || { log 'live OLLAMA_HOST differs'; return 1; }
  [[ -z "$(live_env_value "$pid" OLLAMA_CONTEXT_LENGTH)" ]] || { log 'live context override remains'; return 1; }
  timeout 5s curl -fsS --connect-timeout 2 http://127.0.0.1:11434/api/version >/dev/null || { log 'Ollama health failed'; return 1; }
  psjson="$(curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:11434/api/ps)" || { log 'Ollama /api/ps failed'; return 1; }
  count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("models",[])))' <<<"$psjson")" || return 1
  [[ "$count" == 0 ]] || { log 'Ollama model remains resident'; return 1; }
  timeout 10s ollama ps | awk 'NR>1 && NF {n++} END {exit(n==0 ? 0 : 1)}' || { log 'ollama ps not empty or timed out'; return 1; }
  check_vllm || return 1
  vstart="$(proc_start_ticks "$(get_state "$id" VLLM_PID)")" || return 1
  [[ "$vstart" == "$(get_state "$id" VLLM_START_TICKS)" ]] || { log 'vLLM process identity changed'; return 1; }
  [[ "$(proc_start_ticks "$(get_state "$id" VLLM_ENGINE_PID)")" == "$(get_state "$id" VLLM_ENGINE_START_TICKS)" ]] || { log 'vLLM EngineCore process identity changed'; return 1; }
  [[ ! -e "$(get_state "$id" DROPIN_FILE)" ]] || { log 'temporary drop-in still exists'; return 1; }
  [[ "$(get_state "$id" DROPIN_REMOVED)" == 1 ]] || { log 'drop-in removal not recorded'; return 1; }
  [[ "$(get_state "$id" STATE)" == ROLLING_BACK || "$(get_state "$id" STATE)" == RESTORE_FAILED ]] || return 1
  return 0
}

rearm_watchdog() {
  local id="$1" work="$2" unit
  unit="$(head -n 1 "$work/watchdog-units" 2>/dev/null || true)"
  if [[ -z "$unit" ]] || ! systemctl is-active --quiet "$unit.timer"; then
    log "WATCHDOG_UNAVAILABLE for run $id; operator intervention required immediately"
    return 1
  fi
  log "rollback incomplete for run $id; recurring watchdog remains armed as $unit.timer"
}

rollback_impl() {
  local id="$1" mode="$2" work state_file lock_fd dropin expected_hash present model_count model_name unload_rc=0 restart_rc=0 unexpected_models=0
  verify_active_owner "$id"
  work="$(workdir_for "$id")"
  state_file="$(state_file_for "$id")"
  EXPECTED_HOST="$(get_state "$id" EXPECTED_HOST)"
  VLLM_BASE="$(get_state "$id" VLLM_BASE)"
  VLLM_PID="$(get_state "$id" VLLM_PID)"
  [[ "$(get_state "$id" EXPECTED_HOST)" == "$(hostnamectl --static 2>/dev/null || hostname)" ]] || die 'rollback host identity mismatch'
  check_host_identity
  exec 8>>"$ROLLBACK_LOCK"
  flock -w 5 8 || { log 'rollback lock wait expired'; rearm_watchdog "$id" "$work"; return 1; }
  exec 6>>"$work/operation.lock"
  # A request child cannot retain this lock. Keep the wait short and fail visibly.
  flock -w 5 6 || { log 'operation lock wait expired while request owner may still be active'; rearm_watchdog "$id" "$work"; return 1; }
  if [[ "$(get_state "$id" STATE)" == RESTORED ]]; then
    cancel_watchdogs_after_restore "$work" || { rearm_watchdog "$id" "$work"; return 1; }
    printf '%s\n' "$id" >"$ROOT/last"
    chmod 0600 "$ROOT/last"
    rm -f -- "$ROOT/active"
    return 0
  fi
  set_state STATE ROLLING_BACK "$state_file"
  dropin="$(get_state "$id" DROPIN_FILE)"
  expected_hash="$(get_state "$id" DROPIN_SHA)"

  # Stop only the model owned by this run. Unexpected models are not unloaded.
  local psjson
  psjson="$(curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:11434/api/ps 2>/dev/null)" || psjson=''
  if [[ -n "$psjson" ]]; then
    model_count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("models",[])))' <<<"$psjson" 2>/dev/null || echo -1)"
    if [[ "$model_count" == 1 ]]; then
      model_name="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["models"][0].get("name", ""))' <<<"$psjson")"
      if [[ "$model_name" == "$MODEL" && "$(get_state "$id" LOAD_REQUEST_ATTEMPTED)" == 1 ]]; then
        if ! timeout 30s ollama stop "$MODEL"; then unload_rc=1; log 'ollama stop failed; continuing with configuration restoration'
        fi
      elif [[ "$model_name" == "$MODEL" ]]; then
        log 'test model is resident but this run did not record issuing its load request; refusing to unload another owner model'
        unexpected_models=1
      elif [[ -n "$model_name" ]]; then
        log "unexpected resident model '$model_name'; will not stop or restart another owner's model"
        unexpected_models=1
      fi
    elif [[ "$model_count" != 0 ]]; then
      log 'unexpected Ollama resident set; will not stop or restart any resident model'
      unexpected_models=1
    fi
  else
    log 'Ollama API unavailable during model unload; will restore only the owned file and avoid an unverified service restart'
    unload_rc=1
    unexpected_models=1
  fi

  local staged
  staged="$(get_state "$id" DROPIN_TMP_FILE)"
  if [[ -e "$staged" ]]; then
    [[ "$staged" == "$work/dropin-staged-runtime.conf" ]] || die 'staged drop-in path is outside this run directory'
    present="$(sha256sum "$staged" | awk '{print $1}')"
    [[ "$present" == "$expected_hash" ]] || { log 'staged drop-in hash mismatch; refusing deletion'; set_state STATE RESTORE_FAILED "$state_file"; rearm_watchdog "$id" "$work"; return 1; }
    rm -- "$staged"
  fi
  if [[ -e "$dropin" ]]; then
    [[ "$dropin" == /run/systemd/system/ollama.service.d/zzzzzzzz-p43-capability-04b-context-*.conf ]] || die 'recorded drop-in path is outside the owned runtime naming pattern'
    present="$(sha256sum "$dropin" | awk '{print $1}')"
    [[ "$present" == "$expected_hash" ]] || { log 'owned drop-in hash mismatch; refusing deletion'; set_state STATE RESTORE_FAILED "$state_file"; rearm_watchdog "$id" "$work"; return 1; }
    if ! grep -Fq "# P43-CAPABILITY-04B RUN_ID=$id" "$dropin"; then
      log 'drop-in ownership marker mismatch; refusing deletion'
      set_state STATE RESTORE_FAILED "$state_file"
      rearm_watchdog "$id" "$work"
      return 1
    fi
    rm -- "$dropin"
    set_state DROPIN_REMOVED 1 "$state_file"
  else
    # No file exists to delete; still verify effective and live state below.
    set_state DROPIN_REMOVED 1 "$state_file"
  fi
  if ! timeout -k 2s 20s systemctl daemon-reload; then
    log 'daemon-reload failed during rollback'
    set_state STATE RESTORE_FAILED "$state_file"
    rearm_watchdog "$id" "$work"
    return 1
  fi

  if (( unexpected_models != 0 )); then
    set_state STATE RESTORE_FAILED "$state_file"
    log 'owned drop-in removed, but another/unknown resident state prevents a safe restart; operator intervention required'
    rearm_watchdog "$id" "$work"
    return 1
  fi

  # The restoration restart is attempted once only. Retries remain observational.
  if [[ "$(get_state "$id" TEST_RESTART_ATTEMPTED)" == 1 && "$(get_state "$id" RESTORE_RESTART_ATTEMPTED)" != 1 ]]; then
    set_state RESTORE_RESTART_ATTEMPTED 1 "$state_file"
    if ! timeout 60s systemctl restart ollama; then
      restart_rc=1
      log 'restoration restart failed; no automatic restart retry will occur'
    fi
  fi
  if (( restart_rc != 0 )) || ! restore_checks "$id" "$work"; then
    set_state STATE RESTORE_FAILED "$state_file"
    log 'RESTORE_FAILED; preserve run state and require GX10 operator intervention'
    rearm_watchdog "$id" "$work"
    return 1
  fi
  if (( unload_rc != 0 )); then
    # The restart may have cleared the model; final-state checks decide restoration.
    set_state UNLOAD_COMMAND_FAILED 1 "$state_file"
  fi
  set_state STATE RESTORED "$state_file"
  if ! cancel_watchdogs_after_restore "$work"; then
    set_state STATE RESTORE_FAILED "$state_file"
    log 'watchdog cancellation verification failed'
    rearm_watchdog "$id" "$work"
    return 1
  fi
  printf '%s\n' "$id" >"$ROOT/last"
  chmod 0600 "$ROOT/last"
  rm -f -- "$ROOT/active"
  log "run $id restored and verified"
  note 'Restoration verified: persistent hashes unchanged, temporary drop-in absent, Ollama healthy with no resident model, vLLM healthy/idle, watchdog timers inactive.'
  return 0
}

# Every rollback state transition happens under operation.lock. Holding it here
# means no attempt is in flight, so ROLLING_BACK can only be an interrupted
# attempt. Any other state (RESTORED, RESTORE_FAILED, pre-rollback) is kept.
record_incomplete_rollback() {
  local id="$1" mode="$2" rc="$3" lock_file current
  lock_file="$(workdir_for "$id")/operation.lock"
  if [[ ! -f "$lock_file" ]] || ! { exec 5>>"$lock_file"; } 2>/dev/null; then
    log "bounded $mode rollback rc=$rc; operation lock unavailable, state not changed"
    return 0
  fi
  if ! flock -w 5 5; then
    exec 5>&-
    log "bounded $mode rollback rc=$rc; another rollback holds the operation lock and owns the state outcome"
    return 0
  fi
  if transition_state ROLLING_BACK RESTORE_FAILED "$(state_file_for "$id")"; then
    log "RESTORE_FAILED: bounded $mode rollback rc=$rc left an interrupted attempt; watchdog remains armed"
  else
    current="$(get_state "$id" STATE 2>/dev/null || true)"
    log "bounded $mode rollback rc=$rc; state '$current' kept without overwrite"
  fi
  flock -u 5
  exec 5>&-
}

bounded_rollback() {
  local id="$1" mode="$2" rc=0
  P43_INTERNAL_ROLLBACK=1 timeout --signal=TERM --kill-after=5s 295s \
    "$SCRIPT_PATH" _rollback-child "$id" "$mode" || rc=$?
  if (( rc == 0 )); then return 0; fi
  if (( rc == 124 || rc == 137 )); then log "bounded $mode rollback attempt timed out (rc=$rc)"; fi
  record_incomplete_rollback "$id" "$mode" "$rc"
  return 1
}

check_live_guards() {
  local id="$1" work="$2" avail swap_total swap_free psin psout oomkill psi_total psi_avg ollama_pid vpid rss base_rss cgroup_now cgroup_base start mem_now mem_base
  [[ "$(get_state "$id" STATE)" == LOAD_IN_PROGRESS || "$(get_state "$id" STATE)" == OBSERVING ]] || return 1
  read -r avail swap_total swap_free < <(awk '/^MemAvailable:/ {a=$2} /^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print a,t,f}' /proc/meminfo)
  (( avail >= 24 * GIB_KIB )) || { log 'MemAvailable below 24 GiB'; return 1; }
  local baseline_avail
  baseline_avail="$(awk '$1=="MemAvailable:" {print $2}' "$work/memory.before")"
  [[ "$baseline_avail" =~ ^[0-9]+$ ]] || return 1
  (( baseline_avail - avail <= 20 * GIB_KIB )) || { log 'MemAvailable decline exceeded 20 GiB'; return 1; }
  (( swap_total == swap_free )) || { log 'swap is now in use'; return 1; }
  psin="$(awk '$1=="pswpin" {print $2}' /proc/vmstat)"
  psout="$(awk '$1=="pswpout" {print $2}' /proc/vmstat)"
  (( psin == $(get_state "$id" PSWPIN_BASELINE) && psout == $(get_state "$id" PSWPOUT_BASELINE) )) || { log 'swap-in/out activity observed'; return 1; }
  oomkill="$(awk '$1=="oom_kill" {print $2}' /proc/vmstat)"
  (( oomkill == $(get_state "$id" VM_OOM_KILL_BASELINE) )) || { log 'new host OOM kill observed'; return 1; }
  local kernel_events
  kernel_events="$(journalctl -k --since "@$(get_state "$id" START_EPOCH)" -o cat --no-pager 2>/dev/null)" || { log 'kernel journal telemetry unavailable during request'; return 1; }
  if grep -Eiq 'out of memory|oom-kill|killed process|xid [0-9]' <<<"$kernel_events"; then
    log 'new kernel OOM or GPU Xid event observed'
    return 1
  fi
  psi_total="$(awk '/^full / {for(i=1;i<=NF;i++) if($i ~ /^total=/){sub("total=", "", $i); print $i}}' /proc/pressure/memory)"
  psi_avg="$(awk '/^full / {for(i=1;i<=NF;i++) if($i ~ /^avg10=/){sub("avg10=", "", $i); print $i}}' /proc/pressure/memory)"
  [[ "$psi_total" =~ ^[0-9]+$ && "$psi_avg" =~ ^[0-9]+([.][0-9]+)?$ ]] || { log 'memory PSI telemetry unavailable or malformed'; return 1; }
  (( psi_total <= $(get_state "$id" PSI_FULL_TOTAL_BASELINE) )) || { log 'memory PSI full stall observed'; return 1; }
  awk -v x="$psi_avg" 'BEGIN{exit(x+0 > 0 ? 0 : 1)}' && { log 'memory PSI full avg10 is nonzero'; return 1; }
  for cgroup_now in "$(get_state "$id" OLLAMA_CGROUP_EVENTS)" "$(get_state "$id" VLLM_CGROUP_EVENTS)"; do
    [[ -r "$cgroup_now" ]] || { log 'cgroup OOM telemetry disappeared'; return 1; }
    if awk '$1=="oom" || $1=="oom_kill" {if($2>0) found=1} END{exit(found?0:1)}' "$cgroup_now"; then
      # Compare current event counters with the run-start snapshot below.
      if [[ "$cgroup_now" == "$(get_state "$id" OLLAMA_CGROUP_EVENTS)" ]]; then cgroup_base="$(get_state "$id" OLLAMA_CGROUP_EVENTS_BASELINE)"; else cgroup_base="$(get_state "$id" VLLM_CGROUP_EVENTS_BASELINE)"; fi
      local current_events
      current_events="$(awk '$1=="oom" || $1=="oom_kill" {printf "%s:%s ",$1,$2}' "$cgroup_now")"
      [[ "$current_events" == "$cgroup_base" ]] || { log 'new cgroup OOM event observed'; return 1; }
    fi
  done
  vpid="$(get_state "$id" VLLM_PID)"
  [[ -r "/proc/$vpid/status" ]] || { log 'vLLM process disappeared'; return 1; }
  start="$(proc_start_ticks "$vpid")" || return 1
  [[ "$start" == "$(get_state "$id" VLLM_START_TICKS)" ]] || { log 'vLLM process identity changed'; return 1; }
  [[ "$(proc_start_ticks "$(get_state "$id" VLLM_ENGINE_PID)")" == "$(get_state "$id" VLLM_ENGINE_START_TICKS)" ]] || { log 'vLLM EngineCore process identity changed'; return 1; }
  local vllm_mem_path
  vllm_mem_path="$(get_state "$id" VLLM_CGROUP_MEMORY)"
  mem_now="$(cat "$vllm_mem_path")" || { log 'vLLM cgroup allocation unavailable'; return 1; }
  mem_base="$(get_state "$id" VLLM_CGROUP_MEMORY_BASELINE)"
  (( mem_now - mem_base <= 2 * 1073741824 )) || { log 'vLLM cgroup memory growth exceeded 2 GiB best-effort alarm'; return 1; }
  ollama_pid="$(systemd_value MainPID)"
  [[ "$ollama_pid" == "$(get_state "$id" TEST_OLLAMA_PID)" ]] || { log 'Ollama process identity changed during residency'; return 1; }
  [[ "$ollama_pid" =~ ^[1-9][0-9]*$ && "$(systemd_value ActiveState)" == active ]] || { log 'Ollama service unhealthy'; return 1; }
  [[ "$(live_env_value "$ollama_pid" OLLAMA_CONTEXT_LENGTH)" == "$CONTEXT" ]] || { log 'live Ollama context differs from 32768'; return 1; }
  [[ "$(live_env_value "$ollama_pid" OLLAMA_HOST)" == 0.0.0.0 ]] || { log 'live Ollama host binding changed'; return 1; }
  rss="$(proc_rss_kib "$ollama_pid")" || { log 'Ollama RSS unavailable'; return 1; }
  base_rss="$(get_state "$id" OLLAMA_RSS_BASELINE_KIB)"
  (( rss - base_rss <= 20 * GIB_KIB )) || { log 'Ollama process RSS growth exceeded best-effort 20 GiB alarm'; return 1; }
  local ollama_mem_path
  ollama_mem_path="$(get_state "$id" OLLAMA_CGROUP_MEMORY)"
  mem_now="$(cat "$ollama_mem_path")" || { log 'Ollama cgroup allocation unavailable'; return 1; }
  mem_base="$(get_state "$id" OLLAMA_CGROUP_MEMORY_BASELINE)"
  (( mem_now - mem_base <= 20 * 1073741824 )) || { log 'Ollama cgroup memory growth exceeded 20 GiB best-effort alarm'; return 1; }
  check_vllm || return 1
  timeout 5s curl -fsS --connect-timeout 2 http://127.0.0.1:11434/api/version >/dev/null || { log 'Ollama health check failed'; return 1; }
  return 0
}

verify_residency() {
  local psjson count name ctx cli_rows
  psjson="$(curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:11434/api/ps)" || return 1
  read -r count name ctx < <(python3 -c 'import json,sys; m=json.load(sys.stdin).get("models",[]); print(len(m), (m[0].get("name", "") if len(m)==1 else "-"), (m[0].get("context_length", "-") if len(m)==1 else "-"))' <<<"$psjson") || return 1
  [[ "$count" == 1 && "$name" == "$MODEL" && "$ctx" == "$CONTEXT" ]] || return 1
  cli_rows="$(ollama ps | awk -v n="$MODEL" 'NR>1 && $1==n {c++} END{print c+0}')"
  [[ "$cli_rows" == 1 ]] || return 1
  note "Residency observed: $name context_length=$ctx"
}

run_operation() {
  local work state_file dropin content_hash start_now remaining req_timeout obs_deadline
  baseline_checks
  check_trusted_script
  [[ -d /run/lock && -w /run/lock ]] || die '/run/lock is not writable'
  [[ ! -e "$ROOT/active" ]] || die 'an active or unresolved run marker exists; inspect with status/rollback'
  confirm_run
  install -d -o root -g root -m 0700 "$ROOT"
  exec 9>>"$OWNER_LOCK"
  flock -n 9 || die 'another 04B run owns the host-wide lock'
  [[ ! -e "$ROOT/active" ]] || die 'run marker appeared after confirmation; refusing concurrent run'
  RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  work="$(workdir_for "$RUN_ID")"
  install -d -o root -g root -m 0700 "$work"
  state_file="$(state_file_for "$RUN_ID")"
  : >"$state_file"
  chmod 0600 "$state_file"
  : >"$work/operation.lock"
  chmod 0600 "$work/operation.lock"
  save_baseline "$work" "$VLLM_PID" "$VLLM_PID"
  set_state STATE PREPARED "$state_file"
  set_state PSWPIN_BASELINE "$(awk '$1=="pswpin"{print $2}' /proc/vmstat)" "$state_file"
  set_state PSWPOUT_BASELINE "$(awk '$1=="pswpout"{print $2}' /proc/vmstat)" "$state_file"
  set_state PSI_FULL_TOTAL_BASELINE "$(awk '/^full /{for(i=1;i<=NF;i++)if($i~/^total=/){sub("total=", "", $i); print $i}}' /proc/pressure/memory)" "$state_file"
  install -o root -g root -m 0700 "$SCRIPT_PATH" "$work/rollback-exec.sh"
  set_state ROLLBACK_SCRIPT_SHA "$(sha256sum "$work/rollback-exec.sh" | awk '{print $1}')" "$state_file"
  : >"$work/watchdog-units"
  chmod 0600 "$work/watchdog-units"
  set_state DROPIN_FILE "/run/systemd/system/ollama.service.d/zzzzzzzz-p43-capability-04b-context-$RUN_ID.conf" "$state_file"
  dropin="$(get_state "$RUN_ID" DROPIN_FILE)"
  set_state DROPIN_TMP_FILE "$work/dropin-staged-runtime.conf" "$state_file"
  printf '# P43-CAPABILITY-04B RUN_ID=%s\n[Service]\nEnvironment=OLLAMA_CONTEXT_LENGTH=%s\n' "$RUN_ID" "$CONTEXT" >"$work/dropin-staged.conf"
  chmod 0600 "$work/dropin-staged.conf"
  content_hash="$(sha256sum "$work/dropin-staged.conf" | awk '{print $1}')"
  set_state DROPIN_SHA "$content_hash" "$state_file"
  set_state DROPIN_REMOVED 1 "$state_file"
  set_state STATE PREPARED "$state_file"
  set_state ROLLBACK_READY 1 "$state_file"
  if [[ ! -d /run/systemd/system/ollama.service.d ]]; then
    install -d -o root -g root -m 0755 /run/systemd/system/ollama.service.d
  fi
  [[ ! -L /run/systemd/system/ollama.service.d && "$(stat -c %u /run/systemd/system/ollama.service.d)" == 0 ]] || die 'runtime drop-in directory ownership/type is unexpected'
  # The timer starts before the drop-in is installed, so its 10-minute delay
  # is conservative relative to the 15-minute configuration lifetime.
  set_state WATCHDOG_DEADLINE_EPOCH "$(( $(date +%s) + WATCHDOG_DELAY_SECONDS ))" "$state_file"
  arm_timer "$RUN_ID" "$work" "${WATCHDOG_DELAY_SECONDS}s" watchdog "$VLLM_BASE" "$EXPECTED_HOST" "$VLLM_PID"
  [[ ! -e "$dropin" ]] || die 'runtime drop-in filename collision'
  set_state DROPIN_REMOVED 0 "$state_file"
  local staged="$work/dropin-staged-runtime.conf"
  install -o root -g root -m 0644 "$work/dropin-staged.conf" "$staged"
  [[ "$(sha256sum "$staged" | awk '{print $1}')" == "$content_hash" ]] || die 'staged runtime drop-in hash mismatch'
  mv -- "$staged" "$dropin"
  set_state CONFIG_INSTALLED_EPOCH "$(date +%s)" "$state_file"
  set_state STATE CONFIGURED "$state_file"
  if ! timeout -k 2s 20s systemctl daemon-reload; then die 'daemon-reload failed; watchdog will restore'; fi
  local paths env_files unset_env
  paths="$(systemd_value DropInPaths)"
  env_files="$(systemd_value EnvironmentFiles)"
  unset_env="$(systemd_value UnsetEnvironment)"
  [[ "$paths" == *"$OVERRIDE_FILE"* && "$paths" == *"$dropin"* ]] || die 'effective DropInPaths does not contain both persistent override and run drop-in'
  [[ "$(env_value OLLAMA_CONTEXT_LENGTH)" == "$CONTEXT" ]] || die 'effective systemd context is not 32768'
  [[ -z "$env_files" || "$env_files" == - ]] || die 'EnvironmentFiles appeared; refusing ambiguous precedence'
  [[ "$unset_env" != *OLLAMA_CONTEXT_LENGTH* ]] || die 'UnsetEnvironment removes the requested context'
  set_state DROPIN_EFFECTIVE 1 "$state_file"
  set_state RESTORE_RESTART_ATTEMPTED 0 "$state_file"
  set_state STATE RESTARTING_FOR_TEST "$state_file"
  set_state TEST_RESTART_ATTEMPTED 1 "$state_file"
  if ! timeout 60s systemctl restart ollama; then die 'Ollama test restart failed; rollback will run'; fi
  systemctl is-active --quiet ollama || die 'Ollama is not active after test restart'
  local pid
  pid="$(systemd_value MainPID)"
  [[ "$(live_env_value "$pid" OLLAMA_CONTEXT_LENGTH)" == "$CONTEXT" ]] || die 'live Ollama process context is not 32768'
  [[ "$(live_env_value "$pid" OLLAMA_HOST)" == 0.0.0.0 ]] || die 'live Ollama process host binding changed'
  timeout 5s curl -fsS --connect-timeout 2 http://127.0.0.1:11434/api/version >/dev/null || die 'Ollama unhealthy after test restart'
  check_vllm || die 'vLLM health or idle verification failed after restart'
  set_state TEST_OLLAMA_PID "$pid" "$state_file"
  set_state STATE CONTEXT_VERIFIED "$state_file"

  start_now="$(date +%s)"
  remaining=$(( $(get_state "$RUN_ID" WATCHDOG_DEADLINE_EPOCH) - start_now ))
  (( remaining >= 480 )) || die 'less than eight minutes remain before the recovery deadline; aborting before model load'
  req_timeout=300
  (( req_timeout <= remaining - OBSERVE_SECONDS )) || req_timeout=$((remaining - OBSERVE_SECONDS))
  (( req_timeout > 2 )) || die 'insufficient time for the single bounded model request'
  [[ "$(get_state "$RUN_ID" STATE)" == CONTEXT_VERIFIED ]] || die 'run was stopped before model load'
  exec 6>>"$work/operation.lock"
  flock -x 6
  if ! transition_state CONTEXT_VERIFIED LOAD_IN_PROGRESS "$state_file"; then
    flock -u 6
    die 'rollback claimed this run before model load'
  fi
  # Exactly one request; no retry path exists.
  set_state LOAD_REQUEST_ATTEMPTED 1 "$state_file"
  # Do not pass owner or operation locks into the request process. timeout
  # bounds the client even if this shell is killed; curl's own max-time remains
  # the protocol-level request bound.
  timeout -k 2s "$((req_timeout - 2))s" 6>&- 9>&- curl -fsS --connect-timeout 3 --max-time "$req_timeout" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen3-coder:30b","prompt":"","stream":false,"keep_alive":"5m","options":{"num_predict":1}}' \
    http://127.0.0.1:11434/api/generate >"$work/load-response.json" &
  local request_pid=$! request_rc
  set_state LOAD_REQUEST_PID "$request_pid" "$state_file"
  request_rc=0
  while kill -0 "$request_pid" 2>/dev/null; do
    if (( STOP_REQUESTED )); then
      log 'stop signal received during model-load request; cancelling before rollback'
      kill -TERM "$request_pid" 2>/dev/null || true
      break
    fi
    sleep 5 || true
    check_live_guards "$RUN_ID" "$work" || {
      log 'resource or health stop signal during model-load request; cancelling before rollback'
      kill -TERM "$request_pid" 2>/dev/null || true
      break
    }
  done
  if wait "$request_pid"; then request_rc=0; else request_rc=$?; fi
  set_state LOAD_REQUEST_PID 0 "$state_file"
  if (( request_rc != 0 || STOP_REQUESTED )); then
    die 'single model-load request failed or timed out; rollback required'
  fi
  chmod 0600 "$work/load-response.json"
  if ! transition_state LOAD_IN_PROGRESS OBSERVING "$state_file"; then
    flock -u 6
    die 'rollback claimed the run while the request was in flight'
  fi
  flock -u 6
  exec 6>&-
  verify_residency || die 'exact residency/context verification failed'
  obs_deadline=$(( $(date +%s) + OBSERVE_SECONDS ))
  while (( $(date +%s) < obs_deadline )); do
    (( STOP_REQUESTED == 0 )) || die 'stop signal received during residency observation'
    sleep 5 || true
    check_live_guards "$RUN_ID" "$work" || die 'a best-effort resource or health stop signal fired'
    verify_residency || die 'residency/context changed during observation'
    remaining=$(( $(get_state "$RUN_ID" WATCHDOG_DEADLINE_EPOCH) - $(date +%s) ))
    (( remaining > 15 )) || die 'watchdog recovery deadline is near; aborting residency observation'
  done
  exec 6>>"$work/operation.lock"
  flock -x 6
  if ! transition_state OBSERVING OBSERVED "$state_file"; then
    flock -u 6
    die 'rollback claimed the run before observation completion'
  fi
  flock -u 6
  exec 6>&-
  note 'Residency observed within best-effort guards; beginning immediate restoration.'
  bounded_rollback "$RUN_ID" manual
}

status_operation() {
  require_root
  require_commands
  if ! read_status_run; then note 'No P43-CAPABILITY-04B run record.'; return 0; fi
  local terminal_state
  terminal_state="$(get_state "$RUN_ID" STATE)"
  if [[ "$terminal_state" == RESTORED || "$terminal_state" == ABORTED_BEFORE_MUTATION ]]; then
    note "Last run $RUN_ID: $terminal_state"
    return 0
  fi
  verify_active_owner "$RUN_ID"
  local sf work state dropin unit
  sf="$(state_file_for "$RUN_ID")"; work="$(workdir_for "$RUN_ID")"
  state="$(get_state "$RUN_ID" STATE)"; dropin="$(get_state "$RUN_ID" DROPIN_FILE)"
  printf 'Run: %s\nState: %s\nExpected host: %s\nTemporary drop-in present: ' "$RUN_ID" "$state" "$(get_state "$RUN_ID" EXPECTED_HOST)"
  [[ -e "$dropin" ]] && echo yes || echo no
  printf 'Ollama active: %s\n' "$(systemd_value ActiveState 2>/dev/null || echo unknown)"
  printf 'Ollama MemAvailable: '
  awk '/^MemAvailable:/ {printf "%.1f GiB\n", $2/1048576}' /proc/meminfo
  printf 'vLLM URL: %s\n' "$(get_state "$RUN_ID" VLLM_BASE)"
  printf 'Watchdog timers:\n'
  if [[ -f "$work/watchdog-units" ]]; then
    while IFS= read -r unit; do [[ -n "$unit" ]] && printf '  %s: %s\n' "$unit.timer" "$(systemctl is-active "$unit.timer" 2>/dev/null || echo inactive)"; done <"$work/watchdog-units"
  fi
  note 'status is read-only; use rollback for operator-requested restoration.'
}

manual_rollback_operation() {
  require_root
  require_commands
  (( INTERNAL_WATCHDOG == 0 )) || return 0
  if ! read_active_run; then
    if read_status_run; then
      local prior_state
      prior_state="$(get_state "$RUN_ID" STATE)"
      if [[ "$prior_state" == RESTORED || "$prior_state" == ABORTED_BEFORE_MUTATION ]]; then
        note "Run $RUN_ID is already $prior_state; rollback is idempotent."
        return 0
      fi
    fi
    die 'no active run marker to roll back'
  fi
  verify_active_owner "$RUN_ID"
  check_trusted_script
  [[ "$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')" == "$(get_state "$RUN_ID" ROLLBACK_SCRIPT_SHA)" ]] || die 'installed script differs from the run-owned rollback copy'
  [[ -t 0 ]] || die 'manual rollback requires interactive confirmation'
  local phrase answer
  phrase="RESTORE P43-CAPABILITY-04B $RUN_ID"
  printf 'Type exactly to restore this run: %s\n> ' "$phrase" >&2
  IFS= read -r answer
  [[ "$answer" == "$phrase" ]] || die 'rollback confirmation did not match'
  rollback_impl "$RUN_ID" manual
}

watchdog_rollback_operation() {
  local id="$1" work main_pid unit_cgroup process_cgroup command_line
  require_root
  require_commands
  verify_active_owner "$id"
  work="$(workdir_for "$id")"
  [[ "$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')" == "$(get_state "$id" ROLLBACK_SCRIPT_SHA)" ]] || die 'watchdog rollback script hash mismatch'
  [[ -n "$WATCHDOG_UNIT" && "${P43_WATCHDOG_UNIT:-}" == "$WATCHDOG_UNIT" ]] || die 'watchdog invocation identity is missing'
  [[ "$WATCHDOG_UNIT" == "p43-cap-04b-${id//[^a-zA-Z0-9]/-}-watchdog.service" ]] || die 'watchdog unit does not belong to this run'
  [[ "$(systemctl show "$WATCHDOG_UNIT" -p ActiveState --value)" == active ]] || die 'watchdog service is not active'
  main_pid="$(systemctl show "$WATCHDOG_UNIT" -p MainPID --value)"
  [[ "$main_pid" =~ ^[1-9][0-9]*$ && -r "/proc/$main_pid/cgroup" ]] || die 'watchdog service MainPID is unavailable'
  unit_cgroup="$(systemctl show "$WATCHDOG_UNIT" -p ControlGroup --value)"
  [[ -n "$unit_cgroup" ]] || die 'watchdog service ControlGroup is unavailable'
  process_cgroup="$(proc_cgroup_path "$$")" || die 'watchdog shell cgroup is unavailable'
  [[ "$process_cgroup" == "$unit_cgroup" || "$process_cgroup" == "$unit_cgroup"/* ]] || die 'watchdog shell is outside its transient service cgroup'
  process_cgroup="$(proc_cgroup_path "$main_pid")" || die 'transient service MainPID cgroup is unavailable'
  [[ "$process_cgroup" == "$unit_cgroup" || "$process_cgroup" == "$unit_cgroup"/* ]] || die 'transient service MainPID is outside its service cgroup'
  command_line="$(tr '\0' ' ' <"/proc/$$/cmdline")"
  [[ "$command_line" == *rollback*--watchdog*"$id"*"$WATCHDOG_UNIT"* ]] || die 'watchdog command line does not carry this run and unit identity'
  bounded_rollback "$id" watchdog
}

run_exit_trap() {
  local rc=$?
  trap - EXIT INT TERM
  # EXIT traps run before shell descriptors close. Release the observation lock
  # first so this same process can enter the serialized rollback procedure.
  if { true >&6; } 2>/dev/null; then
    flock -u 6 2>/dev/null || true
    exec 6>&-
  fi
  if [[ -n "$RUN_ID" && -f "$(state_file_for "$RUN_ID")" && -f "$ROOT/active" && "$(cat "$ROOT/active")" == "$RUN_ID" ]]; then
    if [[ "$(get_state "$RUN_ID" ROLLBACK_READY)" == 1 && "$(get_state "$RUN_ID" STATE)" != RESTORED ]]; then
      log "run $RUN_ID exited before verified restoration; invoking fail-safe rollback"
      bounded_rollback "$RUN_ID" watchdog || true
    else
      set_state STATE ABORTED_BEFORE_MUTATION "$(state_file_for "$RUN_ID")" || true
      printf '%s\n' "$RUN_ID" >"$ROOT/last"
      rm -f -- "$ROOT/active"
      log "run $RUN_ID aborted before runtime configuration mutation"
    fi
  fi
  exit "$rc"
}

main() {
  parse_args "$@"
  case "$OP" in
    check) baseline_checks ;;
    run)
      trap run_exit_trap EXIT
      trap 'STOP_REQUESTED=1' INT TERM
      run_operation
      trap - EXIT INT TERM
      ;;
    status) status_operation ;;
    rollback)
      if (( INTERNAL_WATCHDOG )); then watchdog_rollback_operation "$RUN_ID"; else manual_rollback_operation; fi
      ;;
    _rollback-child)
      [[ "${P43_INTERNAL_ROLLBACK:-}" == 1 && $# == 3 ]] || die 'internal rollback entry point is not callable directly'
      require_root
      require_commands
      rollback_impl "$2" "$3"
      ;;
  esac
}

main "$@"
