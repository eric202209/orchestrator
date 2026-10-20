"""Synthetic-only tests for the future P0B wheel lock policy."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.maintenance.phase39_p0b.runtime_wheelhouse_policy import (
    APPROVED_INDEX,
    TARGET,
    WheelhousePolicyError,
    offline_install_argv,
    validate_wheelhouse,
)


class RuntimeWheelhousePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p0b-wheel-policy-")
        self.root = Path(self.temporary.name)
        self.wheelhouse = self.root / "wheelhouse"
        self.wheelhouse.mkdir()
        self.filename = "demo_pkg-1.0-py3-none-any.whl"
        self._write_wheel(self.filename)
        self.lock = self._lock()
        self.authorization = {
            "id": "TEST-FIXTURE-ONLY",
            "status": "fixture-only",
            "approved_index": APPROVED_INDEX,
            "target": TARGET,
            "action": "download-only",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_wheel(
        self,
        filename: str,
        *,
        name: str = "demo-pkg",
        version: str = "1.0",
        tag: str = "py3-none-any",
        extra_members: dict[str, bytes] | None = None,
    ) -> Path:
        path = self.wheelhouse / filename
        stem = filename[:-4]
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        metadata = (
            "Metadata-Version: 2.1\n" f"Name: {name}\n" f"Version: {version}\n\n"
        ).encode()
        wheel = (
            "Wheel-Version: 1.0\n" "Root-Is-Purelib: true\n" f"Tag: {tag}\n\n"
        ).encode()
        members = {
            f"{dist_info}/METADATA": metadata,
            f"{dist_info}/WHEEL": wheel,
            f"{name.replace('-', '_')}/__init__.py": b"# synthetic fixture\n",
        }
        members.update(extra_members or {})
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for member, data in members.items():
                archive.writestr(member, data)
        return path

    def _package(self, filename: str | None = None) -> dict[str, object]:
        filename = filename or self.filename
        path = self.wheelhouse / filename
        return {
            "name": "demo-pkg",
            "version": "1.0",
            "filename": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "tags": {"python": "py3", "abi": "none", "platform": "any"},
            "source_index": APPROVED_INDEX,
            "acquired_at": "2026-10-20T00:00:00Z",
            "resolver_version": "fixture-resolver-1",
            "provenance_status": "fixture-only",
            "artifact_review_status": "fixture-only",
            "license_review_status": "fixture-only",
            "security_review_status": "fixture-only",
            "editable": False,
            "direct_url": None,
            "native_code": False,
            "native_members": [],
            "native_review_status": "fixture-only",
            "requires": [],
        }

    def _lock(self, package: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "fixture-only",
            "target": dict(TARGET),
            "index_url": APPROVED_INDEX,
            "resolved_at": "2026-10-20T00:00:00Z",
            "resolver": {"name": "fixture-resolver", "version": "fixture-resolver-1"},
            "authorization_id": "TEST-FIXTURE-ONLY",
            "packages": [package or self._package()],
        }

    def _validate(self, lock=None, authorization=None) -> None:
        validate_wheelhouse(
            lock or self.lock,
            self.wheelhouse,
            self.authorization if authorization is None else authorization,
            fixture_only=True,
        )

    def _rejects(self, code: str, lock=None, authorization=None) -> None:
        with self.assertRaisesRegex(WheelhousePolicyError, code):
            self._validate(lock, authorization)

    def test_correct_synthetic_offline_wheelhouse(self) -> None:
        self._validate()

    def test_missing_wheel(self) -> None:
        (self.wheelhouse / self.filename).unlink()
        self._rejects("runtime_wheel_missing")

    def test_missing_wheel_hash(self) -> None:
        package = self._package()
        package["sha256"] = ""
        self._rejects("runtime_wheel_hash_missing_or_invalid", self._lock(package))

    def test_wrong_wheel_filename(self) -> None:
        package = self._package()
        package["filename"] = "different-1.0-py3-none-any.whl"
        self._rejects("runtime_wheel_filename_name_mismatch", self._lock(package))

    def test_altered_wheel(self) -> None:
        with (self.wheelhouse / self.filename).open("ab") as stream:
            stream.write(b"altered")
        self._rejects("runtime_wheel_hash_mismatch")

    def test_wrong_wheel_version(self) -> None:
        wrong = self._write_wheel("demo_pkg-1.0-py3-none-any.whl", version="2.0")
        package = self._package()
        package["sha256"] = hashlib.sha256(wrong.read_bytes()).hexdigest()
        self._rejects("runtime_wheel_metadata_version_mismatch", self._lock(package))

    def test_wrong_architecture(self) -> None:
        filename = "demo_pkg-1.0-cp312-cp312-manylinux_2_39_x86_64.whl"
        self._write_wheel(filename, tag="cp312-cp312-manylinux_2_39_x86_64")
        package = self._package(filename)
        package["tags"] = {
            "python": "cp312",
            "abi": "cp312",
            "platform": "manylinux_2_39_x86_64",
        }
        self._rejects("runtime_wheel_target_tag_unsupported", self._lock(package))

    def test_wrong_python_abi(self) -> None:
        filename = "demo_pkg-1.0-cp311-cp311-manylinux_2_39_aarch64.whl"
        self._write_wheel(filename, tag="cp311-cp311-manylinux_2_39_aarch64")
        package = self._package(filename)
        package["tags"] = {
            "python": "cp311",
            "abi": "cp311",
            "platform": "manylinux_2_39_aarch64",
        }
        self._rejects("runtime_wheel_target_tag_unsupported", self._lock(package))

    def test_unexpected_sdist(self) -> None:
        (self.wheelhouse / "other-1.0.tar.gz").write_bytes(b"synthetic sdist")
        self._rejects("runtime_sdist_forbidden")

    def test_unreviewed_direct_url(self) -> None:
        package = self._package()
        package["direct_url"] = "https://example.invalid/demo.whl"
        self._rejects("runtime_direct_url_forbidden", self._lock(package))

    def test_editable_dependency(self) -> None:
        package = self._package()
        package["editable"] = True
        self._rejects("runtime_editable_dependency_forbidden", self._lock(package))

    def test_duplicate_or_conflicting_version(self) -> None:
        package = self._package()
        lock = self._lock(package)
        lock["packages"].append(dict(package, version="2.0"))
        self._rejects("runtime_package_duplicate_or_conflicting_version", lock)

    def test_missing_transitive_dependency(self) -> None:
        package = self._package()
        package["requires"] = ["transitive-dependency"]
        self._rejects("runtime_transitive_dependency_missing", self._lock(package))

    def test_unapproved_package_index(self) -> None:
        package = self._package()
        package["source_index"] = "https://mirror.example.invalid/simple"
        self._rejects("runtime_package_index_unapproved", self._lock(package))

    def test_unknown_artifact_provenance(self) -> None:
        package = self._package()
        package["provenance_status"] = "unknown"
        self._rejects("runtime_artifact_provenance_unknown", self._lock(package))

    def test_unexpected_native_extension(self) -> None:
        filename = "demo_pkg-1.0-cp312-cp312-manylinux_2_39_aarch64.whl"
        native_path = self._write_wheel(
            filename,
            tag="cp312-cp312-manylinux_2_39_aarch64",
            extra_members={"demo_pkg/_native.so": b"synthetic ELF placeholder"},
        )
        package = self._package(filename)
        package["tags"] = {
            "python": "cp312",
            "abi": "cp312",
            "platform": "manylinux_2_39_aarch64",
        }
        package["sha256"] = hashlib.sha256(native_path.read_bytes()).hexdigest()
        package["native_members"] = ["demo_pkg/_native.so"]
        self._rejects("runtime_unexpected_native_extension", self._lock(package))

    def test_unapproved_pth_file(self) -> None:
        wheel = self._write_wheel(
            self.filename,
            extra_members={"demo.pth": b"import os; os.system('unsafe')\n"},
        )
        package = self._package()
        package["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        self._rejects("wheel_pth_forbidden", self._lock(package))

    def test_lock_artifact_hash_mismatch(self) -> None:
        package = self._package()
        package["sha256"] = "0" * 64
        self._rejects("runtime_wheel_hash_mismatch", self._lock(package))

    def test_missing_acquisition_authorization(self) -> None:
        with self.assertRaisesRegex(
            WheelhousePolicyError, "acquisition_authorization_missing"
        ):
            validate_wheelhouse(self.lock, self.wheelhouse, None, fixture_only=True)

    def test_fixture_lock_cannot_pass_production_mode(self) -> None:
        with self.assertRaisesRegex(
            WheelhousePolicyError, "runtime_lock_status_unreviewed"
        ):
            validate_wheelhouse(self.lock, self.wheelhouse, self.authorization)

    def test_offline_construction_command_disables_resolution_and_network_indexes(
        self,
    ) -> None:
        argv = offline_install_argv(
            "/opt/runtime/bin/python", "requirements.lock", "wheelhouse"
        )
        self.assertIn("--no-index", argv)
        self.assertIn("--no-deps", argv)
        self.assertIn("--only-binary=:all:", argv)
        self.assertIn("--require-hashes", argv)
        self.assertNotIn("--index-url", argv)
        self.assertNotIn("--extra-index-url", argv)

    def test_offline_construction_rejects_network_enabled_mode(self) -> None:
        with self.assertRaisesRegex(WheelhousePolicyError, "offline_network_forbidden"):
            offline_install_argv(
                "/opt/runtime/bin/python",
                "requirements.lock",
                "wheelhouse",
                network_enabled=True,
            )


if __name__ == "__main__":
    unittest.main()
