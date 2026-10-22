from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.maintenance import backup_preflight as bp
from scripts.maintenance.backup_preflight import (
    Domain,
    build_plan,
    check_destination,
    check_restore_target,
    probe_redis,
    probe_sqlite,
)


def _domains(tmp_path: Path) -> list[Domain]:
    data = tmp_path / "prod" / "data"
    data.mkdir(parents=True)
    (data / "a.bin").write_bytes(b"x" * 100)
    secret = tmp_path / "prod" / "secret.env"
    secret.write_text("API_KEY=do-not-print-me\n")
    return [
        Domain("data", (data,), "tar", "none"),
        Domain("secret", (secret,), "copy, encrypted", "none", confidential=True),
        Domain(
            "optional", (tmp_path / "prod" / "absent",), "tar", "none", required=False
        ),
    ]


def test_plan_sizes_and_never_prints_secret_contents(tmp_path):
    plan = build_plan(_domains(tmp_path), None, None)
    by_name = {d["name"]: d for d in plan["domains"]}
    assert plan["mode"] == "dry_run"
    assert by_name["data"]["bytes"] >= 100
    assert by_name["secret"]["confidential"] is True
    assert by_name["optional"]["paths"][0]["exists"] is False
    assert plan["missing_required_domains"] == []
    assert "do-not-print-me" not in json.dumps(plan)


def test_missing_required_domain_is_reported(tmp_path):
    plan = build_plan(
        [Domain("db", (tmp_path / "nope.db",), "api", "none")], None, None
    )
    assert plan["missing_required_domains"] == ["db"]


def test_destination_inside_production_is_rejected(tmp_path):
    domains = _domains(tmp_path)
    plan = build_plan(domains, tmp_path / "prod" / "data" / "backup", None)
    assert plan["destination"]["accepted"] is False
    assert "inside production path" in plan["destination"]["reasons"][0]


def test_destination_containing_production_is_rejected(tmp_path):
    plan = build_plan(_domains(tmp_path), tmp_path, None)
    assert plan["destination"]["accepted"] is False
    assert any("contains production path" in r for r in plan["destination"]["reasons"])


def test_same_device_destination_is_temporary_tier(tmp_path):
    plan = build_plan(_domains(tmp_path), tmp_path / "backups" / "run1", None)
    dest = plan["destination"]
    assert dest["accepted"] is True
    assert dest["tier"] == "same_device_temporary"


def test_nonempty_destination_and_capacity_are_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "old").write_text("x")
    result = check_destination(dest, [src], 10**18, set())
    assert result["accepted"] is False
    assert any("not empty" in r for r in result["reasons"])
    assert any("insufficient free space" in r for r in result["reasons"])


def test_restore_target_isolation(tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()
    assert check_restore_target(prod / "drill", [prod])["accepted"] is False
    assert check_restore_target(tmp_path, [prod])["accepted"] is False
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "f").write_text("x")
    assert check_restore_target(occupied, [prod])["accepted"] is False
    assert check_restore_target(tmp_path / "drill", [prod])["accepted"] is True


def test_redis_probe_uses_only_allowlisted_readonly_commands():
    calls = []
    replies = {
        (
            "INFO",
            "persistence",
        ): "# Persistence\r\nrdb_changes_since_last_save:3\r\nrdb_bgsave_in_progress:0\r\n",
        ("LLEN", "celery"): "0\n",
        ("EXISTS", "unacked"): "0\n",
    }

    def fake(*args):
        calls.append(args)
        return replies[args]

    result = probe_redis(fake)
    assert set(calls) <= bp._REDIS_READONLY
    assert result["idle"] is True
    assert result["persistence"]["rdb_changes_since_last_save"] == "3"


def test_redis_probe_not_idle_with_unacked():
    replies = {
        ("INFO", "persistence"): "",
        ("LLEN", "celery"): "2\n",
        ("EXISTS", "unacked"): "1\n",
    }
    result = probe_redis(lambda *a: replies[a])
    assert result["idle"] is False


def test_redis_helper_rejects_mutating_commands():
    for args in (("BGSAVE",), ("SAVE",), ("FLUSHALL",), ("CONFIG", "SET")):
        with pytest.raises(ValueError):
            bp._redis(*args)


def test_sqlite_probe_counts_running_without_writing(tmp_path):
    db = tmp_path / "o.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "create table task_executions (id integer primary key, status text);"
        "insert into task_executions (status) values ('RUNNING'), ('DONE');"
    )
    conn.commit()
    conn.close()
    before = db.stat().st_mtime_ns
    assert probe_sqlite(db) == {"active_executions": 1, "idle": False}
    assert db.stat().st_mtime_ns == before


def test_cli_has_no_execute_mode():
    with pytest.raises(SystemExit):
        bp.main(["--execute"])


def test_excluded_subtree_is_not_measured_and_may_hold_destination(tmp_path):
    root = tmp_path / "openclaw"
    workspace = root / "workspace"
    (workspace / "backups").mkdir(parents=True)
    (workspace / "big.bin").write_bytes(b"x" * 10_000)
    (root / "config.json").write_text("{}")
    domains = [Domain("openclaw", (root,), "tar", "none", exclude=(workspace,))]
    plan = build_plan(domains, workspace / "backups" / "run1", None)
    assert plan["domains"][0]["bytes"] < 10_000
    assert plan["destination"]["accepted"] is True
    plan = build_plan(domains, root / "inside", None)
    assert plan["destination"]["accepted"] is False
