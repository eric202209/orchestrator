from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.maintenance.stage_r.gate2_check import evaluate_gate2
from scripts.maintenance.stage_r.preflight import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    evaluate_git,
    evaluate_preflight,
    evaluate_processes,
    evaluate_redis_tasks,
    evaluate_sessions,
    _matches_tool_checkout,
    _read_json,
    collect_sessions,
    venv_inventory_digest,
)


HEAD = "a" * 40
TREE = "b" * 40
SOURCE_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
MODULE_DIGEST = "e" * 64
PIN_DIGEST = "f" * 64


def _task_snapshot(now: datetime | None = None) -> dict[str, object]:
    return {
        "snapshot_schema_version": 1,
        "captured_at": (now or datetime.now(timezone.utc)).isoformat(),
        "active": [],
        "reserved": [],
        "scheduled": [],
    }


def _processes() -> list[dict[str, object]]:
    return [
        {
            "pid": 100,
            "ppid": 1,
            "argv": [
                "/venv/bin/celery",
                "-A",
                "app.celery_app",
                "worker",
                "--concurrency=4",
            ],
        },
        {
            "pid": 101,
            "ppid": 1,
            "argv": ["/venv/bin/celery", "-A", "app.celery_app", "beat"],
        },
    ]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_preflight() -> tuple[dict[str, object], dict[str, object]]:
    runtime = {
        "available": True,
        "venv_inventory_sha256": PIN_DIGEST,
        "python_identity": {"version": "3.12.1", "executable": "/venv/bin/python"},
        "public_identity": {
            "venv_inventory_sha256": PIN_DIGEST,
            "python_identity": {"version": "3.12.1", "executable": "/venv/bin/python"},
        },
    }
    protected = {
        "runtime_manifest_sha256": _digest("runtime"),
        "product_root_identity_sha256": _digest("product"),
    }
    approved = {
        "head": HEAD,
        "tree": TREE,
        "runtime": {
            "venv_inventory_sha256": PIN_DIGEST,
            "python_identity": {"version": "3.12.1", "executable": "/venv/bin/python"},
        },
    }
    observed = {
        "git": {
            "available": True,
            "head": HEAD,
            "tree": TREE,
            "dirty": False,
            "change_count": 0,
        },
        "processes": _processes(),
        "process_table_complete": True,
        "redis": {"available": True, "queue_lengths": {"celery": 0}, "lease_keys": []},
        "task_snapshot": _task_snapshot(),
        "active_session_count": 0,
        "runtime": runtime,
        "protected_approved": protected,
        "protected_observed": dict(protected),
        "pin": {
            "exists": True,
            "symlink": False,
            "regular_file": True,
            "owner_uid": 0,
            "owner_gid": 0,
            "mode": 0o600,
            "expected_owner_uid": 0,
            "expected_owner_gid": 0,
            "expected_mode": 0o600,
            "loader_status": "PIN_LOADED",
        },
        "backup": {
            "schema_version": 1,
            "backup_status": "VERIFIED",
            "source_path": "/root/.orchestrator/runtime",
            "destination_path": "/host-backup/runtime-copy",
            "outside_container": True,
            "file_count": 12,
            "inventory_verified": True,
            "manifest_verified": True,
            "sha256_manifest_digest": _digest("manifest"),
        },
    }
    return approved, observed


def _gate2_fixture() -> dict[str, object]:
    from app.services.orchestration import worker_identity as wi

    root = "/tmp/stage-r-repo"
    executable = "/tmp/stage-r-venv/bin/python"
    worker_script = f"{root}/venv/bin/celery"
    launcher = {
        "launcher": "start.sh:start_workers",
        "executable": executable,
        "cwd": root,
        "argv": [
            executable,
            worker_script,
            "-A",
            "app.celery_app",
            "worker",
            "--concurrency=4",
        ],
    }
    identity = {
        "pin_schema_version": 1,
        "pin_sha256": PIN_DIGEST,
        "candidate_head": HEAD,
        "candidate_tree": TREE,
        "source_manifest_digest": SOURCE_DIGEST,
        "operator_identity": "operator",
        "approval_id": "stage-r-001",
        "approved_at": "2026-10-20T00:00:00+00:00",
        "expires_at": "2026-10-21T00:00:00+00:00",
    }
    pin = {
        "candidate_head": HEAD,
        "candidate_tree": TREE,
        "source_root": root,
        "source_manifest_digest": SOURCE_DIGEST,
        "launcher_identity": launcher,
        "worker_node_identity": "celery@test-node",
        "configuration_schema_version": wi.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        "configuration_fingerprint": CONFIG_DIGEST,
        "configuration_policy_id": wi.CONFIGURATION_POLICY_ID,
    }
    source_launcher = dict(launcher)
    module_path = f"{root}/app/sample.py"
    source = {
        **{
            name: pin[name]
            for name in (
                "candidate_head",
                "candidate_tree",
                "source_root",
                "source_manifest_digest",
            )
        },
        "launcher_identity": source_launcher,
        "affected_modules": ["app.sample"],
        "resolved_modules": [
            {
                "module_name": "app.sample",
                "expected_source_path": module_path,
                "source_sha256": MODULE_DIGEST,
            }
        ],
    }
    module_provenance = [
        {
            "module_name": "app.sample",
            "expected_source_path": module_path,
            "expected_source_sha256": MODULE_DIGEST,
            "source_sha256": MODULE_DIGEST,
            "resolved_source_path": module_path,
            "spec_origin": module_path,
            "loaded": True,
        }
    ]
    projection = {
        "schema_version": wi.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        "projection_policy_id": wi.CONFIGURATION_POLICY_ID,
        "identity_scope": wi.IDENTITY_SCOPE,
        "invocation_identity": wi.INVOCATION_IDENTITY,
        "invocation_identity_reason": wi.INVOCATION_IDENTITY_REASON,
    }
    manifest = {
        "schema_version": wi.SCHEMA_VERSION,
        "candidate_head": HEAD,
        "candidate_tree": TREE,
        "candidate_source_root": root,
        "source_manifest_digest": SOURCE_DIGEST,
        "boot_timestamp": "2026-10-20T12:00:00+00:00",
        "linux_boot_id": "boot-id-1",
        "worker_pid": 200,
        "worker_start_ticks": "9988",
        "worker_executable": executable,
        "worker_cwd": root,
        "worker_argv": launcher["argv"],
        "worker_node_identity": pin["worker_node_identity"],
        "expected_task_registrations": [],
        "registered_task_registrations": [],
        "worker_children": [],
        "affected_module_provenance": module_provenance,
        "configuration_fingerprint": CONFIG_DIGEST,
        "configuration_projection": projection,
        "identity_status": "IDENTITY_VERIFIED",
        "record_id": "boot-id-1-200-9988",
        "configuration_schema_version": pin["configuration_schema_version"],
        "configuration_policy_id": pin["configuration_policy_id"],
        "identity_scope": wi.IDENTITY_SCOPE,
        "invocation_identity": wi.INVOCATION_IDENTITY,
        "invocation_identity_reason": wi.INVOCATION_IDENTITY_REASON,
        "candidate_pin_status": "PIN_MATCHED",
        "candidate_pin_identity": identity,
        "identity_status_reasons": [],
    }
    worksheet = {
        "worksheet_kind": "orchestrator-worker-candidate-pin-worksheet",
        "worksheet_schema_version": 1,
        "approval_state": "APPROVED",
        "OPERATOR_APPROVED_SOURCE": {
            "candidate_head": HEAD,
            "candidate_tree": TREE,
            "source_manifest_digest": SOURCE_DIGEST,
        },
        "OBSERVED_SOURCE": {
            "candidate_head": HEAD,
            "candidate_tree": TREE,
            "source_root": root,
            "source_manifest_digest": SOURCE_DIGEST,
            "digest_launcher_identity": source_launcher,
        },
        "configuration_identity": {
            "configuration_fingerprint": CONFIG_DIGEST,
            "configuration_schema_version": pin["configuration_schema_version"],
            "configuration_policy_id": pin["configuration_policy_id"],
        },
        "proposed_pin_body": {**pin, "approval": None},
    }
    return {
        "worksheet": worksheet,
        "pin": pin,
        "pin_identity": identity,
        "pin_status": "PIN_LOADED",
        "manifest": manifest,
        "source_identity": source,
        "source_classification": "IDENTITY_VERIFIED",
        "live_process": {
            "pid": 200,
            "linux_boot_id": "boot-id-1",
            "start_ticks": "9988",
            "executable": executable,
            "cwd": root,
            "argv": launcher["argv"],
            "ppid": 1,
            "pgid": 200,
            "sid": 200,
        },
        "process_complete": True,
    }


class StageRPreflightTests(unittest.TestCase):
    def test_source_root_guard_rejects_external_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(_matches_tool_checkout(temp))

    def test_json_evidence_reader_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "evidence.json"
            link = root / "evidence-link.json"
            target.write_text('{"safe": true}\n', encoding="utf-8")
            link.symlink_to(target)
            before = target.read_bytes()
            self.assertIsNone(_read_json(link))
            self.assertEqual(target.read_bytes(), before)

    def test_valid_preflight_fixture_passes(self) -> None:
        approved, observed = _valid_preflight()
        result = evaluate_preflight(approved, observed)
        self.assertEqual(result["overall"], PASS)
        self.assertTrue(all(check["status"] == PASS for check in result["checks"]))

    def test_dirty_git_tree_fails(self) -> None:
        state = {
            "available": True,
            "head": HEAD,
            "tree": TREE,
            "dirty": True,
            "change_count": 2,
        }
        result = evaluate_git(HEAD, TREE, state)
        self.assertEqual(result["status"], FAIL)
        self.assertIn("git_worktree_dirty", result["reason_codes"])

    def test_wrong_head_fails(self) -> None:
        result = evaluate_git(
            HEAD,
            TREE,
            {"available": True, "head": "0" * 40, "tree": TREE, "dirty": False},
        )
        self.assertEqual(result["status"], FAIL)
        self.assertEqual(result["differing_fields"], ["HEAD"])

    def test_extra_agent_process_fails(self) -> None:
        records = _processes() + [
            {"pid": 202, "ppid": 1, "argv": ["/usr/bin/codex", "--full-auto"]}
        ]
        result = evaluate_processes(records, complete=True)
        self.assertEqual(result["status"], FAIL)
        self.assertIn("unexpected_editor_process", result["reason_codes"])

    def test_openclaw_agent_subprocess_fails(self) -> None:
        records = _processes() + [
            {
                "pid": 203,
                "ppid": 100,
                "argv": ["openclaw", "agent", "--local", "--session-id", "fixture"],
            }
        ]
        result = evaluate_processes(records, complete=True)
        self.assertEqual(result["status"], FAIL)
        self.assertIn("unexpected_openclaw_agent_process", result["reason_codes"])

    def test_duplicate_worker_fails(self) -> None:
        records = _processes() + [
            {
                "pid": 202,
                "ppid": 1,
                "argv": ["/venv/bin/celery", "-A", "app.celery_app", "worker"],
            }
        ]
        result = evaluate_processes(records, complete=True)
        self.assertEqual(result["status"], FAIL)
        self.assertIn("duplicate_celery_workers", result["reason_codes"])

    def test_duplicate_beat_fails(self) -> None:
        records = _processes() + [
            {
                "pid": 202,
                "ppid": 1,
                "argv": ["/venv/bin/celery", "-A", "app.celery_app", "beat"],
            }
        ]
        result = evaluate_processes(records, complete=True)
        self.assertEqual(result["status"], FAIL)
        self.assertIn("duplicate_celery_beat", result["reason_codes"])

    def test_nonempty_redis_queue_fails(self) -> None:
        result = evaluate_redis_tasks(
            {"available": True, "queue_lengths": {"celery": 1}, "lease_keys": []},
            _task_snapshot(),
        )
        self.assertEqual(result["status"], FAIL)
        self.assertIn("redis_queue_nonempty", result["reason_codes"])

    def test_unexpected_concurrency_lease_key_fails(self) -> None:
        result = evaluate_redis_tasks(
            {
                "available": True,
                "queue_lengths": {"worker": 0},
                "lease_keys": ["orchestrator:backend_slots:local_openclaw"],
            },
            _task_snapshot(),
        )
        self.assertEqual(result["status"], FAIL)
        self.assertIn("redis_concurrency_lease_keys_present", result["reason_codes"])

    def test_redis_unavailable_is_inconclusive(self) -> None:
        result = evaluate_redis_tasks(None, _task_snapshot())
        self.assertEqual(result["status"], INCONCLUSIVE)
        self.assertIn("redis_unavailable", result["reason_codes"])

    def test_active_task_snapshot_fails(self) -> None:
        snapshot = _task_snapshot()
        snapshot["reserved"] = [{"task_id": "synthetic"}]
        result = evaluate_redis_tasks(
            {"available": True, "queue_lengths": {"worker": 0}, "lease_keys": []},
            snapshot,
        )
        self.assertEqual(result["status"], FAIL)
        self.assertIn("tasks_reserved_nonempty", result["reason_codes"])

    def test_active_session_fails(self) -> None:
        self.assertEqual(evaluate_sessions(1)["status"], FAIL)

    def test_sqlite_session_count_is_read_only_and_includes_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "sessions.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE sessions "
                    "(deleted_at TEXT, is_active INTEGER, status TEXT)"
                )
                connection.executemany(
                    "INSERT INTO sessions VALUES (?, ?, ?)",
                    [
                        (None, 0, "pending"),
                        (None, 1, "running"),
                        (None, 0, "completed"),
                        ("deleted", 1, "running"),
                    ],
                )
            before = database.read_bytes()
            self.assertEqual(collect_sessions(database), 2)
            self.assertEqual(database.read_bytes(), before)

    def test_missing_pin_fails(self) -> None:
        approved, observed = _valid_preflight()
        observed["pin"] = {"exists": False}
        result = evaluate_preflight(approved, observed)
        pin_check = next(
            item for item in result["checks"] if item["check_id"] == "pin_metadata"
        )
        self.assertEqual(pin_check["status"], FAIL)

    def test_pin_owner_group_and_mode_mismatch_fail(self) -> None:
        approved, observed = _valid_preflight()
        pin = dict(observed["pin"])
        pin.update(owner_uid=7, owner_gid=8, mode=0o644)
        observed["pin"] = pin
        result = evaluate_preflight(approved, observed)
        pin_check = next(
            item for item in result["checks"] if item["check_id"] == "pin_metadata"
        )
        self.assertEqual(pin_check["status"], FAIL)
        self.assertEqual(
            pin_check["differing_fields"], ["mode", "owner_gid", "owner_uid"]
        )

    def test_gate2_pin_mismatch_fails(self) -> None:
        fixture = _gate2_fixture()
        pin = dict(fixture["pin"])
        pin["candidate_head"] = "0" * 40
        fixture["pin"] = pin
        result = evaluate_gate2(**fixture)
        self.assertEqual(result["overall"], FAIL)
        self.assertIn("candidate_head", result["checks"][0]["differing_fields"])

    def test_valid_gate2_fixture_passes(self) -> None:
        result = evaluate_gate2(**_gate2_fixture())
        self.assertEqual(result["overall"], PASS)
        self.assertEqual(result["checks"][0]["differing_fields"], [])

    def test_manifest_mismatch_fails(self) -> None:
        fixture = _gate2_fixture()
        manifest = dict(fixture["manifest"])
        manifest["configuration_fingerprint"] = "0" * 64
        fixture["manifest"] = manifest
        result = evaluate_gate2(**fixture)
        self.assertEqual(result["overall"], FAIL)
        self.assertIn(
            "configuration_fingerprint", result["checks"][0]["differing_fields"]
        )

    def test_venv_drift_fails(self) -> None:
        approved, observed = _valid_preflight()
        observed["runtime"] = dict(observed["runtime"])
        observed["runtime"]["venv_inventory_sha256"] = "0" * 64
        result = evaluate_preflight(approved, observed)
        runtime = next(
            item
            for item in result["checks"]
            if item["check_id"] == "runtime_environment"
        )
        self.assertEqual(runtime["status"], FAIL)
        self.assertEqual(runtime["differing_fields"], ["venv_inventory_sha256"])

    def test_missing_backup_verification_fails(self) -> None:
        approved, observed = _valid_preflight()
        observed["backup"] = None
        result = evaluate_preflight(approved, observed)
        backup = next(
            item for item in result["checks"] if item["check_id"] == "evidence_backup"
        )
        self.assertEqual(backup["status"], FAIL)
        self.assertIn("backup_verification_missing", backup["reason_codes"])

    def test_protected_digest_difference_fails_by_field_only(self) -> None:
        approved, observed = _valid_preflight()
        observed["protected_observed"] = dict(observed["protected_observed"])
        observed["protected_observed"]["product_root_identity_sha256"] = "0" * 64
        result = evaluate_preflight(approved, observed)
        protected = next(
            item for item in result["checks"] if item["check_id"] == "protected_state"
        )
        self.assertEqual(protected["status"], FAIL)
        self.assertEqual(
            protected["differing_fields"], ["product_root_identity_sha256"]
        )

    def test_secret_canaries_are_not_printed(self) -> None:
        fixture = _gate2_fixture()
        canary = "PIN_SECRET_CANARY_DO_NOT_PRINT"
        fixture["pin_identity"] = dict(
            fixture["pin_identity"], operator_identity=canary
        )
        result = evaluate_gate2(**fixture)
        self.assertNotIn(canary, json.dumps(result))

    def test_read_only_inventory_leaves_write_denied_fixture_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "venv"
            package = root / "lib" / "python3.12" / "site-packages"
            package.mkdir(parents=True)
            file_path = package / "marker.py"
            file_path.write_text("sentinel = 1\n", encoding="utf-8")
            file_path.chmod(0o444)
            package.chmod(0o555)
            before = (
                file_path.read_bytes(),
                file_path.stat().st_mode,
                venv_inventory_digest(root),
            )
            after_digest = venv_inventory_digest(root)
            after = (file_path.read_bytes(), file_path.stat().st_mode, after_digest)
            self.assertEqual(after, before)
            package.chmod(0o755)

    def test_venv_inventory_is_deterministic_and_tracks_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "venv"
            root.mkdir()
            target = root / "pyvenv.cfg"
            target.write_text("version = 3.12\n", encoding="utf-8")
            first = venv_inventory_digest(root)
            self.assertEqual(first, venv_inventory_digest(root))
            target.write_text("version = 3.13\n", encoding="utf-8")
            self.assertNotEqual(first, venv_inventory_digest(root))

    def test_production_pin_parser_accepts_synthetic_approved_pin(self) -> None:
        from app.services.orchestration import worker_identity as wi

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "checkout"
            runtime_root = root / "runtime"
            source_root.mkdir()
            runtime_root.mkdir()
            pin_path = root / "candidate-pin.json"
            now = datetime.now(timezone.utc)
            pin_body = {
                "pin_schema_version": wi.CANDIDATE_PIN_SCHEMA_VERSION,
                "candidate_head": HEAD,
                "candidate_tree": TREE,
                "source_root": str(source_root),
                "source_manifest_digest": SOURCE_DIGEST,
                "launcher_identity": {
                    "launcher": "start.sh:start_workers",
                    "executable": "/usr/bin/python3",
                    "cwd": str(source_root),
                    "argv": [
                        "/usr/bin/python3",
                        str(source_root / "venv/bin/celery"),
                        "worker",
                    ],
                },
                "worker_node_identity": "celery@fixture",
                "configuration_schema_version": 1,
                "configuration_fingerprint": CONFIG_DIGEST,
                "configuration_policy_id": "worker-config-v1",
                "approval": {
                    "operator_identity": "operator",
                    "approval_id": "approval-1",
                    "approved_at": (now.replace(year=now.year - 1)).isoformat(),
                    "expires_at": (now.replace(year=now.year + 1)).isoformat(),
                },
            }
            pin_path.write_text(json.dumps(pin_body), encoding="utf-8")
            pin_path.chmod(0o600)
            loaded = wi.load_candidate_identity_pin(
                pin_path, source_root=source_root, runtime_root=runtime_root, now=now
            )
            self.assertEqual(loaded.status, "PIN_LOADED")
            self.assertIsNotNone(loaded.pin)

    def test_gate2_rejects_inconclusive_identity_and_absent_pin(self) -> None:
        fixture = _gate2_fixture()
        manifest = dict(fixture["manifest"])
        manifest["identity_status"] = "IDENTITY_INCONCLUSIVE"
        fixture["manifest"] = manifest
        self.assertEqual(evaluate_gate2(**fixture)["overall"], FAIL)

        fixture = _gate2_fixture()
        fixture["pin"] = None
        fixture["pin_identity"] = None
        fixture["pin_status"] = "PIN_ABSENT"
        self.assertEqual(evaluate_gate2(**fixture)["overall"], FAIL)


if __name__ == "__main__":
    unittest.main()
