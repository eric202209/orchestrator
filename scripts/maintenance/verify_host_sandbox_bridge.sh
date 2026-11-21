#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

CLASSIFICATION=HOST_ENVIRONMENT_UNAVAILABLE
REPO=
RUN_DIR=
WORK=
WITNESS_PID=
P21_CONTAINER=
PROBE_STATUS=NOT_RUN
INSPECT_STATUS=NOT_RUN
CLEANUP_STATUS=NOT_RUN
INTEGRATION_STATUS=NOT_RUN

finish() {
  local rc=$?
  set +e
  local cleanup_rc=0
  if [[ -n "$P21_CONTAINER" ]]; then docker rm -f "$P21_CONTAINER" >/dev/null 2>&1 || cleanup_rc=1; fi
  if [[ -n "$WITNESS_PID" ]]; then kill "$WITNESS_PID" 2>/dev/null; wait "$WITNESS_PID" 2>/dev/null; fi
  if [[ -n "$WORK" && -d "$WORK" ]]; then rm -rf -- "$WORK" || cleanup_rc=1; fi
  if [[ "$CLEANUP_STATUS" == NOT_RUN ]]; then
    CLEANUP_STATUS=$([[ $cleanup_rc -eq 0 ]] && echo PASS || echo FAIL)
  elif [[ $cleanup_rc -ne 0 ]]; then
    CLEANUP_STATUS=FAIL
  fi
if [[ -n "$RUN_DIR" && -d "$RUN_DIR" ]]; then
    printf 'PRODUCT21_PROBE_STATUS=%s\nPRODUCT21_INSPECT_STATUS=%s\nCLEANUP_STATUS=%s\nINTEGRATION_STATUS=%s\nCLASSIFICATION=%s\n' \
      "$PROBE_STATUS" "$INSPECT_STATUS" "$CLEANUP_STATUS" "$INTEGRATION_STATUS" "$CLASSIFICATION" > "$RUN_DIR/status.txt"
  for evidence_file in "$RUN_DIR"/*; do [[ -f "$evidence_file" ]] && chmod a+rw "$evidence_file"; done
  fi
  if [[ "$CLEANUP_STATUS" == FAIL ]]; then CLASSIFICATION=CLEANUP_FAILED; rc=1; fi
  printf 'P43_PRODUCT_22_HOST=%s PRODUCT21=%s INSPECT=%s CLEANUP=%s INTEGRATION=%s\n' \
    "$CLASSIFICATION" "$PROBE_STATUS" "$INSPECT_STATUS" "$CLEANUP_STATUS" "$INTEGRATION_STATUS"
  exit "$rc"
}
trap finish EXIT INT TERM

if [[ "$(uname -m 2>/dev/null || true)" != aarch64 || "$(ps -p 1 -o comm= 2>/dev/null | xargs || true)" != systemd || \
      "$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)" != "ASUSTeK COMPUTER INC." || \
      "$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)" != GX10 || \
      ! -S /run/docker.sock || "$(docker context show 2>/dev/null || true)" != default ]]; then
  echo 'Host precondition failed: expected ASUS GX10 ARM64 systemd Host with local Docker socket.' >&2
  exit 20
fi
[[ "$(git rev-parse --show-toplevel)" ]] || exit 20
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
OLD_PROBE=docs/roadmap/reports/evidence/p43-product-20r/probe.py
PROBE=docs/roadmap/reports/evidence/p43-product-21-final/probe-v3.py
IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
printf '%s  %s\n' c6fae51b1e8d6b927faadc4eac36c9134823f586d07e139e6a6555e6ba0aee46 "$OLD_PROBE" | sha256sum -c -
PROBE_V2=docs/roadmap/reports/evidence/p43-product-21-final/probe-v2.py
printf '%s  %s\n' 3599f2670a5e355013ed10869d11f571af3955d3cd051b9e56fa2cf0f1db52e2 "$PROBE_V2" | sha256sum -c -
NEW_PROBE_SHA=$(sha256sum "$PROBE" | cut -d' ' -f1)
[[ "$NEW_PROBE_SHA" == 0d03208f2fb022637f87cb2ba73601f0531b8c43fe3c46a9aba66029a149f41c ]] || { CLASSIFICATION=PROBE_HASH_MISMATCH; exit 22; }
printf 'PRODUCT21_PROBE_V3_SHA256=%s\n' "$NEW_PROBE_SHA"
IMAGE_INFO=$(docker image inspect "$IMAGE" --format '{{.Id}} {{.Os}}/{{.Architecture}}' 2>/dev/null) || {
  CLASSIFICATION=DOCKER_IMAGE_UNAVAILABLE; echo 'Pinned local Docker image is unavailable; no pull attempted.' >&2; exit 21;
}
[[ "$IMAGE_INFO" == 'sha256:3617028275c0b0ffabe98f1d7eaa6b85624af2f457678f185c307ff3c1d31ae6 linux/arm64' ]] || {
  CLASSIFICATION=DOCKER_IMAGE_UNAVAILABLE; echo "Unexpected pinned image identity: $IMAGE_INFO" >&2; exit 21;
}
PINNED_IMAGE_ID=${IMAGE_INFO%% *}

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
EVIDENCE_ROOT="$REPO/docs/roadmap/reports/evidence/p43-product-22-host-integration"
mkdir -p "$EVIDENCE_ROOT"; chmod a+rwx "$EVIDENCE_ROOT"
RUN_DIR="$EVIDENCE_ROOT/$RUN_ID"; mkdir "$RUN_DIR"; chmod a+rwx "$RUN_DIR"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/p43-p22-host.XXXXXX")
chmod 0700 "$WORK"

if docker top ai-agent -eo pid,args > "$RUN_DIR/ai-agent-top.txt" 2> "$RUN_DIR/ai-agent-top-error.txt"; then
  docker inspect ai-agent > "$RUN_DIR/ai-agent-inspect.json" || { CLASSIFICATION=HOST_ENVIRONMENT_UNAVAILABLE; exit 22; }
else
  CLASSIFICATION=HOST_ENVIRONMENT_UNAVAILABLE; echo 'Existing ai-agent container is unavailable for Host-boundary inspection.' >&2; exit 22
fi

# PRODUCT-21 corrected Host procedure. The witness is verified by Host PID and
# ephemeral marker; only its PID is passed to the container probe.
chmod 0777 "$WORK"
printf 'print(1)\n' > "$WORK/task.py"
chmod 0644 "$WORK/task.py"
WITNESS_MARKER="P43_P21_WITNESS_$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
P43_P21_WITNESS="$WITNESS_MARKER" python3 -c 'import time; time.sleep(600)' &
WITNESS_PID=$!
python3 - "$WITNESS_PID" "$WITNESS_MARKER" > "$RUN_DIR/witness-status.txt" <<'PY'
import pathlib, sys
pid, marker = sys.argv[1:]
assert int(pid) > 1 and pathlib.Path(f"/proc/{pid}").is_dir()
assert (b"P43_P21_WITNESS=" + marker.encode()) in pathlib.Path(f"/proc/{pid}/environ").read_bytes()
print("HOST_WITNESS_VERIFIED pid=" + pid)
PY
kill -0 "$WITNESS_PID"
P21_CONTAINER="p43-p21-final-$RUN_ID"
if docker inspect "$P21_CONTAINER" >/dev/null 2>&1; then CLASSIFICATION=HOST_ENVIRONMENT_UNAVAILABLE; echo 'Probe container name collision.' >&2; exit 22; fi
set +e
docker run -i --pull=never --platform linux/arm64 --name "$P21_CONTAINER" \
  --network=none --ipc=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --user=65534:65534 \
  --cpus=1 --memory=512m --memory-swap=512m --pids-limit=64 \
  --ulimit nofile=256:256 --ulimit fsize=16777216:16777216 \
  --mount "type=bind,src=$WORK,dst=/ws" --workdir=/ws \
  --entrypoint /usr/bin/env "$IMAGE" -i \
  HOME=/ws TMPDIR=/ws PATH=/usr/local/bin:/usr/bin:/bin LC_CTYPE=C.UTF-8 \
  python3 -I - /ws "$WITNESS_PID" < "$PROBE" | tee "$RUN_DIR/product21-results.jsonl"
PROBE_STATUS=${PIPESTATUS[0]}
set -e
printf '%s\n' "$PROBE_STATUS" > "$RUN_DIR/product21-exit-status.txt"
kill -0 "$WITNESS_PID" || { CLASSIFICATION=PRODUCT_21_ISOLATION_FAILED; exit 23; }
if docker inspect "$P21_CONTAINER" --format '{{json .}}' > "$RUN_DIR/product21-container-inspect.json"; then
  INSPECT_STATUS=0
else
  INSPECT_STATUS=1
fi
printf '%s\n' "$INSPECT_STATUS" > "$RUN_DIR/product21-inspect-exit-status.txt"
if [[ "$PROBE_STATUS" -ne 0 || "$INSPECT_STATUS" -ne 0 ]] || ! python3 - "$RUN_DIR/product21-results.jsonl" "$RUN_DIR/product21-container-inspect.json" "$WORK" "$P21_CONTAINER" "$RUN_DIR/product21-exit-status.txt" <<'PY'
import json, pathlib, sys
rows=[json.loads(x) for x in pathlib.Path(sys.argv[1]).read_text().splitlines()]
checks={x["check"]:x["status"] for x in rows if "check" in x}
required=("ws_read","ws_write","env_secret_keys","env_keys_allowlist","env_values",
          "pid1_sandbox_identity","pid1_environment","host_pid_environment_isolation",
          "uid_nonroot","cap_eff_zero","cap_bnd_zero","no_new_privs","setuid0",
          "network_namespace_no_egress","write_tmp")
if any(checks.get(k)!="PASS" for k in required): raise SystemExit("PRODUCT-21 required check missing or not PASS")
if any(v in ("FAIL","INVALID_PRECONDITION") for v in checks.values()): raise SystemExit("PRODUCT-21 FAIL/INVALID_PRECONDITION")
ipc=next((x for x in rows if x.get("check")=="write_dev_shm"),None)
mount_obs=next((x for x in rows if x.get("check")=="dev_shm_mount"),None)
if not ipc or ipc.get("status")!="PASS" or not mount_obs or mount_obs.get("status")!="PASS":
    raise SystemExit("PRODUCT-21 IPC observation failed or unavailable")
inspect=json.loads(pathlib.Path(sys.argv[2]).read_text())
if not isinstance(inspect.get("Id"),str) or len(inspect["Id"])!=64 or inspect.get("Name","").lstrip("/")!=sys.argv[4]:
    raise SystemExit("PRODUCT-21 exact-container inspection identity mismatch")
if inspect.get("State",{}).get("ExitCode")!=int(pathlib.Path(sys.argv[5]).read_text().strip()):
    raise SystemExit("PRODUCT-21 probe/inspection exit mismatch")
mounts=inspect.get("Mounts",[])
if len(mounts)!=1 or mounts[0].get("Destination")!="/ws" or not mounts[0].get("RW"):
    raise SystemExit("PRODUCT-21 unexpected mounts")
cfg=inspect["HostConfig"]
if "IpcMode" not in cfg: raise SystemExit("PRODUCT-21 IPC configuration unverified")
if cfg.get("IpcMode")!="none": raise SystemExit("PRODUCT-21 IPC isolation profile mismatch")
if cfg.get("Privileged") or cfg.get("NetworkMode")!="none" or cfg.get("PidMode") or not cfg.get("ReadonlyRootfs"):
    raise SystemExit("PRODUCT-21 Docker boundary mismatch")
if any(m.get("Destination") in ("/dev/shm","/run/shm") for m in mounts):
    raise SystemExit("PRODUCT-21 unexpected shared-memory mount")
if any(p in ("/dev/shm","/run/shm") for p in (cfg.get("Tmpfs") or {})):
    raise SystemExit("PRODUCT-21 unexpected shared-memory tmpfs")
ipc_state=ipc.get("observed")
if ipc_state not in ("path_absent_host_inspection_required","present_nonwritable","present_writable"):
    raise SystemExit("PRODUCT-21 IPC observation invalid")
if ipc_state=="present_writable": raise SystemExit("PRODUCT-21 writable shared-memory surface")
# Absence and denied-write observations pass only after exact-container identity,
# IpcMode=none, no conflicting shm mount, and the single approved /ws mount pass.
if sorted(p.name for p in pathlib.Path(sys.argv[3]).iterdir()) != ["task.py"]:
    raise SystemExit("PRODUCT-21 workspace contents changed unexpectedly")
print("PRODUCT21_GATE_PASS checks="+str(len(checks)))
PY
then
  CLASSIFICATION=PRODUCT_21_ISOLATION_FAILED
  echo 'Gate 1 failed; PRODUCT-22 execution was not started.' >&2
  exit 25
fi
docker rm "$P21_CONTAINER" >/dev/null || { CLASSIFICATION=CLEANUP_FAILED; exit 24; }
P21_CONTAINER=
PROBE_STATUS=PASS
INSPECT_STATUS=PASS
kill "$WITNESS_PID"; wait "$WITNESS_PID" 2>/dev/null || true; WITNESS_PID=
rm -rf -- "$WORK"; WORK=
CLEANUP_STATUS=PASS

# PRODUCT-22 Host integration driver uses a temporary in-memory HMAC key and
# the real Launcher, SandboxClient, SQLite ledger, staging code and DockerExecutor.
set +e
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$REPO/venv/bin/python3" - "$RUN_DIR" "$REPO" "$PINNED_IMAGE_ID" <<'PY'
import hashlib, json, os, pathlib, secrets, socket, subprocess, sys, tempfile, threading, time, uuid
from app.services.sandbox_bridge.contract import ExecutionRequest, PROFILE
from app.services.sandbox_bridge.docker_executor import DockerExecutor
from app.services.sandbox_bridge.ledger import RequestLedger
from app.services.sandbox_bridge.transport import HostSocketLauncher, SandboxClient

evidence=pathlib.Path(sys.argv[1]); repo=pathlib.Path(sys.argv[2]); pinned_image_id=sys.argv[3]
root=pathlib.Path(tempfile.mkdtemp(prefix="p43-product22-", dir="/tmp")); os.chmod(root,0o700)
sock=root/"launcher.sock"; ledger=root/"ledger.sqlite3"; staging=root/"staging"; staging.mkdir(mode=0o700)
key=secrets.token_bytes(32); uid=os.getuid(); docker=DockerExecutor(staging)
stop=None; server=None; run_ids=set(); run_lock=threading.Lock(); watcher_stop=threading.Event()
baseline_raw=subprocess.run(["docker","ps","-aq","--filter","name=^/p43-sbx-"],capture_output=True,text=True,timeout=5,check=True).stdout
baseline_ids=set(baseline_raw.split())
if baseline_ids: raise RuntimeError("preexisting_p43_sandbox_containers_present")
summary={"launcher":"NOT_RUN","socket":"NOT_RUN","auth":"NOT_RUN","execution":"NOT_RUN",
         "workspace":"NOT_RUN","artifact":"NOT_RUN","timeout":"NOT_RUN","cancellation":"NOT_RUN",
         "replay":"NOT_RUN","uncertain_replay":"NOT_RUN","ledger_fail_closed":"NOT_RUN","cleanup":"NOT_RUN",
         "failure":None}

def watch():
    while not watcher_stop.is_set():
        try:
            ids=subprocess.run(["docker","ps","-q","--filter","name=^/p43-sbx-"],capture_output=True,text=True,timeout=2,check=True).stdout.split()
            with run_lock: run_ids.update(ids)
            for cid in ids:
                raw=subprocess.run(["docker","inspect",cid],capture_output=True,text=True,timeout=3,check=True).stdout
                doc=json.loads(raw)[0]
                name=doc.get("Name","").lstrip("/")
                if name.startswith("p43-sbx-"):
                    out=evidence/("docker-inspect-"+name+".json")
                    out.write_text(json.dumps(doc,indent=2,sort_keys=True)+"\n")
        except Exception:
            pass
        watcher_stop.wait(.03)

watcher=threading.Thread(target=watch,daemon=True); watcher.start()

def start_launcher():
    global stop, server
    stop=threading.Event(); launcher=HostSocketLauncher(sock,key,uid,docker.execute,ledger)
    server=threading.Thread(target=launcher.serve_forever,args=(stop,),daemon=True); server.start()
    end=time.monotonic()+5
    while time.monotonic()<end and not sock.exists(): time.sleep(.01)
    if not sock.exists() or not stat_socket(sock): raise RuntimeError("socket_startup_failed")
    if sock.stat().st_uid!=uid or (sock.stat().st_mode&0o777)!=0o660: raise RuntimeError("socket_permissions_failed")
    return launcher

def stat_socket(p): return p.is_socket()
def stop_launcher():
    global stop,server
    stop.set(); server.join(5)
    if server.is_alive() or sock.exists(): raise RuntimeError("launcher_shutdown_failed")

def req(code, timeout=15, request_id=None, token=None):
    return ExecutionRequest(request_id or str(uuid.uuid4()),"task-host-verify","exec-host-verify",str(uuid.uuid4()),PROFILE,code,timeout,int(time.time()),token or secrets.token_hex(32),1)

def execute(r): return SandboxClient(sock,key,connect_timeout=10).execute(r)

try:
    import stat
    launcher=start_launcher(); summary["launcher"]="PASS"
    summary["socket"]="PASS"
    client=SandboxClient(sock,key,connect_timeout=10)
    basic_result=client.execute(req("print(1 + 1)\n",10))
    if basic_result.execution_status!="SUCCESS" or basic_result.exit_code!=0 or basic_result.stdout.strip()!="2":
        raise RuntimeError("basic_execution_failed:"+basic_result.execution_status+":"+str(basic_result.exit_code)+":"+(basic_result.error_code or "none"))
    summary["execution"]="PASS"
    artifact_req=req("from pathlib import Path\nimport time\nPath('/ws/output/result.txt').write_text('sandbox-ok\\n')\nprint('2')\ntime.sleep(2)\n",15)
    result=client.execute(artifact_req)
    if result.execution_status!="SUCCESS" or result.stdout.strip()!="2": raise RuntimeError("trusted_execution_failed")
    summary["auth"]="PASS"
    if result.artifact_manifest!=[{"path":"result.txt","size":11,"sha256":hashlib.sha256(b"sandbox-ok\n").hexdigest()}] or result.artifacts!=[b"sandbox-ok\n"]:
        raise RuntimeError("artifact_manifest_or_bytes_mismatch")
    summary["artifact"]="PASS"
    if list(staging.iterdir()): raise RuntimeError("artifact_workspace_not_clean")
    summary["workspace"]="PASS"
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and not list(evidence.glob("docker-inspect-p43-sbx-*.json")): time.sleep(.05)
    inspections=list(evidence.glob("docker-inspect-p43-sbx-*.json"))
    if not inspections: raise RuntimeError("real_container_inspect_not_captured")
    cfg=json.loads(inspections[0].read_text()); host=cfg["HostConfig"]; mounts=cfg["Mounts"]
    expected_ulimits={x["Name"]:(x["Soft"],x["Hard"]) for x in host.get("Ulimits",[])}
    security=host.get("SecurityOpt",[])
    checks={"image":cfg["Config"].get("Image")=="python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b" and cfg.get("Image")==pinned_image_id,
      "platform":cfg["Config"].get("Image")=="python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b" and cfg.get("Image")==pinned_image_id,
      "network":host.get("NetworkMode")=="none","ipc":host.get("IpcMode")=="none","default_private_pid":not host.get("PidMode"),
      "readonly":host.get("ReadonlyRootfs") is True,"cap_drop_all":"ALL" in host.get("CapDrop",[]),
      "no_new_privileges":any("no-new-privileges" in x for x in security),"user":cfg["Config"].get("User")=="65534:65534",
      "cpus":host.get("NanoCpus")==1_000_000_000,"memory":host.get("Memory")==536870912,"swap":host.get("MemorySwap")==536870912,
      "pids":host.get("PidsLimit")==64,"ulimit_nofile":expected_ulimits.get("nofile")== (256,256),
      "ulimit_fsize":expected_ulimits.get("fsize")== (16777216,16777216),
      "single_rw_ws_mount":len(mounts)==1 and mounts[0].get("Destination")=="/ws" and mounts[0].get("RW") is True and "docker.sock" not in mounts[0].get("Source","")}
    (evidence/"container-config-checks.json").write_text(json.dumps(checks,indent=2,sort_keys=True)+"\n")
    if not all(checks.values()): raise RuntimeError("actual_docker_configuration_mismatch")

    timeout_result=execute(req("import time\ntime.sleep(10)\n",1))
    if timeout_result.execution_status!="TIMEOUT": raise RuntimeError("timeout_status_mismatch:"+timeout_result.execution_status)
    summary["timeout"]="PASS"
    if list(staging.iterdir()): raise RuntimeError("timeout_staging_not_clean")

    long_req=req("import time\nprint('started', flush=True)\ntime.sleep(30)\n",40)
    # The watcher retains historical IDs for final cleanup; clear them so the
    # cancellation readiness check observes this request's container only.
    with run_lock: run_ids.clear()
    box=[]; worker=threading.Thread(target=lambda:box.append(execute(long_req)),daemon=True); worker.start()
    end=time.monotonic()+8
    while time.monotonic()<end:
        with run_lock: active=bool(run_ids)
        if active: break
        time.sleep(.03)
    if not active: raise RuntimeError("cancellation_container_not_observed")
    if client.cancel(long_req.request_id,secrets.token_hex(32)): raise RuntimeError("unauthorized_cancel_accepted")
    other_box=[]; other=threading.Thread(target=lambda:other_box.append(execute(req("print(1 + 1)\n",10))),daemon=True); other.start()
    if not client.cancel(long_req.request_id,long_req.cancellation_token): raise RuntimeError("authorized_cancel_rejected")
    worker.join(12); other.join(12)
    if worker.is_alive() or not box or box[0].execution_status!="CANCELLED": raise RuntimeError("cancel_execution_failed")
    if other.is_alive() or not other_box or other_box[0].execution_status!="SUCCESS" or other_box[0].stdout.strip()!="2": raise RuntimeError("other_request_affected")
    summary["cancellation"]="PASS"

    replay=req("print('first')\n",10)
    if execute(replay).execution_status!="SUCCESS": raise RuntimeError("initial_replay_request_not_accepted")
    duplicate=execute(replay)
    if duplicate.execution_status!="REJECTED" or duplicate.error_code!="duplicate_request": raise RuntimeError("same_launcher_replay_not_rejected")
    stop_launcher(); launcher=start_launcher()
    replay_again=ExecutionRequest(replay.request_id,replay.task_id,replay.execution_id,str(uuid.uuid4()),PROFILE,replay.code,10,int(time.time()),secrets.token_hex(32),1)
    after_restart=execute(replay_again)
    if after_restart.execution_status!="REJECTED" or after_restart.error_code!="duplicate_request": raise RuntimeError("restart_replay_not_rejected")
    summary["replay"]="PASS"

    uncertain_id=str(uuid.uuid4()); launcher.ledger.admit(uncertain_id); stop_launcher(); launcher=start_launcher()
    if launcher.ledger.status(uncertain_id)!="INTERNAL_ERROR": raise RuntimeError("uncertain_admission_not_marked")
    uncertain=req("print('must-not-run')\n",10,request_id=uncertain_id)
    if execute(uncertain).execution_status!="REJECTED": raise RuntimeError("uncertain_request_replayed")
    summary["uncertain_replay"]="PASS"

    bad_ledger=root/"ledger-link.sqlite3"; bad_ledger.symlink_to(ledger)
    try:
        HostSocketLauncher(root/"bad.sock",key,uid,docker.execute,bad_ledger)
        raise RuntimeError("symlink_ledger_failure_was_accepted")
    except RuntimeError as exc:
        if str(exc)=="symlink_ledger_failure_was_accepted": raise
    if (root/"bad.sock").exists(): raise RuntimeError("ledger_failure_created_socket")
    summary["ledger_fail_closed"]="PASS"
    stop_launcher(); summary["cleanup"]="PASS"
except Exception as exc:
    summary["failure"]=type(exc).__name__+":"+str(exc)[:240]
finally:
    try:
        if server is not None and server.is_alive(): stop_launcher()
    except Exception:
        summary["cleanup"]="FAIL"
    watcher_stop.set(); watcher.join(3)
    with run_lock: ids=[cid for cid in run_ids if cid not in baseline_ids]
    for cid in ids:
        try: subprocess.run(["docker","rm","-f",cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
        except Exception: summary["cleanup"]="FAIL"
    if sock.exists() or list(staging.iterdir()): summary["cleanup"]="FAIL"
    subprocess.run(["rm","-rf","--",str(root)],check=False)
    (evidence/"product22-integration-summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    if any(v!="PASS" for k,v in summary.items() if k!="failure"):
        print("PRODUCT22_GATE_FAIL",json.dumps(summary,sort_keys=True),file=sys.stderr)
        raise SystemExit(1)
    print("PRODUCT22_GATE_PASS",json.dumps(summary,sort_keys=True))
PY
INTEGRATION_RC=$?
set -e
if [[ $INTEGRATION_RC -ne 0 ]]; then
  INTEGRATION_STATUS=FAIL
  CLASSIFICATION=DOCKER_EXECUTION_FAILED
  exit 26
fi
INTEGRATION_STATUS=PASS
CLEANUP_STATUS=PASS
CLASSIFICATION=P43_PRODUCT_22_HOST_INTEGRATION_VERIFIED
exit 0
