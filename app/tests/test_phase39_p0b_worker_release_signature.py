from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.tests.test_phase39_p0b_worker_release_manifest import WorkerReleaseFixture
from scripts.maintenance.phase39_p0b import release_entrypoint, release_packaging


OPENSSL = str(release_entrypoint.OPENSSL)
VALID_FROM = "2000-01-01T00:00:00Z"
VALID_UNTIL = "2998-12-31T23:59:59Z"


class WorkerReleaseSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_temp = tempfile.TemporaryDirectory(prefix="worker-release-test-key-")
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
            prefix="worker-release-sign-test-"
        ) as directory:
            root = Path(directory)
            payload_path = root / "payload.json"
            signature_path = root / "payload.sig"
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
        subject_raw: bytes | None = None,
        signature: bytes | None = None,
    ) -> bytes:
        subject = cls._subject(manifest, **(subject_changes or {}))
        payload = subject_raw or release_packaging._worker_canonical_json(subject)
        signature_bytes = cls._sign(payload) if signature is None else signature
        envelope = {
            "envelope_kind": release_entrypoint.WORKER_APPROVAL_ENVELOPE_KIND,
            "schema_version": release_entrypoint.WORKER_APPROVAL_SCHEMA_VERSION,
            "key_id": cls.key_id,
            "signed_subject": base64.b64encode(payload).decode("ascii"),
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

    def _install_test_trust_anchor(self):
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

    def _verify(self, *, anchor: bool = True) -> dict:
        context = (
            self._install_test_trust_anchor() if anchor else mock.patch.dict({}, {})
        )
        with context:
            return release_packaging.verify_worker_release(self.fixture.release)

    def test_01_valid_v2_signature_authenticates_promotion_subject(self) -> None:
        self._write_signature(self._envelope(self.fixture.manifest))
        result = self._verify()
        self.assertEqual(result["signature_status"], "VERIFIED")
        self.assertTrue(result["signature_verified"])
        self.assertTrue(result["promotion_approval_verified"])
        self.assertEqual(result["approval_purpose"], "worker-release-promotion")

    def test_02_invalid_signature_is_rejected(self) -> None:
        envelope = json.loads(self._envelope(self.fixture.manifest))
        signature = bytearray(base64.b64decode(envelope["signature"]))
        signature[0] ^= 0x01
        envelope["signature"] = base64.b64encode(signature).decode("ascii")
        self._write_signature(release_packaging._worker_canonical_json(envelope))
        result = self._verify()
        self.assertEqual(result["status"], "INTEGRITY_VERIFIED_SIGNATURE_REJECTED")
        self.assertIn("approval_signature_invalid", result["error"])

    def test_03_unknown_key_id_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest, envelope_changes={"key_id": "sha256:" + "0" * 64}
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_unknown_key_id", result["error"])

    def test_04_wrong_release_id_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest, subject_changes={"release_id": "wr2-" + "0" * 64}
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_release_binding_mismatch", result["error"])

    def test_05_wrong_execution_artifact_digest_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest,
            subject_changes={"execution_artifact_digest": "0" * 64},
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_release_binding_mismatch", result["error"])

    def test_06_wrong_source_head_or_tree_is_rejected(self) -> None:
        for field, value in (("source_head", "c" * 40), ("source_tree", "d" * 40)):
            with self.subTest(field=field):
                self._write_signature(
                    self._envelope(
                        self.fixture.manifest, subject_changes={field: value}
                    )
                )
                result = self._verify()
                self.assertIn(
                    "worker_approval_release_binding_mismatch", result["error"]
                )

    def test_07_wrong_payload_inventory_digest_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest,
            subject_changes={"payload_inventory_digest": "0" * 64},
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_release_binding_mismatch", result["error"])

    def test_08_wrong_promotion_purpose_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest, subject_changes={"purpose": "worker-stop"}
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_purpose_or_action_mismatch", result["error"])

    def test_09_expired_approval_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest,
            subject_changes={
                "valid_from": "2020-01-01T00:00:00Z",
                "valid_until": "2020-01-02T00:00:00Z",
            },
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_expired", result["error"])

    def test_10_not_yet_valid_approval_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest,
            subject_changes={
                "valid_from": "2999-01-01T00:00:00Z",
                "valid_until": "2999-01-02T00:00:00Z",
            },
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_not_yet_valid", result["error"])

    def test_11_malformed_signature_envelope_is_rejected(self) -> None:
        self._write_signature(b"{not-json")
        result = self._verify()
        self.assertIn("worker_approval_envelope_json_invalid", result["error"])

    def test_12_noncanonical_signed_payload_is_rejected(self) -> None:
        subject = self._subject(self.fixture.manifest)
        raw = json.dumps(subject, sort_keys=False, separators=(",", ":")).encode()
        self._write_signature(self._envelope(self.fixture.manifest, subject_raw=raw))
        result = self._verify()
        self.assertIn("worker_approval_subject_noncanonical", result["error"])

    def test_13_v1_approval_cannot_replay_as_v2_promotion(self) -> None:
        v1 = {
            "approval_kind": "orchestrator-phase39-p0b-operator-approval",
            "schema_version": 1,
            "approval_id": "P39-P0B-RELEASE-TEST-001",
            "operator_identity": "offline-test-operator",
            "release_id": self.fixture.manifest["release_id"],
        }
        raw = release_packaging._worker_canonical_json(v1)
        self._write_signature(self._envelope(self.fixture.manifest, subject_raw=raw))
        result = self._verify()
        self.assertIn(
            "worker_approval_subject_fields_incomplete_or_unknown", result["error"]
        )

    def test_14_worker_stop_or_start_approval_cannot_replay_as_promotion(self) -> None:
        for purpose, action in (
            ("worker-stop", "stop-worker"),
            ("worker-start", "start-worker"),
        ):
            with self.subTest(purpose=purpose):
                envelope = self._envelope(
                    self.fixture.manifest,
                    subject_changes={"purpose": purpose, "action": action},
                )
                self._write_signature(envelope)
                result = self._verify()
                self.assertIn(
                    "worker_approval_purpose_or_action_mismatch", result["error"]
                )

    def test_15_manifest_mutation_after_signing_is_rejected(self) -> None:
        old_envelope = self._envelope(self.fixture.manifest)
        changed_core = release_packaging._worker_identity_core(self.fixture.manifest)
        changed_core["configuration"] = dict(changed_core["configuration"])
        changed_core["configuration"]["policy_id"] = "worker-config-projection/changed"
        changed_manifest = release_packaging.build_worker_release_manifest(changed_core)
        root = self.fixture.release
        root.chmod(0o755)
        manifest_path = root / release_packaging.WORKER_RELEASE_MANIFEST
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(
            release_packaging._worker_canonical_json(changed_manifest)
        )
        manifest_path.chmod(0o444)
        root.chmod(0o555)
        new_release = root.parent / changed_manifest["release_id"]
        root.rename(new_release)
        self.fixture.release = new_release
        self.fixture.manifest = changed_manifest
        self._write_signature(old_envelope)
        result = self._verify()
        self.assertIn("worker_approval_release_binding_mismatch", result["error"])

    def test_16_replaced_signature_from_another_release_is_rejected(self) -> None:
        other_core = release_packaging._worker_identity_core(self.fixture.manifest)
        other_core["configuration"] = dict(other_core["configuration"])
        other_core["configuration"]["policy_id"] = "worker-config-projection/other"
        other = release_packaging.build_worker_release_manifest(other_core)
        self._write_signature(self._envelope(other))
        result = self._verify()
        self.assertIn("worker_approval_release_binding_mismatch", result["error"])

    def test_17_duplicate_json_fields_are_rejected(self) -> None:
        envelope = json.loads(self._envelope(self.fixture.manifest))
        items = list(envelope.items())
        fields = [json.dumps(key) + ":" + json.dumps(value) for key, value in items]
        raw = ("{" + ",".join(fields + [fields[0]]) + "}").encode()
        self._write_signature(raw)
        result = self._verify()
        self.assertIn("worker_approval_duplicate_json_field", result["error"])

    def test_18_unsupported_envelope_schema_is_rejected(self) -> None:
        envelope = self._envelope(
            self.fixture.manifest, envelope_changes={"schema_version": 1}
        )
        self._write_signature(envelope)
        result = self._verify()
        self.assertIn("worker_approval_envelope_schema_unsupported", result["error"])

    def test_19_missing_signature_is_rejected(self) -> None:
        result = release_packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "INTEGRITY_VERIFIED_UNSIGNED_REJECTED")
        self.assertFalse(result["signature_verified"])
        self.assertFalse(result["promotion_approval_verified"])

    def test_20_valid_signature_never_makes_candidate_activation_eligible(self) -> None:
        self._write_signature(self._envelope(self.fixture.manifest))
        with mock.patch.object(
            socket, "socket", side_effect=AssertionError("network used")
        ):
            result = self._verify()
        self.assertTrue(result["signature_verified"])
        self.assertTrue(result["promotion_approval_verified"])
        self.assertFalse(result["activation_eligible"])
        self.assertFalse(result["runtime_closure_complete"])
        self.assertFalse(result["mount_boundary_verified"])
        self.assertFalse(result["worker_identity_verified"])
        self.assertFalse(result["quiescence_verified"])
        self.assertFalse(result["durable_one_time_claim_consumed"])


if __name__ == "__main__":
    unittest.main()
