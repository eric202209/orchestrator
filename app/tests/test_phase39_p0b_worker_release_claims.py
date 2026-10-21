from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from app.tests.test_phase39_p0b_worker_release_manifest import WorkerReleaseFixture
from scripts.maintenance.phase39_p0b import (
    promotion_claims as claims,
    release_entrypoint,
    release_packaging,
)

UTC = dt.timezone.utc
OPENSSL = str(release_entrypoint.OPENSSL)
VALID_FROM = "2000-01-01T00:00:00Z"
VALID_UNTIL = "2998-12-31T23:59:59Z"


def _concurrent_claim(
    root: str,
    record: dict,
    barrier: Any,
    result_queue: Any,
) -> None:
    store = claims.OfflinePromotionClaimStore(root)
    barrier.wait(timeout=10)
    try:
        store.claim(record)
        result_queue.put("won")
    except claims.ClaimCollisionError:
        result_queue.put("collision")
    except BaseException as exc:  # surfaced in the parent with its type
        result_queue.put(f"error:{type(exc).__name__}:{exc}")


class WorkerReleasePromotionClaimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_temp = tempfile.TemporaryDirectory(
            prefix="worker-release-claim-test-key-"
        )
        cls.key_root = Path(cls.key_temp.name)
        cls.private_key = cls.key_root / "ephemeral-test-only-ed25519.pem"
        cls.public_key = cls.key_root / "ephemeral-test-only-ed25519.pub"
        subprocess.run(
            [OPENSSL, "genpkey", "-algorithm", "ED25519", "-out", str(cls.private_key)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                OPENSSL,
                "pkey",
                "-in",
                str(cls.private_key),
                "-pubout",
                "-out",
                str(cls.public_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.public_key.chmod(0o444)
        cls.key_id = "sha256:" + hashlib.sha256(cls.public_key.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.key_temp.cleanup()

    def setUp(self) -> None:
        self.fixture = WorkerReleaseFixture()
        self.addCleanup(self.fixture.close)
        self.claim_temp = tempfile.TemporaryDirectory(
            prefix="worker-release-claim-store-test-"
        )
        self.addCleanup(self.claim_temp.cleanup)
        self.claim_root = Path(self.claim_temp.name) / "claims"
        self.claim_root.mkdir(mode=0o700)
        self.claim_root.chmod(0o700)
        self.store = claims.OfflinePromotionClaimStore(self.claim_root)

    @staticmethod
    def _subject(manifest: dict, **changes: object) -> dict:
        subject = {
            "approval_kind": release_entrypoint.WORKER_APPROVAL_SUBJECT_KIND,
            "schema_version": 2,
            "approval_id": "P39-P0B-WR2-TEST-001",
            "operator_identity": "offline-test-operator",
            "purpose": release_entrypoint.WORKER_APPROVAL_PURPOSE,
            "action": release_entrypoint.WORKER_APPROVAL_ACTION,
            "valid_from": VALID_FROM,
            "valid_until": VALID_UNTIL,
            "manifest_kind": manifest["manifest_kind"],
            "manifest_schema_version": manifest["schema_version"],
            "release_id": manifest["release_id"],
            "execution_artifact_digest": manifest["execution_artifact_digest"],
            "release_manifest_sha256": hashlib.sha256(
                release_packaging._worker_canonical_json(manifest)
            ).hexdigest(),
            "source_head": manifest["source"]["head"],
            "source_tree": manifest["source"]["tree"],
            "source_manifest_digest": manifest["source"]["source_manifest_digest"],
            "payload_inventory_digest": manifest["payload_inventory_digest"],
            "platform_target": manifest["platform_target"],
            "configuration_policy_id": manifest["configuration"]["policy_id"],
            "configuration_profile_reference": manifest["configuration"][
                "profile_reference"
            ],
        }
        subject.update(changes)
        return subject

    @classmethod
    def _sign(cls, payload: bytes) -> bytes:
        with tempfile.TemporaryDirectory(
            prefix="worker-release-claim-sign-test-"
        ) as directory:
            root = Path(directory)
            payload_path = root / "subject.json"
            signature_path = root / "subject.sig"
            payload_path.write_bytes(payload)
            subprocess.run(
                [
                    OPENSSL,
                    "pkeyutl",
                    "-sign",
                    "-inkey",
                    str(cls.private_key),
                    "-rawin",
                    "-in",
                    str(payload_path),
                    "-out",
                    str(signature_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return signature_path.read_bytes()

    @classmethod
    def _envelope(
        cls,
        manifest: dict,
        *,
        subject_changes: dict[str, object] | None = None,
        envelope_changes: dict[str, object] | None = None,
        signature: bytes | None = None,
    ) -> bytes:
        subject = cls._subject(manifest, **(subject_changes or {}))
        subject_raw = release_packaging._worker_canonical_json(subject)
        signature_bytes = cls._sign(subject_raw) if signature is None else signature
        envelope = {
            "envelope_kind": release_entrypoint.WORKER_APPROVAL_ENVELOPE_KIND,
            "schema_version": release_entrypoint.WORKER_APPROVAL_SCHEMA_VERSION,
            "key_id": cls.key_id,
            "signed_subject": base64.b64encode(subject_raw).decode("ascii"),
            "signature": base64.b64encode(signature_bytes).decode("ascii"),
        }
        envelope.update(envelope_changes or {})
        return release_packaging._worker_canonical_json(envelope)

    def _write_signature(self, envelope: bytes) -> None:
        root = self.fixture.release
        root.chmod(0o755)
        path = root / release_packaging.WORKER_RELEASE_SIGNATURE
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(envelope)
        path.chmod(0o444)
        root.chmod(0o555)

    def _test_anchor(self):
        original = release_entrypoint._read_protected_file

        def read_test_anchor(path: Path, *, uid: int = 0):
            if Path(path) == self.public_key:
                return original(path, uid=os.geteuid())
            return original(path, uid=uid)

        return mock.patch.multiple(
            release_entrypoint,
            TRUSTED_KEY=self.public_key,
            _read_protected_file=read_test_anchor,
        )

    def _claim(self, *, subject_changes: dict[str, object] | None = None):
        self._write_signature(
            self._envelope(self.fixture.manifest, subject_changes=subject_changes)
        )
        with self._test_anchor():
            return claims.claim_worker_release(self.fixture.release, self.store)

    def _verified_result(
        self, *, subject_changes: dict[str, object] | None = None
    ) -> dict:
        self._write_signature(
            self._envelope(self.fixture.manifest, subject_changes=subject_changes)
        )
        with self._test_anchor():
            return release_packaging.verify_worker_release(self.fixture.release)

    def test_01_first_valid_claim_succeeds_without_promotion_readiness(self) -> None:
        receipt = self._claim()
        self.assertTrue(receipt.durable_claim_created)
        self.assertEqual(receipt.claim_state, claims.CLAIMED)
        self.assertFalse(receipt.activation_eligible)
        self.assertFalse(receipt.promotion_performed)
        record = json.loads(
            (self.claim_root / f"{receipt.claim_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["claim_schema_version"], claims.CLAIM_SCHEMA_VERSION)
        self.assertEqual(record["approval_schema"]["subject_schema_version"], 2)
        self.assertEqual(record["approval_purpose"], "worker-release-promotion")
        self.assertEqual(record["promotion_action"], "promote-worker-release")
        self.assertEqual(
            record["approved_configuration_profile"]["policy_id"],
            "worker-config-projection/v3",
        )
        self.assertRegex(record["claim_created_at"], r"Z$")
        result = self.store.reconcile(
            receipt.approval_id,
            expected_operation_identity=receipt.claim_operation_identity,
        )
        self.assertEqual(result.status, claims.CLAIMED_NOT_STARTED)

    def test_02_same_approval_cannot_be_claimed_twice(self) -> None:
        receipt = self._claim()
        with self._test_anchor(), self.assertRaises(claims.ClaimCollisionError):
            claims.claim_worker_release(self.fixture.release, self.store)
        self.assertEqual(
            self.store.reconcile(receipt.approval_id).status,
            claims.CLAIMED_NOT_STARTED,
        )

    def test_03_concurrent_processes_yield_one_winner(self) -> None:
        verified = self._verified_result()
        record = claims._record_from_verification(verified)
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_claim,
                args=(str(self.claim_root), record, barrier, result_queue),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        outcomes = [result_queue.get(timeout=5) for _ in processes]
        self.assertEqual(sorted(outcomes), ["collision", "won"])
        self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_04_restart_preserves_consumed_approval(self) -> None:
        receipt = self._claim()
        restarted_store = claims.OfflinePromotionClaimStore(self.claim_root)
        self.assertEqual(
            restarted_store.reconcile(receipt.approval_id).status,
            claims.CLAIMED_NOT_STARTED,
        )
        with self._test_anchor(), self.assertRaises(claims.ClaimCollisionError):
            claims.claim_worker_release(self.fixture.release, restarted_store)

    def test_05_wrong_release_id_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(subject_changes={"release_id": "wr2-" + "0" * 64})
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_06_wrong_artifact_digest_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(subject_changes={"execution_artifact_digest": "0" * 64})
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_07_wrong_promotion_purpose_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(subject_changes={"purpose": "worker-maintenance"})
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_08_worker_stop_approval_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(
                subject_changes={"purpose": "worker-stop", "action": "stop-worker"}
            )
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_09_worker_start_approval_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(
                subject_changes={"purpose": "worker-start", "action": "start-worker"}
            )
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_10_expired_approval_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self._claim(
                subject_changes={
                    "valid_from": "2000-01-01T00:00:00Z",
                    "valid_until": "2001-01-01T00:00:00Z",
                }
            )
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_11_invalid_signature_rejected(self) -> None:
        signature = bytearray(self._sign(b"not the signed subject"))
        signature[0] ^= 1
        self._write_signature(
            self._envelope(self.fixture.manifest, signature=bytes(signature))
        )
        with self._test_anchor(), self.assertRaises(claims.ClaimValidationError):
            claims.claim_worker_release(self.fixture.release, self.store)
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_12_claim_path_traversal_rejected(self) -> None:
        with self.assertRaises(claims.ClaimValidationError):
            self.store.reconcile("../outside")
        with self.assertRaises(claims.ClaimValidationError):
            self.store.transition(
                "../outside", expected=claims.CLAIMED, state=claims.OPERATION_STARTED
            )

    def test_13_symlink_substitution_is_invalid(self) -> None:
        receipt = self._claim()
        claim_path = self.claim_root / f"{receipt.claim_id}.json"
        target = Path(self.claim_temp.name) / "outside-record"
        target.write_text("outside", encoding="utf-8")
        target.chmod(0o600)
        claim_path.unlink()
        claim_path.symlink_to(target)
        result = self.store.reconcile(receipt.approval_id)
        self.assertEqual(result.status, claims.CLAIM_INVALID)
        with self.assertRaises(claims.ClaimCollisionError):
            self.store.claim(claims._record_from_verification(self._verified_result()))

    def test_14_hard_link_substitution_is_invalid(self) -> None:
        receipt = self._claim()
        claim_path = self.claim_root / f"{receipt.claim_id}.json"
        target = Path(self.claim_temp.name) / "outside-record"
        target.write_text("outside", encoding="utf-8")
        target.chmod(0o600)
        claim_path.unlink()
        os.link(target, claim_path)
        result = self.store.reconcile(receipt.approval_id)
        self.assertEqual(result.status, claims.CLAIM_INVALID)

    def test_15_corrupt_claim_record_rejected(self) -> None:
        receipt = self._claim()
        claim_path = self.claim_root / f"{receipt.claim_id}.json"
        claim_path.write_bytes(b'{"claim_schema_version":')
        result = self.store.reconcile(receipt.approval_id)
        self.assertEqual(result.status, claims.CLAIM_INVALID)

    def test_16_invalid_state_transition_rejected(self) -> None:
        receipt = self._claim()
        with self.assertRaises(claims.ClaimTransitionError):
            self.store.transition(
                receipt.claim_id,
                expected=claims.CLAIMED,
                state=claims.OPERATION_SUCCEEDED,
            )
        self.assertEqual(
            self.store.reconcile(receipt.approval_id).status,
            claims.CLAIMED_NOT_STARTED,
        )

    def test_17_interrupted_started_operation_remains_consumed(self) -> None:
        receipt = self._claim()
        self.store.transition(
            receipt.claim_id,
            expected=claims.CLAIMED,
            state=claims.OPERATION_STARTED,
        )
        restarted_store = claims.OfflinePromotionClaimStore(self.claim_root)
        self.assertEqual(
            restarted_store.reconcile(receipt.approval_id).status,
            claims.OPERATION_STARTED,
        )
        with self._test_anchor(), self.assertRaises(claims.ClaimCollisionError):
            claims.claim_worker_release(self.fixture.release, restarted_store)

    def test_18_outcome_unknown_blocks_replay(self) -> None:
        receipt = self._claim()
        self.store.transition(
            receipt.claim_id,
            expected=claims.CLAIMED,
            state=claims.OPERATION_STARTED,
        )
        self.store.transition(
            receipt.claim_id,
            expected=claims.OPERATION_STARTED,
            state=claims.OUTCOME_UNKNOWN,
        )
        restarted_store = claims.OfflinePromotionClaimStore(self.claim_root)
        self.assertEqual(
            restarted_store.reconcile(receipt.approval_id).status,
            claims.OUTCOME_UNKNOWN,
        )
        with self._test_anchor(), self.assertRaises(claims.ClaimCollisionError):
            claims.claim_worker_release(self.fixture.release, restarted_store)

    def test_19_reconciliation_is_read_only(self) -> None:
        receipt = self._claim()
        claim_path = self.claim_root / f"{receipt.claim_id}.json"
        before_bytes = claim_path.read_bytes()
        before_names = sorted(path.name for path in self.claim_root.iterdir())
        before_stat = claim_path.stat()
        result = self.store.reconcile(receipt.approval_id)
        after_stat = claim_path.stat()
        self.assertEqual(result.status, claims.CLAIMED_NOT_STARTED)
        self.assertEqual(claim_path.read_bytes(), before_bytes)
        self.assertEqual(
            sorted(path.name for path in self.claim_root.iterdir()), before_names
        )
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_20_no_production_or_runtime_side_effects(self) -> None:
        receipt = self._claim()
        self.assertEqual(
            sorted(path.name for path in self.claim_root.iterdir()),
            [f"{receipt.claim_id}.json"],
        )
        self.assertFalse(receipt.promotion_performed)
        self.assertFalse(receipt.activation_eligible)
        with self.assertRaises(claims.ClaimStoreConfigurationError):
            claims.OfflinePromotionClaimStore(
                Path(__file__).absolute().parent / "claims-never-created"
            )

    def test_claim_id_collision_covers_different_operation_bindings(self) -> None:
        verified = self._verified_result()
        first = claims._record_from_verification(verified)
        second = copy.deepcopy(first)
        second["release_id"] = "wr2-" + "f" * 64
        second["execution_artifact_digest"] = "f" * 64
        second["canonical_manifest_sha256"] = "e" * 64
        second["claim_operation_identity"] = claims._operation_identity(second)
        claims._validate_record(second)
        self.store.claim(first)
        with self.assertRaises(claims.ClaimCollisionError):
            self.store.claim(second)
        result = self.store.reconcile(
            second["approval_id"],
            expected_operation_identity=second["claim_operation_identity"],
        )
        self.assertEqual(result.status, claims.CLAIM_INVALID)
        self.assertEqual(result.reason, "claim_identity_mismatch")

    def test_approval_schema_identity_is_closed(self) -> None:
        verified = self._verified_result()
        record = claims._record_from_verification(verified)
        reordered = copy.deepcopy(record)
        reordered["approval_schema"] = dict(
            reversed(list(reordered["approval_schema"].items()))
        )
        self.assertEqual(
            claims._operation_identity(record), claims._operation_identity(reordered)
        )
        record["approval_schema"]["subject_schema_version"] = 1
        with self.assertRaises(claims.ClaimRecordInvalid):
            self.store.claim(record)

    def test_operation_succeeded_and_failed_are_terminal_states(self) -> None:
        succeeded = self._claim()
        self.store.transition(
            succeeded.claim_id,
            expected=claims.CLAIMED,
            state=claims.OPERATION_STARTED,
        )
        self.store.transition(
            succeeded.claim_id,
            expected=claims.OPERATION_STARTED,
            state=claims.OPERATION_SUCCEEDED,
        )
        self.assertEqual(
            self.store.reconcile(succeeded.approval_id).status,
            claims.OPERATION_SUCCEEDED,
        )
        with self.assertRaises(claims.ClaimTransitionError):
            self.store.transition(
                succeeded.claim_id,
                expected=claims.OPERATION_SUCCEEDED,
                state=claims.OPERATION_FAILED,
            )

        failed = self._claim(subject_changes={"approval_id": "P39-P0B-WR2-TEST-002"})
        self.store.transition(
            failed.claim_id,
            expected=claims.CLAIMED,
            state=claims.OPERATION_STARTED,
        )
        self.store.transition(
            failed.claim_id,
            expected=claims.OPERATION_STARTED,
            state=claims.OPERATION_FAILED,
        )
        self.assertEqual(
            self.store.reconcile(failed.approval_id).status,
            claims.OPERATION_FAILED,
        )

    def test_valid_signature_without_claim_is_not_promotion_readiness(self) -> None:
        verification = self._verified_result()
        self.assertTrue(verification["signature_verified"])
        self.assertFalse(verification["durable_one_time_claim_consumed"])
        self.assertFalse(verification["activation_eligible"])
        with self.assertRaises(claims.ClaimStoreConfigurationError):
            claims.claim_worker_release(self.fixture.release, None)  # type: ignore[arg-type]

    def test_payload_integrity_is_required_before_claim(self) -> None:
        payload = self.fixture.release / "source/app/tasks/worker.py"
        self.fixture.release.chmod(0o755)
        payload.chmod(0o644)
        payload.write_bytes(b"def task():\n    return 'modified'\n")
        payload.chmod(0o444)
        self.fixture.release.chmod(0o555)
        self._write_signature(self._envelope(self.fixture.manifest))
        with self._test_anchor(), self.assertRaises(claims.ClaimValidationError):
            claims.claim_worker_release(self.fixture.release, self.store)
        self.assertEqual(
            self.store.reconcile("P39-P0B-WR2-TEST-001").status, claims.NO_CLAIM
        )

    def test_ambiguous_create_failure_is_not_retried_or_replaced(self) -> None:
        record = claims._record_from_verification(self._verified_result())
        with mock.patch.object(
            claims.os, "fsync", side_effect=OSError("injected fsync failure")
        ):
            with self.assertRaises(claims.ClaimPersistenceUncertain):
                self.store.claim(record)
        claim_path = self.claim_root / f"{record['claim_id']}.json"
        self.assertTrue(claim_path.exists())
        with self.assertRaises(claims.ClaimCollisionError):
            self.store.claim(record)


if __name__ == "__main__":
    unittest.main()
