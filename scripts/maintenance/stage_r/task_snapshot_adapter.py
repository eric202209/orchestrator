#!/usr/bin/env python3
"""Collect a bounded, sanitized Celery task snapshot for Stage R Gate 1.

This operator-side tool publishes Celery remote-control and reply traffic to
the configured broker. It does not submit, acknowledge, reserve, or mutate
application tasks. Run it only with separate operator approval.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery import __version__ as CELERY_VERSION
from celery.exceptions import DuplicateNodenameWarning
from celery.utils.nodenames import default_nodename, host_format

if __package__:
    from .preflight import collect_processes
else:  # Direct script execution places this directory, not the repo root, on sys.path.
    from preflight import collect_processes

COLLECTOR_VERSION = "1.0.0"
SNAPSHOT_SCHEMA_VERSION = 1
CATEGORIES = ("active", "reserved", "scheduled")
CONTROL_TIMEOUT_SECONDS = 5.0
MAX_COLLECTION_DURATION_SECONDS = 20.0
_WORKER_NODE = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+\Z")
_TOPOLOGY_REF = re.compile(r"[A-Za-z0-9_.#-]{1,128}\Z")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class SnapshotCollectionError(Exception):
    """A sanitized, stable failure reason safe to print to an operator."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProcessCensus:
    complete: bool
    worker_nodes: tuple[str, ...]
    source: str = "/proc"


@dataclass(frozen=True)
class CollectionResult:
    snapshot: dict[str, Any]
    evidence: dict[str, Any]


def _is_celery_worker(argv: Sequence[str]) -> bool:
    names = {Path(item).name.lower() for item in argv}
    return "celery" in names and "worker" in {item.lower() for item in argv}


def _node_from_worker_argv(argv: Sequence[str]) -> str:
    configured: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("-n", "--hostname"):
            if index + 1 >= len(argv):
                raise SnapshotCollectionError("process_worker_identity_unavailable")
            configured.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--hostname="):
            configured.append(token.partition("=")[2])
        index += 1
    if len(set(configured)) > 1:
        raise SnapshotCollectionError("process_worker_identity_ambiguous")
    value = configured[0] if configured else ""
    node = host_format(default_nodename(value))
    if not _WORKER_NODE.fullmatch(node):
        raise SnapshotCollectionError("process_worker_identity_unavailable")
    return node


def collect_local_process_census() -> ProcessCensus:
    """Return the complete local Celery worker-master view from the Stage R scanner."""

    try:
        records, complete = collect_processes()
    except Exception as exc:
        raise SnapshotCollectionError("process_census_unavailable") from exc

    worker_records = [
        record
        for record in records
        if _is_celery_worker(tuple(str(value) for value in record.get("argv", ())))
    ]
    worker_pids = {int(record.get("pid", -1)) for record in worker_records}
    masters = [
        record
        for record in worker_records
        if int(record.get("ppid", -1)) not in worker_pids
    ]
    nodes: list[str] = []
    try:
        for record in masters:
            argv = tuple(str(value) for value in record.get("argv", ()))
            nodes.append(_node_from_worker_argv(argv))
    except SnapshotCollectionError:
        return ProcessCensus(False, tuple(sorted(nodes)))
    return ProcessCensus(bool(complete), tuple(sorted(nodes)))


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise SnapshotCollectionError("collector_clock_not_timezone_aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    return (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _validate_expected_nodes(nodes: Sequence[str]) -> tuple[str, ...]:
    if not nodes:
        raise SnapshotCollectionError("expected_worker_identity_required")
    if any(
        not isinstance(node, str) or not _WORKER_NODE.fullmatch(node) for node in nodes
    ):
        raise SnapshotCollectionError("expected_worker_identity_invalid")
    if len(set(nodes)) != len(nodes):
        raise SnapshotCollectionError("duplicate_expected_worker_identity")
    return tuple(sorted(nodes))


def _validate_process_census(
    census: ProcessCensus, expected_nodes: tuple[str, ...]
) -> dict[str, Any]:
    if not isinstance(census, ProcessCensus) or census.complete is not True:
        raise SnapshotCollectionError("process_census_incomplete")
    nodes = census.worker_nodes
    if any(
        not isinstance(node, str) or not _WORKER_NODE.fullmatch(node) for node in nodes
    ):
        raise SnapshotCollectionError("process_worker_identity_unavailable")
    if len(set(nodes)) != len(nodes):
        raise SnapshotCollectionError("duplicate_local_worker_identity")
    observed = tuple(sorted(nodes))
    if observed != expected_nodes:
        if set(observed) - set(expected_nodes):
            raise SnapshotCollectionError("unexpected_local_worker_master")
        raise SnapshotCollectionError("expected_local_worker_master_missing")
    return {
        "source": census.source,
        "complete": True,
        "worker_master_count": len(observed),
        "worker_nodes": list(observed),
        "matches_expected_worker_set": True,
    }


def _safe_worker_key(value: Any) -> str:
    if isinstance(value, str) and _WORKER_NODE.fullmatch(value):
        return value
    return "<invalid-worker-identity>"


def _response_workers(response: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(response, Mapping):
        raise SnapshotCollectionError("malformed_worker_response")
    try:
        items = list(response.items())
    except Exception as exc:
        raise SnapshotCollectionError("malformed_worker_response") from exc
    keys: list[str] = []
    normalized: dict[str, Any] = {}
    for key, value in items:
        safe_key = _safe_worker_key(key)
        keys.append(safe_key)
        if safe_key == "<invalid-worker-identity>" or safe_key in normalized:
            continue
        normalized[safe_key] = value
    return normalized, sorted(keys)


def _task_identity(category: str, worker: str, row: Any) -> dict[str, str]:
    if not isinstance(row, Mapping):
        raise SnapshotCollectionError("malformed_task_record")
    task = row
    if category == "scheduled":
        task = row.get("request")
        if not isinstance(task, Mapping):
            raise SnapshotCollectionError("malformed_task_record")
    task_id = task.get("id")
    task_name = task.get("name")
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > 1024
        or not isinstance(task_name, str)
        or not task_name
        or len(task_name) > 1024
    ):
        raise SnapshotCollectionError("malformed_task_identity")
    try:
        task_id_bytes = task_id.encode("utf-8")
        task_name_bytes = task_name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SnapshotCollectionError("malformed_task_identity") from exc
    identity = {
        "worker_node": worker,
        "task_id_sha256": hashlib.sha256(task_id_bytes).hexdigest(),
    }
    identity["task_name_sha256"] = hashlib.sha256(task_name_bytes).hexdigest()
    return identity


def _default_inspect_factory(timeout: float) -> Any:
    # Importing the app does not connect to Redis; the first inspect call does.
    if CELERY_VERSION != "5.6.3":
        raise SnapshotCollectionError("unsupported_celery_version")
    project_root = str(Path(__file__).resolve().parents[3])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from app.celery_app import celery_app

    return celery_app.control.inspect(timeout=timeout)


def collect_snapshot(
    *,
    expected_worker_nodes: Sequence[str],
    topology_confirmation_ref: str | None,
    control_traffic_approved: bool,
    inspect_factory: Callable[[float], Any] = _default_inspect_factory,
    process_census_provider: Callable[[], ProcessCensus] = collect_local_process_census,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
) -> CollectionResult:
    """Collect exactly one response round for each of the three task categories."""

    expected = _validate_expected_nodes(expected_worker_nodes)
    if not topology_confirmation_ref or not _TOPOLOGY_REF.fullmatch(
        topology_confirmation_ref
    ):
        raise SnapshotCollectionError("broker_topology_unverified")
    if control_traffic_approved is not True:
        raise SnapshotCollectionError("redis_control_approval_required")

    started_at = _format_utc(utc_now())
    started = monotonic()
    try:
        before = process_census_provider()
    except SnapshotCollectionError:
        raise
    except Exception as exc:
        raise SnapshotCollectionError("process_census_unavailable") from exc
    before_evidence = _validate_process_census(before, expected)

    try:
        inspector = inspect_factory(CONTROL_TIMEOUT_SECONDS)
    except Exception as exc:
        raise SnapshotCollectionError("collector_initialization_failed") from exc

    snapshot_rows: dict[str, list[dict[str, str]]] = {
        category: [] for category in CATEGORIES
    }
    call_evidence: list[dict[str, Any]] = []

    for category in CATEGORIES:
        call_started = monotonic()
        method = getattr(inspector, category, None)
        if not callable(method):
            raise SnapshotCollectionError("missing_category")
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DuplicateNodenameWarning)
                response = method(safe=True) if category == "active" else method()
        except TimeoutError as exc:
            raise SnapshotCollectionError("remote_control_timeout") from exc
        except Exception as exc:
            raise SnapshotCollectionError("collector_exception") from exc
        call_ended = monotonic()
        duration = max(0.0, call_ended - call_started)
        total_duration = call_ended - started
        if total_duration > MAX_COLLECTION_DURATION_SECONDS:
            raise SnapshotCollectionError("excessive_collection_duration")
        if any(issubclass(item.category, DuplicateNodenameWarning) for item in caught):
            raise SnapshotCollectionError("duplicate_worker_identity")
        if response is None:
            raise SnapshotCollectionError("no_worker_response")
        workers, observed = _response_workers(response)
        if "<invalid-worker-identity>" in observed:
            raise SnapshotCollectionError("malformed_worker_identity")
        observed_set = set(observed)
        expected_set = set(expected)
        if observed_set != expected_set:
            if observed_set - expected_set:
                raise SnapshotCollectionError("unexpected_worker_response")
            if observed_set:
                raise SnapshotCollectionError("partial_worker_response")
            raise SnapshotCollectionError("missing_worker_response")
        if len(observed) != len(expected):
            raise SnapshotCollectionError("duplicate_worker_identity")

        for worker in expected:
            rows = workers[worker]
            if not isinstance(rows, list):
                raise SnapshotCollectionError("non_list_category_response")
            snapshot_rows[category].extend(
                _task_identity(category, worker, row) for row in rows
            )
        call_evidence.append(
            {
                "category": category,
                "completion_status": "COMPLETE",
                "duration_seconds": round(duration, 6),
                "observed_worker_set": observed,
            }
        )

    try:
        after = process_census_provider()
    except SnapshotCollectionError:
        raise
    except Exception as exc:
        raise SnapshotCollectionError("process_census_unavailable") from exc
    after_evidence = _validate_process_census(after, expected)
    if before_evidence["worker_nodes"] != after_evidence["worker_nodes"]:
        raise SnapshotCollectionError("local_worker_topology_changed")

    ended = monotonic()
    duration = ended - started
    if duration > MAX_COLLECTION_DURATION_SECONDS:
        raise SnapshotCollectionError("excessive_collection_duration")
    ended_at = _format_utc(utc_now())
    snapshot = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": ended_at,
        "active": snapshot_rows["active"],
        "reserved": snapshot_rows["reserved"],
        "scheduled": snapshot_rows["scheduled"],
    }
    evidence = {
        "evidence_schema_version": 1,
        "collector_version": COLLECTOR_VERSION,
        "celery_version": CELERY_VERSION,
        "collection_started_at": started_at,
        "collection_ended_at": ended_at,
        "collection_duration_seconds": round(duration, 6),
        "expected_worker_nodes": list(expected),
        "broker_topology_confirmation_ref": topology_confirmation_ref,
        "broker_topology_limitations": [
            "The local /proc census cannot detect a remote or off-boundary worker.",
            "The confirmation reference is operator-provided and is not independently verified.",
        ],
        "observed_worker_set_per_call": call_evidence,
        "process_census": {
            "before": before_evidence,
            "after": after_evidence,
            "complete": True,
            "limitation": "Two point-in-time local /proc censuses do not prove broker-wide worker absence.",
        },
        "snapshot_sha256": hashlib.sha256(_snapshot_bytes(snapshot)).hexdigest(),
        "collection_method": "Celery app.control.inspect(timeout=5.0): active(safe=True), reserved(), scheduled()",
        "non_atomicity_limitation": "The three Celery control calls are separate observations and are not an atomic snapshot.",
    }
    return CollectionResult(snapshot=snapshot, evidence=evidence)


def _open_directory_chain(path: str | Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SnapshotCollectionError("secure_output_primitive_unavailable")
    absolute = os.path.abspath(os.fspath(path))
    parts = Path(absolute).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(os.sep, flags)
    try:
        for part in parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_private_file(directory_fd: int, name: str, payload: bytes) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotCollectionError("secure_output_primitive_unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise SnapshotCollectionError("output_write_failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise SnapshotCollectionError("atomic_no_replace_unavailable") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise SnapshotCollectionError("output_exists")
        raise SnapshotCollectionError("atomic_output_publish_failed")


def _cleanup_stage(parent_fd: int, stage_name: str) -> None:
    try:
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError:
        return
    try:
        for name in ("task-snapshot.json", "task-snapshot-evidence.json"):
            try:
                os.unlink(name, dir_fd=stage_fd)
            except OSError:
                pass
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=parent_fd)
    except OSError:
        pass


def write_output_bundle(
    output_dir: str | Path, snapshot: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[Path, Path]:
    """Atomically publish a new private directory containing the two artifacts."""

    destination = Path(output_dir)
    if not destination.name or destination.name in (".", ".."):
        raise SnapshotCollectionError("output_path_invalid")
    parent_fd: int | None = None
    stage_name: str | None = None
    try:
        parent_fd = _open_directory_chain(destination.parent)
        try:
            existing = os.stat(
                destination.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise SnapshotCollectionError("output_symlink_rejected")
            raise SnapshotCollectionError("output_exists")

        snapshot_bytes = _snapshot_bytes(snapshot)
        evidence_document = dict(evidence)
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        if evidence_document.get("snapshot_sha256") not in (None, snapshot_digest):
            raise SnapshotCollectionError("snapshot_digest_mismatch")
        evidence_document["snapshot_sha256"] = snapshot_digest
        evidence_bytes = (
            json.dumps(evidence_document, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        stage_name = f".{destination.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(stage_fd, 0o700)
            _write_private_file(stage_fd, "task-snapshot.json", snapshot_bytes)
            _write_private_file(stage_fd, "task-snapshot-evidence.json", evidence_bytes)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        _rename_noreplace(parent_fd, stage_name, destination.name)
        stage_name = None
        os.fsync(parent_fd)
        return (
            destination / "task-snapshot.json",
            destination / "task-snapshot-evidence.json",
        )
    except SnapshotCollectionError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SnapshotCollectionError("output_symlink_rejected") from exc
        raise SnapshotCollectionError("output_write_failed") from exc
    finally:
        if parent_fd is not None:
            if stage_name is not None:
                _cleanup_stage(parent_fd, stage_name)
            os.close(parent_fd)


def _get_celery_inspector(timeout: float) -> Any:
    return _default_inspect_factory(timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a sanitized Stage R Celery task snapshot."
    )
    parser.add_argument("--expected-worker-node", action="append", default=[])
    parser.add_argument("--broker-topology-evidence-ref")
    parser.add_argument("--approve-redis-control-reply-writes", action="store_true")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        result = collect_snapshot(
            expected_worker_nodes=args.expected_worker_node,
            topology_confirmation_ref=args.broker_topology_evidence_ref,
            control_traffic_approved=args.approve_redis_control_reply_writes,
            inspect_factory=_get_celery_inspector,
        )
        write_output_bundle(args.output_dir, result.snapshot, result.evidence)
    except SnapshotCollectionError as exc:
        print(f"INCONCLUSIVE: {exc.reason}", file=sys.stderr)
        return 2
    except Exception:
        print("INCONCLUSIVE: collector_internal_error", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "SNAPSHOT_COLLECTED",
                "snapshot_sha256": result.evidence.get("snapshot_sha256"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
