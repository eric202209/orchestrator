#!/usr/bin/env python3
"""Phase 39 A2: read-only backup/restore preflight for the GX10 deployment.

Dry-run only. This script never copies data, never writes to production
paths, never reads file contents, and has no execute mode. It reports:

- per-domain source paths, sizes (lstat only), and device ids;
- whether a proposed backup destination is safe (not inside or containing
  a production path, enough free space, and whether it is on the same
  physical device as production data);
- whether a proposed restore-drill target is isolated from production;
- a read-only quiescence probe (Redis queue/unacked/persistence counters
  and active SQLite executions) when ``--probe`` is given.

The operator runs the actual capture commands from the A2 readiness
report after reviewing this output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CHECKOUT = Path("/root/.openclaw/workspace/vault/orchestrator")
PROJECTS_ROOT = Path("/root/.openclaw/workspace/vault/projects")
DEFAULT_DB_PATH = CHECKOUT / "orchestrator.db"

# Capacity margin: raw bytes x this factor must fit at the destination
# (uncompressed copy plus one previous run kept for rollback).
CAPACITY_FACTOR = 2.0

# app.models.TaskStatus is stored by enum name.
ACTIVE_EXECUTION_STATUSES = ("RUNNING",)

# Only these read-only Redis commands may be issued by the probe.
_REDIS_READONLY = {("LLEN", "celery"), ("EXISTS", "unacked"), ("INFO", "persistence")}
_REDIS_INFO_KEYS = (
    "rdb_bgsave_in_progress",
    "rdb_changes_since_last_save",
    "rdb_last_bgsave_status",
    "rdb_last_save_time",
)


@dataclass(frozen=True)
class Domain:
    name: str
    paths: tuple[Path, ...]
    mechanism: str
    quiescence: str
    confidential: bool = False
    required: bool = True
    # Subtrees covered by another domain; skipped when measuring and not
    # treated as production when checking destinations.
    exclude: tuple[Path, ...] = ()


def default_domains() -> list[Domain]:
    orch = Path("/root/.orchestrator")
    return [
        Domain(
            "sqlite",
            (DEFAULT_DB_PATH,),
            "sqlite3 backup API (dogfood_backup.backup_database)",
            "none",
        ),
        Domain(
            "redis",
            (Path("/var/lib/redis/dump.rdb"), Path("/etc/redis/redis.conf")),
            "redis-cli --rdb <dest> (approval required)",
            "broker idle: no queued or unacked messages",
        ),
        Domain(
            "qdrant",
            (CHECKOUT / "qdrant" / "data",),
            "Qdrant collection snapshot API (approval required)",
            "none",
        ),
        Domain(
            "openclaw",
            (Path("/root/.openclaw"),),
            "tar excluding workspace/ (separate domain), encrypted",
            "no OpenClaw agent child running",
            confidential=True,
            exclude=(Path("/root/.openclaw/workspace"),),
        ),
        Domain(
            "ssh",
            (Path("/root/.ssh"),),
            "tar, encrypted",
            "none",
            confidential=True,
        ),
        Domain(
            "orchestrator_runtime",
            (orch / "runtime", orch / "worker-pin", orch / "rollback"),
            "tar of /root/.orchestrator, preserving modes",
            "no active task executions",
        ),
        Domain(
            "workspaces",
            (PROJECTS_ROOT,),
            "tar of projects root",
            "no active task executions",
        ),
        Domain(
            "checkout_state",
            (
                CHECKOUT / "docs",
                CHECKOUT / "checkpoints",
                CHECKOUT / "candidate-content",
                CHECKOUT / "knowledge",
                CHECKOUT / "run" / "celerybeat-schedule.db",
            ),
            "tar (docs/ is Git-ignored and otherwise unprotected)",
            "no active task executions",
        ),
        Domain(
            "deployment_config",
            (CHECKOUT / ".env",),
            "copy, encrypted; host compose/build context captured on host",
            "none",
            confidential=True,
        ),
        Domain(
            "runtime_components",
            (
                Path("/usr/lib/node_modules/openclaw"),
                CHECKOUT / "venv",
                CHECKOUT / "qdrant" / "bin",
            ),
            "host: docker image save of the image ID; tar of Git-ignored "
            "runtimes that cannot be rebuilt offline",
            "none",
            required=False,
        ),
        Domain(
            "logs",
            (Path("/var/log/openclaw"), CHECKOUT / "logs"),
            "tar",
            "none",
            required=False,
        ),
    ]


def measure(path: Path, exclude: tuple[Path, ...] = ()) -> dict:
    """lstat-only size/device for a path. Never opens file contents."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "bytes": 0, "device": None}
    total = st.st_size
    if path.is_dir() and not path.is_symlink():
        skip = {str(p) for p in exclude}
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
            for name in dirs + files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    return {"path": str(path), "exists": True, "bytes": total, "device": st.st_dev}


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _inside_production(path: Path, src: Path, excluded: list[Path]) -> bool:
    if not _is_within(path, src):
        return False
    return not any(_is_within(path, ex) and _is_within(ex, src) for ex in excluded)


def check_destination(
    dest: Path,
    sources: list[Path],
    required_bytes: int,
    source_devices: set[int],
    excluded: list[Path] = (),
) -> dict:
    """Classify a proposed backup destination. Rejections are hard errors."""
    reasons: list[str] = []
    for src in sources:
        if _inside_production(dest, src, list(excluded)):
            reasons.append(f"destination is inside production path {src}")
        elif _is_within(src, dest):
            reasons.append(f"destination contains production path {src}")
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        reasons.append("destination already exists and is not empty")

    anchor = _existing_ancestor(dest)
    free = shutil.disk_usage(anchor).free
    needed = int(required_bytes * CAPACITY_FACTOR)
    if free < needed:
        reasons.append(f"insufficient free space: {free} < {needed}")

    same_device = anchor.stat().st_dev in source_devices
    return {
        "destination": str(dest),
        "accepted": not reasons,
        "reasons": reasons,
        "free_bytes": free,
        "required_bytes": needed,
        # Same st_dev means same filesystem as production data: a temporary
        # recovery copy only, never disaster recovery.
        "tier": "same_device_temporary" if same_device else "separate_device",
    }


def check_restore_target(
    target: Path, production_paths: list[Path], excluded: list[Path] = ()
) -> dict:
    """A restore drill target must be empty and disjoint from production."""
    reasons: list[str] = []
    for prod in production_paths:
        if _inside_production(target, prod, list(excluded)):
            reasons.append(f"restore target is inside production path {prod}")
        elif _is_within(prod, target):
            reasons.append(f"restore target contains production path {prod}")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        reasons.append("restore target exists and is not an empty directory")
    return {"restore_target": str(target), "accepted": not reasons, "reasons": reasons}


def _redis(*args: str) -> str:
    if tuple(args) not in _REDIS_READONLY:
        raise ValueError(f"redis command not allowed: {args!r}")
    proc = subprocess.run(
        ["redis-cli", *args], capture_output=True, text=True, timeout=5, check=True
    )
    return proc.stdout


def probe_redis(run=_redis) -> dict:
    info = {}
    for line in run("INFO", "persistence").splitlines():
        key, _, value = line.strip().partition(":")
        if key in _REDIS_INFO_KEYS:
            info[key] = value
    queued = int(run("LLEN", "celery").strip() or 0)
    unacked = int(run("EXISTS", "unacked").strip() or 0)
    return {
        "queued": queued,
        "unacked_present": bool(unacked),
        "persistence": info,
        "idle": queued == 0 and not unacked,
    }


def probe_sqlite(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        placeholders = ",".join("?" for _ in ACTIVE_EXECUTION_STATUSES)
        (active,) = conn.execute(
            "select count(*) from task_executions "
            f"where upper(status) in ({placeholders})",
            ACTIVE_EXECUTION_STATUSES,
        ).fetchone()
    finally:
        conn.close()
    return {"active_executions": active, "idle": active == 0}


def build_plan(
    domains: list[Domain],
    destination: Path | None,
    restore_target: Path | None,
) -> dict:
    report_domains = []
    required_bytes = 0
    devices: set[int] = set()
    missing_required: list[str] = []
    all_paths: list[Path] = []
    excluded: list[Path] = []
    for domain in domains:
        measured = [measure(p, domain.exclude) for p in domain.paths]
        size = sum(m["bytes"] for m in measured)
        required_bytes += size
        devices.update(m["device"] for m in measured if m["device"] is not None)
        all_paths.extend(domain.paths)
        excluded.extend(domain.exclude)
        if domain.required and not any(m["exists"] for m in measured):
            missing_required.append(domain.name)
        report_domains.append(
            {
                "name": domain.name,
                "mechanism": domain.mechanism,
                "quiescence": domain.quiescence,
                "confidential": domain.confidential,
                "required": domain.required,
                "bytes": size,
                # Paths and sizes only; contents are never read.
                "paths": [
                    {k: m[k] for k in ("path", "exists", "bytes")} for m in measured
                ],
            }
        )

    plan: dict = {
        "mode": "dry_run",
        "domains": report_domains,
        "total_bytes": required_bytes,
        "missing_required_domains": missing_required,
    }
    if destination is not None:
        plan["destination"] = check_destination(
            destination, all_paths, required_bytes, devices, excluded
        )
    if restore_target is not None:
        plan["restore"] = check_restore_target(restore_target, all_paths, excluded)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run backup/restore preflight. Never copies or writes data."
    )
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--restore-target", type=Path)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="read-only Redis and SQLite quiescence probe",
    )
    args = parser.parse_args(argv)

    plan = build_plan(default_domains(), args.destination, args.restore_target)
    if args.probe:
        plan["quiescence"] = {
            "redis": probe_redis(),
            "sqlite": probe_sqlite(DEFAULT_DB_PATH),
        }

    print(json.dumps(plan, indent=2))
    blocked = bool(plan["missing_required_domains"])
    for key in ("destination", "restore"):
        if key in plan and not plan[key]["accepted"]:
            blocked = True
    if args.probe and not all(v["idle"] for v in plan["quiescence"].values()):
        blocked = True
    return 2 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
