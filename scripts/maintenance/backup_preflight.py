#!/usr/bin/env python3
"""Phase 39 A2: read-only backup/restore preflight for the GX10 deployment.

Dry-run only. This script never copies data or writes to production paths.
It reads the production ``DATABASE_URL`` setting (or its explicit config.py
default); database contents are read only by the optional read-only probe.
It has no execute mode. It reports:

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

PRODUCTION_ROOT = Path(
    "/root/.openclaw/workspace/vault/orchestrator-production"
)
PRODUCTION_RUNTIME_ROOT = Path(
    "/root/.openclaw/workspace/vault/orchestrator-production-runtime"
)
DEVELOPMENT_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_ROOT = Path("/root/.openclaw/workspace/vault/projects")

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


class ProductionTargetError(ValueError):
    """The authoritative production root or database is ambiguous or absent."""


def _canonical_production_root(root: Path, development_root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
        development = development_root.resolve(strict=True)
    except OSError as exc:
        raise ProductionTargetError(f"production root cannot be resolved: {root}") from exc
    if not resolved.is_dir():
        raise ProductionTargetError(f"production root is not a directory: {resolved}")
    if resolved == development:
        raise ProductionTargetError("production root resolves to the development checkout")
    if not (resolved / "app" / "config.py").is_file():
        raise ProductionTargetError(
            f"production configuration is missing under {resolved}"
        )
    return resolved


def _production_database_url(root: Path) -> str:
    """Read DATABASE_URL from production .env or use the config.py root default."""
    env_file = root / ".env"
    if env_file.is_symlink() or not env_file.is_file():
        raise ProductionTargetError(f"production .env is missing or unsafe: {env_file}")
    values: list[str] = []
    try:
        lines = env_file.read_text().splitlines()
    except OSError as exc:
        raise ProductionTargetError("production .env cannot be read") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == "DATABASE_URL":
            value = value.strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                if len(value) < 2 or value[-1] != quote:
                    raise ProductionTargetError("production DATABASE_URL quoting is invalid")
                value = value[1:-1]
            values.append(value)
    if len(values) > 1:
        raise ProductionTargetError("production .env has multiple DATABASE_URL values")
    if values:
        if not values[0] or "${" in values[0] or "\n" in values[0]:
            raise ProductionTargetError("production DATABASE_URL is unresolved")
        return values[0]

    # This is the explicit default in app/config.py: DATABASE_URL defaults to
    # sqlite:///{BASE_DIR}/orchestrator.db, where BASE_DIR is this checkout.
    try:
        config_text = (root / "app" / "config.py").read_text()
    except OSError as exc:
        raise ProductionTargetError("production app/config.py cannot be read") from exc
    if (
        'DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR}/orchestrator.db"' not in config_text
        or "DATABASE_URL: str = DEFAULT_DATABASE_URL" not in config_text
    ):
        raise ProductionTargetError(
            "production DATABASE_URL is absent and config.py has no verified checkout-local default"
        )
    return f"sqlite:///{root / 'orchestrator.db'}"


def resolve_production_database(
    production_root: Path = PRODUCTION_ROOT,
    *,
    development_root: Path = DEVELOPMENT_ROOT,
) -> tuple[Path, str]:
    root = _canonical_production_root(production_root, development_root)
    url = _production_database_url(root)
    if not url.startswith("sqlite:///"):
        raise ProductionTargetError(
            "A2 SQLite preflight cannot resolve the configured production database URL"
        )
    raw_path = url[len("sqlite:///") :].split("?", 1)[0]
    if not raw_path or raw_path == ":memory:":
        raise ProductionTargetError("production DATABASE_URL has no persistent file target")
    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    db_path = db_path.resolve()
    try:
        db_path.relative_to(development_root.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ProductionTargetError(
            "production DATABASE_URL resolves into the development checkout"
        )
    return db_path, url


def _canonical_production_runtime(
    runtime_root: Path, production_root: Path, development_root: Path
) -> Path:
    try:
        runtime = runtime_root.resolve(strict=True)
        development = development_root.resolve(strict=True)
    except OSError as exc:
        raise ProductionTargetError(
            f"production runtime root cannot be resolved: {runtime_root}"
        ) from exc
    if not runtime.is_dir():
        raise ProductionTargetError(f"production runtime root is not a directory: {runtime}")
    if runtime == development or development in runtime.parents:
        raise ProductionTargetError(
            "production runtime root resolves into the development checkout"
        )
    if runtime == production_root or production_root in runtime.parents or runtime in production_root.parents:
        raise ProductionTargetError("production source and runtime roots overlap")
    return runtime


def default_domains(
    production_root: Path = PRODUCTION_ROOT,
    production_runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
    *,
    development_root: Path = DEVELOPMENT_ROOT,
) -> list[Domain]:
    root = _canonical_production_root(production_root, development_root)
    db_path, _ = resolve_production_database(
        root, development_root=development_root
    )
    runtime_root = _canonical_production_runtime(
        production_runtime_root, root, development_root
    )
    return [
        Domain(
            "sqlite",
            (db_path,),
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
            (root / "qdrant" / "data",),
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
            (runtime_root,),
            "tar of the isolated production runtime, preserving modes",
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
                root / "docs",
                root / "checkpoints",
                root / "candidate-content",
                root / "knowledge",
                root / "run" / "celerybeat-schedule.db",
            ),
            "tar (docs/ is Git-ignored and otherwise unprotected)",
            "no active task executions",
        ),
        Domain(
            "deployment_config",
            (root / ".env",),
            "copy, encrypted; host compose/build context captured on host",
            "none",
            confidential=True,
        ),
        Domain(
            "runtime_components",
            (
                Path("/usr/lib/node_modules/openclaw"),
                root / "venv",
                root / "qdrant" / "bin",
            ),
            "host: docker image save of the image ID; tar of Git-ignored "
            "runtimes that cannot be rebuilt offline",
            "none",
            required=False,
        ),
        Domain(
            "logs",
            (Path("/var/log/openclaw"), root / "logs"),
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
        "--production-root",
        type=Path,
        default=PRODUCTION_ROOT,
        help="authoritative production checkout (defaults to the isolated production source)",
    )
    parser.add_argument(
        "--production-runtime-root",
        type=Path,
        default=PRODUCTION_RUNTIME_ROOT,
        help="isolated production runtime backup root",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="read-only Redis and SQLite quiescence probe",
    )
    args = parser.parse_args(argv)

    try:
        domains = default_domains(
            args.production_root,
            args.production_runtime_root,
        )
        database_path, _ = resolve_production_database(args.production_root)
    except ProductionTargetError as exc:
        print(json.dumps({"mode": "blocked", "production_target_error": str(exc)}, indent=2))
        return 2

    plan = build_plan(domains, args.destination, args.restore_target)
    plan["production_root"] = str(args.production_root.resolve())
    plan["production_database_path"] = str(database_path)
    if args.probe:
        db_path = next(
            path
            for domain in domains
            if domain.name == "sqlite"
            for path in domain.paths
        )
        if db_path.is_file():
            plan["quiescence"] = {
                "redis": probe_redis(),
                "sqlite": probe_sqlite(db_path),
            }
        else:
            plan["quiescence"] = {
                "production_database": {
                    "path": str(db_path),
                    "idle": False,
                    "error": "configured production database target is missing",
                }
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
