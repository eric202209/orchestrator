"""Offline evidence and compatibility regressions for Phase 39 P0B WP2B3."""

from __future__ import annotations

import builtins
import hashlib
import json
import socket
import subprocess
import sys
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b.runtime_lock_contract import (
    RuntimeLockError,
    _parse_requirement,
    evaluate_marker,
    validate_runtime_lock,
)
from scripts.maintenance.phase39_p0b.runtime_resolution_approval_packet import (
    canonical_json_bytes,
    packet_identity,
    validate_packet,
)
from scripts.maintenance.phase39_p0b.worker_dependency_resolution import (
    TARGET_MARKERS,
    evaluate_target_marker,
    parse_requirement_source,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PHASE39_DIR = REPOSITORY_ROOT / "scripts/maintenance/phase39_p0b"
LEDGER_PATH = PHASE39_DIR / "worker-runtime-resolution-evidence-ledger.json"
PACKET_PATH = PHASE39_DIR / "runtime-resolution-approval-packet.json"


class WorkerRuntimeResolutionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger_bytes = LEDGER_PATH.read_bytes()
        self.ledger = json.loads(self.ledger_bytes)

    def test_bcrypt_api_and_worker_call_sites_in_isolated_python(self) -> None:
        """Exercise extracted auth functions without importing app.auth/settings."""
        probe = r"""
import ast
import bcrypt
import importlib.metadata
import json
import pathlib
import sys

source_path = pathlib.Path(sys.argv[1])
tree = ast.parse(source_path.read_text(encoding="utf-8"))
names = {"_password_bytes", "get_password_hash", "verify_password"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
if {node.name for node in functions} != names:
    raise AssertionError("auth callsite functions changed")
namespace = {"bcrypt": bcrypt}
module = ast.Module(body=functions, type_ignores=[])
exec(compile(module, str(source_path), "exec"), namespace)

password = "pāssword"
hashed = namespace["get_password_hash"](password)
wrong_password = namespace["verify_password"]("wrong", hashed)
correct_password = namespace["verify_password"](password, hashed)
encoded = namespace["_password_bytes"]("é" * 36)
try:
    namespace["_password_bytes"]("é" * 37)
except ValueError:
    over_limit_rejected = True
else:
    over_limit_rejected = False
try:
    namespace["verify_password"]("password", "not a bcrypt hash")
except ValueError as exc:
    malformed_hash_error = type(exc).__name__ + ": " + str(exc)
else:
    malformed_hash_error = None
try:
    bcrypt.hashpw(b"x" * 73, bcrypt.gensalt(rounds=4))
except ValueError as exc:
    long_api_error = type(exc).__name__
else:
    long_api_error = None

print(json.dumps({
    "version": importlib.metadata.version("bcrypt"),
    "salt_type": type(bcrypt.gensalt()).__name__,
    "hash_type": type(bcrypt.hashpw(b"probe", bcrypt.gensalt(rounds=4))).__name__,
    "hash_prefix": hashed[:4],
    "stored_hash_utf8_roundtrip": hashed.encode("utf-8").decode("utf-8") == hashed,
    "correct_password": correct_password,
    "wrong_password": wrong_password,
    "72_utf8_bytes_accepted": len(encoded) == 72,
    "74_utf8_bytes_rejected": over_limit_rejected,
    "long_bcrypt_api_error": long_api_error,
    "malformed_hash_error": malformed_hash_error,
    "application_modules_loaded": sorted(name for name in sys.modules if name == "app" or name.startswith("app.")),
}, sort_keys=True))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(REPOSITORY_ROOT / "app/auth.py")],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["version"], "5.0.0")
        self.assertEqual(evidence["salt_type"], "bytes")
        self.assertEqual(evidence["hash_type"], "bytes")
        self.assertEqual(evidence["hash_prefix"], "$2b$")
        self.assertTrue(evidence["stored_hash_utf8_roundtrip"])
        self.assertTrue(evidence["correct_password"])
        self.assertFalse(evidence["wrong_password"])
        self.assertTrue(evidence["72_utf8_bytes_accepted"])
        self.assertTrue(evidence["74_utf8_bytes_rejected"])
        self.assertEqual(evidence["long_bcrypt_api_error"], "ValueError")
        self.assertEqual(evidence["malformed_hash_error"], "ValueError: Invalid salt")
        self.assertEqual(evidence["application_modules_loaded"], [])

    def test_evidence_classifications_and_direct_requirement_coverage(self) -> None:
        inputs = json.loads((PHASE39_DIR / "worker-resolution-inputs.json").read_text())
        source = (PHASE39_DIR / "worker-requirements.in").read_bytes()
        parsed = parse_requirement_source(source)
        self.assertEqual(len(parsed), 18)
        self.assertEqual(len(self.ledger["direct_requirements"]), 18)
        self.assertEqual(
            [row["original_declaration"] for row in self.ledger["direct_requirements"]],
            [row["original_declaration"] for row in inputs["direct_requirements"]],
        )
        self.assertEqual(
            [row["original_declaration"] for row in parsed],
            [row["original_declaration"] for row in inputs["direct_requirements"]],
        )

        counts = self.ledger["classification_counts"]
        self.assertEqual(counts["EXACT_OFFLINE_CANDIDATE_EVIDENCE"], 0)
        self.assertEqual(counts["METADATA_ONLY"], 18)
        self.assertEqual(counts["OBSERVED_INSTALLED_VERSION"], 18)
        self.assertEqual(counts["MISSING_ARTIFACT"], 18)
        self.assertEqual(counts["UNRESOLVED"], 18)
        for row in self.ledger["direct_requirements"]:
            self.assertEqual(row["metadata_classification"], "METADATA_ONLY")
            self.assertEqual(
                row["observed_install"]["classification"], "OBSERVED_INSTALLED_VERSION"
            )
            self.assertTrue(
                row["observed_install"]["requires_dist_classification"]
                == "METADATA_ONLY"
            )
            self.assertEqual(
                row["artifact_evidence"]["classification"], "MISSING_ARTIFACT"
            )
            self.assertIsNone(row["artifact_evidence"]["verified_sha256"])
            self.assertEqual(row["resolution_classification"], "UNRESOLVED")

        self.assertFalse(self.ledger["production_lock_created"])
        self.assertFalse(self.ledger["transitive_resolution"]["closure_complete"])
        self.assertFalse(
            self.ledger["transitive_resolution"][
                "transitive_markers_evaluated_for_production_candidate"
            ]
        )
        self.assertFalse(
            self.ledger["transitive_resolution"][
                "extras_closure_evaluated_for_production_candidate"
            ]
        )

    def test_ledger_is_canonical_and_its_identity_is_digest_bound(self) -> None:
        self.assertEqual(self.ledger_bytes, canonical_json_bytes(self.ledger))
        body = dict(self.ledger)
        recorded = body.pop("ledger_sha256")
        self.assertEqual(
            recorded, hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        )
        self.assertEqual(self.ledger["decision"], "OFFLINE_RESOLUTION_BLOCKED")

    def test_source_and_packet_digests_match_current_inputs(self) -> None:
        for relative_path, expected in self.ledger["source_bindings"].items():
            actual = hashlib.sha256(
                (REPOSITORY_ROOT / relative_path).read_bytes()
            ).hexdigest()
            self.assertEqual(expected, actual, relative_path)

        packet_bytes = PACKET_PATH.read_bytes()
        packet = json.loads(packet_bytes)
        binding = self.ledger["approval_packet_binding"]
        self.assertEqual(
            binding["file_sha256"], hashlib.sha256(packet_bytes).hexdigest()
        )
        self.assertEqual(binding["packet_sha256"], packet["packet_sha256"])
        self.assertEqual(packet_identity(packet), packet["packet_sha256"])
        validate_packet(packet, REPOSITORY_ROOT)

        inputs = json.loads(
            (PHASE39_DIR / "worker-resolution-inputs.json").read_bytes()
        )
        for source in inputs["source_declarations"]:
            self.assertEqual(
                source["sha256"],
                self.ledger["source_bindings"][source["path"]],
                source["path"],
            )

    def test_incomplete_transitive_closure_is_rejected(self) -> None:
        from app.tests.test_phase39_p0b_runtime_lock_contract import (
            RuntimeLockContractTests,
        )

        fixture = RuntimeLockContractTests()
        fixture.setUp()
        try:
            lock = fixture._build_lock(
                package_specs={
                    "demo-pkg": {"version": "1.0", "requires": ("child-pkg>=1.0",)},
                    "child-pkg": {"version": "1.2", "requires": ()},
                },
                direct_requirements=("demo-pkg==1.0",),
            )
            lock["packages"] = [
                package for package in lock["packages"] if package["name"] == "demo-pkg"
            ]
            with self.assertRaisesRegex(
                RuntimeLockError, "runtime_transitive_dependency_missing"
            ):
                validate_runtime_lock(lock, fixture_only=True)
        finally:
            fixture.tearDown()

    def test_markers_and_requested_extras_are_evaluated_separately(self) -> None:
        source = (PHASE39_DIR / "worker-requirements.in").read_bytes()
        direct = {row["name"]: row for row in parse_requirement_source(source)}
        self.assertEqual(direct["python-jose"]["extras"], ["cryptography"])
        self.assertEqual(direct["celery"]["extras"], ["redis"])
        self.assertIsNone(direct["python-jose"]["marker"])
        self.assertIsNone(direct["celery"]["marker"])

        jose_edge = _parse_requirement("cryptography>=3.4.0; extra == 'cryptography'")
        celery_edge = _parse_requirement('kombu[redis]; extra == "redis"')
        self.assertEqual(jose_edge["name"], "cryptography")
        self.assertEqual(celery_edge["name"], "kombu")
        self.assertEqual(celery_edge["extras"], ["redis"])
        env = dict(TARGET_MARKERS)
        env["extra"] = "cryptography"
        self.assertTrue(evaluate_marker(jose_edge["marker"], env))
        env["extra"] = ""
        self.assertFalse(evaluate_marker(jose_edge["marker"], env))
        self.assertIs(
            evaluate_target_marker(
                "python_version >= '3.12' and platform_machine == 'aarch64'"
            ),
            True,
        )
        self.assertIsNone(evaluate_target_marker("python_full_version >= '3.12.1'"))

    def test_evidence_validation_never_uses_network_or_package_installation(
        self,
    ) -> None:
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pip" or name.startswith("pip.") or name.startswith("app."):
                raise AssertionError(f"forbidden import: {name}")
            return original_import(name, *args, **kwargs)

        with (
            patch.object(
                socket, "create_connection", side_effect=AssertionError("network")
            ) as connect,
            patch.object(
                socket.socket, "connect", side_effect=AssertionError("network")
            ) as socket_connect,
            patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("network")
            ) as urlopen,
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("subprocess")
            ) as popen,
            patch.object(
                subprocess, "run", side_effect=AssertionError("subprocess")
            ) as run,
            patch.object(builtins, "__import__", side_effect=guarded_import),
        ):
            packet = json.loads(PACKET_PATH.read_bytes())
            validate_packet(packet, REPOSITORY_ROOT)
            canonical_json_bytes(self.ledger)
        connect.assert_not_called()
        socket_connect.assert_not_called()
        urlopen.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()
        policy = json.loads(
            (PHASE39_DIR / "worker-resolution-inputs.json").read_bytes()
        )["acquisition_policy"]
        self.assertFalse(policy["acquisition_authorized"])
        self.assertEqual(policy["network_fallback"], "FORBIDDEN")
        self.assertEqual(
            policy["dependency_resolution_in_this_work_package"], "FORBIDDEN"
        )


if __name__ == "__main__":
    unittest.main()
