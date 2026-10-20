#!/usr/bin/env python3
"""Read-only Stage R Gate 1 preflight checks.

The checker reports only classifications, stable reason codes, field names,
and non-secret identity digests. It does not install pins, inspect ProductRoot,
or issue Celery control commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACTIVE_SESSION_STATUSES = {
    "active",
    "awaiting_input",
    "pending",
    "paused",
    "recovering",
    "retry_pending",
    "running",
    "waiting_for_input",
}


def _check(
    check_id: str,
    status: str,
    reasons: Sequence[str] = (),
    *,
    fields: Sequence[str] = (),
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "differing_fields": sorted(set(fields)),
        "evidence": dict(evidence or {}),
    }


def overall_status(checks: Sequence[Mapping[str, Any]]) -> str:
    statuses = {check.get("status") for check in checks}
    if FAIL in statuses:
        return FAIL
    if INCONCLUSIVE in statuses:
        return INCONCLUSIVE
    return PASS


def evaluate_git(
    approved_head: str | None,
    approved_tree: str | None,
    observed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not approved_head or not approved_tree:
        return _check(
            "git_source_identity",
            INCONCLUSIVE,
            ["approved_git_identity_unavailable"],
            fields=["approved_head", "approved_tree"],
        )
    if not isinstance(observed, Mapping) or not observed.get("available"):
        return _check("git_source_identity", INCONCLUSIVE, ["git_state_unavailable"])
    fields: list[str] = []
    reasons: list[str] = []
    incomplete = False
    if observed.get("head") != approved_head:
        fields.append("HEAD")
        reasons.append("git_head_mismatch")
    if observed.get("tree") != approved_tree:
        fields.append("HEAD_tree")
        reasons.append("git_tree_mismatch")
    if observed.get("dirty") is True:
        fields.append("worktree")
        reasons.append("git_worktree_dirty")
    elif observed.get("dirty") is not False:
        incomplete = True
        reasons.append("git_worktree_unavailable")
    return _check(
        "git_source_identity",
        FAIL if fields else (INCONCLUSIVE if incomplete else PASS),
        reasons,
        fields=fields,
        evidence={
            "head": observed.get("head"),
            "tree": observed.get("tree"),
            "change_count": observed.get("change_count"),
        },
    )


def _tokens(record: Mapping[str, Any]) -> tuple[str, ...]:
    argv = record.get("argv", ())
    return tuple(str(item) for item in argv) if isinstance(argv, (list, tuple)) else ()


def _is_worker(tokens: Sequence[str]) -> bool:
    lowered = [item.lower() for item in tokens]
    return "celery" in [Path(item).name for item in lowered] and "worker" in lowered


def _is_beat(tokens: Sequence[str]) -> bool:
    lowered = [item.lower() for item in tokens]
    return "celery" in [Path(item).name for item in lowered] and "beat" in lowered


def _is_openclaw_agent(tokens: Sequence[str]) -> bool:
    lowered = [item.lower() for item in tokens]
    return (
        "openclaw" in [Path(item).name for item in lowered]
        and "agent" in lowered
        and "--local" in lowered
    )


def _is_editor(tokens: Sequence[str]) -> bool:
    names = {Path(item).name.lower() for item in tokens}
    return bool(names & {"codex", "claude", "claude-code"})


def evaluate_processes(
    records: Sequence[Mapping[str, Any]] | None,
    *,
    complete: bool,
) -> dict[str, Any]:
    if records is None:
        return _check("process_quiescence", INCONCLUSIVE, ["process_table_unavailable"])
    editors: list[int] = []
    openclaw: list[int] = []
    workers: list[Mapping[str, Any]] = []
    beats: list[int] = []
    for record in records:
        pid = int(record.get("pid", -1))
        tokens = _tokens(record)
        if _is_editor(tokens):
            editors.append(pid)
        if _is_openclaw_agent(tokens):
            openclaw.append(pid)
        if _is_worker(tokens):
            workers.append(record)
        if _is_beat(tokens):
            beats.append(pid)

    worker_pids = {int(item.get("pid", -1)) for item in workers}
    worker_masters = [
        int(item.get("pid", -1))
        for item in workers
        if int(item.get("ppid", -1)) not in worker_pids
    ]
    reasons: list[str] = []
    fields: list[str] = []
    if editors:
        reasons.append("unexpected_editor_process")
        fields.append("codex_or_claude_processes")
    if openclaw:
        reasons.append("unexpected_openclaw_agent_process")
        fields.append("openclaw_agent_processes")
    if len(worker_masters) > 1:
        reasons.append("duplicate_celery_workers")
        fields.append("celery_worker_masters")
    if len(beats) > 1:
        reasons.append("duplicate_celery_beat")
        fields.append("celery_beat_processes")
    status = FAIL if reasons else (INCONCLUSIVE if not complete else PASS)
    if not complete and not reasons:
        reasons.append("process_table_partial")
    return _check(
        "process_quiescence",
        status,
        reasons,
        fields=fields,
        evidence={
            "unexpected_editor_pids": sorted(editors),
            "unexpected_openclaw_agent_pids": sorted(openclaw),
            "celery_worker_master_pids": sorted(worker_masters),
            "celery_beat_pids": sorted(beats),
        },
    )


def _snapshot_task_status(
    snapshot: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {"available": False, "reason": "task_snapshot_missing"}
    if snapshot.get("snapshot_schema_version") != 1:
        return {"available": False, "reason": "task_snapshot_invalid"}
    try:
        captured = datetime.fromisoformat(
            str(snapshot["captured_at"]).replace("Z", "+00:00")
        )
        if captured.tzinfo is None:
            raise ValueError
        age = ((now or datetime.now(timezone.utc)) - captured).total_seconds()
    except (KeyError, TypeError, ValueError):
        return {"available": False, "reason": "task_snapshot_invalid"}
    if age < -5 or age > 120:
        return {"available": False, "reason": "task_snapshot_stale"}
    counts: dict[str, int] = {}
    for name in ("active", "reserved", "scheduled"):
        value = snapshot.get(name)
        if not isinstance(value, list):
            return {"available": False, "reason": "task_snapshot_invalid"}
        counts[name] = len(value)
    return {"available": True, "counts": counts}


def evaluate_redis_tasks(
    redis_state: Mapping[str, Any] | None,
    task_snapshot: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    fields: list[str] = []
    status = PASS
    queue_lengths: Mapping[str, int] | None = None
    lease_count: int | None = None
    unexpected_lease_count: int | None = None
    if not isinstance(redis_state, Mapping) or not redis_state.get("available"):
        status = INCONCLUSIVE
        reasons.append("redis_unavailable")
    else:
        queue_lengths = redis_state.get("queue_lengths")
        lease_keys = redis_state.get("lease_keys")
        if (
            not isinstance(queue_lengths, Mapping)
            or not queue_lengths
            or any(type(value) is not int for value in queue_lengths.values())
            or not isinstance(lease_keys, list)
        ):
            status = INCONCLUSIVE
            reasons.append("redis_response_invalid")
        else:
            lease_count = len(lease_keys)
            unexpected = sorted(set(str(key) for key in lease_keys))
            unexpected_lease_count = len(unexpected)
            nonempty_queues = sorted(
                name for name, length in queue_lengths.items() if length
            )
            if nonempty_queues:
                status = FAIL
                reasons.append("redis_queue_nonempty")
                fields.extend(f"redis_queues.{name}" for name in nonempty_queues)
            if unexpected:
                status = FAIL
                reasons.append("redis_concurrency_lease_keys_present")
                fields.append("concurrency_lease_keys")

    task_state = _snapshot_task_status(task_snapshot, now=now)
    if not task_state["available"]:
        if status != FAIL:
            status = INCONCLUSIVE
        reasons.append(str(task_state["reason"]))
    else:
        for name, count in task_state["counts"].items():
            if count:
                status = FAIL
                reasons.append(f"tasks_{name}_nonempty")
                fields.append(f"celery_{name}_tasks")
    return _check(
        "redis_and_task_quiescence",
        status,
        reasons,
        fields=fields,
        evidence={
            "queue_lengths": dict(queue_lengths) if queue_lengths is not None else None,
            "lease_key_count": lease_count,
            "unexpected_lease_key_count": unexpected_lease_count,
            "task_counts": task_state.get("counts"),
        },
    )


def evaluate_sessions(active_count: int | None) -> dict[str, Any]:
    if type(active_count) is not int or active_count < 0:
        return _check("active_sessions", INCONCLUSIVE, ["session_state_unavailable"])
    if active_count:
        return _check(
            "active_sessions",
            FAIL,
            ["active_sessions_present"],
            fields=["active_sessions"],
            evidence={"active_session_count": active_count},
        )
    return _check("active_sessions", PASS, evidence={"active_session_count": 0})


def evaluate_runtime(
    current: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(current, Mapping) or not current.get("available"):
        return _check(
            "runtime_environment", INCONCLUSIVE, ["runtime_inventory_unavailable"]
        )
    evidence = {
        "venv_inventory_sha256": current.get("venv_inventory_sha256"),
        "python_identity": current.get("python_identity"),
    }
    if not isinstance(baseline, Mapping):
        return _check(
            "runtime_environment",
            INCONCLUSIVE,
            ["runtime_baseline_missing"],
            evidence=evidence,
        )
    fields: list[str] = []
    if current.get("venv_inventory_sha256") != baseline.get("venv_inventory_sha256"):
        fields.append("venv_inventory_sha256")
    if current.get("python_identity") != baseline.get("python_identity"):
        fields.append("python_identity")
    if fields:
        return _check(
            "runtime_environment",
            FAIL,
            ["runtime_environment_drift"],
            fields=fields,
            evidence=evidence,
        )
    return _check("runtime_environment", PASS, evidence=evidence)


def evaluate_protected_state(
    approved: Mapping[str, Any] | None,
    observed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(approved, Mapping) or not isinstance(observed, Mapping):
        return _check(
            "protected_state", INCONCLUSIVE, ["protected_digests_unavailable"]
        )
    key = "product_root_identity_sha256"
    if key not in approved or key not in observed:
        return _check("protected_state", INCONCLUSIVE, ["product_root_digest_missing"])
    for values in (approved, observed):
        if any(
            not isinstance(value, str) or not _SHA256.fullmatch(value)
            for value in values.values()
        ):
            return _check("protected_state", INCONCLUSIVE, ["protected_digest_invalid"])
    fields = sorted(
        set(approved) ^ set(observed)
        | {
            name
            for name in set(approved) & set(observed)
            if approved[name] != observed[name]
        }
    )
    if fields:
        return _check(
            "protected_state",
            FAIL,
            ["protected_state_digest_mismatch"],
            fields=fields,
            evidence={"digest_field_count": len(observed)},
        )
    return _check(
        "protected_state",
        PASS,
        evidence={"digest_field_count": len(observed), "product_root_traversed": False},
    )


def evaluate_pin(pin: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(pin, Mapping):
        return _check("pin_metadata", INCONCLUSIVE, ["pin_metadata_unavailable"])
    if pin.get("exists") is None:
        return _check("pin_metadata", INCONCLUSIVE, ["pin_metadata_unavailable"])
    if not pin.get("exists"):
        return _check("pin_metadata", FAIL, ["pin_missing"], fields=["pin_path"])
    fields: list[str] = []
    reasons: list[str] = []
    policy_missing = any(
        pin.get(name) is None
        for name in ("expected_owner_uid", "expected_owner_gid", "expected_mode")
    )
    if policy_missing:
        reasons.append("pin_policy_unavailable")
    if pin.get("expected_owner_uid") is not None and pin.get("owner_uid") != pin.get(
        "expected_owner_uid"
    ):
        fields.append("owner_uid")
        reasons.append("pin_owner_mismatch")
    if pin.get("expected_owner_gid") is not None and pin.get("owner_gid") != pin.get(
        "expected_owner_gid"
    ):
        fields.append("owner_gid")
        reasons.append("pin_group_mismatch")
    if pin.get("expected_mode") is not None and pin.get("mode") != pin.get(
        "expected_mode"
    ):
        fields.append("mode")
        reasons.append("pin_mode_mismatch")
    if pin.get("symlink") is True or pin.get("regular_file") is not True:
        fields.append("pin_file_type")
        reasons.append("pin_file_type_invalid")
    if (
        pin.get("loader_status") is not None
        and pin.get("loader_status") != "PIN_LOADED"
    ):
        fields.append("pin_schema")
        reasons.append("pin_schema_invalid")
    if fields:
        return _check("pin_metadata", FAIL, reasons, fields=fields)
    if policy_missing:
        return _check("pin_metadata", INCONCLUSIVE, reasons)
    if pin.get("loader_status") is None:
        return _check("pin_metadata", INCONCLUSIVE, ["pin_schema_unavailable"])
    return _check(
        "pin_metadata",
        PASS,
        evidence={
            "owner_uid": pin.get("owner_uid"),
            "owner_gid": pin.get("owner_gid"),
            "mode": pin.get("mode"),
            "schema_valid": True,
        },
    )


def evaluate_backup(backup: Mapping[str, Any] | None) -> dict[str, Any]:
    if backup is None:
        return _check(
            "evidence_backup",
            FAIL,
            ["backup_verification_missing"],
            fields=["backup_verification"],
        )
    if not isinstance(backup, Mapping):
        return _check(
            "evidence_backup", INCONCLUSIVE, ["backup_verification_unavailable"]
        )
    required = (
        backup.get("schema_version") == 1
        and backup.get("backup_status") == "VERIFIED"
        and backup.get("source_path") == "/root/.orchestrator/runtime"
        and backup.get("outside_container") is True
        and isinstance(backup.get("destination_path"), str)
        and Path(str(backup.get("destination_path"))).is_absolute()
        and type(backup.get("file_count")) is int
        and backup.get("file_count", 0) > 0
        and backup.get("inventory_verified") is True
        and backup.get("manifest_verified") is True
        and isinstance(backup.get("sha256_manifest_digest"), str)
        and bool(_SHA256.fullmatch(str(backup.get("sha256_manifest_digest"))))
    )
    if not required:
        return _check(
            "evidence_backup",
            FAIL,
            ["backup_verification_invalid"],
            fields=["backup_verification"],
        )
    return _check(
        "evidence_backup",
        PASS,
        evidence={
            "file_count": backup["file_count"],
            "manifest_verified": True,
            "outside_container": True,
        },
    )


def evaluate_preflight(
    approved: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    checks = [
        evaluate_git(
            approved.get("head", ""), approved.get("tree", ""), observed.get("git")
        ),
        evaluate_processes(
            observed.get("processes"),
            complete=bool(observed.get("process_table_complete")),
        ),
        evaluate_redis_tasks(
            observed.get("redis"),
            observed.get("task_snapshot"),
            now=observed.get("now"),
        ),
        evaluate_sessions(observed.get("active_session_count")),
        evaluate_runtime(observed.get("runtime"), approved.get("runtime")),
        evaluate_protected_state(
            observed.get("protected_approved"), observed.get("protected_observed")
        ),
        evaluate_pin(observed.get("pin")),
        evaluate_backup(observed.get("backup")),
    ]
    runtime = observed.get("runtime")
    public_runtime = (
        runtime.get("public_identity") if isinstance(runtime, Mapping) else None
    )
    return {
        "schema_version": 1,
        "tool": "stage-r-preflight",
        "overall": overall_status(checks),
        "checks": checks,
        "current_runtime": public_runtime,
    }


def python_identity() -> dict[str, Any]:
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "cache_tag": getattr(sys.implementation, "cache_tag", None),
        "executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
    }


def _tool_checkout_root() -> Path:
    return Path(__file__).absolute().parents[3]


def _matches_tool_checkout(path: str | Path) -> bool:
    return Path(os.path.abspath(path)) == _tool_checkout_root()


def venv_inventory_digest(root: str | Path) -> str:
    """Hash a stable inventory without following symlinks or writing caches."""

    base = Path(root)
    if not base.is_dir():
        raise OSError("venv_unavailable")
    rows: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: os.fsencode(item.name))
        for entry in ordered:
            rel = Path(entry.path).relative_to(base).as_posix()
            if entry.name == "__pycache__" or entry.name.endswith(".pyc"):
                continue
            info = entry.stat(follow_symlinks=False)
            row: dict[str, Any] = {
                "path": rel,
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
            if stat.S_ISLNK(info.st_mode):
                row.update(kind="symlink", target=os.readlink(entry.path))
            elif stat.S_ISDIR(info.st_mode):
                row["kind"] = "directory"
                visit(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                descriptor = os.open(entry.path, flags)
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                finally:
                    os.close(descriptor)
                row.update(kind="file", sha256=digest.hexdigest(), size=info.st_size)
            else:
                row["kind"] = "special"
            rows.append(row)

    visit(base)
    rows.sort(key=lambda row: os.fsencode(row["path"]))
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def collect_git(source_root: str | Path) -> dict[str, Any]:
    if not _matches_tool_checkout(source_root):
        raise OSError("source_root_not_tool_checkout")
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"

    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source_root), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout.strip()

    head = run("rev-parse", "HEAD")
    tree = run("rev-parse", "HEAD^{tree}")
    status = run("status", "--short", "--untracked-files=all")
    return {
        "available": True,
        "head": head,
        "tree": tree,
        "dirty": bool(status),
        "change_count": len(status.splitlines()),
    }


def collect_processes(
    proc_root: str | Path = "/proc",
) -> tuple[list[dict[str, Any]], bool]:
    root = Path(proc_root)
    records: list[dict[str, Any]] = []
    complete = True
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            stat_text = (entry / "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        argv = tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)
        if not argv:
            continue
        close = stat_text.rfind(")")
        if close < 0:
            complete = False
            continue
        fields = stat_text[close + 1 :].split()
        if len(fields) < 2:
            complete = False
            continue
        try:
            ppid = int(fields[1])
            pid = int(entry.name)
        except ValueError:
            complete = False
            continue
        records.append({"pid": pid, "ppid": ppid, "argv": argv})
    return records, complete


def _resp_read(sock: socket.socket) -> Any:
    lead = sock.recv(1)
    if not lead:
        raise OSError("redis_connection_closed")
    line = bytearray()
    while not line.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("redis_connection_closed")
        line.extend(chunk)
    payload = bytes(line[:-2])
    if lead == b"+":
        return payload.decode("utf-8", "replace")
    if lead == b"-":
        raise OSError("redis_command_error")
    if lead == b":":
        return int(payload)
    if lead == b"$":
        length = int(payload)
        if length < 0:
            return None
        data = bytearray()
        while len(data) < length + 2:
            chunk = sock.recv(length + 2 - len(data))
            if not chunk:
                raise OSError("redis_connection_closed")
            data.extend(chunk)
        return bytes(data[:-2])
    if lead == b"*":
        count = int(payload)
        if count < 0:
            return None
        return [_resp_read(sock) for _ in range(count)]
    raise OSError("redis_response_invalid")


def _redis_command(sock: socket.socket, *parts: str) -> Any:
    encoded = [str(part).encode("utf-8") for part in parts]
    body = b"*%d\r\n" % len(encoded)
    for part in encoded:
        body += b"$%d\r\n" % len(part) + part + b"\r\n"
    sock.sendall(body)
    return _resp_read(sock)


def collect_redis(redis_url: str, queue_names: Sequence[str]) -> dict[str, Any]:
    if not queue_names or any(not name for name in queue_names):
        raise ValueError("redis_queue_names_unavailable")
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise ValueError("redis_url_invalid")
    database = int((parsed.path or "/0").lstrip("/") or "0")
    context = ssl.create_default_context() if parsed.scheme == "rediss" else None
    with socket.create_connection(
        (parsed.hostname, parsed.port or 6379), timeout=2.0
    ) as raw:
        sock: socket.socket = (
            context.wrap_socket(raw, server_hostname=parsed.hostname)
            if context
            else raw
        )
        sock.settimeout(2.0)
        username = unquote(parsed.username) if parsed.username else ""
        password = unquote(parsed.password) if parsed.password else ""
        if password:
            if username:
                _redis_command(sock, "AUTH", username, password)
            else:
                _redis_command(sock, "AUTH", password)
        if database:
            _redis_command(sock, "SELECT", str(database))
        queue_lengths = {
            queue_name: _redis_command(sock, "LLEN", queue_name)
            for queue_name in queue_names
        }
        if any(type(length) is not int for length in queue_lengths.values()):
            raise OSError("redis_queue_response_invalid")
        keys: list[str] = []
        for pattern in (
            "orchestrator:backend_slots:*",
            "orchestrator:backend_slot_owners:*",
        ):
            cursor = "0"
            while True:
                page = _redis_command(
                    sock, "SCAN", cursor, "MATCH", pattern, "COUNT", "100"
                )
                if (
                    not isinstance(page, list)
                    or len(page) != 2
                    or not isinstance(page[1], list)
                ):
                    raise OSError("redis_scan_response_invalid")
                cursor = str(page[0])
                keys.extend(
                    (
                        item.decode("utf-8", "replace")
                        if isinstance(item, bytes)
                        else str(item)
                    )
                    for item in page[1]
                )
                if cursor == "0":
                    break
        return {
            "available": True,
            "queue_lengths": queue_lengths,
            "lease_keys": sorted(set(keys)),
        }


def collect_sessions(database: str | Path) -> int:
    path = Path(database).resolve(strict=True)
    uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL "
            "AND (COALESCE(is_active, 0) = 1 OR lower(COALESCE(status, '')) IN ("
            + ",".join("?" for _ in _ACTIVE_SESSION_STATUSES)
            + "))",
            tuple(sorted(_ACTIVE_SESSION_STATUSES)),
        ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def _read_json(path: str | Path | None) -> Any:
    if path is None:
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(4 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(raw) > 4 * 1024 * 1024:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None


def collect_pin(
    pin_path: str | Path | None,
    *,
    source_root: str | Path | None,
    runtime_root: str | Path | None,
    expected_owner_uid: int | None = None,
    expected_owner_gid: int | None = None,
    expected_mode: int | None = None,
) -> dict[str, Any]:
    if pin_path is None:
        return {"exists": None}
    path = Path(pin_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError:
        return {"exists": None}
    is_link = stat.S_ISLNK(info.st_mode)
    is_regular = stat.S_ISREG(info.st_mode)
    loader_status = None
    if (
        source_root is not None
        and runtime_root is not None
        and _matches_tool_checkout(source_root)
        and not is_link
        and is_regular
    ):
        try:
            sys.dont_write_bytecode = True
            from app.services.orchestration import worker_identity as wi

            loaded = wi.load_candidate_identity_pin(
                path,
                source_root=source_root,
                runtime_root=runtime_root,
            )
            loader_status = loaded.status
        except Exception:
            loader_status = "PIN_INVALID"
    return {
        "exists": True,
        "symlink": is_link,
        "regular_file": is_regular,
        "owner_uid": info.st_uid,
        "owner_gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "expected_owner_uid": expected_owner_uid,
        "expected_owner_gid": expected_owner_gid,
        "expected_mode": expected_mode,
        "loader_status": loader_status,
    }


def _runtime_observation(venv: str | Path, source_root: str | Path) -> dict[str, Any]:
    if not _matches_tool_checkout(source_root):
        raise OSError("source_root_not_tool_checkout")
    if Path(os.path.abspath(venv)) != _tool_checkout_root() / "venv":
        raise OSError("venv_not_checkout_environment")
    digest = venv_inventory_digest(venv)
    identity = python_identity()
    return {
        "available": True,
        "venv_inventory_sha256": digest,
        "python_identity": identity,
        "public_identity": {
            "venv_inventory_sha256": digest,
            "python_identity": identity,
        },
    }


def _task_snapshot(path: str | Path | None) -> Any:
    return _read_json(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only Stage R Gate 1 checks.")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--approved-head")
    parser.add_argument("--approved-tree")
    parser.add_argument("--runtime-baseline", type=Path)
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--redis-url", default=os.environ.get("STAGE_R_REDIS_URL", ""))
    parser.add_argument("--queue-name", action="append", default=[])
    parser.add_argument("--task-snapshot", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--pin", type=Path)
    parser.add_argument("--pin-owner-uid", type=int)
    parser.add_argument("--pin-owner-gid", type=int)
    parser.add_argument("--pin-mode", type=lambda value: int(value, 8))
    parser.add_argument("--protected-approved", type=Path)
    parser.add_argument("--protected-observed", type=Path)
    parser.add_argument("--backup-verification", type=Path)
    args = parser.parse_args(argv)

    try:
        git_state: Mapping[str, Any] | None = (
            collect_git(args.source_root) if args.source_root else None
        )
    except Exception:
        git_state = None
    try:
        processes, process_complete = collect_processes()
    except OSError:
        processes, process_complete = None, False
    try:
        redis_state = (
            collect_redis(args.redis_url, args.queue_name) if args.redis_url else None
        )
    except Exception:
        redis_state = None
    try:
        active_sessions = collect_sessions(args.database) if args.database else None
    except Exception:
        active_sessions = None
    try:
        runtime = (
            _runtime_observation(args.venv, args.source_root)
            if args.venv and args.source_root
            else None
        )
    except Exception:
        runtime = None
    pin = collect_pin(
        args.pin,
        source_root=args.source_root,
        runtime_root=args.runtime_root,
        expected_owner_uid=args.pin_owner_uid,
        expected_owner_gid=args.pin_owner_gid,
        expected_mode=args.pin_mode,
    )
    baseline_doc = _read_json(args.runtime_baseline)
    baseline = (
        baseline_doc.get("runtime") if isinstance(baseline_doc, Mapping) else None
    )
    observed = {
        "git": git_state,
        "processes": processes,
        "process_table_complete": process_complete,
        "redis": redis_state,
        "task_snapshot": _task_snapshot(args.task_snapshot),
        "active_session_count": active_sessions,
        "runtime": runtime,
        "protected_approved": _read_json(args.protected_approved),
        "protected_observed": _read_json(args.protected_observed),
        "pin": pin,
        "backup": _read_json(args.backup_verification),
    }
    approved = {
        "head": args.approved_head,
        "tree": args.approved_tree,
        "runtime": baseline,
    }
    result = evaluate_preflight(approved, observed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return {PASS: 0, FAIL: 1, INCONCLUSIVE: 2}[result["overall"]]


if __name__ == "__main__":
    raise SystemExit(main())
