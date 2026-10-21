from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b import operator_tooling as tooling

_PRODUCTION_PIN_OWNER = tooling._pin_owner
_REFRESH_AUTHORITY_CLASS = tooling.RefreshAuthority


def _make_test_authority(path: Path, *, clock=None):
    return _REFRESH_AUTHORITY_CLASS(
        path,
        required_uid=os.geteuid(),
        euid=os.geteuid,
        clock=clock,
    )


class PinOwnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        owner_patch = patch.object(
            tooling, "_pin_owner", lambda: (os.geteuid(), os.getegid())
        )
        owner_patch.start()
        self.addCleanup(owner_patch.stop)


TEST_SOURCE_ROOT = Path(__file__).resolve().parents[2]
# The suite uses a deliberately synthetic, non-authoritative approval policy.
# Production defaults stay unusable until the protected entry point loads a
# separately signed operator approval.
TEST_APPROVAL_ID = "TEST-ONLY-P39-P0B"
tooling.APPROVED_SOURCE_ROOT = str(TEST_SOURCE_ROOT)
tooling.APPROVED_HEAD = "1" * 40
tooling.APPROVED_TREE = "2" * 40
tooling.APPROVED_SOURCE_MANIFEST_DIGEST = "3" * 64
tooling.APPROVED_CONFIGURATION_FINGERPRINT = "4" * 64
tooling.APPROVED_CONFIGURATION_SCHEMA_VERSION = 2
tooling.APPROVED_CONFIGURATION_POLICY_ID = "test-only-policy"
tooling.APPROVED_WORKER_NODE = "test-only-worker"
tooling.APPROVED_OPERATOR = "test-only-operator"
tooling.APPROVED_APPROVAL_ID = TEST_APPROVAL_ID
tooling.APPROVED_WORKSHEET_SHA256 = "5" * 64
tooling.APPROVED = replace(
    tooling.APPROVED,
    source_root=str(TEST_SOURCE_ROOT),
    candidate_head=tooling.APPROVED_HEAD,
    candidate_tree=tooling.APPROVED_TREE,
    source_manifest_digest=tooling.APPROVED_SOURCE_MANIFEST_DIGEST,
    configuration_fingerprint=tooling.APPROVED_CONFIGURATION_FINGERPRINT,
    configuration_schema_version=tooling.APPROVED_CONFIGURATION_SCHEMA_VERSION,
    configuration_policy_id=tooling.APPROVED_CONFIGURATION_POLICY_ID,
    worker_node_identity=tooling.APPROVED_WORKER_NODE,
    operator_identity=tooling.APPROVED_OPERATOR,
    approval_id=tooling.APPROVED_APPROVAL_ID,
    worksheet_sha256=tooling.APPROVED_WORKSHEET_SHA256,
)


UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
PIN_SHA = "a" * 64
BOOT_WORKER = "11111111-1111-4111-8111-111111111111"
BOOT_GATE = "22222222-2222-4222-8222-222222222222"
BOOT_OTHER = "33333333-3333-4333-8333-333333333333"


class TimestampTests(unittest.TestCase):
    def test_nanosecond_conversion_preserves_microsecond_precision(self) -> None:
        instant = dt.datetime(2026, 10, 20, 4, 9, 12, 345678, tzinfo=UTC)
        expected = 1_792_469_352_345_678_000
        self.assertEqual(tooling._timestamp_ns(instant), expected)


def make_policy(root: Path, *, module_count: int = 150) -> tooling.ApprovedIdentity:
    return replace(
        tooling.APPROVED,
        source_root=str(root),
        affected_module_count=module_count,
        affected_module_names=tuple(
            f"pkg{index:03d}.module" for index in range(module_count)
        ),
    )


def make_worksheet(policy: tooling.ApprovedIdentity, path: Path) -> tuple[Path, str]:
    proposed = {
        "pin_schema_version": 1,
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "launcher_identity": policy.launcher_identity,
        "worker_node_identity": policy.worker_node_identity,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_policy_id": policy.configuration_policy_id,
        "approval": None,
    }
    document = {
        "worksheet_kind": "orchestrator-worker-candidate-pin-worksheet",
        "worksheet_schema_version": 1,
        "approval_state": "NOT_APPROVED",
        "OPERATOR_APPROVED_SOURCE": None,
        "OBSERVED_SOURCE": {
            "candidate_head": policy.candidate_head,
            "candidate_tree": policy.candidate_tree,
            "source_root": policy.source_root,
            "source_manifest_digest": policy.source_manifest_digest,
        },
        "configuration_identity": {
            "configuration_fingerprint": policy.configuration_fingerprint,
            "configuration_schema_version": policy.configuration_schema_version,
            "configuration_policy_id": policy.configuration_policy_id,
            "worker_concurrency": 4,
        },
        "proposed_pin_body": proposed,
        "derivation": {"affected_module_count": policy.affected_module_count},
    }
    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def make_old_pin(
    directory: Path, *, approval_id: str = tooling.PREVIOUS_APPROVAL_ID
) -> bytes:
    raw = json.dumps(
        {"approval": {"approval_id": approval_id}}, sort_keys=True
    ).encode()
    path = directory / tooling.PIN_FILE_NAME
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return raw


def fake_loader(path: Path, *, policy: tooling.ApprovedIdentity, runtime_root: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    approval = value.get("approval") or {}
    return {
        "approval_id": approval.get("approval_id"),
        "operator_identity": approval.get("operator_identity"),
        "approved_at": approval.get("approved_at"),
        "expires_at": approval.get("expires_at"),
        "candidate_head": value.get("candidate_head"),
        "candidate_tree": value.get("candidate_tree"),
        "source_manifest_digest": value.get("source_manifest_digest"),
        "pin_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def make_installed_receipt(
    policy: tooling.ApprovedIdentity,
    pin_path: Path,
    *,
    approved_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
) -> dict:
    pin_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(pin_path.parent, 0o700)
    approved = approved_at or FIXED_NOW - dt.timedelta(minutes=30)
    expires = expires_at or approved + dt.timedelta(hours=24)
    approval = {
        "operator_identity": policy.operator_identity,
        "approval_id": policy.approval_id,
        "approved_at": tooling._timestamp(approved),
        "expires_at": tooling._timestamp(expires),
    }
    body = {
        "pin_schema_version": 1,
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "launcher_identity": policy.launcher_identity,
        "worker_node_identity": policy.worker_node_identity,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_policy_id": policy.configuration_policy_id,
        "approval": approval,
    }
    raw = tooling._json_bytes(body)
    pin_path.write_bytes(raw)
    os.chmod(pin_path, 0o600)
    source_identity = {
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_policy_id": policy.configuration_policy_id,
        "launcher_identity": policy.launcher_identity,
        "worker_node_identity": policy.worker_node_identity,
    }
    return {
        "installation": "INSTALLED",
        "launch": "NOT_RUN",
        "readiness": "NOT_RUN",
        "gate_a": "NOT_RUN",
        "approval_id": policy.approval_id,
        "pin_sha256": hashlib.sha256(raw).hexdigest(),
        "path": str(pin_path.absolute()),
        "owner": "0:0",
        "mode": "0o600",
        "dir_mode": "0o700",
        "size": len(raw),
        "approval": approval,
        "loader_status": "PIN_LOADED",
        "receipt_schema_version": tooling.INSTALL_RECEIPT_SCHEMA_VERSION,
        "installation_receipt_id": str(uuid.uuid4()),
        "worksheet_sha256": policy.worksheet_sha256,
        "source_identity": source_identity,
    }


class InstallerTests(PinOwnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="p39-installer-test-")
        self.root = Path(self.temp.name)
        self.policy = make_policy(self.root)
        self.worksheet, self.worksheet_sha = make_worksheet(
            self.policy, self.root / "worksheet.json"
        )
        self.pin_dir = self.root / "pin-dir"
        self.pin_dir.mkdir(mode=0o700)
        os.chmod(self.pin_dir, 0o700)
        self.old_pin = make_old_pin(self.pin_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install(self, **kwargs):
        pin_dir = kwargs.pop("pin_dir", self.pin_dir)
        expected_worksheet_sha256 = kwargs.pop(
            "expected_worksheet_sha256", self.worksheet_sha
        )
        euid = kwargs.pop("euid", lambda: 0)
        loader = kwargs.pop("loader", fake_loader)
        checkout_verifier = kwargs.pop("checkout_verifier", lambda *_: None)
        return tooling.install_candidate_pin(
            self.worksheet,
            pin_dir=pin_dir,
            runtime_root=self.root / "runtime",
            dry_run=False,
            policy=self.policy,
            expected_worksheet_sha256=expected_worksheet_sha256,
            now=FIXED_NOW,
            euid=euid,
            loader=loader,
            checkout_verifier=checkout_verifier,
            **kwargs,
        )

    def test_production_pin_owner_defaults_to_root(self) -> None:
        self.assertEqual(_PRODUCTION_PIN_OWNER(), (0, 0))

    def test_dry_run_uses_production_loader_and_never_opens_pin_directory(self) -> None:
        missing_pin_dir = self.root / "must-not-be-opened"
        called = []

        def loader(path, *, policy, runtime_root):
            called.append(path)
            return fake_loader(path, policy=policy, runtime_root=Path(runtime_root))

        with patch.object(
            tooling,
            "_open_directory_no_symlink",
            side_effect=AssertionError("dry run touched the pin directory"),
        ):
            result = tooling.install_candidate_pin(
                self.worksheet,
                pin_dir=missing_pin_dir,
                runtime_root=self.root / "runtime",
                dry_run=True,
                policy=self.policy,
                expected_worksheet_sha256=self.worksheet_sha,
                now=FIXED_NOW,
                loader=loader,
                temp_root=self.root,
            )
        self.assertEqual(result["installation"], "NOT_INSTALLED")
        self.assertEqual(result["dry_run"], "PIN_LOADED")
        self.assertEqual(result["launch"], "NOT_RUN")
        self.assertEqual(len(called), 1)
        self.assertFalse(missing_pin_dir.exists())

    def test_approved_worksheet_installs_atomically_and_preserves_history(self) -> None:
        old_inode = (self.pin_dir / tooling.PIN_FILE_NAME).stat().st_ino
        result = self.install()
        new_path = self.pin_dir / tooling.PIN_FILE_NAME
        self.assertEqual(result["installation"], "INSTALLED")
        self.assertEqual(
            result["approval"]["approval_id"], tooling.APPROVED_APPROVAL_ID
        )
        approved_at = tooling._parse_timestamp(result["approval"]["approved_at"])
        expires_at = tooling._parse_timestamp(result["approval"]["expires_at"])
        self.assertEqual(expires_at - approved_at, dt.timedelta(hours=24))
        self.assertNotIn("pin_body", result)
        self.assertEqual(
            result["previous_pin_sha256"], hashlib.sha256(self.old_pin).hexdigest()
        )
        self.assertEqual(
            (self.pin_dir / tooling.HISTORY_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertEqual(
            json.loads(new_path.read_text())["approval"]["approval_id"],
            tooling.APPROVED_APPROVAL_ID,
        )
        self.assertNotEqual(new_path.stat().st_ino, old_inode)
        self.assertEqual(stat.S_IMODE(new_path.stat().st_mode), 0o600)
        self.assertFalse((self.pin_dir / tooling.INSTALL_TEMP_NAME).exists())

    def test_wrong_worksheet_sha_is_rejected_before_pin_directory_access(self) -> None:
        with patch.object(
            tooling, "_open_directory_no_symlink", side_effect=AssertionError
        ):
            with self.assertRaisesRegex(tooling.ToolingError, "worksheet SHA-256"):
                self.install(expected_worksheet_sha256="0" * 64)

    def test_source_drift_blocks_install_before_touching_pin_directory(self) -> None:
        with self.assertRaisesRegex(tooling.ToolingError, "source drift"):
            self.install(
                checkout_verifier=lambda *_: (_ for _ in ()).throw(
                    tooling.ToolingError("source drift before installation")
                )
            )
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertFalse((self.pin_dir / tooling.HISTORY_FILE_NAME).exists())

    def test_concurrent_installer_conflict_fails_closed(self) -> None:
        directory_fd = os.open(self.pin_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(tooling.ToolingError, "concurrent operation"):
                self.install()
        finally:
            os.close(directory_fd)
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertFalse((self.pin_dir / tooling.HISTORY_FILE_NAME).exists())

    def test_worksheet_with_wrong_approved_field_is_rejected_even_with_matching_digest(
        self,
    ) -> None:
        document = json.loads(self.worksheet.read_text())
        document["proposed_pin_body"]["candidate_head"] = "0" * 40
        raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
        self.worksheet.write_bytes(raw)
        with self.assertRaisesRegex(tooling.ToolingError, "candidate_head"):
            self.install(expected_worksheet_sha256=hashlib.sha256(raw).hexdigest())

    def test_worksheet_identity_and_schema_fields_fail_closed(self) -> None:
        mutations = [
            (
                lambda d: d["OBSERVED_SOURCE"].update(candidate_tree="0" * 40),
                "candidate_tree",
            ),
            (
                lambda d: d["proposed_pin_body"].update(source_root="/wrong"),
                "source_root",
            ),
            (
                lambda d: d["OBSERVED_SOURCE"].update(source_manifest_digest="0" * 64),
                "source_manifest_digest",
            ),
            (
                lambda d: d["configuration_identity"].update(
                    configuration_fingerprint="0" * 64
                ),
                "configuration identity mismatch",
            ),
            (
                lambda d: d["configuration_identity"].update(
                    configuration_schema_version=7
                ),
                "configuration identity mismatch",
            ),
            (
                lambda d: d["configuration_identity"].update(
                    configuration_policy_id="wrong"
                ),
                "configuration identity mismatch",
            ),
            (
                lambda d: d["configuration_identity"].update(worker_concurrency=8),
                "configuration identity mismatch",
            ),
            (
                lambda d: d["proposed_pin_body"].update(launcher_identity={"argv": []}),
                "launcher_identity",
            ),
            (
                lambda d: d["proposed_pin_body"].update(worker_node_identity="wrong"),
                "worker_node_identity",
            ),
            (
                lambda d: d["proposed_pin_body"].update(
                    configuration_fingerprint="0" * 64
                ),
                "configuration mismatch",
            ),
            (
                lambda d: d["proposed_pin_body"].update(
                    approval={"approval_id": "already-approved"}
                ),
                "pre-existing approval",
            ),
            (
                lambda d: d["proposed_pin_body"].update(pin_schema_version=2),
                "pin schema",
            ),
            (lambda d: d["proposed_pin_body"].update(unexpected=True), "pin schema"),
            (
                lambda d: d["derivation"].update(affected_module_count=149),
                "module count",
            ),
            (lambda d: d.update(approval_state="APPROVED"), "approval state"),
        ]
        original = json.loads(self.worksheet.read_text())
        for mutate, expected_error in mutations:
            with self.subTest(expected_error=expected_error):
                document = copy.deepcopy(original)
                mutate(document)
                raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
                self.worksheet.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                with self.assertRaisesRegex(tooling.ToolingError, expected_error):
                    tooling._approved_worksheet_body(
                        self.worksheet,
                        self.policy,
                        expected_worksheet_sha256=digest,
                    )
        self.worksheet.write_text(json.dumps(original, sort_keys=True, indent=2) + "\n")

    def test_non_root_real_install_is_rejected(self) -> None:
        with self.assertRaisesRegex(tooling.ToolingError, "requires root"):
            self.install(euid=lambda: 1000)

    def test_symlink_pin_directory_is_rejected(self) -> None:
        link = self.root / "pin-link"
        link.symlink_to(self.pin_dir, target_is_directory=True)
        with self.assertRaisesRegex(tooling.ToolingError, "symlink"):
            self.install(pin_dir=link)

    def test_wrong_pin_directory_mode_is_rejected(self) -> None:
        os.chmod(self.pin_dir, 0o750)
        with self.assertRaisesRegex(tooling.ToolingError, "mode 0700"):
            self.install()

    def test_wrong_pin_directory_owner_is_rejected(self) -> None:
        with patch.object(
            tooling,
            "_pin_owner",
            return_value=(os.geteuid() + 1, os.getegid()),
        ):
            with self.assertRaisesRegex(tooling.ToolingError, "root:root"):
                self.install()

    def test_existing_pin_symlink_is_rejected(self) -> None:
        path = self.pin_dir / tooling.PIN_FILE_NAME
        path.unlink()
        (self.pin_dir / "elsewhere").write_bytes(self.old_pin)
        path.symlink_to(self.pin_dir / "elsewhere")
        with self.assertRaises(tooling.ToolingError):
            self.install()
        self.assertTrue(path.is_symlink())

    def test_existing_pin_wrong_mode_is_rejected(self) -> None:
        os.chmod(self.pin_dir / tooling.PIN_FILE_NAME, 0o644)
        with self.assertRaisesRegex(tooling.ToolingError, "mode 0600"):
            self.install()

    def test_existing_pin_wrong_owner_is_rejected(self) -> None:
        read_at = tooling._read_at_regular

        def wrong_owner_read(directory_fd, name, **kwargs):
            kwargs["uid"] = os.geteuid() + 1
            kwargs["gid"] = os.getegid()
            return read_at(directory_fd, name, **kwargs)

        with patch.object(tooling, "_read_at_regular", side_effect=wrong_owner_read):
            with self.assertRaisesRegex(tooling.ToolingError, "root:root"):
                self.install()

    def test_unexpected_existing_temp_is_never_deleted(self) -> None:
        temp_path = self.pin_dir / tooling.INSTALL_TEMP_NAME
        temp_path.write_text("operator-owned temporary data")
        os.chmod(temp_path, 0o600)
        with self.assertRaisesRegex(tooling.ToolingError, "unexpected temporary"):
            self.install()
        self.assertEqual(temp_path.read_text(), "operator-owned temporary data")
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )

    def test_conflicting_history_copy_blocks_without_replacement(self) -> None:
        history = self.pin_dir / tooling.HISTORY_FILE_NAME
        history.write_bytes(b"different historical evidence")
        os.chmod(history, 0o600)
        with self.assertRaisesRegex(tooling.ToolingError, "differs from installed pin"):
            self.install()
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )

    def test_history_symlink_is_not_followed_or_overwritten(self) -> None:
        history = self.pin_dir / tooling.HISTORY_FILE_NAME
        destination = self.root / "history-target"
        destination.write_bytes(self.old_pin)
        history.symlink_to(destination)
        with self.assertRaises(tooling.ToolingError):
            self.install()
        self.assertTrue(history.is_symlink())

    def test_loader_failure_before_replace_preserves_installed_pin_and_cleans_only_own_temp(
        self,
    ) -> None:
        calls = 0

        def reject(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise tooling.ToolingError("injected loader rejection")

        with self.assertRaisesRegex(tooling.ToolingError, "injected loader"):
            self.install(loader=reject)
        self.assertEqual(calls, 1)
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertEqual(
            (self.pin_dir / tooling.HISTORY_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertFalse((self.pin_dir / tooling.INSTALL_TEMP_NAME).exists())

    def test_atomic_replace_failure_leaves_installed_pin_unchanged(self) -> None:
        with patch.object(
            tooling.os, "replace", side_effect=OSError("injected rename failure")
        ):
            with self.assertRaisesRegex(
                tooling.ToolingError, "atomic pin replacement failed"
            ):
                self.install()
        self.assertEqual(
            (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertEqual(
            (self.pin_dir / tooling.HISTORY_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertFalse((self.pin_dir / tooling.INSTALL_TEMP_NAME).exists())

    def test_post_replace_loader_failure_reports_suspect_pin_without_rollback(
        self,
    ) -> None:
        calls = 0

        def fail_after_replace(path, *, policy, runtime_root):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise tooling.ToolingError("injected post-replace rejection")
            return fake_loader(path, policy=policy, runtime_root=Path(runtime_root))

        with self.assertRaisesRegex(tooling.ToolingError, "post-replace"):
            self.install(loader=fail_after_replace)
        new_pin = (self.pin_dir / tooling.PIN_FILE_NAME).read_bytes()
        self.assertNotEqual(new_pin, self.old_pin)
        self.assertEqual(
            (self.pin_dir / tooling.HISTORY_FILE_NAME).read_bytes(), self.old_pin
        )
        self.assertFalse((self.pin_dir / tooling.INSTALL_TEMP_NAME).exists())

    def test_historical_worksheet_does_not_cover_worker_only_launcher(self) -> None:
        document = json.loads(self.worksheet.read_text(encoding="utf-8"))
        document["proposed_pin_body"]["launcher_identity"] = {"argv": []}
        actual_worksheet = self.root / "historical-worksheet-fixture.json"
        raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
        actual_worksheet.write_bytes(raw)
        with self.assertRaisesRegex(
            tooling.ToolingError, "source_root|launcher_identity"
        ):
            tooling.install_candidate_pin(
                actual_worksheet,
                pin_dir=self.root / "never-opened",
                runtime_root=tooling.DEFAULT_RUNTIME_ROOT,
                dry_run=True,
                policy=tooling.APPROVED,
                expected_worksheet_sha256=hashlib.sha256(raw).hexdigest(),
                now=dt.datetime.now(UTC),
            )

    def test_production_loader_rejects_invalid_private_pin_file(self) -> None:
        pin_path = self.root / "private-pin.json"
        pin_path.write_text("{}\n", encoding="utf-8")
        os.chmod(pin_path, 0o600)
        with self.assertRaisesRegex(
            tooling.ToolingError, "production pin loader rejected"
        ):
            tooling.production_pin_loader(
                pin_path,
                policy=tooling.APPROVED,
                runtime_root=self.root / "runtime",
            )

    def test_production_loader_accepts_private_installed_pin_and_receipt_binding(
        self,
    ) -> None:
        base_policy = tooling.APPROVED
        worksheet, worksheet_sha = make_worksheet(
            base_policy, self.root / "worksheet.json"
        )
        policy = replace(base_policy, worksheet_sha256=worksheet_sha)
        pin_dir = self.root / "private-pin-dir"
        pin_dir.mkdir(mode=0o700)
        os.chmod(pin_dir, 0o700)
        previous_pin = make_old_pin(pin_dir)
        pin_now = dt.datetime.now(UTC)
        result = tooling.install_candidate_pin(
            worksheet,
            pin_dir=pin_dir,
            runtime_root=self.root / "runtime",
            dry_run=False,
            policy=policy,
            expected_worksheet_sha256=worksheet_sha,
            now=pin_now,
            euid=lambda: 0,
            loader=tooling.production_pin_loader,
            checkout_verifier=lambda *_: None,
        )
        self.assertEqual(result["installation"], "INSTALLED")
        verified_sha = tooling._verify_install_receipt(
            result,
            pin_path=pin_dir / tooling.PIN_FILE_NAME,
            runtime_root=self.root / "runtime",
            policy=policy,
            now=pin_now,
            loader=tooling.production_pin_loader,
        )
        self.assertEqual(verified_sha, result["pin_sha256"])
        self.assertEqual(
            (pin_dir / tooling.HISTORY_FILE_NAME).read_bytes(),
            previous_pin,
        )

    def test_production_loader_rejects_expired_dry_run_pin(self) -> None:
        policy = tooling.APPROVED
        worksheet, worksheet_sha = make_worksheet(
            policy, self.root / "expired-worksheet.json"
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "production pin loader rejected"
        ):
            tooling.install_candidate_pin(
                worksheet,
                pin_dir=self.root / "never-opened",
                runtime_root=self.root / "runtime",
                dry_run=True,
                policy=policy,
                expected_worksheet_sha256=worksheet_sha,
                now=dt.datetime.now(UTC) - dt.timedelta(days=2),
            )


class InstallReceiptTests(PinOwnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="p39-receipt-test-")
        self.root = Path(self.temp.name)
        self.policy = make_policy(self.root)
        self.pin_path = self.root / "pin-dir" / tooling.PIN_FILE_NAME
        self.receipt = make_installed_receipt(self.policy, self.pin_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def verify(self, receipt=None, **kwargs):
        return tooling._verify_install_receipt(
            receipt or self.receipt,
            pin_path=self.pin_path,
            runtime_root=self.root / "runtime",
            policy=self.policy,
            now=FIXED_NOW,
            loader=kwargs.pop("loader", fake_loader),
            **kwargs,
        )

    def test_minimal_fabricated_receipt_is_rejected(self) -> None:
        with self.assertRaisesRegex(tooling.ToolingError, "incomplete"):
            self.verify(
                {
                    "installation": "INSTALLED",
                    "approval_id": self.policy.approval_id,
                    "pin_sha256": self.receipt["pin_sha256"],
                }
            )

    def test_stale_install_receipt_is_rejected(self) -> None:
        stale = copy.deepcopy(self.receipt)
        stale["approval"]["expires_at"] = tooling._timestamp(
            FIXED_NOW - dt.timedelta(seconds=1)
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "stale or approval is expired"
        ):
            self.verify(stale)

    def test_installed_pin_changed_after_receipt_is_rejected(self) -> None:
        self.pin_path.write_text("changed pin\n", encoding="utf-8")
        os.chmod(self.pin_path, 0o600)
        with self.assertRaisesRegex(tooling.ToolingError, "differs from its receipt"):
            self.verify()

    def test_missing_installed_pin_is_rejected(self) -> None:
        self.pin_path.unlink()
        with self.assertRaisesRegex(tooling.ToolingError, "missing, symlinked"):
            self.verify()

    def test_production_loader_rejection_blocks_receipt(self) -> None:
        def reject(*_args, **_kwargs):
            raise tooling.ToolingError("production loader rejection")

        with self.assertRaisesRegex(
            tooling.ToolingError, "production loader rejection"
        ):
            self.verify(loader=reject)

    def test_receipt_path_and_pin_metadata_must_match(self) -> None:
        wrong_path = copy.deepcopy(self.receipt)
        wrong_path["path"] = str(self.root / "other-pin.json")
        with self.assertRaisesRegex(tooling.ToolingError, "pin path"):
            self.verify(wrong_path)
        wrong_mode = copy.deepcopy(self.receipt)
        wrong_mode["mode"] = "0o644"
        with self.assertRaisesRegex(tooling.ToolingError, "metadata"):
            self.verify(wrong_mode)


def make_identity(policy: tooling.ApprovedIdentity, pid: int, ticks: str, argv=None):
    return tooling.ProcessIdentity(
        pid=pid,
        start_ticks=ticks,
        linux_boot_id=BOOT_WORKER,
        executable=policy.launcher_identity["executable"],
        cwd=policy.source_root,
        argv=tuple(argv if argv is not None else policy.launcher_identity["argv"]),
    )


def make_beat_identity(
    policy: tooling.ApprovedIdentity,
    runtime_root: Path,
    *,
    pid: int = 10,
    ticks: str = "300",
    boot_id: str = BOOT_WORKER,
) -> tooling.ProcessIdentity:
    return tooling.ProcessIdentity(
        pid=pid,
        start_ticks=ticks,
        linux_boot_id=boot_id,
        executable=policy.launcher_identity["executable"],
        cwd=policy.source_root,
        argv=tooling._expected_beat_argv(policy, runtime_root),
    )


class FakeObserver:
    def __init__(self, *, old, children, beat, new=None):
        self.beat = beat
        self.current = {old.pid: old, beat.pid: beat}
        self.children_by_parent = {old.pid: list(children)}
        self.matches = [old.pid, *(item.pid for item in children)]
        self.env = {}
        self.new = new
        self.wait_result = True
        self.leave_matches = []
        self.beat_after_signal = None
        self.remove_beat_after_signal = False
        self.identity_sequence = []

    def identity(self, pid):
        if self.identity_sequence and pid == self.identity_sequence[0][0]:
            _, value = self.identity_sequence.pop(0)
            return value
        return self.current.get(pid)

    def children(self, pid):
        return list(self.children_by_parent.get(pid, []))

    def worker_match_pids(self):
        return list(self.matches)

    def beat_match_pids(self):
        matches = []
        for pid, identity in self.current.items():
            argv = identity.argv
            if any(
                tuple(argv[index : index + len(tooling.BEAT_MATCH_ARGS)])
                == tooling.BEAT_MATCH_ARGS
                for index in range(max(0, len(argv) - len(tooling.BEAT_MATCH_ARGS) + 1))
            ):
                matches.append(pid)
        return sorted(matches)

    def environment_value(self, pid, name):
        return self.env.get((pid, name))

    def apply_pidfd_signal(self, pid, signum):
        if signum == signal.SIGTERM:
            old = self.current.pop(pid, None)
            if old is not None:
                for child in self.children_by_parent.get(pid, []):
                    self.current.pop(child.pid, None)
            if self.beat_after_signal is not None:
                self.current[self.beat_after_signal.pid] = self.beat_after_signal
            if self.remove_beat_after_signal:
                self.current.pop(self.beat.pid, None)
            self.matches = list(self.leave_matches)


class FakePidfdOperations:
    """Call-order mock; it does not prove kernel pidfd target semantics."""

    def __init__(self, observer):
        self.observer = observer
        self.next_fd = 900
        self.targets = {}
        self.exited = set()
        self.opened = []
        self.closed = []
        self.signal_calls = []
        self.signal_fds = []
        self.wait_timeout = None
        self.wait_result = True
        self.before_open = None
        self.after_open = None
        self.invalid_fds = set()
        self.open_error = None
        self.signal_error = None
        self.target_error = None

    def open(self, pid):
        if self.open_error is not None:
            raise self.open_error
        if self.before_open:
            self.before_open(pid)
        fd = self.next_fd
        self.next_fd += 1
        self.targets[fd] = pid
        self.opened.append((pid, fd))
        if self.after_open:
            self.after_open(pid, fd)
        return fd

    def is_exited(self, pidfd):
        return pidfd in self.exited

    def target_pid(self, pidfd):
        if self.target_error is not None:
            raise self.target_error
        if pidfd in self.invalid_fds or pidfd not in self.targets:
            raise tooling.PidfdError("mock invalid pidfd")
        return self.targets[pidfd]

    def send_signal(self, pidfd, signum):
        if self.signal_error is not None:
            raise self.signal_error
        target = self.target_pid(pidfd)
        self.signal_calls.append((target, signum))
        self.signal_fds.append(pidfd)
        self.observer.apply_pidfd_signal(target, signum)

    def wait_for_exit(self, pidfd, timeout_seconds):
        self.target_pid(pidfd)
        self.wait_timeout = timeout_seconds
        return self.wait_result

    def close(self, pidfd):
        self.closed.append(pidfd)


class StaticBeatObserver:
    def __init__(self, beat):
        self.current = {beat.pid: beat}

    def beat_match_pids(self):
        matches = []
        for pid, identity in self.current.items():
            argv = identity.argv
            if any(
                tuple(argv[index : index + len(tooling.BEAT_MATCH_ARGS)])
                == tooling.BEAT_MATCH_ARGS
                for index in range(max(0, len(argv) - len(tooling.BEAT_MATCH_ARGS) + 1))
            ):
                matches.append(pid)
        return sorted(matches)

    def identity(self, pid):
        return self.current.get(pid)


class RefreshTests(PinOwnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="p39-refresh-test-")
        self.root = Path(self.temp.name)
        # Use a private, controlled copy for the test's approved interpreter.
        # Hosted toolcache ownership and write bits vary between CI runners;
        # production still requires the fixed root-owned system interpreter.
        approved_python = self.root / "approved-system-python"
        shutil.copyfile(sys.executable, approved_python)
        os.chmod(approved_python, 0o755)
        self._approved_python_patch = patch.multiple(
            tooling,
            _APPROVED_SYSTEM_PYTHON=approved_python,
            _APPROVED_SYSTEM_PYTHON_UID=os.geteuid(),
        )
        self._approved_python_patch.start()
        self.addCleanup(self._approved_python_patch.stop)
        self.policy = make_policy(self.root)
        self.runtime_root = self.root / "runtime"
        self.pin_path = self.root / "pin.json"
        self.old = make_identity(self.policy, 44, "900")
        self.child = make_identity(self.policy, 45, "901", argv=("celery-child",))
        self.beat = make_beat_identity(self.policy, self.runtime_root)
        self.new = make_identity(self.policy, 55, "1000")
        self.observer = FakeObserver(
            old=self.old, children=[self.child], beat=self.beat, new=self.new
        )
        self.pidfd = FakePidfdOperations(self.observer)
        self.authority = _make_test_authority(
            self.root / "refresh-authority", clock=lambda: FIXED_NOW
        )
        self.observer.current[self.child.pid] = self.child
        self.preflight = {
            "observed_at": tooling._timestamp(FIXED_NOW - dt.timedelta(seconds=2)),
            "master_pid": self.old.pid,
            "master_start_ticks": self.old.start_ticks,
            "approval_id": self.policy.approval_id,
            "children": [self.child.to_dict()],
            "beat": self.beat.to_dict(),
            "active": [],
            "reserved": [],
            "scheduled": [],
            "queue_depth": 0,
            "slot_keys": [],
            "leases": [],
            "active_sessions": 0,
            "environment_configuration_overrides": [],
            "configuration_fingerprint": self.policy.configuration_fingerprint,
        }
        self.receipt = make_installed_receipt(self.policy, self.pin_path)
        self.manifest_path = tooling._manifest_path_for_identity(
            self.runtime_root, self.new
        )
        self.manifest_path.parent.mkdir(parents=True)
        pin_identity = {
            "pin_schema_version": 1,
            "pin_sha256": self.receipt["pin_sha256"],
            "candidate_head": self.policy.candidate_head,
            "candidate_tree": self.policy.candidate_tree,
            "source_manifest_digest": self.policy.source_manifest_digest,
            "operator_identity": self.policy.operator_identity,
            "approval_id": self.policy.approval_id,
            **self.receipt["approval"],
        }
        self.manifest = {
            "candidate_head": self.policy.candidate_head,
            "candidate_tree": self.policy.candidate_tree,
            "candidate_source_root": self.policy.source_root,
            "source_manifest_digest": self.policy.source_manifest_digest,
            "configuration_fingerprint": self.policy.configuration_fingerprint,
            "configuration_schema_version": self.policy.configuration_schema_version,
            "configuration_policy_id": self.policy.configuration_policy_id,
            "worker_node_identity": self.policy.worker_node_identity,
            "identity_status": "IDENTITY_VERIFIED",
            "candidate_pin_status": "PIN_MATCHED",
            "identity_status_reasons": [],
            "worker_executable": self.new.executable,
            "worker_cwd": self.new.cwd,
            "worker_argv": list(self.new.argv),
            "worker_pid": self.new.pid,
            "worker_start_ticks": self.new.start_ticks,
            "linux_boot_id": self.new.linux_boot_id,
            "record_id": f"{self.new.linux_boot_id}-{self.new.pid}-{self.new.start_ticks}",
            "worker_children": [],
            "boot_timestamp": tooling._timestamp(FIXED_NOW),
            "candidate_pin_identity": pin_identity,
        }
        self.launch_calls = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def launcher(self, root, pin_path):
        self.launch_calls.append((str(root), str(pin_path)))
        self.observer.current[self.new.pid] = self.new
        self.observer.matches = [self.new.pid]
        self.observer.env[(self.new.pid, "WORKER_CANDIDATE_PIN_PATH")] = str(pin_path)
        return {
            "worker_pid": self.new.pid,
            "returncode": 0,
            "launch_attempts": 1,
        }

    def manifest_reader(
        self, runtime_root, *, pid, start_ticks, linux_boot_id, launched_at
    ):
        self.assertEqual((pid, start_ticks), (self.new.pid, self.new.start_ticks))
        self.assertEqual(linux_boot_id, self.new.linux_boot_id)
        self.manifest_path.write_bytes(tooling._json_bytes(self.manifest))
        os.chmod(self.manifest_path, 0o600)
        mtime_ns = tooling._timestamp_ns(launched_at) + 1_000
        os.utime(self.manifest_path, ns=(mtime_ns, mtime_ns))
        return self.manifest, self.manifest_path

    def run_refresh(self, **kwargs):
        master_pid = kwargs.pop("master_pid", self.old.pid)
        expected_start_ticks = kwargs.pop("expected_start_ticks", self.old.start_ticks)
        checkout_verifier = kwargs.pop("checkout_verifier", lambda *_: None)
        launcher_path_verifier = kwargs.pop("launcher_path_verifier", lambda *_: None)
        pin_loader = kwargs.pop("pin_loader", fake_loader)
        installation_receipt = kwargs.pop("installation_receipt", self.receipt)
        launcher = kwargs.pop("launcher", self.launcher)
        manifest_reader = kwargs.pop("manifest_reader", self.manifest_reader)
        ping = kwargs.pop("ping", lambda root, node: True)
        with patch.object(
            tooling, "DEFAULT_REFRESH_AUTHORITY_ROOT", str(self.authority.root)
        ):
            with patch.object(
                tooling, "RefreshAuthority", lambda *_args, **_kwargs: self.authority
            ):
                with patch.object(tooling, "PidfdOperations", lambda: self.pidfd):
                    return tooling.refresh_worker(
                        master_pid=master_pid,
                        expected_start_ticks=expected_start_ticks,
                        preflight=self.preflight,
                        installation_receipt=installation_receipt,
                        observer=self.observer,
                        launcher=launcher,
                        manifest_reader=manifest_reader,
                        ping=ping,
                        source_root=self.root,
                        pin_path=self.pin_path,
                        runtime_root=self.runtime_root,
                        policy=self.policy,
                        now=lambda: FIXED_NOW,
                        checkout_verifier=checkout_verifier,
                        launcher_path_verifier=launcher_path_verifier,
                        pin_loader=pin_loader,
                        **kwargs,
                    )

    def test_refresh_sends_one_sigterm_preserves_beat_and_requires_fresh_ping(
        self,
    ) -> None:
        result = self.run_refresh()
        self.assertEqual(result["installation"], "INSTALLED")
        self.assertEqual(result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(result["process_observation"], "PROCESS_OBSERVED")
        self.assertEqual(result["identity_verification"], "IDENTITY_VERIFIED")
        self.assertEqual(result["readiness"], "READY")
        self.assertEqual(result["gate_a"], "NOT_RUN")
        self.assertEqual(
            result["beat_process_identity"],
            {"role": tooling.BEAT_ROLE, **self.beat.to_dict()},
        )
        self.assertEqual(
            self.pidfd.signal_calls,
            [(self.old.pid, signal.SIGTERM)],
        )
        self.assertEqual(self.pidfd.signal_fds, [self.pidfd.opened[0][1]])
        self.assertEqual(self.pidfd.closed, [self.pidfd.opened[0][1]])
        self.assertEqual(self.pidfd.wait_timeout, tooling.SHUTDOWN_TIMEOUT_SECONDS)
        self.assertEqual(
            self.launch_calls, [(str(self.root), str(self.root / "pin.json"))]
        )

    def test_stale_pid_ticks_refuse_before_signal(self) -> None:
        self.preflight["master_start_ticks"] = "899"
        with self.assertRaisesRegex(tooling.ToolingError, "PID/start ticks"):
            self.run_refresh(expected_start_ticks="899")
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_busy_worker_or_nonempty_queue_blocks(self) -> None:
        self.preflight["active"] = [{"id": "busy"}]
        with self.assertRaisesRegex(tooling.ToolingError, "not idle"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.preflight["active"] = []
        self.preflight["queue_depth"] = 1
        with self.assertRaisesRegex(tooling.ToolingError, "queue"):
            self.run_refresh()

    def test_checkout_and_launcher_environment_preflight_fail_before_signal(
        self,
    ) -> None:
        with self.assertRaisesRegex(tooling.ToolingError, "source drift"):
            self.run_refresh(
                checkout_verifier=lambda *_: (_ for _ in ()).throw(
                    tooling.ToolingError("source drift injected")
                )
            )
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_every_idle_preflight_dimension_is_fail_closed(self) -> None:
        cases = [
            ("active", [{"id": "busy"}], "not idle"),
            ("reserved", [{"id": "reserved"}], "not idle"),
            ("scheduled", [{"id": "scheduled"}], "not idle"),
            ("queue_depth", None, "queue"),
            ("slot_keys", ["slot"], "not idle"),
            ("leases", ["lease"], "not idle"),
            ("active_sessions", 1, "active sessions"),
            (
                "environment_configuration_overrides",
                ["override"],
                "unapproved environment overrides",
            ),
            ("configuration_fingerprint", "wrong", "configuration fingerprint"),
        ]
        for field, value, expected_error in cases:
            with self.subTest(field=field):
                original = self.preflight[field]
                self.preflight[field] = value
                try:
                    with self.assertRaisesRegex(tooling.ToolingError, expected_error):
                        self.run_refresh()
                finally:
                    self.preflight[field] = original
        self.assertEqual(self.pidfd.signal_calls, [])
        env_path = self.root / ".env"
        env_path.write_text("WORKER_CANDIDATE_PIN_PATH=/wrong/path\n")
        with self.assertRaisesRegex(tooling.ToolingError, "must not override"):
            self.run_refresh()
        env_path.write_text("CELERY_WORKER_CONCURRENCY=8\n")
        with self.assertRaisesRegex(tooling.ToolingError, "concurrency"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_checkout_is_reverified_before_launch(self) -> None:
        calls = []

        def verify(root, policy):
            calls.append("verify")
            if len(calls) == 2:
                raise tooling.ToolingError("source drift after shutdown")

        with self.assertRaisesRegex(tooling.ToolingError, "after shutdown"):
            self.run_refresh(checkout_verifier=verify)
        self.assertEqual(calls, ["verify", "verify"])
        self.assertEqual(self.pidfd.signal_calls, [(self.old.pid, signal.SIGTERM)])
        self.assertEqual(self.launch_calls, [])

    def test_worker_match_to_another_process_refuses_without_signalling(self) -> None:
        self.observer.matches.append(999)
        with self.assertRaisesRegex(tooling.ToolingError, "match set"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(self.launch_calls, [])

    def test_pid_reuse_immediately_before_signal_refuses(self) -> None:
        reused = replace(self.old, start_ticks="9999")
        self.observer.identity_sequence = [
            (self.old.pid, self.old),
            (self.old.pid, reused),
        ]
        with self.assertRaisesRegex(tooling.ToolingError, "immediately before SIGTERM"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_pid_replaced_between_observation_and_pidfd_acquisition_is_refused(
        self,
    ) -> None:
        reused = replace(
            self.old,
            start_ticks="9999",
            executable="/usr/bin/sleep",
            argv=("sleep", "10"),
        )
        self.pidfd.before_open = lambda _pid: self.observer.current.__setitem__(
            self.old.pid, reused
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "pidfd target after acquisition"
        ):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(len(self.pidfd.closed), 1)

    def test_pid_replaced_immediately_after_pidfd_acquisition_is_refused(self) -> None:
        reused = replace(
            self.old,
            start_ticks="9999",
            executable="/usr/bin/sleep",
            argv=("sleep", "10"),
        )
        self.pidfd.after_open = lambda _pid, _fd: self.observer.current.__setitem__(
            self.old.pid, reused
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "pidfd target after acquisition"
        ):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(len(self.pidfd.closed), 1)

    def test_pidfd_target_must_match_all_process_identity_fields(self) -> None:
        replacements = [
            replace(self.old, start_ticks="901"),
            replace(self.old, linux_boot_id=BOOT_OTHER),
            replace(self.old, executable="/usr/bin/sleep"),
            replace(self.old, cwd="/tmp"),
            replace(self.old, argv=("sleep", "10")),
        ]
        for replacement in replacements:
            with self.subTest(identity=replacement.to_dict()):
                self.observer.current[self.old.pid] = self.old
                self.observer.matches = [self.old.pid, self.child.pid]
                self.pidfd = FakePidfdOperations(self.observer)
                self.pidfd.after_open = lambda _pid, _fd, replacement=replacement: self.observer.current.__setitem__(
                    self.old.pid, replacement
                )
                with self.assertRaisesRegex(
                    tooling.ToolingError, "process identity changed"
                ):
                    self.run_refresh()
                self.assertEqual(self.pidfd.signal_calls, [])
                self.assertEqual(len(self.pidfd.closed), 1)

    def test_pid_replaced_during_final_receipt_validation_is_never_signaled(
        self,
    ) -> None:
        reused = replace(
            self.old,
            start_ticks="999999",
            executable="/usr/bin/sleep",
            cwd="/tmp",
            argv=("sleep", "10"),
        )
        calls = 0

        def replace_during_validation(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.observer.current[self.old.pid] = reused
            return self.receipt["pin_sha256"]

        with self.assertRaisesRegex(tooling.ToolingError, "after durable PREPARED"):
            self.run_refresh(receipt_validator=replace_during_validation)
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(len(self.pidfd.closed), 1)

    def test_pidfd_exit_before_signal_and_pid_reuse_after_exit_are_refused(
        self,
    ) -> None:
        reused = replace(
            self.old,
            start_ticks="1000000",
            executable="/usr/bin/sleep",
            argv=("sleep", "10"),
        )

        def exit_and_reuse(_pid, fd):
            self.pidfd.exited.add(fd)
            self.observer.current[self.old.pid] = reused

        self.pidfd.after_open = exit_and_reuse
        with self.assertRaisesRegex(tooling.ToolingError, "exited before signaling"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(len(self.pidfd.closed), 1)

    def test_pidfd_invalid_unsupported_and_permission_errors_fail_closed_and_close(
        self,
    ) -> None:
        cases = [
            (
                "invalid pidfd",
                lambda fd: self.pidfd.invalid_fds.add(fd),
                "mock invalid pidfd",
            ),
            (
                "unsupported syscall",
                lambda _fd: setattr(
                    self.pidfd,
                    "signal_error",
                    tooling.UnsupportedPidfdError("unsupported"),
                ),
                "unsupported",
            ),
            (
                "permission denied",
                lambda _fd: setattr(
                    self.pidfd, "signal_error", PermissionError(1, "denied")
                ),
                "denied",
            ),
        ]
        for label, configure, error in cases:
            with self.subTest(case=label):
                self.authority = _make_test_authority(
                    self.root / f"pidfd-error-authority-{label.replace(' ', '-')}",
                    clock=lambda: FIXED_NOW,
                )
                self.pidfd = FakePidfdOperations(self.observer)
                self.pidfd.after_open = lambda _pid, fd, configure=configure: configure(
                    fd
                )
                with self.assertRaisesRegex(tooling.ToolingError, error):
                    self.run_refresh()
                self.assertEqual(self.pidfd.signal_calls, [])
                self.assertEqual(len(self.pidfd.closed), 1)

    def test_authority_failure_after_sigterm_returns_recovery_required(self) -> None:
        append_stage = self.authority.append_stage

        def fail_sigterm_stage(directory_fd, refresh_id, stage, payload):
            if stage == "SIGTERM_SENT":
                raise tooling.RefreshAuthorityError("injected durable stage failure")
            return append_stage(directory_fd, refresh_id, stage, payload)

        self.authority.append_stage = fail_sigterm_stage
        with self.assertRaises(tooling.RecoveryRequiredError) as caught:
            self.run_refresh()
        self.assertEqual(caught.exception.result["decision"], "RECOVERY_REQUIRED")
        self.assertEqual(self.pidfd.signal_calls, [(self.old.pid, signal.SIGTERM)])
        self.assertEqual(self.launch_calls, [])
        record = self.authority.load(caught.exception.result["refresh_id"])
        self.assertEqual(record.last_stage, "RECOVERY_REQUIRED")

    def test_pre_signal_authority_fsync_failure_stops_before_sigterm(self) -> None:
        with self.authority.locked(exclusive=True, create=True):
            pass

        def fail_fsync(_fd):
            raise OSError("injected authority fsync failure")

        self.authority.fsync = fail_fsync
        with self.assertRaisesRegex(tooling.ToolingError, "fsync failure"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(len(self.pidfd.closed), 1)

    def test_ready_write_failure_is_marked_recovery_and_cannot_remain_ready(
        self,
    ) -> None:
        append_stage = self.authority.append_stage

        def fail_after_ready_append(directory_fd, refresh_id, stage, payload):
            record = append_stage(directory_fd, refresh_id, stage, payload)
            if stage == "READY":
                raise tooling.RefreshAuthorityError("injected READY durability failure")
            return record

        self.authority.append_stage = fail_after_ready_append
        with self.assertRaises(tooling.RecoveryRequiredError) as caught:
            self.run_refresh()
        self.assertEqual(caught.exception.result["decision"], "RECOVERY_REQUIRED")
        record = self.authority.load(caught.exception.result["refresh_id"])
        self.assertEqual(record.last_stage, "RECOVERY_REQUIRED")
        with self.assertRaisesRegex(tooling.RefreshAuthorityError, "complete READY"):
            self.authority.final_receipt(record)

    def test_shutdown_timeout_does_not_escalate_or_launch(self) -> None:
        self.pidfd.wait_result = False
        with self.assertRaisesRegex(tooling.ToolingError, "no escalation"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [(self.old.pid, signal.SIGTERM)])
        self.assertEqual(self.launch_calls, [])

    def test_remaining_worker_match_after_shutdown_blocks_launch(self) -> None:
        self.observer.leave_matches = [777]
        with self.assertRaisesRegex(
            tooling.ToolingError, "remain after graceful shutdown"
        ):
            self.run_refresh()
        self.assertEqual(self.launch_calls, [])

    def test_beat_change_blocks_launch(self) -> None:
        self.observer.beat_after_signal = replace(self.beat, start_ticks="301")
        with self.assertRaisesRegex(tooling.ToolingError, "Beat after shutdown"):
            self.run_refresh()
        self.assertEqual(self.launch_calls, [])

    def test_new_worker_must_have_pin_path_in_environment(self) -> None:
        def launch_without_pin(root, pin_path):
            self.observer.current[self.new.pid] = self.new
            self.observer.matches = [self.new.pid]
            return {"worker_pid": self.new.pid, "launch_attempts": 1}

        with self.assertRaisesRegex(tooling.ToolingError, "approved pin path"):
            self.run_refresh(launcher=launch_without_pin)

    def test_ping_failure_does_not_report_ready(self) -> None:
        with self.assertRaisesRegex(tooling.ToolingError, "independent Celery ping"):
            self.run_refresh(ping=lambda *_: False)

    def _make_isolated_start_fixture(
        self, *, worker_exit: int = 0
    ) -> tuple[Path, Path, Path]:
        root = self.root / "isolated-start"
        (root / "run").mkdir(parents=True)
        (root / "logs").mkdir()
        (root / "checkpoints").mkdir()
        (root / "venv/bin").mkdir(parents=True)
        system_bin = root / "system-bin"
        system_bin.mkdir()
        (system_bin / "env").symlink_to("/usr/bin/env")
        (system_bin / "nohup").symlink_to("/usr/bin/nohup")
        (root / "logs/worker.log").write_text("", encoding="utf-8")
        shutil.copyfile(sys.executable, root / "venv/bin/python3")
        os.chmod(root / "venv/bin/python3", 0o755)
        start = root / "start.sh"
        shutil.copyfile(Path(tooling.APPROVED_SOURCE_ROOT) / "start.sh", start)
        source = start.read_text(encoding="utf-8")
        process_check_start = source.index("worker_only_check_processes() {")
        process_check_end = source.index(
            "\n\n# Start one worker attempt", process_check_start
        )
        source = (
            source[:process_check_start]
            + "worker_only_check_processes() { return 1; }"
            + source[process_check_end:]
        )
        source = source.replace(
            "/usr/bin/python3.12", str(tooling._APPROVED_SYSTEM_PYTHON)
        )
        source = source.replace("/usr/bin/env", str(system_bin / "env"))
        source = source.replace("/usr/bin/nohup", str(system_bin / "nohup"))
        start.write_text(source, encoding="utf-8")
        celery_log = root / "logs/worker.log"
        fake_celery = root / "venv/bin/celery"
        fake_celery.write_text(
            f"#!{root}/venv/bin/python3\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['WORKER_CANDIDATE_PIN_PATH'] + '.argv').write_text(json.dumps(sys.argv))\n"
            f"raise SystemExit({worker_exit})\n",
            encoding="utf-8",
        )
        os.chmod(fake_celery, 0o755)
        beat_pid = root / "run/beat.pid"
        beat_pid.write_text("321\n", encoding="ascii")
        return root, celery_log, beat_pid

    def _wait_for_worker_argv(self, root: Path) -> list[str]:
        path = root / "candidate-pin.json.argv"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.01)
        self.assertTrue(path.exists(), "fake worker did not record its argv")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_fixed_worker_executable_accepts_trusted_system_symlink(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        original_lstat = os.lstat
        original_readlink = os.readlink
        symlink_path = "/usr/bin/nohup"

        def lstat_with_system_symlink(path, *args, **kwargs):
            if os.fsdecode(path) == symlink_path:
                info = original_lstat(path)
                fields = list(info)
                fields[0] = stat.S_IFLNK | 0o777
                return os.stat_result(fields)
            return original_lstat(path, *args, **kwargs)

        def readlink_with_system_symlink(path, *args, **kwargs):
            if os.fsdecode(path) == symlink_path:
                return "/usr/bin/env"
            return original_readlink(path, *args, **kwargs)

        with patch.object(tooling.os, "lstat", side_effect=lstat_with_system_symlink):
            with patch.object(
                tooling.os, "readlink", side_effect=readlink_with_system_symlink
            ):
                tooling._verify_worker_only_executables(root)

    def test_fixed_worker_executable_rejects_untrusted_symlink_target(self) -> None:
        target = self.root / "untrusted-worker-tool"
        shutil.copyfile(sys.executable, target)
        os.chmod(target, 0o755)
        original_lstat = os.lstat
        original_readlink = os.readlink
        symlink_path = "/usr/bin/nohup"

        def lstat_with_system_symlink(path, *args, **kwargs):
            if os.fsdecode(path) == symlink_path:
                info = original_lstat(path)
                fields = list(info)
                fields[0] = stat.S_IFLNK | 0o777
                return os.stat_result(fields)
            return original_lstat(path, *args, **kwargs)

        def readlink_with_untrusted_target(path, *args, **kwargs):
            if os.fsdecode(path) == symlink_path:
                return str(target)
            return original_readlink(path, *args, **kwargs)

        with patch.object(tooling.os, "lstat", side_effect=lstat_with_system_symlink):
            with patch.object(
                tooling.os, "readlink", side_effect=readlink_with_untrusted_target
            ):
                with self.assertRaisesRegex(tooling.ToolingError, "unexpected"):
                    tooling._verify_fixed_worker_executable(symlink_path)

    def test_actual_start_worker_only_bash_fixture_launches_exactly_one_worker(
        self,
    ) -> None:
        root, celery_log, beat_pid = self._make_isolated_start_fixture()
        original_beat_pid = beat_pid.read_bytes()
        result = tooling.run_start_worker_only_launcher(
            root, root / "candidate-pin.json"
        )
        argv = self._wait_for_worker_argv(root)
        self.assertEqual(result["launch_attempts"], 1)
        self.assertEqual(result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(
            argv,
            [
                str(root / "venv/bin/celery"),
                "-A",
                "app.celery_app",
                "worker",
                "--concurrency=4",
                "--loglevel=info",
            ],
        )
        self.assertEqual(celery_log.read_text(), "")
        self.assertEqual(beat_pid.read_bytes(), original_beat_pid)
        self.assertFalse((root / "run/celerybeat-schedule").exists())
        self.assertFalse((root / "run/worker.pid").exists())

    def test_caller_path_shadowing_and_unexpected_commands_never_execute(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        shadow = root / "caller-path"
        shadow.mkdir()
        names = (
            "env",
            "setsid",
            "nohup",
            "dirname",
            "pgrep",
            "pkill",
            "sleep",
            "readlink",
        )
        markers = []
        for name in names:
            marker = root / f"{name}.executed"
            wrapper = shadow / name
            wrapper.write_text(
                f"#!/bin/sh\n: > '{marker}'\nexit 93\n", encoding="utf-8"
            )
            os.chmod(wrapper, 0o755)
            markers.append(marker)
        completed = subprocess.run(
            [
                "/usr/bin/bash",
                str(root / "start.sh"),
                "start_worker_only",
                str(root / "candidate-pin.json"),
            ],
            cwd=root,
            env={"PATH": str(shadow), "HOME": "/root", "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self._wait_for_worker_argv(root)
        self.assertRegex(completed.stdout, r"^LAUNCH_ATTEMPTED pid=[1-9][0-9]*\n$")
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_env_path_shadowing_blocks_before_process_creation(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        shadow = root / "dotenv-path"
        shadow.mkdir()
        marker = root / "dotenv-shadow-executed"
        wrapper = shadow / "setsid"
        wrapper.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 93\n", encoding="utf-8")
        os.chmod(wrapper, 0o755)
        for assignment in (
            f"PATH={shadow}:/usr/bin:/bin\n",
            f"export PATH={shadow}:/usr/bin:/bin\n",
            f"export\tPATH={shadow}:/usr/bin:/bin\n",
        ):
            (root / ".env").write_text(assignment, encoding="utf-8")
            with self.subTest(assignment=assignment.split("=", 1)[0]):
                with self.assertRaisesRegex(
                    tooling.ToolingError, r"\.env must not set PATH"
                ):
                    tooling.run_start_worker_only_launcher(
                        root, root / "candidate-pin.json"
                    )
        self.assertFalse(marker.exists())
        self.assertFalse((root / "candidate-pin.json.argv").exists())

    def test_actual_bash_env_path_shadowing_blocks_before_worker_creation(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        shadow = root / "direct-dotenv-path"
        shadow.mkdir()
        marker = root / "direct-dotenv-shadow-executed"
        fake = shadow / "setsid"
        fake.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 93\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        start = root / "start.sh"
        for assignment in (
            f"PATH={shadow}:/usr/bin:/bin\n",
            f"export PATH={shadow}:/usr/bin:/bin\n",
            f"export\tPATH={shadow}:/usr/bin:/bin\n",
        ):
            (root / ".env").write_text(assignment, encoding="utf-8")
            with self.subTest(assignment=assignment.split("=", 1)[0]):
                completed = subprocess.run(
                    [
                        "/usr/bin/bash",
                        str(start),
                        "start_worker_only",
                        str(root / "candidate-pin.json"),
                    ],
                    cwd=root,
                    env={"PATH": str(shadow), "HOME": "/root", "LANG": "C.UTF-8"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("worker .env must not set PATH", completed.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((root / "candidate-pin.json.argv").exists())

    def test_unauthorized_setsid_resolution_is_never_used(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        shadow = root / "shadow-setsid"
        shadow.mkdir()
        marker = root / "setsid-shadow-executed"
        fake = shadow / "setsid"
        fake.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 93\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        with patch.dict(os.environ, {"PATH": str(shadow)}):
            tooling.run_start_worker_only_launcher(root, root / "candidate-pin.json")
        self._wait_for_worker_argv(root)
        self.assertFalse(marker.exists())

    def test_unauthorized_nohup_resolution_is_never_used(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        shadow = root / "shadow-nohup"
        shadow.mkdir()
        marker = root / "nohup-shadow-executed"
        fake = shadow / "nohup"
        fake.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 93\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        with patch.dict(os.environ, {"PATH": str(shadow)}):
            tooling.run_start_worker_only_launcher(root, root / "candidate-pin.json")
        self._wait_for_worker_argv(root)
        self.assertFalse(marker.exists())

    def test_start_worker_only_rejects_attempted_beat_command_and_has_no_beat_branch(
        self,
    ) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture()
        source = (root / "start.sh").read_text(encoding="utf-8")
        worker_entry = source.split("start_worker_only() {", 1)[1].split(
            "\n# Function to start frontend", 1
        )[0]
        self.assertNotIn("app.celery_app beat", worker_entry)
        self.assertNotIn("beat.pid", worker_entry)
        self.assertNotIn("celerybeat-schedule", worker_entry)
        completed = subprocess.run(
            [
                "/usr/bin/bash",
                str(root / "start.sh"),
                "start_worker_only",
                str(root / "candidate-pin.json"),
                "beat",
            ],
            cwd=root,
            env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 64)
        self.assertFalse((root / "candidate-pin.json.argv").exists())

    def test_stale_worker_log_and_pid_file_do_not_report_success_or_get_changed(
        self,
    ) -> None:
        root, celery_log, _beat_pid = self._make_isolated_start_fixture()
        worker_pid_file = root / "run/worker.pid"
        worker_pid_file.write_text("999999\n", encoding="ascii")
        ready_log = root / "logs/worker.log"
        ready_log.write_text("worker ready from yesterday\n", encoding="utf-8")
        result = tooling.run_start_worker_only_launcher(
            root, root / "candidate-pin.json"
        )
        self.assertEqual(result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(worker_pid_file.read_text(), "999999\n")
        self.assertEqual(ready_log.read_text(), "worker ready from yesterday\n")
        self.assertEqual(celery_log.read_text(), "worker ready from yesterday\n")

    def test_worker_command_exit_nonzero_remains_only_a_launch_attempt(self) -> None:
        root, _celery_log, _beat_pid = self._make_isolated_start_fixture(worker_exit=42)
        result = tooling.run_start_worker_only_launcher(
            root, root / "candidate-pin.json"
        )
        argv = self._wait_for_worker_argv(root)
        self.assertEqual(result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(argv[3], "worker")

    def test_fresh_manifest_reader_rejects_stale_or_ambiguous_records(self) -> None:
        runtime_root = self.root / "manifest-selection"
        directory = runtime_root / "worker-identities"
        directory.mkdir(parents=True, exist_ok=True)
        real_now = dt.datetime.now(UTC)
        launched_at = real_now - dt.timedelta(seconds=2)
        record_id = f"{self.new.linux_boot_id}-{self.new.pid}-{self.new.start_ticks}"
        path = directory / f"worker-{record_id}.json"
        manifest = {
            "record_id": record_id,
            "worker_pid": self.new.pid,
            "worker_start_ticks": self.new.start_ticks,
            "linux_boot_id": self.new.linux_boot_id,
            "boot_timestamp": tooling._timestamp(real_now),
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")
        mtime_ns = tooling._timestamp_ns(launched_at) + 1_000
        os.utime(path, ns=(mtime_ns, mtime_ns))
        manifest, found_path = tooling.find_fresh_manifest(
            runtime_root,
            pid=self.new.pid,
            start_ticks=self.new.start_ticks,
            linux_boot_id=self.new.linux_boot_id,
            launched_at=launched_at,
        )
        self.assertEqual(found_path, path)
        self.assertEqual(manifest["worker_pid"], self.new.pid)
        stale = json.loads(path.read_text(encoding="utf-8"))
        stale["boot_timestamp"] = tooling._timestamp(
            launched_at - dt.timedelta(seconds=1)
        )
        path.write_text(json.dumps(stale), encoding="utf-8")
        os.utime(path, ns=(mtime_ns, mtime_ns))
        with self.assertRaisesRegex(tooling.ToolingError, "exactly one fresh"):
            tooling.find_fresh_manifest(
                runtime_root,
                pid=self.new.pid,
                start_ticks=self.new.start_ticks,
                linux_boot_id=self.new.linux_boot_id,
                launched_at=launched_at,
            )
        path.write_text(
            json.dumps(
                {
                    "record_id": record_id,
                    "worker_pid": self.new.pid,
                    "worker_start_ticks": self.new.start_ticks,
                    "linux_boot_id": self.new.linux_boot_id,
                    "boot_timestamp": tooling._timestamp(real_now),
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, ns=(mtime_ns, mtime_ns))
        duplicate = directory / "worker-second-55-1000.json"
        duplicate.write_text(path.read_text())
        with self.assertRaisesRegex(tooling.ToolingError, "exactly one fresh"):
            tooling.find_fresh_manifest(
                runtime_root,
                pid=self.new.pid,
                start_ticks=self.new.start_ticks,
                linux_boot_id=self.new.linux_boot_id,
                launched_at=launched_at,
            )

    def test_installed_receipt_is_required(self) -> None:
        self.receipt["installation"] = "NOT_INSTALLED"
        with self.assertRaisesRegex(tooling.ToolingError, "INSTALLED receipt"):
            self.run_refresh()

    def test_fabricated_or_stale_receipt_blocks_before_sigterm(self) -> None:
        fabricated = {
            "installation": "INSTALLED",
            "approval_id": self.policy.approval_id,
            "pin_sha256": self.receipt["pin_sha256"],
        }
        with self.assertRaisesRegex(tooling.ToolingError, "receipt is incomplete"):
            self.run_refresh(installation_receipt=fabricated)
        stale = copy.deepcopy(self.receipt)
        stale["approval"]["expires_at"] = tooling._timestamp(
            FIXED_NOW - dt.timedelta(seconds=1)
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "stale or approval is expired"
        ):
            self.run_refresh(installation_receipt=stale)
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_missing_changed_pin_and_loader_rejection_block_before_sigterm(
        self,
    ) -> None:
        self.pin_path.unlink()
        with self.assertRaisesRegex(tooling.ToolingError, "missing, symlinked"):
            self.run_refresh()
        self.receipt = make_installed_receipt(self.policy, self.pin_path)
        self.pin_path.write_text("replacement\n", encoding="utf-8")
        os.chmod(self.pin_path, 0o600)
        with self.assertRaisesRegex(tooling.ToolingError, "differs from its receipt"):
            self.run_refresh()
        self.receipt = make_installed_receipt(self.policy, self.pin_path)

        def reject(*_args, **_kwargs):
            raise tooling.ToolingError("production loader rejected installed pin")

        with self.assertRaisesRegex(tooling.ToolingError, "production loader rejected"):
            self.run_refresh(pin_loader=reject)
        self.assertEqual(self.pidfd.signal_calls, [])

    def test_beat_disappearance_before_launch_blocks_launch(self) -> None:
        self.observer.remove_beat_after_signal = True
        with self.assertRaisesRegex(
            tooling.ToolingError, "exactly one approved Celery Beat"
        ):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [(self.old.pid, signal.SIGTERM)])
        self.assertEqual(self.launch_calls, [])

    def test_beat_disappearance_during_launch_requires_recovery_without_retry(
        self,
    ) -> None:
        def launch_and_remove_beat(root, pin_path):
            result = self.launcher(root, pin_path)
            self.observer.current.pop(self.beat.pid, None)
            return result

        with self.assertRaises(tooling.RecoveryRequiredError) as caught:
            self.run_refresh(launcher=launch_and_remove_beat)
        self.assertEqual(caught.exception.result["decision"], "RECOVERY_REQUIRED")
        self.assertEqual(caught.exception.result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(len(self.launch_calls), 1)

    def test_existing_worker_after_shutdown_blocks_second_launch(self) -> None:
        self.observer.leave_matches = [self.old.pid]
        with self.assertRaisesRegex(tooling.ToolingError, "worker processes remain"):
            self.run_refresh()
        self.assertEqual(self.launch_calls, [])

    def test_unexpected_worker_process_before_shutdown_blocks_refresh(self) -> None:
        self.observer.matches = [self.old.pid, self.child.pid, 999]
        with self.assertRaisesRegex(
            tooling.ToolingError, "unexpected process match set"
        ):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(self.launch_calls, [])

    def test_partial_worker_startup_returns_recovery_required_identity(self) -> None:
        wrong = replace(self.new, argv=("celery", "unexpected"))

        def launch_partial(root, pin_path):
            self.launch_calls.append((str(root), str(pin_path)))
            self.observer.current[wrong.pid] = wrong
            self.observer.matches = [wrong.pid]
            self.observer.env[(wrong.pid, "WORKER_CANDIDATE_PIN_PATH")] = str(pin_path)
            return {"worker_pid": wrong.pid, "returncode": 0, "launch_attempts": 1}

        with self.assertRaises(tooling.RecoveryRequiredError) as caught:
            self.run_refresh(launcher=launch_partial)
        result = caught.exception.result
        self.assertEqual(result["process_observation"], "PROCESS_OBSERVED")
        self.assertEqual(result["identity_verification"], "NOT_VERIFIED")
        self.assertEqual(result["worker_process_identity"], wrong.to_dict())
        self.assertEqual(len(self.launch_calls), 1)

    def test_exited_worker_command_is_not_ready_and_requires_recovery(self) -> None:
        def exited_worker(root, pin_path):
            self.launch_calls.append((str(root), str(pin_path)))
            return {"worker_pid": self.new.pid, "returncode": 0, "launch_attempts": 1}

        with self.assertRaises(tooling.RecoveryRequiredError) as caught:
            self.run_refresh(launcher=exited_worker)
        self.assertEqual(caught.exception.result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(caught.exception.result["process_observation"], "NOT_OBSERVED")
        self.assertEqual(caught.exception.result["readiness"], "NOT_READY")
        self.assertEqual(len(self.launch_calls), 1)

    def test_non_beat_process_cannot_satisfy_beat_preservation(self) -> None:
        impostor = replace(
            self.beat,
            executable="/usr/bin/sleep",
            argv=("sleep", "1000"),
        )
        self.preflight["beat"] = impostor.to_dict()
        self.observer.current[self.beat.pid] = impostor
        with self.assertRaisesRegex(tooling.ToolingError, "approved Celery Beat"):
            self.run_refresh()
        self.assertEqual(self.pidfd.signal_calls, [])
        self.assertEqual(self.launch_calls, [])

    def test_manifest_full_identity_and_freshness_matrix_blocks_ready(self) -> None:
        cases = [
            ("linux_boot_id", BOOT_OTHER, "process identity differs"),
            ("worker_executable", "/usr/bin/wrong", "process identity differs"),
            ("worker_cwd", "/wrong", "process identity differs"),
            ("worker_argv", ["celery", "wrong"], "process identity differs"),
            ("worker_start_ticks", "9999", "process identity differs"),
            ("worker_node_identity", "celery@wrong", "worker_node_identity"),
            ("identity_status", "IDENTITY_INCONCLUSIVE", "status is not verified"),
            ("candidate_pin_status", "PIN_MISMATCHED", "status is not verified"),
            ("identity_status_reasons", ["mismatch"], "status is not verified"),
            ("boot_timestamp", "2026-10-20T03:58:00Z", "predates the launch second"),
        ]
        original = copy.deepcopy(self.manifest)
        attempts = 0
        authority_index = 0

        def reset_process_fixture():
            nonlocal authority_index
            self.authority = _make_test_authority(
                self.root / f"manifest-matrix-authority-{authority_index}",
                clock=lambda: FIXED_NOW,
            )
            authority_index += 1
            self.observer.current = {
                self.old.pid: self.old,
                self.child.pid: self.child,
                self.beat.pid: self.beat,
            }
            self.observer.children_by_parent = {self.old.pid: [self.child]}
            self.observer.matches = [self.old.pid, self.child.pid]
            self.observer.env = {}
            self.pidfd.signal_calls = []
            self.observer.wait_result = True
            self.observer.leave_matches = []
            self.observer.beat_after_signal = None
            self.observer.remove_beat_after_signal = False
            self.observer.identity_sequence = []
            self.launch_calls = []

        for field, value, message in cases:
            with self.subTest(field=field):
                reset_process_fixture()
                self.manifest = copy.deepcopy(original)
                self.manifest[field] = value
                with self.assertRaisesRegex(tooling.ToolingError, message):
                    self.run_refresh()
                attempts += len(self.launch_calls)
                self.assertEqual(len(self.launch_calls), 1)
        reset_process_fixture()
        self.manifest = copy.deepcopy(original)
        del self.manifest["worker_argv"]
        with self.assertRaisesRegex(tooling.ToolingError, "missing identity fields"):
            self.run_refresh()
        attempts += len(self.launch_calls)
        self.assertEqual(attempts, len(cases) + 1)

    def test_refresh_receipt_must_record_exactly_one_worker_only_attempt(self) -> None:
        def reports_duplicate_attempt(root, pin_path):
            result = self.launcher(root, pin_path)
            result["launch_attempts"] = 2
            return result

        with self.assertRaises(tooling.RecoveryRequiredError):
            self.run_refresh(launcher=reports_duplicate_attempt)
        self.assertEqual(len(self.launch_calls), 1)


class ProcfsTests(unittest.TestCase):
    def test_proc_stat_parser_handles_comm_with_spaces_and_parentheses(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p39-procfs-") as temp:
            root = Path(temp)
            proc = root / "44"
            proc.mkdir()
            # Fields after the final ')' start at field 3; starttime is field 22.
            tail = ["S", "1"] + ["0"] * 17 + ["8765"]
            (proc / "stat").write_text("44 (worker (with spaces)) " + " ".join(tail))
            observer = tooling.ProcfsObserver(root)
            fields = observer._stat_fields(44)
            self.assertEqual(fields[19], "8765")

    def test_launcher_output_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p39-launcher-paths-") as temp:
            root = Path(temp)
            (root / "run").mkdir()
            (root / "logs").mkdir()
            target = root / "outside-log"
            target.write_text("worker ready\n")
            (root / "logs/worker.log").symlink_to(target)
            with self.assertRaisesRegex(tooling.ToolingError, "regular file"):
                tooling.verify_launcher_paths(root)


class GateATests(PinOwnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="p39-gatea-test-")
        self.root = Path(self.temp.name)
        self.policy = make_policy(self.root)
        self.runtime_root = self.root / "runtime"
        self.pin_path = self.root / "pin.json"
        self.installation_receipt = make_installed_receipt(self.policy, self.pin_path)
        self.pin_sha = self.installation_receipt["pin_sha256"]
        self.module_records = []
        for index in range(self.policy.affected_module_count):
            module_name = f"pkg{index:03d}.module"
            path = self.root / f"pkg{index:03d}/module.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"VALUE = {index}\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            path_text = str(path)
            self.module_records.append(
                {
                    "module_name": module_name,
                    "resolved_source_path": path_text,
                    "expected_source_path": path_text,
                    "source_sha256": digest,
                    "expected_source_sha256": digest,
                    "spec_origin": path_text,
                    "loaded": True,
                    "load_timing": "boot",
                }
            )
        self.master = tooling.ProcessIdentity(
            pid=555,
            start_ticks="777",
            linux_boot_id=BOOT_GATE,
            executable=self.policy.launcher_identity["executable"],
            cwd=self.policy.source_root,
            argv=tuple(self.policy.launcher_identity["argv"]),
        )
        self.child = tooling.ProcessIdentity(
            pid=556,
            start_ticks="778",
            linux_boot_id=BOOT_GATE,
            executable="/usr/bin/python3.12",
            cwd=self.policy.source_root,
            argv=("celery-child",),
        )
        self.beat = make_beat_identity(
            self.policy,
            self.runtime_root,
            pid=10,
            ticks="300",
            boot_id=BOOT_GATE,
        )
        self.beat_observer = StaticBeatObserver(self.beat)
        pin_identity = {
            "pin_schema_version": 1,
            "pin_sha256": self.pin_sha,
            "candidate_head": self.policy.candidate_head,
            "candidate_tree": self.policy.candidate_tree,
            "source_manifest_digest": self.policy.source_manifest_digest,
            "operator_identity": self.policy.operator_identity,
            "approval_id": self.policy.approval_id,
            **self.installation_receipt["approval"],
        }
        self.manifest = {
            "candidate_head": self.policy.candidate_head,
            "candidate_tree": self.policy.candidate_tree,
            "candidate_source_root": self.policy.source_root,
            "source_manifest_digest": self.policy.source_manifest_digest,
            "configuration_fingerprint": self.policy.configuration_fingerprint,
            "configuration_schema_version": self.policy.configuration_schema_version,
            "configuration_policy_id": self.policy.configuration_policy_id,
            "worker_node_identity": self.policy.worker_node_identity,
            "identity_status": "IDENTITY_VERIFIED",
            "candidate_pin_status": "PIN_MATCHED",
            "identity_status_reasons": [],
            "worker_argv": list(self.policy.launcher_identity["argv"]),
            "worker_cwd": self.policy.launcher_identity["cwd"],
            "worker_executable": self.policy.launcher_identity["executable"],
            "worker_pid": self.master.pid,
            "worker_start_ticks": self.master.start_ticks,
            "linux_boot_id": self.master.linux_boot_id,
            "record_id": f"{self.master.linux_boot_id}-{self.master.pid}-{self.master.start_ticks}",
            "worker_children": [self.child.to_dict()],
            "affected_module_provenance": copy.deepcopy(self.module_records),
            "boot_timestamp": "2026-10-20T03:59:00Z",
            "candidate_pin_identity": pin_identity,
        }
        self.launch_started_at = dt.datetime(
            2026, 10, 20, 3, 58, 59, 500000, tzinfo=UTC
        )
        self.ready_at = dt.datetime(2026, 10, 20, 3, 59, 30, tzinfo=UTC)
        self.manifest_path = tooling._manifest_path_for_identity(
            self.runtime_root, self.master
        )
        self.manifest_path.parent.mkdir(parents=True)
        self._write_manifest(self.manifest)
        manifest_bytes = self.manifest_path.read_bytes()
        manifest_info = self.manifest_path.stat()
        self.refresh_id = str(uuid.uuid4())
        self.authority = _make_test_authority(
            self.root / "refresh-authority", clock=lambda: FIXED_NOW
        )
        old_worker = replace(
            make_identity(self.policy, 554, "776"),
            linux_boot_id=self.beat.linux_boot_id,
        )
        authority_receipt = {
            "receipt_schema_version": tooling.REFRESH_RECEIPT_SCHEMA_VERSION,
            "refresh_id": self.refresh_id,
            "source_identity": tooling._policy_source_identity(self.policy),
            "launcher_identity": self.policy.launcher_identity,
            "installation": "INSTALLED",
            "approval_id": self.policy.approval_id,
            "pin_sha256": self.pin_sha,
            "pin_path": str(self.pin_path.absolute()),
            "installation_receipt_sha256": hashlib.sha256(
                tooling._json_bytes(self.installation_receipt)
            ).hexdigest(),
            "worker_node_identity": self.policy.worker_node_identity,
            "pre_signal_worker_identity": old_worker.to_dict(),
            "beat_process_identity": {"role": tooling.BEAT_ROLE, **self.beat.to_dict()},
            "pre_signal_observed_at": "2026-10-20T03:58:00.000000Z",
        }
        with self.authority.locked(exclusive=True, create=True) as authority_fd:
            self.authority.begin(authority_fd, self.refresh_id, authority_receipt)
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "SIGTERM_SENT",
                {"signum": signal.SIGTERM, "pidfd_bound": True},
            )
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "WORKER_EXITED",
                {"pid": old_worker.pid, "start_ticks": old_worker.start_ticks},
            )
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "LAUNCH_STARTED",
                {
                    "launch_started_at": tooling._timestamp_precise(
                        self.launch_started_at
                    )
                },
            )
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "LAUNCH_ATTEMPTED",
                {"worker_pid": self.master.pid, "returncode": 0, "launch_attempts": 1},
            )
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "PROCESS_OBSERVED",
                {"worker_process_identity": self.master.to_dict()},
            )
            self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "IDENTITY_VERIFIED",
                {"worker_process_identity": self.master.to_dict()},
            )
            record = self.authority.append_stage(
                authority_fd,
                self.refresh_id,
                "READY",
                {
                    "worker_process_identity": self.master.to_dict(),
                    "master_pid": self.master.pid,
                    "worker_start_ticks": self.master.start_ticks,
                    "linux_boot_id": self.master.linux_boot_id,
                    "manifest_path": str(self.manifest_path),
                    "manifest_record_id": self.manifest["record_id"],
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "manifest_mtime_ns": manifest_info.st_mtime_ns,
                    "launch_started_at": tooling._timestamp_precise(
                        self.launch_started_at
                    ),
                    "ready_at": tooling._timestamp_precise(self.ready_at),
                    "ping": "OK",
                    "launcher_returncode": 0,
                },
            )
        self.refresh_receipt = self.authority.final_receipt(record)
        self.evidence = {
            "observed_at": "2026-10-20T03:59:59Z",
            "refresh_id": self.refresh_id,
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_file_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "installation": "INSTALLED",
            "launch": "LAUNCH_ATTEMPTED",
            "process_observation": "PROCESS_OBSERVED",
            "identity_verification": "IDENTITY_VERIFIED",
            "readiness": "READY",
            "current_source_head": self.policy.candidate_head,
            "current_source_tree": self.policy.candidate_tree,
            "current_source_manifest_digest": self.policy.source_manifest_digest,
            "current_configuration_fingerprint": self.policy.configuration_fingerprint,
            "current_configuration_schema_version": self.policy.configuration_schema_version,
            "current_configuration_policy_id": self.policy.configuration_policy_id,
            "source_clean": True,
            "pin_sha256": self.pin_sha,
            "worker_pin_path": str(self.pin_path),
            "master": self.master.to_dict(),
            "children": [self.child.to_dict()],
            "worker_match_pids": [self.master.pid, self.child.pid],
            "beat_before": self.beat.to_dict(),
            "beat_after": self.beat.to_dict(),
            "ping_ok": True,
            "ping_node": self.policy.worker_node_identity,
            "active": [],
            "reserved": [],
            "scheduled": [],
            "queue_depth": 0,
            "slot_keys": [],
            "leases": [],
            "active_sessions": 0,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_manifest(self, manifest: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_bytes(tooling._json_bytes(manifest))
        os.chmod(self.manifest_path, 0o600)
        mtime_ns = tooling._timestamp_ns(self.launch_started_at) + 1_000
        os.utime(self.manifest_path, ns=(mtime_ns, mtime_ns))

    def verify(self, manifest=None, evidence=None, now=FIXED_NOW):
        with patch.object(
            tooling, "DEFAULT_REFRESH_AUTHORITY_ROOT", str(self.authority.root)
        ):
            with patch.object(
                tooling,
                "RefreshAuthority",
                lambda path, **kwargs: _make_test_authority(
                    Path(path), clock=kwargs.get("clock")
                ),
            ):
                return tooling.verify_gate_a(
                    manifest or self.manifest,
                    evidence or self.evidence,
                    refresh_id=self.refresh_id,
                    expected_pin_sha256=self.pin_sha,
                    installation_receipt=self.installation_receipt,
                    refresh_receipt=self.refresh_receipt,
                    manifest_path=self.manifest_path,
                    pin_path=self.pin_path,
                    runtime_root=self.runtime_root,
                    policy=self.policy,
                    now=now,
                    pin_loader=fake_loader,
                    observer=self.beat_observer,
                )

    def test_complete_evidence_keeps_four_gate_results_distinct(self) -> None:
        result = self.verify()
        self.assertEqual(result["installation"], "INSTALLED")
        self.assertEqual(result["launch"], "LAUNCH_ATTEMPTED")
        self.assertEqual(result["process_observation"], "PROCESS_OBSERVED")
        self.assertEqual(result["identity_verification"], "IDENTITY_VERIFIED")
        self.assertEqual(result["readiness"], "READY")
        self.assertEqual(result["gate_a"], "GATE_A_VERIFIED")

    def test_gate_a_loads_authoritative_receipt_without_caller_receipt_json(
        self,
    ) -> None:
        self.refresh_receipt = None
        result = self.verify()
        self.assertEqual(result["gate_a"], "GATE_A_VERIFIED")

    def test_gate_a_rejects_wrong_id_missing_authority_and_stale_history(self) -> None:
        wrong_id = str(uuid.uuid4())
        original_id = self.refresh_id
        self.refresh_id = wrong_id
        with self.assertRaisesRegex(
            tooling.ToolingError, "protected refresh authority"
        ):
            self.verify()
        self.refresh_id = original_id
        missing = _make_test_authority(self.root / "missing-authority")
        original_authority = self.authority
        self.authority = missing
        with self.assertRaisesRegex(
            tooling.ToolingError, "protected refresh authority"
        ):
            self.verify()
        self.authority = original_authority
        with self.assertRaisesRegex(tooling.ToolingError, "stale or from the future"):
            self.verify(now=FIXED_NOW + dt.timedelta(hours=2))

    def test_source_configuration_launcher_status_approval_and_pin_failures(
        self,
    ) -> None:
        mutations = [
            ("identity_status", "IDENTITY_MISMATCH", "identity_status_not_verified"),
            (
                "candidate_pin_status",
                "PIN_MISMATCHED",
                "candidate_pin_status_not_matched",
            ),
            ("candidate_head", "0" * 40, "candidate_head"),
            ("candidate_tree", "1" * 40, "candidate_tree"),
            ("candidate_source_root", "/wrong", "candidate_source_root"),
            ("source_manifest_digest", "2" * 64, "source_manifest_digest"),
            ("configuration_fingerprint", "3" * 64, "configuration_fingerprint"),
            ("configuration_schema_version", 99, "configuration_schema_version"),
            ("configuration_policy_id", "wrong", "configuration_policy_id"),
            ("worker_argv", ["wrong"], "launcher_argv"),
            ("worker_cwd", "/wrong", "launcher_cwd"),
            ("worker_executable", "/wrong", "launcher_executable"),
        ]
        for field, bad_value, expected_reason in mutations:
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest[field] = bad_value
                with self.assertRaisesRegex(tooling.ToolingError, expected_reason):
                    self.verify(manifest=manifest)
        for field, bad_value, expected_reason in [
            ("approval_id", "P39-P0B-GATEB-001", "approval_id_mismatch"),
            ("operator_identity", "someone-else", "operator_identity_mismatch"),
            ("pin_sha256", "c" * 64, "pin_sha256_mismatch"),
        ]:
            with self.subTest(pin_field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest["candidate_pin_identity"][field] = bad_value
                with self.assertRaisesRegex(tooling.ToolingError, expected_reason):
                    self.verify(manifest=manifest)

    def test_identity_reasons_must_be_empty(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["identity_status_reasons"] = ["mismatch"]
        with self.assertRaisesRegex(tooling.ToolingError, "reasons_not_empty"):
            self.verify(manifest=manifest)

    def test_gate_a_requires_full_manifest_and_pin_identity_fields(self) -> None:
        cases = [
            ("linux_boot_id", BOOT_OTHER, "worker_master_process_identity_mismatch"),
            ("worker_start_ticks", "9999", "worker_master_process_identity_mismatch"),
            ("worker_executable", "/usr/bin/wrong", "launcher_executable"),
            ("worker_cwd", "/wrong", "launcher_cwd"),
            ("worker_argv", ["celery", "wrong"], "launcher_argv"),
            (
                "candidate_pin_status",
                "PIN_MISMATCHED",
                "candidate_pin_status_not_matched",
            ),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest[field] = value
                with self.assertRaisesRegex(tooling.ToolingError, reason):
                    self.verify(manifest=manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["candidate_pin_identity"]["pin_sha256"] = "c" * 64
        with self.assertRaisesRegex(tooling.ToolingError, "pin_sha256_mismatch"):
            self.verify(manifest=manifest)
        manifest = copy.deepcopy(self.manifest)
        del manifest["linux_boot_id"]
        with self.assertRaisesRegex(tooling.ToolingError, "worker_process_identity"):
            self.verify(manifest=manifest)

    def test_exact_module_set_rejects_substitution_missing_and_extra_modules(
        self,
    ) -> None:
        substituted = copy.deepcopy(self.manifest)
        extra_source = self.root / "pkg999/module.py"
        extra_source.parent.mkdir(parents=True, exist_ok=True)
        extra_source.write_text("VALUE = 999\n", encoding="utf-8")
        extra_digest = hashlib.sha256(extra_source.read_bytes()).hexdigest()
        extra_path = str(extra_source)
        substituted["affected_module_provenance"][0] = {
            "module_name": "pkg999.module",
            "resolved_source_path": extra_path,
            "expected_source_path": extra_path,
            "source_sha256": extra_digest,
            "expected_source_sha256": extra_digest,
            "spec_origin": extra_path,
            "loaded": True,
            "load_timing": "boot",
        }
        with self.assertRaisesRegex(
            tooling.ToolingError, "affected_module_set_mismatch"
        ):
            self.verify(manifest=substituted)
        missing = copy.deepcopy(self.manifest)
        missing["affected_module_provenance"].pop()
        with self.assertRaisesRegex(
            tooling.ToolingError, "affected_module_count_mismatch"
        ):
            self.verify(manifest=missing)
        additional = copy.deepcopy(self.manifest)
        additional["affected_module_provenance"].append(
            copy.deepcopy(additional["affected_module_provenance"][0])
        )
        with self.assertRaisesRegex(
            tooling.ToolingError, "affected_module_count_mismatch"
        ):
            self.verify(manifest=additional)

    def test_non_beat_process_cannot_pass_gate_a_by_remaining_unchanged(self) -> None:
        impostor = replace(
            self.beat,
            executable="/usr/bin/sleep",
            argv=("sleep", "1000"),
        )
        evidence = copy.deepcopy(self.evidence)
        evidence["beat_before"] = impostor.to_dict()
        evidence["beat_after"] = impostor.to_dict()
        with self.assertRaisesRegex(
            tooling.ToolingError, "health_beat_before_conflicts_with_refresh_receipt"
        ):
            self.verify(evidence=evidence)

    def test_original_gate_a_false_positive_rejects_substituted_beat_json(self) -> None:
        replacement = make_beat_identity(
            self.policy, self.runtime_root, pid=42, ticks="999", boot_id=BOOT_GATE
        )
        evidence = copy.deepcopy(self.evidence)
        evidence["beat_before"] = replacement.to_dict()
        evidence["beat_after"] = replacement.to_dict()
        with self.assertRaisesRegex(
            tooling.ToolingError, "health_beat_before_conflicts_with_refresh_receipt"
        ):
            self.verify(evidence=evidence)

    def test_gate_a_requires_pre_refresh_beat_identity_in_receipt(self) -> None:
        self.refresh_receipt.pop("beat_process_identity")
        with self.assertRaisesRegex(
            tooling.ToolingError, "caller refresh receipt conflicts"
        ):
            self.verify()

    def test_substituted_historical_receipt_beat_is_rejected_against_current_process(
        self,
    ) -> None:
        replacement = make_beat_identity(
            self.policy, self.runtime_root, pid=42, ticks="999", boot_id=BOOT_GATE
        )
        self.refresh_receipt["beat_process_identity"] = {
            "role": tooling.BEAT_ROLE,
            **replacement.to_dict(),
        }
        evidence = copy.deepcopy(self.evidence)
        evidence["beat_before"] = replacement.to_dict()
        evidence["beat_after"] = replacement.to_dict()
        with self.assertRaisesRegex(
            tooling.ToolingError,
            "caller refresh receipt conflicts with protected authority",
        ):
            self.verify(evidence=evidence)

    def test_gate_a_rejects_replaced_current_beat_identity_fields(self) -> None:
        replacements = [
            replace(self.beat, pid=42),
            replace(self.beat, start_ticks="301"),
            replace(self.beat, linux_boot_id=BOOT_OTHER),
            replace(self.beat, executable="/usr/bin/sleep"),
            replace(self.beat, cwd="/tmp"),
            replace(self.beat, argv=("celery", "-A", "app.celery_app", "beat")),
        ]
        for replacement in replacements:
            with self.subTest(identity=replacement.to_dict()):
                self.beat_observer = StaticBeatObserver(replacement)
                with self.assertRaises(tooling.ToolingError):
                    self.verify()
        self.beat_observer = StaticBeatObserver(self.beat)

    def test_receipt_from_another_refresh_is_rejected(self) -> None:
        self.refresh_receipt["refresh_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(
            tooling.ToolingError,
            "caller refresh receipt conflicts with protected authority",
        ):
            self.verify()

    def test_refresh_manifest_hash_path_and_identity_are_bound(self) -> None:
        original = copy.deepcopy(self.refresh_receipt)
        self.refresh_receipt["manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            tooling.ToolingError, "caller refresh receipt conflicts"
        ):
            self.verify()
        self.refresh_receipt = copy.deepcopy(original)
        self.refresh_receipt["manifest_record_id"] = "earlier-worker"
        with self.assertRaisesRegex(
            tooling.ToolingError, "caller refresh receipt conflicts"
        ):
            self.verify()
        self.refresh_receipt = copy.deepcopy(original)
        self.refresh_receipt["worker_process_identity"]["start_ticks"] = "9999"
        with self.assertRaisesRegex(
            tooling.ToolingError, "caller refresh receipt conflicts"
        ):
            self.verify()
        self.refresh_receipt = copy.deepcopy(original)
        evidence = copy.deepcopy(self.evidence)
        evidence["manifest_path"] = str(self.root / "substituted-manifest.json")
        with self.assertRaisesRegex(tooling.ToolingError, "manifest_path"):
            self.verify(evidence=evidence)

    def test_gate_a_health_must_bind_to_refresh_and_all_evidence_fields(self) -> None:
        evidence = copy.deepcopy(self.evidence)
        evidence["refresh_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(
            tooling.ToolingError, "health_evidence_refresh_identity_mismatch"
        ):
            self.verify(evidence=evidence)
        evidence = copy.deepcopy(self.evidence)
        del evidence["ping_node"]
        with self.assertRaisesRegex(
            tooling.ToolingError, "missing_required_evidence_fields"
        ):
            self.verify(evidence=evidence)

    def test_gate_a_rejects_manifest_file_stale_for_refresh(self) -> None:
        old_ns = tooling._timestamp_ns(self.launch_started_at) - 1_000_000_000
        os.utime(self.manifest_path, ns=(old_ns, old_ns))
        with self.assertRaisesRegex(
            tooling.ToolingError, "manifest_file_stale_for_refresh"
        ):
            self.verify()

    def test_current_source_identity_and_each_prior_stage_are_required(self) -> None:
        cases = [
            ("current_source_head", "wrong", "current_source_head"),
            ("current_source_tree", "wrong", "current_source_tree"),
            (
                "current_source_manifest_digest",
                "wrong",
                "current_source_manifest_digest",
            ),
            (
                "current_configuration_fingerprint",
                "wrong",
                "current_configuration_fingerprint",
            ),
            (
                "current_configuration_schema_version",
                8,
                "current_configuration_schema_version",
            ),
            (
                "current_configuration_policy_id",
                "wrong",
                "current_configuration_policy_id",
            ),
            ("source_clean", False, "current_source_not_clean"),
            ("installation", "NOT_INSTALLED", "installation_not_installed"),
            ("launch", "NOT_RUN", "worker_not_launched"),
            ("process_observation", "NOT_OBSERVED", "worker_process_not_observed"),
            ("identity_verification", "NOT_VERIFIED", "worker_identity_not_verified"),
            ("readiness", "NOT_RUN", "worker_not_ready"),
            ("pin_sha256", "d" * 64, "installed_pin_sha256_mismatch"),
            ("worker_pin_path", "/wrong/pin.json", "worker_pin_path_mismatch"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = value
                with self.assertRaisesRegex(tooling.ToolingError, reason):
                    self.verify(evidence=evidence)

    def test_module_count_loaded_path_hash_and_live_source_are_checked(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["affected_module_provenance"].pop()
        with self.assertRaisesRegex(tooling.ToolingError, "module_count_mismatch"):
            self.verify(manifest=manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["affected_module_provenance"][0]["loaded"] = False
        with self.assertRaisesRegex(tooling.ToolingError, "module_not_loaded"):
            self.verify(manifest=manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["affected_module_provenance"][0]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(tooling.ToolingError, "module_hash_mismatch"):
            self.verify(manifest=manifest)
        source = self.root / "pkg000/module.py"
        source.write_text("changed after worker boot\n")
        with self.assertRaisesRegex(tooling.ToolingError, "module_hash_mismatch"):
            self.verify()

    def test_worker_master_children_match_set_and_beat_are_checked(self) -> None:
        for field, value, reason in [
            (
                "master",
                {**self.master.to_dict(), "start_ticks": "bad"},
                "process_identity_evidence_invalid",
            ),
            ("children", [], "child_process_identity"),
            ("worker_match_pids", [self.master.pid], "process_match_set"),
            (
                "beat_after",
                {**self.beat.to_dict(), "start_ticks": "301"},
                "health_beat_after_conflicts_with_refresh_receipt",
            ),
        ]:
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = value
                with self.assertRaisesRegex(tooling.ToolingError, reason):
                    self.verify(evidence=evidence)

    def test_ping_queue_lease_and_session_state_are_checked(self) -> None:
        cases = [
            ("ping_ok", False, "worker_ping_failed"),
            ("ping_node", "celery@wrong", "worker_ping_node_mismatch"),
            ("active", [{"id": "busy"}], "worker_not_idle:active"),
            ("reserved", [{"id": "reserved"}], "worker_not_idle:reserved"),
            ("scheduled", [{"id": "scheduled"}], "worker_not_idle:scheduled"),
            ("queue_depth", 1, "queue_not_empty"),
            ("slot_keys", ["slot"], "worker_not_idle:slot_keys"),
            ("leases", ["lease"], "worker_not_idle:leases"),
            ("active_sessions", 1, "active_sessions_present"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = value
                with self.assertRaisesRegex(tooling.ToolingError, reason):
                    self.verify(evidence=evidence)

    def test_stale_evidence_and_manifest_digest_mismatch_fail(self) -> None:
        evidence = copy.deepcopy(self.evidence)
        manifest = copy.deepcopy(self.manifest)
        manifest["boot_timestamp"] = "2026-10-20T03:58:00Z"
        with self.assertRaisesRegex(
            tooling.ToolingError, "manifest_predates_refresh_launch"
        ):
            self.verify(manifest=manifest, evidence=evidence)
        stale_evidence = copy.deepcopy(self.evidence)
        stale_evidence["observed_at"] = "2026-10-20T03:59:59Z"
        with self.assertRaisesRegex(
            tooling.ToolingError, "runtime evidence is stale or from the future"
        ):
            self.verify(
                evidence=stale_evidence,
                now=dt.datetime(2026, 10, 20, 4, 10, tzinfo=UTC),
            )
        evidence = copy.deepcopy(self.evidence)
        evidence["manifest_file_sha256"] = "c" * 64
        with self.assertRaisesRegex(
            tooling.ToolingError, "manifest_file_sha256_evidence_mismatch"
        ):
            self.verify(evidence=evidence)


class ApprovedModuleSetTests(unittest.TestCase):
    def test_approved_module_allowlist_is_exactly_150(self) -> None:
        names = tooling._canonical_module_names(tooling.APPROVED)
        self.assertEqual(len(names), 150)
        self.assertEqual(len(set(names)), 150)


if __name__ == "__main__":
    unittest.main(verbosity=2)
