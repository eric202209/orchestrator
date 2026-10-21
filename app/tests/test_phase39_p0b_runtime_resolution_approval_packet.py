from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b.runtime_resolution_approval_packet import (
    ApprovalPacketError,
    SOURCE_PATHS,
    build_packet,
    canonical_json_bytes,
    packet_identity,
    validate_packet,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKET_PATH = (
    REPOSITORY_ROOT
    / "scripts/maintenance/phase39_p0b/runtime-resolution-approval-packet.json"
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rehash(packet: dict) -> dict:
    packet["packet_sha256"] = packet_identity(packet)
    return packet


class RuntimeResolutionApprovalPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.packet = json.loads(PACKET_PATH.read_text(encoding="utf-8"))

    def _copy_sources(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for relative_path in SOURCE_PATHS.values():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / relative_path, target)
        return temporary, root

    def test_worker_source_digest_consistency(self) -> None:
        relative = SOURCE_PATHS["worker_source"]
        digest = _sha256((REPOSITORY_ROOT / relative).read_bytes())
        self.assertEqual(
            self.packet["source_bindings"]["worker_source"]["sha256"], digest
        )
        inputs = json.loads(
            (REPOSITORY_ROOT / SOURCE_PATHS["resolution_inputs"]).read_text()
        )
        source_row = next(
            row for row in inputs["source_declarations"] if row["path"] == relative
        )
        self.assertEqual(source_row["sha256"], digest)

        temporary, root = self._copy_sources()
        with temporary:
            source = root / relative
            source.write_bytes(source.read_bytes() + b"# changed after packet\n")
            with self.assertRaisesRegex(
                ApprovalPacketError, "source_digest_mismatch:worker_source"
            ):
                build_packet(root)

    def test_classification_digest_consistency(self) -> None:
        relative = SOURCE_PATHS["classification"]
        digest = _sha256((REPOSITORY_ROOT / relative).read_bytes())
        self.assertEqual(
            self.packet["source_bindings"]["classification"]["sha256"], digest
        )
        temporary, root = self._copy_sources()
        with temporary:
            path = root / relative
            value = json.loads(path.read_text())
            value["dependency_closure_status"][
                "transitive_dependencies"
            ] += "; tampered"
            path.write_bytes(canonical_json_bytes(value))
            with self.assertRaisesRegex(
                ApprovalPacketError, "source_digest_mismatch:classification"
            ):
                build_packet(root)

    def test_import_graph_digest_consistency(self) -> None:
        relative = SOURCE_PATHS["import_graph"]
        digest = _sha256((REPOSITORY_ROOT / relative).read_bytes())
        self.assertEqual(
            self.packet["source_bindings"]["import_graph"]["sha256"], digest
        )
        temporary, root = self._copy_sources()
        with temporary:
            path = root / relative
            value = json.loads(path.read_text())
            value["autodiscovery_candidates"].append("app.tasks.unapproved")
            path.write_bytes(canonical_json_bytes(value))
            with self.assertRaisesRegex(
                ApprovalPacketError, "source_digest_mismatch:import_graph"
            ):
                build_packet(root)

    def test_resolution_input_digest_consistency(self) -> None:
        relative = SOURCE_PATHS["resolution_inputs"]
        raw = (REPOSITORY_ROOT / relative).read_bytes()
        self.assertEqual(
            self.packet["source_bindings"]["resolution_inputs"]["sha256"], _sha256(raw)
        )
        temporary, root = self._copy_sources()
        with temporary:
            path = root / relative
            value = json.loads(path.read_text())
            value["status"] = "TAMPERED_AFTER_PACKET_CREATION"
            path.write_bytes(canonical_json_bytes(value))
            with self.assertRaisesRegex(
                ApprovalPacketError, "packet_source_binding_mismatch:resolution_inputs"
            ):
                validate_packet(self.packet, repository_root=root)

    def test_packet_identity_is_canonical_and_digest_bound(self) -> None:
        raw = PACKET_PATH.read_bytes()
        self.assertEqual(raw, canonical_json_bytes(self.packet))
        self.assertEqual(self.packet["packet_sha256"], packet_identity(self.packet))
        self.assertEqual(self.packet, build_packet(REPOSITORY_ROOT))

    def test_unknown_field_rejected(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["unreviewed_extension"] = True
        with self.assertRaisesRegex(ApprovalPacketError, "packet_fields_invalid"):
            validate_packet(_rehash(packet))

    def test_unknown_nested_field_rejected(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["candidate_origin_policy"]["reviewed_origin"]["redirect_override"] = True
        with self.assertRaisesRegex(
            ApprovalPacketError, "reviewed_origin_fields_invalid"
        ):
            validate_packet(_rehash(packet))

    def test_missing_field_rejected(self) -> None:
        packet = copy.deepcopy(self.packet)
        del packet["resolution_policy"]
        with self.assertRaisesRegex(ApprovalPacketError, "packet_fields_invalid"):
            validate_packet(_rehash(packet))

    def test_duplicate_package_rejected(self) -> None:
        packet = copy.deepcopy(self.packet)
        direct = packet["worker_source_review"]["direct_requirements"]
        direct[-1] = copy.deepcopy(direct[0])
        with self.assertRaisesRegex(
            ApprovalPacketError, "direct_requirement_duplicate"
        ):
            validate_packet(_rehash(packet))

    def test_unresolved_decisions_are_preserved(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["unresolved_decisions"] = [
            item
            for item in packet["unresolved_decisions"]
            if item["id"] != "bcrypt_compatibility_range"
        ]
        with self.assertRaisesRegex(
            ApprovalPacketError, "unresolved_decision_set_invalid"
        ):
            validate_packet(_rehash(packet))

    def test_candidate_origin_is_not_treated_as_approved(self) -> None:
        packet = copy.deepcopy(self.packet)
        policy = packet["candidate_origin_policy"]
        policy["operator_approved_origin"] = policy["candidate_index_url"]
        policy["operator_approval_status"] = "APPROVED"
        with self.assertRaisesRegex(
            ApprovalPacketError, "candidate_origin_approval_confusion"
        ):
            validate_packet(_rehash(packet))

    def test_native_review_requirements_are_mandatory(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["native_review_policy"]["required_evidence"].remove("dt_needed")
        with self.assertRaisesRegex(
            ApprovalPacketError, "native_review_requirements_invalid"
        ):
            validate_packet(_rehash(packet))

    def test_packet_construction_has_no_network_or_install_side_effects(self) -> None:
        blocked = AssertionError("network or subprocess side effect attempted")
        with (
            patch.object(urllib.request, "urlopen", side_effect=blocked),
            patch.object(urllib.request, "urlretrieve", side_effect=blocked),
            patch.object(socket, "create_connection", side_effect=blocked),
            patch.object(socket.socket, "connect", side_effect=blocked),
            patch.object(subprocess, "run", side_effect=blocked),
            patch.object(subprocess, "Popen", side_effect=blocked),
            patch.object(subprocess, "check_call", side_effect=blocked),
            patch.object(os, "system", side_effect=blocked),
        ):
            packet = build_packet(REPOSITORY_ROOT)
        self.assertEqual(packet["packet_sha256"], self.packet["packet_sha256"])


if __name__ == "__main__":
    unittest.main()
