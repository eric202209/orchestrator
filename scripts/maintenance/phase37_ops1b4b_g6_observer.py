#!/usr/bin/env python3
"""Read-only G6 observer for one OPS1-B4B live AC7 dispatch (Phase 37 Pre-B).

Watches ONE managed OpenClaw binding root and correlates, by ``/proc``
metadata only, the binding created for an expected ``task_execution_id`` with
the ``openclaw agent`` CLI child that inherits its lock, then observes the
binding's release.  It never imports Orchestrator application code, never
invokes OpenClaw or any network endpoint, never sends signals, never takes a
blocking lock, and never deletes or modifies anything outside its own new
output directory.

Data minimization happens at collection time:

* binding metadata: allowlisted keys only;
* bound ``openclaw.json``: only the expected agent entry's ``workspace``,
  ``model.primary`` and ``model.fallbacks`` are extracted (the file is parsed
  in memory and discarded; nothing else is ever serialized);
* ``agent/auth-profiles.json``: existence only (``lexists``), never opened;
* process environment: only ``OPENCLAW_CONFIG_PATH`` / ``OPENCLAW_STATE_DIR``
  are decoded, and only for processes that hold the binding lock;
* process argv: only the command structure (``agent``, ``--agent <id>``,
  ``--local``); the ``--message`` prompt and every other value is discarded.

Production observation needs ``--mode production --confirm-read-only-production``.
``--mode synthetic`` refuses the default production binding root.

    python3 scripts/maintenance/phase37_ops1b4b_g6_observer.py --mode synthetic \\
        --binding-root /tmp/x/bindings --expected-task-execution-id 1 \\
        --expected-runtime-workspace /tmp/x/ws --output-dir /tmp/x/out
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Mirrors app/services/orchestration/execution/executor_workspace_binding.py
# (pinned by the observer test suite; this script must not import app code).
BINDING_DIR_PREFIX = "orchestrator-openclaw-binding-"
BINDING_METADATA_NAME = ".orchestrator-binding.json"
BINDING_LOCK_NAME = ".orchestrator-binding.lock"
BINDING_METADATA_SCHEMA = "orchestrator.openclaw_binding.v1"
BINDING_ARTIFACT_TYPE = "openclaw_runtime_binding"
EPHEMERAL_AGENT_ID = "orchestrator-runtime"
DEFAULT_MODEL_REF = "openai/qwen-local"
DEFAULT_PERSISTENT_CONFIG = "/root/.openclaw/openclaw.json"
ENV_KEYS = ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR")

# Observer classifications (NOT production binding lifecycle states).
NOT_OBSERVED = "NOT_OBSERVED"
BINDING_DISCOVERED = "BINDING_DISCOVERED"
BINDING_ACTIVE = "BINDING_ACTIVE"
CLI_CORRELATED = "CLI_CORRELATED"
RELEASE_OBSERVED = "RELEASE_OBSERVED"
RESIDUE_DETECTED = "RESIDUE_DETECTED"
INCONCLUSIVE = "INCONCLUSIVE"
OBSERVATION_ERROR = "OBSERVATION_ERROR"

PASS, FAIL = "PASS", "FAIL"
EXIT_CODES = {PASS: 0, FAIL: 1, INCONCLUSIVE: 2, OBSERVATION_ERROR: 3}

_METADATA_MAX_BYTES = 64 * 1024
_CONFIG_MAX_BYTES = 4 * 1024 * 1024
_SAFE_TEXT = re.compile(r"^[\x20-\x7e]{0,1024}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_DIR_NAME = re.compile(r"^" + re.escape(BINDING_DIR_PREFIX) + r"[A-Za-z0-9_]{1,64}$")
_TITLE = re.compile(r"^openclaw-[a-z][a-z0-9-]{0,31}$")
_KNOWN_FLAGS = frozenset(
    {
        "--agent",
        "--local",
        "--session-id",
        "--message",
        "--json",
        "--timeout",
        "--thinking",
    }
)

_METADATA_ALLOWLIST = {
    "schema": str,
    "artifact_type": str,
    "binding_id": str,
    "artifact_dir_name": str,
    "created_at_epoch": (int, float),
    "owner_uid": int,
    "owner_pid": int,
    "owner_pid_start_ticks": int,
    "owner_pid_namespace": str,
    "boot_id": str,
    "task_execution_id": int,
    "owner_hostname": str,
}

# Explicit output schema: every persisted/printed record is built only from
# these keys.  Values must be None/bool/int/float, safe printable text, or
# lists of those.  Anything else is rejected before it can be written.
EVENT_SCHEMA: Dict[str, Tuple[str, ...]] = {
    "observer_started": (
        "mode",
        "binding_root",
        "expected_task_execution_id",
        "expected_runtime_workspace",
        "expected_agent_id",
        "expected_model_ref",
        "expected_cli_pid",
        "interval_seconds",
        "duration_seconds",
        "root_present",
        "preexisting_entries",
        "openclaw_agent_processes",
    ),
    "unsafe_entry": ("entry", "reason"),
    "foreign_binding": ("entry", "binding_id", "task_execution_id"),
    "binding_metadata_invalid": ("entry", "reason"),
    "binding_discovered": (
        "entry",
        "binding_id",
        "task_execution_id",
        "owner_pid",
        "owner_pid_start_ticks",
        "owner_alive",
        "boot_id_matches",
        "pid_namespace_matches",
        "owner_hostname",
        "created_at_epoch",
        "lock_inode",
        "credential_copy_present",
    ),
    "agent_dir_activity": (
        "entry",
        "binding_id",
        "ephemeral_agent_models_json_present",
    ),
    "lock_state": ("entry", "binding_id", "lock_held", "method", "detail"),
    "config_checked": (
        "entry",
        "binding_id",
        "agent_entries",
        "workspace",
        "model_primary",
        "fallbacks",
        "workspace_matches",
        "model_matches",
        "fallbacks_empty",
        "reason",
    ),
    "process_correlated": (
        "entry",
        "binding_id",
        "pid",
        "ppid",
        "pgid",
        "sid",
        "start_ticks",
        "role",
        "comm",
        "exe",
        "argv_form",
        "argv_subcommand",
        "argv_agent",
        "argv_local",
        "argv_message_present",
        "argv_unknown_flag_count",
        "process_title",
        "config_path",
        "state_dir",
        "config_path_ok",
        "state_dir_ok",
        "config_is_persistent",
        "holds_lock",
        "own_session",
        "parent_is_owner",
        "expected_identity_match",
    ),
    "process_access_denied": ("entry", "binding_id", "pid", "what"),
    "process_exited": ("entry", "binding_id", "pid", "start_ticks", "role"),
    "binding_removed": (
        "entry",
        "binding_id",
        "lock_still_held",
        "cli_alive",
        "owner_alive",
    ),
    "residue": (
        "entry",
        "binding_id",
        "lock_held",
        "metadata_valid",
        "credential_copy_present",
        "target",
    ),
    "observer_finished": (
        "classification",
        "verdict",
        "checks",
        "targets",
        "interrupted",
        "elapsed_seconds",
        "openclaw_agent_processes",
        "notes",
    ),
    "observer_error": ("stage", "error_type"),
}


class ObserverError(Exception):
    """Fail-closed configuration or safety error (message never has data)."""


def _safe_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if not _SAFE_TEXT.match(value):
            raise ObserverError("unsafe text value rejected")
        return value
    if isinstance(value, (list, tuple)) and depth < 3:
        return [_safe_value(item, depth + 1) for item in value]
    if isinstance(value, dict) and depth < 3:
        return {
            _safe_value(str(k), depth + 1): _safe_value(v, depth + 1)
            for k, v in value.items()
        }
    raise ObserverError("unsupported value type rejected")


def build_event(event: str, **fields: Any) -> Dict[str, Any]:
    allowed = EVENT_SCHEMA.get(event)
    if allowed is None:
        raise ObserverError("unknown event type")
    unexpected = set(fields) - set(allowed)
    if unexpected:
        raise ObserverError("event field outside schema")
    record = {"event": event, "t": round(time.time(), 3)}
    for key in allowed:
        if key in fields:
            record[key] = _safe_value(fields[key])
    return record


def _text_or_marker(value: Any, pattern: re.Pattern = _SAFE_TEXT) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str) and pattern.match(value):
        return value
    return "<unrecordable>"


def default_production_root() -> str:
    return f"/tmp/orchestrator-openclaw-bindings-{os.getuid()}"


# --------------------------------------------------------------------------
# /proc access (metadata only)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start_ticks: int


class ProcReader:
    """Minimal read-only view of a procfs tree (real ``/proc`` or synthetic)."""

    def __init__(self, root: str = "/proc") -> None:
        self.root = Path(root)

    def pids(self) -> List[int]:
        try:
            return sorted(int(n) for n in os.listdir(self.root) if n.isdigit())
        except OSError:
            return []

    def stat(self, pid: int) -> Optional[ProcStat]:
        try:
            raw = (self.root / str(pid) / "stat").read_text()
            rest = raw.rsplit(")", 1)[1].split()
            return ProcStat(
                pid, int(rest[1]), int(rest[2]), int(rest[3]), int(rest[19])
            )
        except (OSError, ValueError, IndexError):
            return None

    def comm(self, pid: int) -> Optional[str]:
        try:
            return (self.root / str(pid) / "comm").read_text().strip()
        except OSError:
            return None

    def exe_basename(self, pid: int) -> Optional[str]:
        try:
            return os.path.basename(os.readlink(self.root / str(pid) / "exe"))
        except OSError:
            return None

    def holds_inode(self, pid: int, dev: int, ino: int) -> bool:
        """True if any open fd of ``pid`` refers to (dev, ino).

        Uses ``stat`` on each fd link (metadata only); fd contents are never
        read and the fd table is never recorded.  PermissionError propagates.
        """
        fd_dir = self.root / str(pid) / "fd"
        for name in os.listdir(fd_dir):
            try:
                st = os.stat(fd_dir / name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    raise PermissionError(exc.errno, "fd stat denied") from None
                continue
            if st.st_ino == ino and st.st_dev == dev:
                return True
        return False

    def cmdline(self, pid: int) -> List[bytes]:
        raw = (self.root / str(pid) / "cmdline").read_bytes()
        tokens = raw.split(b"\0")
        while tokens and tokens[-1].strip(b" ") == b"":
            tokens.pop()
        return tokens

    def environ_select(self, pid: int, keys: Iterable[str]) -> Dict[str, str]:
        """Decode only the requested keys; every other entry stays raw bytes
        inside this function and is discarded."""
        wanted = {k.encode() + b"=": k for k in keys}
        found: Dict[str, str] = {}
        raw = (self.root / str(pid) / "environ").read_bytes()
        for item in raw.split(b"\0"):
            for prefix, key in wanted.items():
                if item.startswith(prefix):
                    found[key] = item[len(prefix) :].decode("utf-8", "replace")
        del raw
        return found

    def locked_inodes(self) -> Optional[Set[int]]:
        """Inodes with an active lock per ``/proc/locks`` (passive)."""
        try:
            lines = (self.root / "locks").read_text().splitlines()
        except OSError:
            return None
        inodes: Set[int] = set()
        for line in lines:
            parts = line.split()
            if "->" in parts:
                continue  # blocked waiter, not a holder
            for part in parts:
                if part.count(":") == 2:
                    try:
                        inodes.add(int(part.rsplit(":", 1)[1]))
                    except ValueError:
                        pass
                    break
        return inodes

    def boot_id(self) -> Optional[str]:
        try:
            return (self.root / "sys/kernel/random/boot_id").read_text().strip() or None
        except OSError:
            return None

    def pid_namespace(self) -> Optional[str]:
        try:
            return os.readlink(self.root / "self/ns/pid")
        except OSError:
            return None

    def openclaw_agent_processes(self) -> List[List[int]]:
        out = []
        for pid in self.pids():
            if (self.comm(pid) or "").startswith("openclaw-agent"):
                st = self.stat(pid)
                if st is not None:
                    out.append([pid, st.start_ticks])
        return out


def parse_argv_structure(tokens: List[bytes]) -> Dict[str, Any]:
    """Extract only the command structure; prompt and values are discarded."""
    result: Dict[str, Any] = {
        "argv_form": "unrecognized",
        "argv_subcommand": None,
        "argv_agent": None,
        "argv_local": None,
        "argv_message_present": None,
        "argv_unknown_flag_count": None,
        "process_title": None,
    }
    if not tokens:
        result["argv_form"] = "empty"
        return result
    first = tokens[0].decode("ascii", "replace")
    if len(tokens) == 1 and _TITLE.match(first.strip()):
        result["argv_form"] = "process_title"
        result["process_title"] = first.strip()
        return result
    try:
        idx = tokens.index(b"agent")
    except ValueError:
        return result
    result["argv_form"] = "original_argv"
    result["argv_subcommand"] = "agent"
    rest = tokens[idx + 1 :]
    unknown = 0
    message_present = False
    local = False
    agent: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == b"--message":
            message_present = True
            i += 2  # skip the prompt without decoding it
            continue
        if tok == b"--agent" and i + 1 < len(rest):
            candidate = rest[i + 1].decode("ascii", "replace")
            if agent is not None:
                agent = "<ambiguous>"  # repeated flag: never pick one
            else:
                agent = candidate if _SAFE_ID.match(candidate) else "<invalid>"
            i += 2
            continue
        if tok == b"--local":
            local = True
            i += 1
            continue
        if tok.startswith(b"--"):
            if tok.decode("ascii", "replace") not in _KNOWN_FLAGS:
                unknown += 1
                i += 1
                continue
            i += 1
            continue
        i += 1  # flag value (session id, timeout, ...) -- discarded
    result.update(
        argv_agent=agent,
        argv_local=local,
        argv_message_present=message_present,
        argv_unknown_flag_count=unknown,
    )
    return result


# --------------------------------------------------------------------------
# Binding inspection (fixed child names only; never recursive)
# --------------------------------------------------------------------------


def _open_regular_nofollow(path: Path, max_bytes: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ObserverError("not a regular file")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ObserverError("file too large")
        return data
    finally:
        os.close(fd)


def read_metadata(directory: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        raw = _open_regular_nofollow(
            directory / BINDING_METADATA_NAME, _METADATA_MAX_BYTES
        )
    except FileNotFoundError:
        return None, "missing"
    except ObserverError:
        return None, "not_regular_or_too_large"
    except OSError as exc:
        return None, "unreadable:" + errno.errorcode.get(exc.errno or 0, "E")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, "malformed_json"
    if not isinstance(parsed, dict):
        return None, "not_an_object"
    metadata: Dict[str, Any] = {}
    for key, kind in _METADATA_ALLOWLIST.items():
        value = parsed.get(key)
        if isinstance(value, bool) or (
            value is not None and not isinstance(value, kind)
        ):
            metadata[key] = None
        elif isinstance(value, str):
            metadata[key] = _text_or_marker(value)
        else:
            metadata[key] = value
    del parsed
    for key in (
        "schema",
        "artifact_type",
        "binding_id",
        "artifact_dir_name",
        "owner_uid",
    ):
        if metadata.get(key) is None:
            return None, f"missing_or_invalid:{key}"
    if metadata["schema"] != BINDING_METADATA_SCHEMA:
        return None, "unknown_schema"
    if metadata["artifact_type"] != BINDING_ARTIFACT_TYPE:
        return None, "unknown_artifact_type"
    if metadata["artifact_dir_name"] != directory.name:
        return None, "artifact_dir_name_mismatch"
    if not re.match(r"^[0-9a-f]{32}$", metadata["binding_id"]):
        return None, "invalid_binding_id"
    if metadata["owner_uid"] != os.getuid():
        return None, "owner_uid_mismatch"
    return metadata, ""


def extract_bound_agent(config_path: Path, agent_id: str) -> Dict[str, Any]:
    """Allowlist extraction from the bound config.  The parsed object never
    leaves this function; secret-bearing keys are never touched."""
    try:
        raw = _open_regular_nofollow(config_path, _CONFIG_MAX_BYTES)
    except FileNotFoundError:
        return {"reason": "config_missing"}
    except ObserverError:
        return {"reason": "config_not_regular_or_too_large"}
    except OSError as exc:
        return {
            "reason": "config_unreadable:" + errno.errorcode.get(exc.errno or 0, "E")
        }
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"reason": "config_malformed"}
    finally:
        del raw
    try:
        agents = (
            ((parsed.get("agents") or {}).get("list") or [])
            if isinstance(parsed, dict)
            else []
        )
        matches = [a for a in agents if isinstance(a, dict) and a.get("id") == agent_id]
        out: Dict[str, Any] = {"agent_entries": len(matches), "reason": None}
        if len(matches) != 1:
            out["reason"] = "agent_entry_count_not_one"
            return out
        entry = matches[0]
        workspace = entry.get("workspace")
        model = entry.get("model")
        out["workspace"] = (
            _text_or_marker(workspace) if isinstance(workspace, str) else None
        )
        if isinstance(model, dict):
            primary = model.get("primary")
            fallbacks = model.get("fallbacks")
            out["model_primary"] = (
                _text_or_marker(primary, _SAFE_ID) if isinstance(primary, str) else None
            )
            if isinstance(fallbacks, list):
                out["fallbacks"] = [
                    _text_or_marker(f, _SAFE_ID) if isinstance(f, str) else "<invalid>"
                    for f in fallbacks
                ]
            else:
                out["fallbacks"] = None
        else:
            out["model_primary"] = None
            out["fallbacks"] = None
        return out
    finally:
        del parsed


def probe_lock(lock_path: Path) -> str:
    """Non-blocking shared probe.  Returns held / free / missing / error:*.

    Only called once metadata exists: the owner takes its exclusive lock
    before metadata is written, so the probe can never race creation.  A
    free lock is released immediately; the owner's lock is never touched.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return "error:" + errno.errorcode.get(exc.errno or 0, "E")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "error:not_regular"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError as exc:
            return "error:" + errno.errorcode.get(exc.errno or 0, "E")
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Observer
# --------------------------------------------------------------------------


@dataclass
class Target:
    entry: str
    binding_id: str
    owner_pid: Optional[int]
    owner_ticks: Optional[int]
    lock_dev: Optional[int] = None
    lock_ino: Optional[int] = None
    lock_held_seen: bool = False
    lock_free_seen_while_present: bool = False
    config: Dict[str, Any] = field(default_factory=dict)
    clis: Dict[Tuple[int, int], Dict[str, Any]] = field(default_factory=dict)
    exited: Set[Tuple[int, int]] = field(default_factory=set)
    removed: bool = False
    removal_lock_held: Optional[bool] = None
    proc_denied: bool = False
    credential_copy_present: Optional[bool] = None
    agent_models_json_seen: bool = False


class Observer:
    def __init__(
        self,
        args: argparse.Namespace,
        proc: Optional[ProcReader] = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.args = args
        self.proc = proc or ProcReader(args.proc_root)
        self.clock = clock
        self.sleep = sleep
        self.root = Path(args.binding_root)
        self.targets: Dict[str, Target] = {}
        self.foreign: Set[str] = set()
        self.reported: Set[Tuple[str, str]] = set()
        self.preexisting: Set[str] = set()
        self.notes: List[str] = []
        self.events: List[Dict[str, Any]] = []
        self.out_dir = Path(args.output_dir)
        self._jsonl_fd: Optional[int] = None
        self.boot_id = self.proc.boot_id()
        self.pid_ns = self.proc.pid_namespace()

    # -- output -----------------------------------------------------------
    def open_output(self) -> None:
        if self.out_dir.exists() or self.out_dir.is_symlink():
            raise ObserverError("output directory must not already exist")
        resolved_parent = self.out_dir.parent.resolve()
        root = self.root.resolve() if self.root.exists() else self.root
        if (
            str(resolved_parent / self.out_dir.name).startswith(str(root) + os.sep)
            or resolved_parent == root
        ):
            raise ObserverError("output directory must be outside the binding root")
        os.mkdir(self.out_dir, 0o700)
        os.chmod(self.out_dir, 0o700)
        self._jsonl_fd = os.open(
            self.out_dir / "g6-observe.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )

    def emit(self, event: str, **fields: Any) -> None:
        record = build_event(event, **fields)
        self.events.append(record)
        if self._jsonl_fd is not None:
            os.write(
                self._jsonl_fd, (json.dumps(record, sort_keys=True) + "\n").encode()
            )

    def emit_once(self, key: Tuple[str, str], event: str, **fields: Any) -> None:
        if key not in self.reported:
            self.reported.add(key)
            self.emit(event, **fields)

    def close_output(self, summary: Dict[str, Any]) -> None:
        if self._jsonl_fd is not None:
            os.close(self._jsonl_fd)
            self._jsonl_fd = None
            fd = os.open(
                self.out_dir / "g6-summary.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    # -- root -------------------------------------------------------------
    def root_entries(self) -> Optional[List[str]]:
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ObserverError("binding root is not a real directory")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ObserverError("binding root ownership or mode is unsafe")
        return sorted(os.listdir(self.root))

    # -- one poll ---------------------------------------------------------
    def poll(self) -> None:
        entries = self.root_entries() or []
        present = set(entries)
        locked = self.proc.locked_inodes()
        for name in entries:
            if name in self.targets or name in self.foreign or name in self.preexisting:
                if name in self.targets:
                    self._refresh_target(self.targets[name], locked)
                continue
            self._classify_new_entry(name, locked)
        for name, target in self.targets.items():
            if not target.removed and name not in present:
                self._observe_removal(target, locked)
            elif target.removed:
                self._track_cli_exit(target)

    def _classify_new_entry(self, name: str, locked: Optional[Set[int]]) -> None:
        path = self.root / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if (
            not _DIR_NAME.match(name)
            or stat.S_ISLNK(st.st_mode)
            or not stat.S_ISDIR(st.st_mode)
        ):
            label = name if _DIR_NAME.match(name) else "<unexpected_name>"
            self.emit_once(
                (name, "unsafe"),
                "unsafe_entry",
                entry=label,
                reason="symlink_or_not_directory_or_bad_name",
            )
            self.foreign.add(name)
            return
        metadata, problem = read_metadata(path)
        if metadata is None:
            if problem != "missing":
                self.emit_once(
                    (name, "meta"),
                    "binding_metadata_invalid",
                    entry=name,
                    reason=problem,
                )
                self.foreign.add(name)
            return  # missing: initializing; re-check next poll
        if metadata.get("task_execution_id") != self.args.expected_task_execution_id:
            self.emit_once(
                (name, "foreign"),
                "foreign_binding",
                entry=name,
                binding_id=metadata["binding_id"],
                task_execution_id=metadata.get("task_execution_id"),
            )
            self.foreign.add(name)
            return
        owner_pid = metadata.get("owner_pid")
        owner_ticks = metadata.get("owner_pid_start_ticks")
        target = Target(
            entry=name,
            binding_id=metadata["binding_id"],
            owner_pid=owner_pid,
            owner_ticks=owner_ticks,
        )
        try:
            lst = os.lstat(path / BINDING_LOCK_NAME)
            if stat.S_ISREG(lst.st_mode):
                target.lock_dev, target.lock_ino = lst.st_dev, lst.st_ino
        except OSError:
            pass
        target.credential_copy_present = os.path.lexists(
            path / "agent" / "auth-profiles.json"
        )
        self.targets[name] = target
        self.emit(
            "binding_discovered",
            entry=name,
            binding_id=target.binding_id,
            task_execution_id=metadata.get("task_execution_id"),
            owner_pid=owner_pid,
            owner_pid_start_ticks=owner_ticks,
            owner_alive=self._owner_alive(target),
            boot_id_matches=(
                metadata.get("boot_id") is not None
                and metadata.get("boot_id") == self.boot_id
            ),
            pid_namespace_matches=(
                metadata.get("owner_pid_namespace") is not None
                and metadata.get("owner_pid_namespace") == self.pid_ns
            ),
            owner_hostname=metadata.get("owner_hostname"),
            created_at_epoch=metadata.get("created_at_epoch"),
            lock_inode=target.lock_ino,
            credential_copy_present=target.credential_copy_present,
        )
        self._refresh_target(target, locked)

    def _owner_alive(self, target: Target) -> Optional[bool]:
        if not isinstance(target.owner_pid, int) or target.owner_ticks is None:
            return None
        st = self.proc.stat(target.owner_pid)
        return st is not None and st.start_ticks == target.owner_ticks

    def _lock_held(
        self, target: Target, locked: Optional[Set[int]]
    ) -> Tuple[Optional[bool], str, str]:
        passive = (
            None
            if locked is None or target.lock_ino is None
            else target.lock_ino in locked
        )
        if self.args.no_lock_probe:
            return passive, "proc_locks", "probe_disabled"
        probe = probe_lock(self.root / target.entry / BINDING_LOCK_NAME)
        if probe == "held":
            return True, "flock_probe", "passive=%s" % passive
        if probe == "free":
            return False, "flock_probe", "passive=%s" % passive
        return passive, "proc_locks", "probe=" + probe

    def _refresh_target(self, target: Target, locked: Optional[Set[int]]) -> None:
        held, method, detail = self._lock_held(target, locked)
        if held:
            target.lock_held_seen = True
        elif held is False and target.lock_held_seen:
            target.lock_free_seen_while_present = True
        key = (target.entry, f"lock:{held}")
        self.emit_once(
            key,
            "lock_state",
            entry=target.entry,
            binding_id=target.binding_id,
            lock_held=held,
            method=method,
            detail=detail,
        )
        if not target.config:
            self._check_config(target)
        if not target.agent_models_json_seen and os.path.lexists(
            self.root / target.entry / "agent" / "models.json"
        ):
            # Existence only (never opened): OpenClaw writes models.json into
            # the agentDir of the agent it selected; only the ephemeral agent
            # points at <binding>/agent.  Corroboration, not proof.
            target.agent_models_json_seen = True
            self.emit(
                "agent_dir_activity",
                entry=target.entry,
                binding_id=target.binding_id,
                ephemeral_agent_models_json_present=True,
            )
        self._correlate_processes(target)

    def _check_config(self, target: Target) -> None:
        info = extract_bound_agent(
            self.root / target.entry / "openclaw.json", self.args.expected_agent_id
        )
        if info.get("reason") == "config_missing":
            return  # not yet written; retry next poll
        workspace = info.get("workspace")
        fallbacks = info.get("fallbacks")
        evaluable = info.get("agent_entries") == 1
        target.config = {
            "agent_entries": info.get("agent_entries"),
            # Not evaluable (None -> INCONCLUSIVE) unless exactly one entry;
            # the entry-count FAIL is carried by bound_agent_entry.
            "workspace_matches": (
                (
                    workspace is not None
                    and os.path.normpath(workspace)
                    == os.path.normpath(self.args.expected_runtime_workspace)
                )
                if evaluable
                else None
            ),
            "model_matches": (
                info.get("model_primary") == self.args.expected_model_ref
                if evaluable
                else None
            ),
            "fallbacks_empty": fallbacks == [] if evaluable else None,
            "reason": info.get("reason"),
        }
        self.emit(
            "config_checked",
            entry=target.entry,
            binding_id=target.binding_id,
            agent_entries=info.get("agent_entries"),
            workspace=workspace,
            model_primary=info.get("model_primary"),
            fallbacks=fallbacks,
            workspace_matches=target.config["workspace_matches"],
            model_matches=target.config["model_matches"],
            fallbacks_empty=target.config["fallbacks_empty"],
            reason=info.get("reason"),
        )

    def _candidate_pids(self, target: Target) -> List[int]:
        """Owner plus its descendants; unrelated processes are not opened."""
        if not isinstance(target.owner_pid, int):
            return []
        children: Dict[int, List[int]] = {}
        for pid in self.proc.pids():
            st = self.proc.stat(pid)
            if st is not None:
                children.setdefault(st.ppid, []).append(pid)
        out, frontier = [], [target.owner_pid]
        while frontier and len(out) < 512:
            pid = frontier.pop()
            out.append(pid)
            frontier.extend(children.get(pid, []))
        for pid, _ticks in list(target.clis):  # keep tracking orphans
            if pid not in out:
                out.append(pid)
        return out

    def _correlate_processes(self, target: Target) -> None:
        if target.lock_ino is None:
            return
        binding_dir = os.path.normpath(str(self.root / target.entry))
        for pid in self._candidate_pids(target):
            st = self.proc.stat(pid)
            if st is None:
                continue
            ident = (pid, st.start_ticks)
            if ident in target.clis:
                continue
            is_owner = pid == target.owner_pid and st.start_ticks == target.owner_ticks
            try:
                holds = self.proc.holds_inode(pid, target.lock_dev, target.lock_ino)
            except PermissionError:
                target.proc_denied = True
                self.emit_once(
                    (target.entry, f"denied:{pid}:fd"),
                    "process_access_denied",
                    entry=target.entry,
                    binding_id=target.binding_id,
                    pid=pid,
                    what="fd",
                )
                continue
            except OSError:
                continue
            if not holds or is_owner:
                continue
            try:
                env = self.proc.environ_select(pid, ENV_KEYS)
                argv = parse_argv_structure(self.proc.cmdline(pid))
            except PermissionError:
                target.proc_denied = True
                self.emit_once(
                    (target.entry, f"denied:{pid}:env"),
                    "process_access_denied",
                    entry=target.entry,
                    binding_id=target.binding_id,
                    pid=pid,
                    what="environ_or_cmdline",
                )
                continue
            except OSError:
                continue  # exited between checks
            config_path = env.get("OPENCLAW_CONFIG_PATH")
            state_dir = env.get("OPENCLAW_STATE_DIR")
            expected_identity = None
            if self.args.expected_cli_pid is not None:
                expected_identity = pid == self.args.expected_cli_pid and (
                    self.args.expected_cli_start_ticks is None
                    or st.start_ticks == self.args.expected_cli_start_ticks
                )
            record = {
                "role": "cli" if st.ppid == target.owner_pid else "cli_descendant",
                "config_path_ok": config_path is not None
                and os.path.normpath(config_path)
                == os.path.join(binding_dir, "openclaw.json"),
                "state_dir_ok": state_dir is not None
                and os.path.normpath(state_dir) == os.path.join(binding_dir, "state"),
                "config_is_persistent": config_path is not None
                and os.path.normpath(config_path)
                == os.path.normpath(self.args.persistent_config_path),
                "own_session": st.sid == pid and st.pgid == pid,
                "parent_is_owner": st.ppid == target.owner_pid,
                "expected_identity_match": expected_identity,
                **argv,
            }
            target.clis[ident] = record
            self.emit(
                "process_correlated",
                entry=target.entry,
                binding_id=target.binding_id,
                pid=pid,
                ppid=st.ppid,
                pgid=st.pgid,
                sid=st.sid,
                start_ticks=st.start_ticks,
                comm=_text_or_marker(self.proc.comm(pid)),
                exe=_text_or_marker(self.proc.exe_basename(pid)),
                config_path=_text_or_marker(config_path),
                state_dir=_text_or_marker(state_dir),
                holds_lock=True,
                **record,
            )

    def _track_cli_exit(self, target: Target) -> None:
        for ident in list(target.clis):
            if ident in target.exited:
                continue
            st = self.proc.stat(ident[0])
            if st is None or st.start_ticks != ident[1]:
                target.exited.add(ident)
                self.emit(
                    "process_exited",
                    entry=target.entry,
                    binding_id=target.binding_id,
                    pid=ident[0],
                    start_ticks=ident[1],
                    role="cli",
                )

    def _observe_removal(self, target: Target, locked: Optional[Set[int]]) -> None:
        self._track_cli_exit(target)
        target.removed = True
        lock_still = (
            None
            if locked is None or target.lock_ino is None
            else target.lock_ino in locked
        )
        target.removal_lock_held = lock_still
        cli_alive = any(ident not in target.exited for ident in target.clis)
        self.emit(
            "binding_removed",
            entry=target.entry,
            binding_id=target.binding_id,
            lock_still_held=lock_still,
            cli_alive=cli_alive,
            owner_alive=self._owner_alive(target),
        )

    # -- run --------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        start = self.clock()
        interrupted = False
        try:
            initial = self.root_entries()
            self.preexisting = set(initial or [])
            self.emit(
                "observer_started",
                mode=self.args.mode,
                binding_root=str(self.root),
                expected_task_execution_id=self.args.expected_task_execution_id,
                expected_runtime_workspace=self.args.expected_runtime_workspace,
                expected_agent_id=self.args.expected_agent_id,
                expected_model_ref=self.args.expected_model_ref,
                expected_cli_pid=self.args.expected_cli_pid,
                interval_seconds=self.args.interval,
                duration_seconds=self.args.duration,
                root_present=initial is not None,
                preexisting_entries=len(self.preexisting),
                openclaw_agent_processes=self.proc.openclaw_agent_processes(),
            )
            settled_at: Optional[float] = None
            while self.clock() - start < self.args.duration:
                self.poll()
                done = self.targets and all(
                    t.removed and all(i in t.exited for i in t.clis)
                    for t in self.targets.values()
                )
                if done:
                    settled_at = settled_at if settled_at is not None else self.clock()
                    if self.clock() - settled_at >= self.args.settle:
                        break
                else:
                    settled_at = None
                self.sleep(self.args.interval)
            else:
                self.notes.append("observation_window_elapsed")
        except KeyboardInterrupt:
            interrupted = True
            self.notes.append("interrupted_by_operator")
        except ObserverError:
            self.emit("observer_error", stage="poll", error_type="ObserverError")
            return self.finish(start, interrupted, error=True)
        return self.finish(start, interrupted)

    def _residue(self) -> List[Dict[str, Any]]:
        try:
            entries = self.root_entries() or []
        except ObserverError:
            self.notes.append("binding_root_unsafe_at_finish")
            return [{"entry": "<root>", "lock_held": None, "target": False}]
        locked = self.proc.locked_inodes()
        found = []
        for name in entries:
            if name in self.preexisting:
                continue
            path = self.root / name
            metadata, _ = read_metadata(path) if _DIR_NAME.match(name) else (None, "")
            if metadata:
                probe = probe_lock(path / BINDING_LOCK_NAME)
                held = True if probe == "held" else (False if probe == "free" else None)
            else:
                # No valid metadata: never probe (could race creation); use
                # passive /proc/locks evidence only, else unknown.
                held = None
                try:
                    lst = os.lstat(path / BINDING_LOCK_NAME)
                    if locked is not None and stat.S_ISREG(lst.st_mode):
                        held = lst.st_ino in locked
                except OSError:
                    pass
            item = {
                "entry": name if _DIR_NAME.match(name) else "<unexpected_name>",
                "binding_id": metadata.get("binding_id") if metadata else None,
                "lock_held": held,
                "metadata_valid": metadata is not None,
                "credential_copy_present": os.path.lexists(
                    path / "agent" / "auth-profiles.json"
                ),
                "target": name in self.targets,
            }
            self.emit("residue", **item)
            found.append(item)
        return found

    def finish(
        self, start: float, interrupted: bool, error: bool = False
    ) -> Dict[str, Any]:
        residue = [] if error else self._residue()
        checks, classification, verdict, targets = self.adjudicate(residue, error)
        processes = self.proc.openclaw_agent_processes()
        self.emit(
            "observer_finished",
            classification=classification,
            verdict=verdict,
            checks=checks,
            targets=targets,
            interrupted=interrupted,
            elapsed_seconds=round(self.clock() - start, 3),
            openclaw_agent_processes=processes,
            notes=self.notes,
        )
        summary = dict(self.events[-1])
        summary["event_count"] = len(self.events)
        self.close_output(summary)
        return summary

    def adjudicate(self, residue: List[Dict[str, Any]], error: bool):
        def tri(ok: Optional[bool]) -> str:
            return PASS if ok is True else (FAIL if ok is False else INCONCLUSIVE)

        if error:
            return {}, OBSERVATION_ERROR, OBSERVATION_ERROR, []
        targets_out = []
        primary: Optional[Target] = None
        for target in self.targets.values():
            correlated = [c for c in target.clis.values() if c["config_path_ok"]]
            targets_out.append(
                {
                    "entry": target.entry,
                    "binding_id": target.binding_id,
                    "lock_held_seen": target.lock_held_seen,
                    "clis": len(target.clis),
                    "cli_exited": len(target.exited),
                    "removed": target.removed,
                    "credential_copy_present": target.credential_copy_present,
                }
            )
            if primary is None and correlated:
                primary = target
        if primary is None and self.targets:
            primary = next(iter(self.targets.values()))
        checks: Dict[str, str] = {}
        if primary is None:
            checks = {"binding_identity": INCONCLUSIVE}
            classification = INCONCLUSIVE
            self.notes.append("target_binding_never_observed")
            if self.foreign:
                self.notes.append("foreign_bindings_observed")
            residue_fail = any(r["lock_held"] is False for r in residue)
            if any(r["lock_held"] is True for r in residue):
                self.notes.append("bindings_still_active_at_window_end")
            if any(r["lock_held"] is None for r in residue):
                self.notes.append("residue_lock_state_unknown")
            if residue_fail:
                checks["no_unexpected_residue"] = FAIL
                classification = RESIDUE_DETECTED
            elif residue:
                checks["no_unexpected_residue"] = INCONCLUSIVE
            verdict = FAIL if residue_fail else INCONCLUSIVE
            return checks, classification, verdict, targets_out

        clis = list(primary.clis.values())
        cli = clis[0] if clis else None
        cfg = primary.config
        checks["binding_identity"] = PASS
        checks["task_execution_correlation"] = PASS
        checks["lock_held"] = tri(True if primary.lock_held_seen else None)
        if cli is None:
            unknown = INCONCLUSIVE
            checks.update(
                lock_inherited_by_cli=unknown,
                ephemeral_config_path=unknown,
                ephemeral_state_dir=unknown,
                cli_agent_identity=unknown,
            )
            if primary.proc_denied:
                self.notes.append("proc_access_denied")
        else:
            checks["lock_inherited_by_cli"] = PASS
            checks["ephemeral_config_path"] = tri(
                all(c["config_path_ok"] for c in clis)
                and not any(c["config_is_persistent"] for c in clis)
            )
            checks["ephemeral_state_dir"] = tri(all(c["state_dir_ok"] for c in clis))
            direct = [c for c in clis if c["role"] == "cli"]
            argv_agents = [
                c["argv_agent"] for c in direct if c["argv_form"] == "original_argv"
            ]
            if any(a != self.args.expected_agent_id for a in argv_agents):
                checks["cli_agent_identity"] = FAIL
            elif argv_agents:
                checks["cli_agent_identity"] = PASS
            else:
                # OpenClaw rewrites the CLI title to "openclaw-agent" (argv is
                # zero-filled), so the --agent value was not captured.  The bound
                # config is a full copy that also lists other agents, so it cannot
                # prove the selection on its own: stay INCONCLUSIVE and report
                # corroboration separately for operator adjudication.
                checks["cli_agent_identity"] = INCONCLUSIVE
                self.notes.append("cli_argv_agent_not_captured_title_rewritten")
            checks["cli_agent_corroboration_agent_dir"] = tri(
                True if primary.agent_models_json_seen else None
            )
            if any(
                c["argv_local"] is False
                for c in direct
                if c["argv_form"] == "original_argv"
            ):
                checks["cli_local_flag"] = FAIL
            elif any(c["argv_local"] for c in direct):
                checks["cli_local_flag"] = PASS
            if self.args.expected_cli_pid is not None:
                checks["expected_cli_identity"] = tri(
                    any(c["expected_identity_match"] for c in clis)
                )
        checks["bound_agent_entry"] = tri(
            None if not cfg else cfg.get("agent_entries") == 1
        )
        checks["runtime_workspace"] = tri(cfg.get("workspace_matches") if cfg else None)
        checks["model_primary"] = tri(cfg.get("model_matches") if cfg else None)
        checks["fallbacks_empty"] = tri(cfg.get("fallbacks_empty") if cfg else None)
        all_exited = all(i in primary.exited for i in primary.clis)
        if primary.removed and all_exited and primary.removal_lock_held is not True:
            checks["release_observed"] = PASS
        elif primary.removed and not all_exited:
            checks["release_observed"] = FAIL
            self.notes.append("cli_alive_after_binding_removed")
        else:
            checks["release_observed"] = INCONCLUSIVE
        # Only a positively free lock is stale residue; held = still active,
        # unknown (no valid metadata, no passive evidence) = not adjudicable.
        stale = [r for r in residue if r["lock_held"] is False]
        undecided = [r for r in residue if r["lock_held"] is not False]
        if any(r["lock_held"] is True for r in residue):
            self.notes.append("bindings_still_active_at_window_end")
        if any(r["lock_held"] is None for r in residue):
            self.notes.append("residue_lock_state_unknown")
        checks["no_unexpected_residue"] = (
            FAIL if stale else (INCONCLUSIVE if undecided else PASS)
        )
        checks["no_cli_remaining"] = (
            tri(True if all_exited else None) if primary.removed else INCONCLUSIVE
        )
        if (
            checks["no_cli_remaining"] == INCONCLUSIVE
            and primary.removed
            and not all_exited
        ):
            checks["no_cli_remaining"] = FAIL

        values = set(checks.values())
        verdict = (
            FAIL if FAIL in values else (PASS if values == {PASS} else INCONCLUSIVE)
        )
        if stale or checks.get("release_observed") == FAIL:
            classification = RESIDUE_DETECTED
        elif checks["release_observed"] == PASS:
            classification = RELEASE_OBSERVED
        elif cli is not None:
            classification = CLI_CORRELATED
        elif primary.lock_held_seen:
            classification = BINDING_ACTIVE
        else:
            classification = BINDING_DISCOVERED
        return checks, classification, verdict, targets_out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("synthetic", "production"), required=True)
    parser.add_argument("--confirm-read-only-production", action="store_true")
    parser.add_argument("--binding-root", required=True)
    parser.add_argument("--expected-task-execution-id", type=int, required=True)
    parser.add_argument("--expected-runtime-workspace", required=True)
    parser.add_argument("--expected-agent-id", default=EPHEMERAL_AGENT_ID)
    parser.add_argument("--expected-model-ref", default=DEFAULT_MODEL_REF)
    parser.add_argument("--expected-cli-pid", type=int, default=None)
    parser.add_argument("--expected-cli-start-ticks", type=int, default=None)
    parser.add_argument(
        "--persistent-config-path",
        default=DEFAULT_PERSISTENT_CONFIG,
        help="Compared as a path only; never opened.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="Poll interval; short so the CLI argv can be seen before "
        "OpenClaw rewrites its title.",
    )
    parser.add_argument("--duration", type=float, default=900.0)
    parser.add_argument("--settle", type=float, default=5.0)
    parser.add_argument(
        "--no-lock-probe",
        action="store_true",
        help="Use only passive /proc/locks lock evidence.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 0.05 <= args.interval <= 10:
        raise ObserverError("interval must be within [0.05, 10] seconds")
    if not 0 < args.duration <= 4000:
        raise ObserverError("duration must be within (0, 4000] seconds")
    if not 0 <= args.settle <= 120:
        raise ObserverError("settle must be within [0, 120] seconds")
    if not _SAFE_ID.match(args.expected_agent_id) or not _SAFE_ID.match(
        args.expected_model_ref
    ):
        raise ObserverError("expected agent/model must be simple identifiers")
    for value in (args.binding_root, args.expected_runtime_workspace, args.output_dir):
        if not _SAFE_TEXT.match(value) or not os.path.isabs(value):
            raise ObserverError("paths must be absolute printable ASCII")
    is_prod_root = os.path.normpath(args.binding_root) == default_production_root()
    if args.mode == "synthetic" and is_prod_root:
        raise ObserverError("synthetic mode refuses the production binding root")
    if args.mode == "production" and not args.confirm_read_only_production:
        raise ObserverError("production mode requires --confirm-read-only-production")
    if args.mode == "synthetic" and args.confirm_read_only_production:
        raise ObserverError(
            "--confirm-read-only-production is only valid in production mode"
        )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        observer = Observer(args)
        observer.open_output()
    except ObserverError as exc:
        print(
            json.dumps(
                {
                    "event": "observer_error",
                    "stage": "startup",
                    "error_type": "ObserverError",
                    "detail": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return EXIT_CODES[OBSERVATION_ERROR]
    except OSError as exc:
        print(
            json.dumps(
                {
                    "event": "observer_error",
                    "stage": "startup",
                    "error_type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return EXIT_CODES[OBSERVATION_ERROR]
    try:
        summary = observer.run()
    except Exception as exc:  # noqa: BLE001 - never leak data in errors
        print(
            json.dumps(
                {
                    "event": "observer_error",
                    "stage": "run",
                    "error_type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return EXIT_CODES[OBSERVATION_ERROR]
    print(
        json.dumps(
            {k: summary[k] for k in ("classification", "verdict", "checks")},
            sort_keys=True,
        )
    )
    return EXIT_CODES.get(summary["verdict"], EXIT_CODES[INCONCLUSIVE])


if __name__ == "__main__":
    sys.exit(main())
