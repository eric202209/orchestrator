"""Isolated synthetic tests for the offline Worker runtime lock contract."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.tests.test_phase39_p0b_worker_release_manifest import WorkerReleaseFixture
from scripts.maintenance.phase39_p0b import release_packaging
from scripts.maintenance.phase39_p0b.runtime_lock_contract import (
    APPROVED_INDEX,
    AUTHORIZATION_KIND,
    LOCK_KIND,
    LOCK_SCHEMA_VERSION,
    PLATFORM_TARGET,
    PYTHON_RUNTIME_POLICY,
    RuntimeLockError,
    canonical_json_bytes,
    evaluate_offline_acquisition,
    parse_canonical_runtime_lock,
    runtime_lock_digest,
    validate_runtime_lock,
)


class RuntimeLockContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p0b-runtime-lock-")
        self.root = Path(self.temporary.name)
        self.wheelhouse = self.root / "wheelhouse"
        self.wheelhouse.mkdir()
        self.lock = self._build_lock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def _write_wheel(
        self,
        name: str,
        version: str,
        *,
        requires: tuple[str, ...] = (),
        native_members: dict[str, bytes] | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        pure = not native_members
        if pure:
            tag_parts = ("py3", "none", "any")
        else:
            tag_parts = ("cp312", "cp312", "manylinux_2_39_aarch64")
        filename = (
            f"{name.replace('-', '_')}-{version}-"
            f"{tag_parts[0]}-{tag_parts[1]}-{tag_parts[2]}.whl"
        )
        path = self.wheelhouse / filename
        metadata = (
            "Metadata-Version: 2.1\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires)
            + "\n"
        ).encode("utf-8")
        wheel_metadata = (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: "
            + ("true" if pure else "false")
            + f"\nTag: {'-'.join(tag_parts)}\n\n"
        ).encode("utf-8")
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        members = {
            f"{dist_info}/METADATA": metadata,
            f"{dist_info}/WHEEL": wheel_metadata,
            f"{name.replace('-', '_')}/__init__.py": b"# synthetic fixture\n",
        }
        members.update(native_members or {})
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for member, content in sorted(members.items()):
                archive.writestr(member, content)
        tags = ["-".join(tag_parts)]
        native_hashes = {
            member: self._sha(content)
            for member, content in (native_members or {}).items()
        }
        return filename, tags, native_hashes

    def _build_lock(
        self,
        *,
        package_specs: dict[str, dict[str, object]] | None = None,
        direct_requirements: tuple[str, ...] = ("demo-pkg==1.0",),
        external_dependencies: list[dict[str, object]] | None = None,
        elf_status: str = "unverified",
    ) -> dict[str, object]:
        for existing in self.wheelhouse.iterdir():
            if existing.is_file() or existing.is_symlink():
                existing.unlink()
            elif existing.is_dir():
                import shutil

                shutil.rmtree(existing)
        specs = package_specs or {"demo-pkg": {"version": "1.0", "requires": ()}}
        wheel_rows: dict[str, dict[str, object]] = {}
        native_rows: list[dict[str, str]] = []
        for name, spec in sorted(specs.items()):
            version = str(spec["version"])
            requirements = tuple(spec.get("requires", ()))
            native_members = spec.get("native_members", {})
            assert isinstance(native_members, dict)
            filename, tags, native_hashes = self._write_wheel(
                name,
                version,
                requires=requirements,
                native_members=native_members,
            )
            raw = (self.wheelhouse / filename).read_bytes()
            wheel_rows[name] = {
                "name": name,
                "version": version,
                "wheel_filename": filename,
                "wheel_sha256": self._sha(raw),
                "wheel_tags": tags,
                "requested_extras": [],
                "origin": {
                    "index_url": APPROVED_INDEX,
                    "artifact_url": f"https://files.pythonhosted.org/packages/{filename}",
                    "acquired_at": "2026-10-21T00:00:00Z",
                    "provenance_status": "fixture-only",
                },
                "review": {
                    "artifact": "fixture-only",
                    "license": "fixture-only",
                    "security": "fixture-only",
                    "native": "fixture-only",
                },
            }
            for member, digest in sorted(native_hashes.items()):
                native_rows.append(
                    {"package": name, "member": member, "sha256": digest}
                )

        declaration_text = "".join(f"{item}\n" for item in direct_requirements).encode(
            "utf-8"
        )
        self.declaration_content = declaration_text
        sources = [
            {
                "path": "requirements.txt",
                "sha256": self._sha(declaration_text),
                "classification": "mixed",
            }
        ]
        source_digest = sources[0]["sha256"]
        direct_rows: list[dict[str, object]] = []
        requested_extras: dict[str, set[str]] = {name: set() for name in wheel_rows}
        parsed_direct = []
        for line_number, raw_requirement in enumerate(direct_requirements, 1):
            requirement = self._parse_requirement_for_fixture(raw_requirement)
            parsed_direct.append((line_number, raw_requirement, requirement))
            applies = self._marker_applies(requirement["marker"], "")
            direct_rows.append(
                {
                    "requirement": raw_requirement,
                    "resolved_name": requirement["name"],
                    "extras": requirement["extras"],
                    "marker": requirement["marker"],
                    "applies_to_target": applies,
                    "source_path": "requirements.txt",
                    "source_sha256": source_digest,
                    "line": line_number,
                }
            )
            if applies and requirement["name"] in requested_extras:
                requested_extras[requirement["name"]].update(requirement["extras"])

        edge_rows: list[dict[str, object]] = []
        changed = True
        while changed:
            changed = False
            for parent, spec in sorted(specs.items()):
                for raw_requirement in tuple(spec.get("requires", ())):
                    requirement = self._parse_requirement_for_fixture(raw_requirement)
                    applies = any(
                        self._marker_applies(requirement["marker"], extra)
                        for extra in (requested_extras[parent] or {""})
                    )
                    if applies and requirement["name"] in requested_extras:
                        before = len(requested_extras[requirement["name"]])
                        requested_extras[requirement["name"]].update(
                            requirement["extras"]
                        )
                        changed = (
                            changed
                            or len(requested_extras[requirement["name"]]) != before
                        )
        for parent, spec in sorted(specs.items()):
            for raw_requirement in sorted(tuple(spec.get("requires", ()))):
                requirement = self._parse_requirement_for_fixture(raw_requirement)
                applies = any(
                    self._marker_applies(requirement["marker"], extra)
                    for extra in (requested_extras[parent] or {""})
                )
                edge_rows.append(
                    {
                        "parent": parent,
                        "requirement": raw_requirement,
                        "child": requirement["name"],
                        "extras": requirement["extras"],
                        "marker": requirement["marker"],
                        "applies_to_target": applies,
                    }
                )
        edge_rows.sort(
            key=lambda row: (row["parent"], row["child"], row["requirement"])
        )
        for name, package in wheel_rows.items():
            package["requested_extras"] = sorted(requested_extras[name])

        lock: dict[str, object] = {
            "lock_kind": LOCK_KIND,
            "schema_version": LOCK_SCHEMA_VERSION,
            "review_status": "fixture-only",
            "platform_target": dict(PLATFORM_TARGET),
            "python_runtime": {
                **PYTHON_RUNTIME_POLICY,
                "version": "3.12.3",
            },
            "declaration_sources": sources,
            "direct_requirements": direct_rows,
            "packages": [wheel_rows[name] for name in sorted(wheel_rows)],
            "dependency_edges": edge_rows,
            "external_dependencies": external_dependencies or [],
            "native_extensions": sorted(
                native_rows, key=lambda item: (item["package"], item["member"])
            ),
            "elf_closure": {
                "status": elf_status,
                "evidence_sha256": None,
                "inspector": None,
                "base_image_digest": None,
                "system_packages": [],
            },
            "build_provenance": {
                "builder_image_digest": self._sha(b"fixture-builder"),
                "python_full_version": "3.12.3",
                "resolver_name": "fixture-resolver",
                "resolver_version": "1.0",
                "resolved_at": "2026-10-21T00:00:00Z",
                "toolchain_digest": self._sha(b"fixture-toolchain"),
            },
            "acquisition": {
                "approval_id": "TEST-FIXTURE-ONLY",
                "action": "download-only",
                "index_url": APPROVED_INDEX,
            },
        }
        return lock

    @staticmethod
    def _parse_requirement_for_fixture(value: str) -> dict[str, object]:
        from scripts.maintenance.phase39_p0b.runtime_lock_contract import (
            _parse_requirement,
        )

        return _parse_requirement(value)

    @staticmethod
    def _marker_applies(marker: str | None, extra: str) -> bool:
        from scripts.maintenance.phase39_p0b.runtime_lock_contract import (
            evaluate_marker,
        )

        env = {
            "python_version": "3.12",
            "python_full_version": "3.12.3",
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
            "os_name": "posix",
            "sys_platform": "linux",
            "platform_machine": "aarch64",
            "platform_system": "Linux",
            "implementation_version": "3.12.3",
            "extra": extra,
        }
        return evaluate_marker(marker, env)

    def _authorization(self, lock: dict[str, object]) -> dict[str, object]:
        artifacts = []
        for package in lock["packages"]:
            artifacts.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "filename": package["wheel_filename"],
                    "sha256": package["wheel_sha256"],
                    "index_url": package["origin"]["index_url"],
                    "artifact_url": package["origin"]["artifact_url"],
                }
            )
        return {
            "authorization_kind": AUTHORIZATION_KIND,
            "schema_version": 1,
            "status": "fixture-only",
            "approval_id": lock["acquisition"]["approval_id"],
            "action": "download-only",
            "runtime_lock_digest": self._sha(canonical_json_bytes(lock)),
            "platform_target": lock["platform_target"],
            "python_runtime": lock["python_runtime"],
            "approved_index_url": lock["acquisition"]["index_url"],
            "artifacts": sorted(artifacts, key=lambda item: item["name"]),
        }

    def _evaluate(
        self, lock: dict[str, object] | None = None, wheelhouse: Path | None = None
    ):
        actual_lock = self.lock if lock is None else lock
        return evaluate_offline_acquisition(
            actual_lock,
            self.wheelhouse if wheelhouse is None else wheelhouse,
            self._authorization(actual_lock),
            declaration_contents={
                source["path"]: self.declaration_content
                for source in actual_lock["declaration_sources"]
            },
            fixture_only=True,
        )

    def _rejects(self, code: str, lock: dict[str, object] | None = None) -> None:
        with self.assertRaisesRegex(RuntimeLockError, code):
            validate_runtime_lock(
                self.lock if lock is None else lock, fixture_only=True
            )

    def test_canonical_lock_determinism(self) -> None:
        validate_runtime_lock(self.lock, fixture_only=True)
        digest = runtime_lock_digest(self.lock)
        self.assertEqual(digest, self._sha(canonical_json_bytes(self.lock)))
        self.assertEqual(len(digest), 64)

    def test_field_ordering_invariance(self) -> None:
        reversed_fields = dict(reversed(list(self.lock.items())))
        reversed_fields["platform_target"] = dict(
            reversed(list(self.lock["platform_target"].items()))
        )
        self.assertEqual(
            canonical_json_bytes(self.lock), canonical_json_bytes(reversed_fields)
        )
        self.assertEqual(
            runtime_lock_digest(self.lock), runtime_lock_digest(reversed_fields)
        )

    def test_noncanonical_json_rejected(self) -> None:
        raw = b'{ "lock_kind": "wrong" }'
        with self.assertRaisesRegex(RuntimeLockError, "runtime_lock_json_noncanonical"):
            parse_canonical_runtime_lock(raw)

    def test_unknown_field_rejected(self) -> None:
        lock = dict(self.lock, unexpected=True)
        self._rejects("runtime_lock_fields_invalid", lock)

    def test_missing_field_rejected(self) -> None:
        lock = dict(self.lock)
        lock.pop("build_provenance")
        self._rejects("runtime_lock_fields_invalid", lock)

    def test_duplicate_package_rejected(self) -> None:
        lock = dict(self.lock)
        lock["packages"] = list(lock["packages"]) + [dict(lock["packages"][0])]
        self._rejects("runtime_package_duplicate", lock)

    def test_unpinned_version_rejected(self) -> None:
        lock = dict(self.lock)
        lock["packages"] = [dict(lock["packages"][0], version=">=1.0")]
        self._rejects("runtime_package_version_unpinned", lock)

    def test_missing_wheel_hash_rejected(self) -> None:
        lock = dict(self.lock)
        lock["packages"] = [dict(lock["packages"][0], wheel_sha256="")]
        self._rejects("runtime_wheel_hash_missing_or_invalid", lock)

    def test_wrong_wheel_hash_rejected(self) -> None:
        wheel = self.wheelhouse / self.lock["packages"][0]["wheel_filename"]
        wheel.write_bytes(wheel.read_bytes() + b"changed")
        with self.assertRaisesRegex(RuntimeLockError, "runtime_wheel_hash_mismatch"):
            self._evaluate()

    def test_unsupported_platform_tag_rejected(self) -> None:
        lock = dict(self.lock)
        package = dict(self.lock["packages"][0])
        package["wheel_filename"] = "demo_pkg-1.0-cp312-cp312-manylinux_2_39_x86_64.whl"
        lock["packages"] = [package]
        self._rejects("runtime_wheel_tag_unsupported", lock)

    def test_wrong_python_abi_rejected(self) -> None:
        lock = dict(self.lock)
        package = dict(self.lock["packages"][0])
        package["wheel_filename"] = (
            "demo_pkg-1.0-cp311-cp311-manylinux_2_39_aarch64.whl"
        )
        lock["packages"] = [package]
        self._rejects("runtime_wheel_tag_unsupported", lock)

    def test_unsupported_target_rejected(self) -> None:
        lock = dict(self.lock)
        lock["platform_target"] = dict(PLATFORM_TARGET, architecture="x86_64")
        self._rejects("runtime_target_unsupported", lock)

    def test_unsafe_wheel_filename_rejected(self) -> None:
        lock = dict(self.lock)
        lock["packages"] = [
            dict(self.lock["packages"][0], wheel_filename="../demo.whl")
        ]
        self._rejects("runtime_wheel_filename_unsafe", lock)

    def test_missing_transitive_dependency_rejected(self) -> None:
        lock = self._build_lock(
            package_specs={
                "demo-pkg": {"version": "1.0", "requires": ("child-pkg>=1.0",)},
                "child-pkg": {"version": "1.2", "requires": ()},
            },
            direct_requirements=("demo-pkg==1.0",),
        )
        lock["packages"] = [
            package for package in lock["packages"] if package["name"] != "child-pkg"
        ]
        with self.assertRaisesRegex(
            RuntimeLockError,
            "runtime_transitive_dependency_missing|runtime_dependency_parent_missing",
        ):
            validate_runtime_lock(lock, fixture_only=True)

    def test_locked_transitive_version_must_satisfy_declared_edge(self) -> None:
        lock = self._build_lock(
            package_specs={
                "demo-pkg": {"version": "1.0", "requires": ("child-pkg>=2.0",)},
                "child-pkg": {"version": "1.2", "requires": ()},
            },
            direct_requirements=("demo-pkg==1.0",),
        )
        self._rejects("runtime_version_constraint_mismatch", lock)

    def test_environment_marker_mismatch_rejected(self) -> None:
        lock = self._build_lock(
            direct_requirements=("demo-pkg==1.0; python_version < '3.12'",)
        )
        lock["direct_requirements"][0]["applies_to_target"] = True
        self._rejects("runtime_environment_marker_mismatch", lock)

    def test_requested_extras_mismatch_rejected(self) -> None:
        lock = self._build_lock(direct_requirements=("demo-pkg[fast]==1.0",))
        lock["packages"][0]["requested_extras"] = []
        self._rejects("runtime_extras_mismatch", lock)

    def test_native_extension_declaration_is_bound(self) -> None:
        native_bytes = b"synthetic ELF fixture, not an executable artifact"
        lock = self._build_lock(
            package_specs={
                "demo-pkg": {
                    "version": "1.0",
                    "requires": (),
                    "native_members": {"demo_pkg/_native.so": native_bytes},
                }
            }
        )
        decision = self._evaluate(lock)
        self.assertEqual(decision.status, "ARTIFACTS_AVAILABLE_CLOSURE_UNVERIFIED")
        self.assertIn("elf_closure_evidence_missing", decision.blockers)
        self.assertEqual(lock["native_extensions"][0]["member"], "demo_pkg/_native.so")

    def test_missing_elf_closure_evidence_blocks_readiness(self) -> None:
        decision = self._evaluate()
        self.assertEqual(decision.status, "ARTIFACTS_AVAILABLE_CLOSURE_UNVERIFIED")
        self.assertIn("elf_closure_evidence_missing", decision.blockers)
        lock = dict(self.lock)
        lock["elf_closure"] = {
            "status": "verified",
            "evidence_sha256": None,
            "inspector": None,
            "base_image_digest": None,
            "system_packages": [],
        }
        self._rejects("runtime_elf_closure_evidence_missing", lock)

    def test_elf_digest_alone_does_not_verify_native_closure(self) -> None:
        lock = dict(self.lock)
        lock["elf_closure"] = {
            "status": "verified",
            "evidence_sha256": self._sha(b"claimed evidence"),
            "inspector": "fixture-inspector 1.0",
            "base_image_digest": self._sha(b"fixture base image"),
            "system_packages": ["libc6:2.39-fixture"],
        }
        decision = self._evaluate(lock)
        self.assertIn(
            "elf_closure_artifact_not_inspected_by_wheel_policy", decision.blockers
        )
        self.assertFalse(decision.runtime_assembly_eligible)

    def test_external_executable_declaration_is_explicit(self) -> None:
        unresolved = {
            "kind": "executable",
            "name": "openclaw-cli",
            "identity_status": "unresolved",
            "path": None,
            "endpoint": None,
            "version": None,
            "sha256": None,
            "origin": None,
        }
        lock = self._build_lock(external_dependencies=[unresolved])
        decision = self._evaluate(lock)
        self.assertIn("external:executable:openclaw-cli", decision.blockers)
        malformed = dict(lock)
        malformed["external_dependencies"] = [
            dict(unresolved, path="/usr/bin/openclaw")
        ]
        self._rejects("runtime_external_unresolved_identity_ambiguous", malformed)

    def test_acquisition_origin_mismatch_rejected(self) -> None:
        lock = dict(self.lock)
        package = dict(self.lock["packages"][0])
        package["origin"] = dict(
            package["origin"], index_url="https://mirror.example.invalid/simple"
        )
        lock["packages"] = [package]
        with self.assertRaisesRegex(RuntimeLockError, "runtime_origin_unapproved"):
            self._evaluate(lock)

    def test_missing_artifact_returns_unresolved(self) -> None:
        (self.wheelhouse / self.lock["packages"][0]["wheel_filename"]).unlink()
        decision = self._evaluate()
        self.assertEqual(decision.status, "UNRESOLVED_ARTIFACTS")
        self.assertEqual(
            decision.missing_artifacts, (self.lock["packages"][0]["wheel_filename"],)
        )
        self.assertFalse(decision.runtime_assembly_eligible)

    def test_acquisition_authorization_binds_exact_artifacts(self) -> None:
        authorization = self._authorization(self.lock)
        authorization["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(
            RuntimeLockError, "runtime_authorized_artifact_identity_mismatch"
        ):
            evaluate_offline_acquisition(
                self.lock, self.wheelhouse, authorization, fixture_only=True
            )

    def test_declaration_source_digest_and_direct_line_are_verified(self) -> None:
        with self.assertRaisesRegex(
            RuntimeLockError, "runtime_declaration_source_digest_mismatch"
        ):
            evaluate_offline_acquisition(
                self.lock,
                self.wheelhouse,
                self._authorization(self.lock),
                declaration_contents={"requirements.txt": b"demo-pkg==2.0\n"},
                fixture_only=True,
            )

    def test_missing_declaration_source_bytes_are_explicitly_unverified(self) -> None:
        decision = evaluate_offline_acquisition(
            self.lock,
            self.wheelhouse,
            self._authorization(self.lock),
            fixture_only=True,
        )
        self.assertIn("declaration_sources_not_verified", decision.blockers)
        self.assertFalse(decision.runtime_assembly_eligible)

    def test_canonical_digest_fits_existing_wp1_manifest_field(self) -> None:
        digest = runtime_lock_digest(self.lock)
        self.assertEqual(
            release_packaging.WORKER_RELEASE_PROVENANCE_ROLES["runtime_lock"],
            "runtime_lock_digest",
        )
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        fixture = WorkerReleaseFixture(signed=False)
        try:
            lock_bytes = canonical_json_bytes(self.lock)
            lock_path = fixture.release / "provenance/runtime-lock.json"
            fixture.release.chmod(0o755)
            lock_path.parent.chmod(0o755)
            lock_path.chmod(0o644)
            lock_path.write_bytes(lock_bytes)
            lock_path.chmod(0o444)
            lock_path.parent.chmod(0o555)
            fixture.release.chmod(0o555)
            core = release_packaging._worker_identity_core(fixture.manifest)
            root_fd = os.open(fixture.release, os.O_RDONLY | os.O_DIRECTORY)
            try:
                core["payload_inventory"] = release_packaging._worker_inventory_release(
                    root_fd
                )
            finally:
                os.close(root_fd)
            core["runtime_lock_digest"] = digest
            references = dict(core["build_provenance"]["references"])
            references["runtime_lock"] = {
                "path": "provenance/runtime-lock.json",
                "sha256": digest,
            }
            core["build_provenance"] = dict(
                core["build_provenance"], references=references
            )
            integrated = release_packaging.build_worker_release_manifest(core)
            self.assertEqual(integrated["runtime_lock_digest"], digest)
        finally:
            fixture.close()

    def test_no_network_or_install_side_effects(self) -> None:
        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network"),
                create=True,
            ) as urlopen,
            mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess"), create=True
            ) as run,
            mock.patch(
                "subprocess.Popen",
                side_effect=AssertionError("subprocess"),
                create=True,
            ) as popen,
        ):
            decision = self._evaluate()
        self.assertTrue(decision.status.startswith("ARTIFACTS_AVAILABLE"))
        urlopen.assert_not_called()
        run.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
