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
``--mode synthetic`` refuses the default production binding root and database.

Two start modes (R1):

* late start: ``--expected-task-execution-id`` is known; a binding already
  present for it is adopted but flagged (``observed_from_creation`` cannot
  PASS);
* pre-dispatch arming (``--arm``): started BEFORE the dispatch with the
  session/task/project ids; validates targets, writes ``g6-armed.json`` and
  discovers the ``TaskExecution`` itself through a strictly read-only SQLite
  connection (``mode=ro`` + ``query_only``; fixed SELECTs on ids, attempt
  numbers, statuses and worker identity only).  It never initiates dispatch.

With a database, the observer also follows the execution to a terminal state
(``terminal_state``) and reports retries; it never cancels or signals.

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
import sqlite3
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
DEFAULT_PRODUCTION_DB = "/root/.openclaw/workspace/vault/orchestrator/orchestrator.db"
ENV_KEYS = ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR")
MAX_DURATION_SECONDS = 15000  # 4 Celery attempts x 3600 s hard limit + margin

# TaskExecution.status is persisted as the TaskStatus member name.
TERMINAL_EXECUTION_STATUSES = frozenset({"DONE", "FAILED", "CANCELLED"})
ACTIVE_EXECUTION_STATUSES = frozenset({"PENDING", "RUNNING"})
ACTIVE_SESSION_STATUSES = frozenset({"running", "active"})

# Fixed read-only statements (pinned by the test suite against app.models).
_SQL_SCHEMA_PROBES = (
    "SELECT id, session_id, task_id, attempt_number, status, worker_pid, "
    "worker_process_start_identity FROM task_executions WHERE 0",
    "SELECT id, project_id, status, continuation_kind, continuation_retry_count "
    "FROM sessions WHERE 0",
    "SELECT id, project_id FROM tasks WHERE 0",
)
_SQL_SESSION = (
    "SELECT project_id, status, continuation_kind, continuation_retry_count "
    "FROM sessions WHERE id = ?"
)
_SQL_TASK_PROJECT = "SELECT project_id FROM tasks WHERE id = ?"
_SQL_EXECUTIONS = (
    "SELECT id, attempt_number, status, worker_pid, worker_process_start_identity "
    "FROM task_executions WHERE session_id = ? AND task_id = ? ORDER BY id"
)
_SQL_EXECUTION_OWNER = "SELECT session_id, task_id FROM task_executions WHERE id = ?"
_SQL_QUERY_ONLY = "PRAGMA query_only = ON"

# Evidence strength for each link of the CLI identity chain (R1).
DIRECTLY_OBSERVED = "DIRECTLY_OBSERVED"
CORROBORATED = "CORROBORATED"
CONTRADICTED = "CONTRADICTED"
_EVIDENCE_RANK = {
    CONTRADICTED: 0,
    "INCONCLUSIVE": 1,
    CORROBORATED: 2,
    DIRECTLY_OBSERVED: 3,
}

# Terminal-state classifications (observer completion is not execution
# completion).
OBSERVATION_WINDOW_ENDED = "OBSERVATION_WINDOW_ENDED"
EXECUTION_TERMINAL = "EXECUTION_TERMINAL"
EXECUTION_STILL_ACTIVE = "EXECUTION_STILL_ACTIVE"
RETRY_DETECTED = "RETRY_DETECTED"
TERMINAL_STATE_INCONCLUSIVE = "TERMINAL_STATE_INCONCLUSIVE"

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
        "armed_mode",
        "db_enabled",
        "expected_session_id",
        "expected_task_id",
        "expected_project_id",
        "expected_runtime_root",
    ),
    "observer_armed": (
        "expected_session_id",
        "expected_task_id",
        "expected_project_id",
        "session_status",
        "existing_executions",
        "root_present",
        "preexisting_entries",
        "openclaw_agent_processes",
        "arm_timeout_seconds",
    ),
    "execution_discovered": (
        "task_execution_id",
        "attempt_number",
        "status",
        "worker_pid",
    ),
    "execution_state": ("task_execution_id", "status", "session_status"),
    "retry_detected": ("task_execution_id", "attempt_number", "reason"),
    "binding_deferred": ("entry", "reason"),
    "lock_holder_pending": ("entry", "binding_id", "pid", "start_ticks"),
    "db_read_error": ("stage", "error_type"),
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
        "discovered_at_start",
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
        "evidence_chain",
        "cli_identity_evidence",
        "terminal_state",
        "executions",
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


class ExecutionDb:
    """Strictly read-only view of the Orchestrator SQLite database.

    ``mode=ro`` URI plus ``PRAGMA query_only``; autocommit, so no read
    transaction is held between polls.  Only the fixed ``_SQL_*`` SELECTs run,
    and only ids, attempt numbers, statuses and worker identity are read --
    never prompts, logs, descriptions or settings.
    """

    def __init__(self, path: str) -> None:
        if any(ch in path for ch in "?#%"):
            raise ObserverError("database path must not contain URI characters")
        try:
            self.conn = sqlite3.connect(
                "file:%s?mode=ro" % path,
                uri=True,
                timeout=1.0,
                isolation_level=None,
            )
        except sqlite3.Error:
            raise ObserverError("database could not be opened read-only") from None
        try:
            self.conn.execute(_SQL_QUERY_ONLY)
            for probe in _SQL_SCHEMA_PROBES:
                self.conn.execute(probe).fetchall()
        except sqlite3.Error:
            self.conn.close()
            raise ObserverError("database schema does not match") from None

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _str(value: Any) -> Optional[str]:
        return _text_or_marker(value, _SAFE_ID) if isinstance(value, str) else None

    def session(self, session_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(_SQL_SESSION, (session_id,)).fetchone()
        if row is None:
            return None
        return {
            "project_id": self._int(row[0]),
            "status": (self._str(row[1]) or "").lower() or None,
            "continuation_kind": self._str(row[2]),
            "continuation_retry_count": self._int(row[3]),
        }

    def task_project(self, task_id: int) -> Optional[int]:
        row = self.conn.execute(_SQL_TASK_PROJECT, (task_id,)).fetchone()
        return None if row is None else self._int(row[0])

    def executions(self, session_id: int, task_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(_SQL_EXECUTIONS, (session_id, task_id)).fetchall()
        return [
            {
                "id": self._int(r[0]),
                "attempt_number": self._int(r[1]),
                "status": (self._str(r[2]) or "").upper() or None,
                "worker_pid": self._int(r[3]),
                "worker_identity": r[4] if isinstance(r[4], str) else None,
            }
            for r in rows
        ]

    def execution_owner(self, execution_id: int) -> Optional[Tuple[int, int]]:
        row = self.conn.execute(_SQL_EXECUTION_OWNER, (execution_id,)).fetchone()
        return None if row is None else (row[0], row[1])


# --------------------------------------------------------------------------
# Observer
# --------------------------------------------------------------------------


@dataclass
class Target:
    entry: str
    binding_id: str
    owner_pid: Optional[int]
    owner_ticks: Optional[int]
    task_execution_id: Optional[int] = None
    owner_boot_id: Optional[str] = None
    owner_alive_at_discovery: Optional[bool] = None
    discovered_at_start: bool = False
    lock_dev: Optional[int] = None
    lock_ino: Optional[int] = None
    lock_held_seen: bool = False
    lock_free_seen_while_present: bool = False
    config: Dict[str, Any] = field(default_factory=dict)
    clis: Dict[Tuple[int, int], Dict[str, Any]] = field(default_factory=dict)
    exited: Set[Tuple[int, int]] = field(default_factory=set)
    pending: Set[Tuple[int, int]] = field(default_factory=set)
    removed: bool = False
    removal_lock_held: Optional[bool] = None
    proc_denied: bool = False
    credential_copy_present: Optional[bool] = None
    agent_models_json_seen: bool = False

    def release_state(self) -> Optional[bool]:
        """True: removed, every CLI gone and the lock positively free.
        False: removed while a correlated CLI is alive.  None: not proven."""
        all_exited = all(i in self.exited for i in self.clis)
        if self.removed and not all_exited:
            return False
        if self.removed and self.removal_lock_held is False:
            return True
        return None


def _tri(ok: Optional[bool]) -> str:
    return PASS if ok is True else (FAIL if ok is False else INCONCLUSIVE)


def _combine(values: List[Optional[bool]]) -> str:
    """FAIL if any False; PASS only if non-empty and all True."""
    if any(v is False for v in values):
        return FAIL
    if values and all(v is True for v in values):
        return PASS
    return INCONCLUSIVE


class Observer:
    def __init__(
        self,
        args: argparse.Namespace,
        proc: Optional[ProcReader] = None,
        clock=time.monotonic,
        sleep=time.sleep,
        announce=None,
    ) -> None:
        self.args = args
        self.proc = proc or ProcReader(args.proc_root)
        self.clock = clock
        self.sleep = sleep
        self.announce = announce
        self.root = Path(args.binding_root)
        self.targets: Dict[str, Target] = {}
        self.foreign: Set[str] = set()
        self.reported: Set[Tuple[str, str]] = set()
        self.preexisting: Set[str] = set()
        self.at_start: Set[str] = set()
        self.notes: List[str] = []
        self.events: List[Dict[str, Any]] = []
        self.out_dir = Path(args.output_dir)
        self._jsonl_fd: Optional[int] = None
        self.boot_id = self.proc.boot_id()
        self.pid_ns = self.proc.pid_namespace()
        # Execution correlation (database modes only).
        self.db: Optional[ExecutionDb] = None
        self.session_id: Optional[int] = getattr(args, "expected_session_id", None)
        self.task_id: Optional[int] = getattr(args, "expected_task_id", None)
        self.executions: Dict[int, Dict[str, Any]] = {}
        self.worker_identity: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
        self.session_row: Optional[Dict[str, Any]] = None
        self.retry_reasons: Set[str] = set()
        self.db_errors = 0
        self.final_db_ok = True
        self.armed_at: Optional[float] = None
        self.window_elapsed = False
        self.arm_timed_out = False

    @property
    def armed(self) -> bool:
        return bool(getattr(self.args, "arm", False))

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

    def _write_private(self, name: str, payload: Dict[str, Any]) -> None:
        fd = os.open(
            self.out_dir / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")

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
            self._write_private("g6-summary.json", summary)

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

    # -- execution database (read-only) -----------------------------------
    def open_db(self) -> None:
        if getattr(self.args, "db_path", None):
            self.db = ExecutionDb(self.args.db_path)

    def _db_error(self, stage: str, exc: BaseException) -> None:
        self.db_errors += 1
        self.emit_once(
            ("db", "error:" + stage),
            "db_read_error",
            stage=stage,
            error_type=type(exc).__name__,
        )

    def _expected_workspace(self, task_execution_id: Optional[int]) -> Optional[str]:
        if not self.armed:
            return self.args.expected_runtime_workspace
        if task_execution_id is None:
            return None
        return os.path.join(
            self.args.expected_runtime_root,
            "tasks",
            str(self.args.expected_project_id),
            str(task_execution_id),
        )

    def _setup_execution_scope(self) -> None:
        """Late start with a database: derive session/task from the row."""
        try:
            owner = self.db.execution_owner(self.args.expected_task_execution_id)
        except sqlite3.Error:
            raise ObserverError("database read failed at startup") from None
        if owner is None:
            raise ObserverError("expected task execution not found")
        self.session_id, self.task_id = owner

    def _arm(self, initial: Optional[List[str]]) -> None:
        """Validate the pre-dispatch targets; fail closed before ARMED."""
        try:
            session = self.db.session(self.session_id)
            task_project = self.db.task_project(self.task_id)
            rows = self.db.executions(self.session_id, self.task_id)
        except sqlite3.Error:
            raise ObserverError("database read failed while arming") from None
        if session is None or session["project_id"] != self.args.expected_project_id:
            raise ObserverError("session does not belong to the expected project")
        if task_project != self.args.expected_project_id:
            raise ObserverError("task does not belong to the expected project")
        if rows:
            raise ObserverError(
                "executions already exist; arming must precede dispatch"
            )
        self.session_row = session
        self.emit(
            "observer_armed",
            expected_session_id=self.session_id,
            expected_task_id=self.task_id,
            expected_project_id=self.args.expected_project_id,
            session_status=session["status"],
            existing_executions=0,
            root_present=initial is not None,
            preexisting_entries=len(self.preexisting),
            openclaw_agent_processes=self.proc.openclaw_agent_processes(),
            arm_timeout_seconds=self.args.arm_timeout,
        )
        record = dict(self.events[-1])
        self._write_private("g6-armed.json", record)
        self.armed_at = self.clock()
        if self.announce is not None:
            self.announce(record)

    def _poll_db(self) -> None:
        if self.db is None or self.session_id is None:
            return
        try:
            rows = self.db.executions(self.session_id, self.task_id)
            session = self.db.session(self.session_id)
        except sqlite3.Error as exc:
            self._db_error("poll", exc)
            return
        session_status = session["status"] if session else None
        if session is not None:
            self.session_row = session
            if (session.get("continuation_retry_count") or 0) > 0:
                self._retry(None, None, "session_continuation_retry")
        for row in rows:
            eid = row["id"]
            if eid is None:
                continue
            if row["worker_pid"] is not None and row["worker_identity"]:
                # Cleared again when runtime ownership is released: latch it.
                self.worker_identity[eid] = (row["worker_pid"], row["worker_identity"])
            known = self.executions.get(eid)
            if known is None:
                self.emit(
                    "execution_discovered",
                    task_execution_id=eid,
                    attempt_number=row["attempt_number"],
                    status=row["status"],
                    worker_pid=row["worker_pid"],
                )
                if self.executions:
                    self._retry(eid, row["attempt_number"], "additional_execution_row")
                if row["attempt_number"] not in (1, None):
                    self._retry(eid, row["attempt_number"], "attempt_number_above_one")
            if known is None or known["status"] != row["status"]:
                self.emit(
                    "execution_state",
                    task_execution_id=eid,
                    status=row["status"],
                    session_status=session_status,
                )
            self.executions[eid] = {
                "id": eid,
                "attempt_number": row["attempt_number"],
                "status": row["status"],
            }

    def _retry(self, eid: Optional[int], attempt: Optional[int], reason: str) -> None:
        self.retry_reasons.add(reason)
        self.emit_once(
            ("retry", "%s:%s" % (reason, eid)),
            "retry_detected",
            task_execution_id=eid,
            attempt_number=attempt,
            reason=reason,
        )

    def _membership(self, name: str, metadata: Dict[str, Any]) -> str:
        """target | foreign | defer for a binding with valid metadata."""
        eid = metadata.get("task_execution_id")
        if self.db is None or self.session_id is None:
            return (
                "target"
                if eid is not None and eid == self.args.expected_task_execution_id
                else "foreign"
            )
        if eid in self.executions:
            return "target"
        if not isinstance(eid, int):
            return "foreign"
        try:
            owner = self.db.execution_owner(eid)
        except sqlite3.Error as exc:
            self._db_error("membership", exc)
            return "defer"
        if owner is None:
            return "defer"  # row not yet visible to this reader
        if owner != (self.session_id, self.task_id):
            return "foreign"
        self._poll_db()
        return "target" if eid in self.executions else "defer"

    # -- one poll ---------------------------------------------------------
    def poll(self) -> None:
        self._poll_db()
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
                self._recheck_removed_lock(target, locked)

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
        membership = self._membership(name, metadata)
        if membership == "defer":
            self.emit_once(
                (name, "deferred"),
                "binding_deferred",
                entry=name,
                reason="execution_row_not_yet_correlated",
            )
            return
        if membership == "foreign":
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
            task_execution_id=metadata.get("task_execution_id"),
            owner_boot_id=metadata.get("boot_id"),
            discovered_at_start=name in self.at_start,
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
        boot_ok = (
            metadata.get("boot_id") is not None
            and metadata.get("boot_id") == self.boot_id
        )
        ns_ok = (
            metadata.get("owner_pid_namespace") is not None
            and metadata.get("owner_pid_namespace") == self.pid_ns
        )
        owner_alive = self._owner_alive(target)
        target.owner_alive_at_discovery = bool(owner_alive and boot_ok and ns_ok)
        self.targets[name] = target
        self.emit(
            "binding_discovered",
            entry=name,
            binding_id=target.binding_id,
            task_execution_id=target.task_execution_id,
            owner_pid=owner_pid,
            owner_pid_start_ticks=owner_ticks,
            owner_alive=owner_alive,
            boot_id_matches=boot_ok,
            pid_namespace_matches=ns_ok,
            owner_hostname=metadata.get("owner_hostname"),
            created_at_epoch=metadata.get("created_at_epoch"),
            lock_inode=target.lock_ino,
            credential_copy_present=target.credential_copy_present,
            discovered_at_start=target.discovered_at_start,
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
        expected_workspace = self._expected_workspace(target.task_execution_id)
        evaluable = info.get("agent_entries") == 1
        target.config = {
            "agent_entries": info.get("agent_entries"),
            # Not evaluable (None -> INCONCLUSIVE) unless exactly one entry;
            # the entry-count FAIL is carried by bound_agent_entry.
            "workspace_matches": (
                (
                    workspace is not None
                    and expected_workspace is not None
                    and os.path.normpath(workspace)
                    == os.path.normpath(expected_workspace)
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

    def _is_pre_exec_child(self, target: Target, pid: int, st: ProcStat) -> bool:
        """A direct child whose comm, exe AND cmdline are still identical to
        the owner's has been forked but has not exec'd the CLI yet: its
        environ is still the Worker's.  These are read BEFORE environ, so
        once any of them differs every later read is post-exec.  The cmdline
        bytes are compared in memory only, never recorded."""
        if st.ppid != target.owner_pid:
            return False
        try:
            owner = (
                self.proc.comm(target.owner_pid),
                self.proc.exe_basename(target.owner_pid),
                self.proc.cmdline(target.owner_pid),
            )
            child = (
                self.proc.comm(pid),
                self.proc.exe_basename(pid),
                self.proc.cmdline(pid),
            )
        except OSError:
            return False
        if None in owner[:2]:
            return False
        return owner == child

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
            if self._is_pre_exec_child(target, pid, st):
                target.pending.add(ident)
                self.emit_once(
                    (target.entry, f"pending:{pid}:{st.start_ticks}"),
                    "lock_holder_pending",
                    entry=target.entry,
                    binding_id=target.binding_id,
                    pid=pid,
                    start_ticks=st.start_ticks,
                )
                continue  # re-examined next poll, after exec
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

    def _removed_lock_state(
        self, target: Target, locked: Optional[Set[int]]
    ) -> Optional[bool]:
        if locked is None or target.lock_ino is None:
            return None
        return target.lock_ino in locked

    def _observe_removal(self, target: Target, locked: Optional[Set[int]]) -> None:
        self._track_cli_exit(target)
        target.removed = True
        lock_still = self._removed_lock_state(target, locked)
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

    def _recheck_removed_lock(self, target: Target, locked: Optional[Set[int]]) -> None:
        """release() removes the directory BEFORE closing the lock, so the
        first post-removal poll can still see the deleted inode locked."""
        if target.removal_lock_held is False:
            return
        held = self._removed_lock_state(target, locked)
        if held != target.removal_lock_held:
            target.removal_lock_held = held
            self.emit_once(
                (target.entry, f"removed_lock:{held}"),
                "lock_state",
                entry=target.entry,
                binding_id=target.binding_id,
                lock_held=held,
                method="proc_locks",
                detail="after_removal",
            )

    # -- run --------------------------------------------------------------
    def _adopt_at_start(self, initial: List[str]) -> None:
        """Late start: a binding already present for the expected execution
        is adopted (flagged); everything else stays pre-existing."""
        for name in initial:
            path = self.root / name
            if not _DIR_NAME.match(name) or os.path.islink(path):
                self.preexisting.add(name)
                continue
            metadata, problem = read_metadata(path)
            if metadata is None and problem == "missing":
                self.at_start.add(name)  # initializing now: classify next poll
            elif (
                metadata is not None
                and metadata.get("task_execution_id")
                == self.args.expected_task_execution_id
            ):
                self.at_start.add(name)
            else:
                self.preexisting.add(name)

    def _alive(self, ident: Tuple[int, int]) -> bool:
        st = self.proc.stat(ident[0])
        return st is not None and st.start_ticks == ident[1]

    def _settled(self) -> bool:
        if not self.targets:
            return False
        for target in self.targets.values():
            if target.release_state() is not True:
                return False
            if any(self._alive(i) for i in target.pending - set(target.clis)):
                return False
        if self.db is not None:
            if not self.executions or self.retry_reasons:
                return False
            if any(
                r["status"] not in TERMINAL_EXECUTION_STATUSES
                for r in self.executions.values()
            ):
                return False
        return True

    def run(self) -> Dict[str, Any]:
        start = self.clock()
        interrupted = False
        stage = "start"
        try:
            initial = self.root_entries()
            if self.armed:
                self.preexisting = set(initial or [])
            else:
                if self.db is not None:
                    self._setup_execution_scope()
                self._adopt_at_start(initial or [])
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
                armed_mode=self.armed,
                db_enabled=self.db is not None,
                expected_session_id=self.session_id,
                expected_task_id=self.task_id,
                expected_project_id=getattr(self.args, "expected_project_id", None),
                expected_runtime_root=getattr(self.args, "expected_runtime_root", None),
            )
            if self.armed:
                stage = "arm"
                self._arm(initial)
            stage = "poll"
            settled_at: Optional[float] = None
            while self.clock() - start < self.args.duration:
                self.poll()
                if (
                    self.armed
                    and not self.executions
                    and self.clock() - self.armed_at >= self.args.arm_timeout
                ):
                    self.arm_timed_out = True
                    self.notes.append("dispatch_not_observed_within_arm_timeout")
                    break
                if self._settled():
                    settled_at = settled_at if settled_at is not None else self.clock()
                    if self.clock() - settled_at >= self.args.settle:
                        break
                else:
                    settled_at = None
                self.sleep(self.args.interval)
            else:
                self.window_elapsed = True
                self.notes.append("observation_window_elapsed")
        except KeyboardInterrupt:
            interrupted = True
            self.notes.append("interrupted_by_operator")
        except ObserverError:
            self.emit("observer_error", stage=stage, error_type="ObserverError")
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
        if self.db is not None and not error:
            errors_before = self.db_errors
            self._poll_db()  # final read-only status refresh
            self.final_db_ok = self.db_errors == errors_before
        residue = [] if error else self._residue()
        checks, classification, verdict, targets = self.adjudicate(residue, error)
        evidence = {} if error else self.evidence_chain(checks)
        overall = self._weakest(list(evidence.values())) if evidence else INCONCLUSIVE
        terminal = TERMINAL_STATE_INCONCLUSIVE if error else self.terminal_state()
        if verdict == PASS and overall != DIRECTLY_OBSERVED:
            verdict = INCONCLUSIVE  # never promote corroboration to PASS
        if self.db_errors:
            self.notes.append("db_read_errors:%d" % self.db_errors)
        if self.db is not None:
            self.db.close()
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
            evidence_chain=evidence,
            cli_identity_evidence=overall,
            terminal_state=terminal,
            executions=[self.executions[k] for k in sorted(self.executions)],
        )
        summary = dict(self.events[-1])
        summary["event_count"] = len(self.events)
        self.close_output(summary)
        return summary

    def adjudicate(self, residue: List[Dict[str, Any]], error: bool):
        if error:
            return {}, OBSERVATION_ERROR, OBSERVATION_ERROR, []
        targets = list(self.targets.values())
        targets_out = [
            {
                "entry": t.entry,
                "binding_id": t.binding_id,
                "task_execution_id": t.task_execution_id,
                "lock_held_seen": t.lock_held_seen,
                "clis": len(t.clis),
                "cli_exited": len(t.exited),
                "removed": t.removed,
                "discovered_at_start": t.discovered_at_start,
                "credential_copy_present": t.credential_copy_present,
            }
            for t in targets
        ]
        checks: Dict[str, str] = {}
        if not targets:
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

        if len(targets) > 1:
            self.notes.append("multiple_target_bindings:%d" % len(targets))
        clis = [c for t in targets for c in t.clis.values()]
        direct = [c for c in clis if c["role"] == "cli"]
        checks["binding_identity"] = PASS
        checks["task_execution_correlation"] = PASS
        # A binding already present when observation began may have had an
        # earlier CLI (and its argv) that was never seen.
        checks["observed_from_creation"] = _tri(
            None if any(t.discovered_at_start for t in targets) else True
        )
        checks["lock_held"] = _combine(
            [True if t.lock_held_seen else None for t in targets]
        )
        if not clis:
            unknown = INCONCLUSIVE
            checks.update(
                lock_inherited_by_cli=unknown,
                ephemeral_config_path=unknown,
                ephemeral_state_dir=unknown,
                cli_agent_identity=unknown,
            )
            if any(t.proc_denied for t in targets):
                self.notes.append("proc_access_denied")
        else:
            checks["lock_inherited_by_cli"] = PASS
            if any(c["config_is_persistent"] for c in clis):
                checks["ephemeral_config_path"] = FAIL
            else:
                checks["ephemeral_config_path"] = _combine(
                    [c["config_path_ok"] for c in direct]
                )
            checks["ephemeral_state_dir"] = _combine(
                [c["state_dir_ok"] for c in direct]
            )
            captured = [c for c in direct if c["argv_form"] == "original_argv"]
            if any(c["argv_agent"] != self.args.expected_agent_id for c in captured):
                checks["cli_agent_identity"] = FAIL
            elif direct and len(captured) == len(direct):
                checks["cli_agent_identity"] = PASS
            else:
                # OpenClaw rewrites the CLI title to "openclaw-agent" (argv is
                # zero-filled), so the --agent value was not captured for every
                # direct CLI.  The bound config is a full copy that also lists
                # other agents, so it cannot prove the selection on its own:
                # stay INCONCLUSIVE and report corroboration separately.
                checks["cli_agent_identity"] = INCONCLUSIVE
                self.notes.append("cli_argv_agent_not_captured_title_rewritten")
            checks["cli_agent_corroboration_agent_dir"] = _tri(
                True if any(t.agent_models_json_seen for t in targets) else None
            )
            if any(c["argv_local"] is False for c in captured):
                checks["cli_local_flag"] = FAIL
            elif any(c["argv_local"] for c in direct):
                checks["cli_local_flag"] = PASS
            if self.args.expected_cli_pid is not None:
                checks["expected_cli_identity"] = _tri(
                    any(c["expected_identity_match"] for c in clis)
                )
        unresolved = [t for t in targets if t.pending - set(t.clis)]
        checks["lock_holders_resolved"] = PASS if not unresolved else INCONCLUSIVE
        if unresolved:
            self.notes.append("lock_holder_never_resolved_after_fork")

        def config_values(key: str) -> List[Optional[bool]]:
            return [t.config.get(key) if t.config else None for t in targets]

        checks["bound_agent_entry"] = _combine(
            [
                None if not t.config else t.config.get("agent_entries") == 1
                for t in targets
            ]
        )
        checks["runtime_workspace"] = _combine(config_values("workspace_matches"))
        checks["model_primary"] = _combine(config_values("model_matches"))
        checks["fallbacks_empty"] = _combine(config_values("fallbacks_empty"))
        releases = [t.release_state() for t in targets]
        checks["release_observed"] = _combine(releases)
        if any(r is False for r in releases):
            self.notes.append("cli_alive_after_binding_removed")
        checks["no_cli_remaining"] = _combine(
            [
                (all(i in t.exited for i in t.clis) if t.removed else None)
                for t in targets
            ]
        )
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
        if self.db is not None:
            checks["worker_identity_correlation"] = _combine(
                [self._worker_matches(t) for t in targets]
            )
            checks["single_attempt"] = (
                PASS
                if len(self.executions) == 1 and not self.retry_reasons
                else INCONCLUSIVE
            )
            checks["execution_terminal"] = (
                PASS if self.terminal_state() == EXECUTION_TERMINAL else INCONCLUSIVE
            )

        values = set(checks.values())
        verdict = (
            FAIL if FAIL in values else (PASS if values == {PASS} else INCONCLUSIVE)
        )
        if stale or checks["release_observed"] == FAIL:
            classification = RESIDUE_DETECTED
        elif checks["release_observed"] == PASS:
            classification = RELEASE_OBSERVED
        elif clis:
            classification = CLI_CORRELATED
        elif any(t.lock_held_seen for t in targets):
            classification = BINDING_ACTIVE
        else:
            classification = BINDING_DISCOVERED
        return checks, classification, verdict, targets_out

    def _worker_matches(self, target: Target) -> Optional[bool]:
        """Binding owner vs. the TaskExecution's recorded worker identity
        (``<boot_id>:<start_ticks>``).  None when either side is unknown."""
        latched = self.worker_identity.get(target.task_execution_id)
        if latched is None or target.owner_pid is None or target.owner_ticks is None:
            return None
        if target.owner_boot_id is None:
            return None
        pid, identity = latched
        return pid == target.owner_pid and identity == "%s:%s" % (
            target.owner_boot_id,
            target.owner_ticks,
        )

    @staticmethod
    def _weakest(labels: List[str]) -> str:
        if not labels:
            return INCONCLUSIVE
        return min(labels, key=lambda label: _EVIDENCE_RANK.get(label, 1))

    def evidence_chain(self, checks: Dict[str, str]) -> Dict[str, str]:
        """TaskExecution -> binding -> Worker -> CLI -> ephemeral env ->
        agent config -> agent selection -> lock lifetime.  Corroboration is
        labelled as such and never counts as direct observation."""

        def from_checks(*names: str) -> str:
            values = [checks.get(n, INCONCLUSIVE) for n in names]
            if FAIL in values:
                return CONTRADICTED
            if all(v == PASS for v in values):
                return DIRECTLY_OBSERVED
            return INCONCLUSIVE

        targets = list(self.targets.values())
        clis = [c for t in targets for c in t.clis.values()]
        chain: Dict[str, str] = {}
        if not targets:
            chain["execution_to_binding"] = INCONCLUSIVE
        elif self.db is not None:
            chain["execution_to_binding"] = DIRECTLY_OBSERVED  # metadata + DB row
        else:
            chain["execution_to_binding"] = DIRECTLY_OBSERVED  # metadata
        worker = checks.get("worker_identity_correlation")
        if worker == FAIL:
            chain["binding_to_worker"] = CONTRADICTED
        elif targets and all(t.owner_alive_at_discovery for t in targets):
            chain["binding_to_worker"] = DIRECTLY_OBSERVED
        else:
            chain["binding_to_worker"] = INCONCLUSIVE
        if any(c["role"] == "cli" and c["parent_is_owner"] for c in clis):
            chain["worker_to_cli"] = DIRECTLY_OBSERVED
        elif clis:
            chain["worker_to_cli"] = CORROBORATED
        else:
            chain["worker_to_cli"] = INCONCLUSIVE
        chain["cli_to_ephemeral_env"] = from_checks(
            "ephemeral_config_path", "ephemeral_state_dir"
        )
        chain["env_to_agent_config"] = from_checks(
            "bound_agent_entry", "runtime_workspace", "model_primary", "fallbacks_empty"
        )
        identity = checks.get("cli_agent_identity", INCONCLUSIVE)
        if identity == PASS:
            chain["cli_agent_selection"] = DIRECTLY_OBSERVED
        elif identity == FAIL:
            chain["cli_agent_selection"] = CONTRADICTED
        elif (
            checks.get("cli_agent_corroboration_agent_dir") == PASS
            and chain["cli_to_ephemeral_env"] == DIRECTLY_OBSERVED
            and checks.get("lock_inherited_by_cli") == PASS
        ):
            chain["cli_agent_selection"] = CORROBORATED
        else:
            chain["cli_agent_selection"] = INCONCLUSIVE
        chain["lock_lifetime"] = from_checks(
            "lock_held", "lock_inherited_by_cli", "release_observed"
        )
        return chain

    def terminal_state(self) -> str:
        if self.db is None:
            return (
                OBSERVATION_WINDOW_ENDED
                if self.window_elapsed
                else TERMINAL_STATE_INCONCLUSIVE
            )
        if self.retry_reasons:
            return RETRY_DETECTED
        if not self.executions:
            if self.window_elapsed or self.arm_timed_out:
                return OBSERVATION_WINDOW_ENDED
            return TERMINAL_STATE_INCONCLUSIVE
        if not self.final_db_ok:
            return TERMINAL_STATE_INCONCLUSIVE
        statuses = [r["status"] for r in self.executions.values()]
        if all(s in TERMINAL_EXECUTION_STATUSES for s in statuses):
            session_status = (self.session_row or {}).get("status")
            runtime_active = any(
                t.release_state() is not True for t in self.targets.values()
            )
            if session_status in ACTIVE_SESSION_STATUSES or runtime_active:
                return TERMINAL_STATE_INCONCLUSIVE
            return EXECUTION_TERMINAL
        if any(s in ACTIVE_EXECUTION_STATUSES for s in statuses):
            return EXECUTION_STILL_ACTIVE
        return TERMINAL_STATE_INCONCLUSIVE


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
    parser.add_argument("--expected-task-execution-id", type=int, default=None)
    parser.add_argument("--expected-runtime-workspace", default=None)
    parser.add_argument(
        "--arm",
        action="store_true",
        help="Pre-dispatch arming: start BEFORE the dispatch; the observer "
        "discovers the TaskExecution itself (read-only) and never dispatches.",
    )
    parser.add_argument("--confirm-pre-dispatch-arming", action="store_true")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Orchestrator SQLite database, opened mode=ro + query_only. "
        "Required with --arm; optional otherwise (terminal-state tracking).",
    )
    parser.add_argument("--expected-session-id", type=int, default=None)
    parser.add_argument("--expected-task-id", type=int, default=None)
    parser.add_argument("--expected-project-id", type=int, default=None)
    parser.add_argument(
        "--expected-runtime-root",
        default=None,
        help="With --arm: the expected workspace is "
        "<root>/tasks/<project_id>/<task_execution_id>.",
    )
    parser.add_argument("--arm-timeout", type=float, default=600.0)
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
    if not 0 < args.duration <= MAX_DURATION_SECONDS:
        raise ObserverError("duration must be within (0, 15000] seconds")
    if not 0 < args.arm_timeout <= 3600:
        raise ObserverError("arm timeout must be within (0, 3600] seconds")
    if not 0 <= args.settle <= 120:
        raise ObserverError("settle must be within [0, 120] seconds")
    if not _SAFE_ID.match(args.expected_agent_id) or not _SAFE_ID.match(
        args.expected_model_ref
    ):
        raise ObserverError("expected agent/model must be simple identifiers")
    if args.arm:
        if args.expected_task_execution_id is not None or (
            args.expected_runtime_workspace is not None
        ):
            raise ObserverError(
                "--arm discovers the execution; omit --expected-task-execution-id "
                "and --expected-runtime-workspace"
            )
        if None in (
            args.db_path,
            args.expected_session_id,
            args.expected_task_id,
            args.expected_project_id,
            args.expected_runtime_root,
        ):
            raise ObserverError(
                "--arm requires --db-path, --expected-session-id, "
                "--expected-task-id, --expected-project-id, --expected-runtime-root"
            )
    else:
        if args.expected_task_execution_id is None or (
            args.expected_runtime_workspace is None
        ):
            raise ObserverError(
                "--expected-task-execution-id and --expected-runtime-workspace "
                "are required without --arm"
            )
        if args.confirm_pre_dispatch_arming or args.expected_runtime_root:
            raise ObserverError("arming options are only valid with --arm")
    paths = [args.binding_root, args.output_dir]
    paths += [
        p
        for p in (
            args.expected_runtime_workspace,
            args.expected_runtime_root,
            args.db_path,
        )
        if p is not None
    ]
    for value in paths:
        if not _SAFE_TEXT.match(value) or not os.path.isabs(value):
            raise ObserverError("paths must be absolute printable ASCII")
    is_prod_root = os.path.normpath(args.binding_root) == default_production_root()
    if args.mode == "synthetic" and is_prod_root:
        raise ObserverError("synthetic mode refuses the production binding root")
    is_prod_db = args.db_path is not None and os.path.normpath(
        args.db_path
    ) == os.path.normpath(DEFAULT_PRODUCTION_DB)
    if args.mode == "synthetic" and is_prod_db:
        raise ObserverError("synthetic mode refuses the production database")
    if args.mode == "production" and args.arm and not args.confirm_pre_dispatch_arming:
        raise ObserverError("production arming requires --confirm-pre-dispatch-arming")
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
        observer = Observer(
            args,
            announce=lambda record: print(
                json.dumps(record, sort_keys=True), flush=True
            ),
        )
        observer.open_db()
        try:
            observer.open_output()
        except BaseException:
            if observer.db is not None:
                observer.db.close()
            raise
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
