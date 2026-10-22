from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b import (
    release_entrypoint,
    release_manager,
    release_packaging,
)


UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC).replace(microsecond=0)
EMPTY_DELTA = hashlib.sha256(b"").hexdigest()


def _run(
    argv: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"command failed: {argv[0]}: {result.stderr.decode(errors='replace')}"
        )
    return result


class SignedAptPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_temp = tempfile.TemporaryDirectory(prefix="p39-p0b-test-keys-")
        cls.key_root = Path(cls.key_temp.name)
        cls.good_home = cls.key_root / "good-home"
        cls.bad_home = cls.key_root / "bad-home"
        cls.good_home.mkdir(mode=0o700)
        cls.bad_home.mkdir(mode=0o700)
        cls.good_fingerprint = cls._generate_key(
            cls.good_home, "Phase39 P0B TEST ONLY <phase39-test-only@example.invalid>"
        )
        cls.bad_fingerprint = cls._generate_key(
            cls.bad_home, "Phase39 P0B WRONG TEST ONLY <phase39-wrong@example.invalid>"
        )
        cls.good_keyring = cls.key_root / "good-test-only.gpg"
        cls.bad_keyring = cls.key_root / "bad-test-only.gpg"
        cls.good_keyring.write_bytes(
            _run(
                [
                    "/usr/bin/gpg",
                    "--batch",
                    "--homedir",
                    str(cls.good_home),
                    "--export",
                    cls.good_fingerprint,
                ]
            ).stdout
        )
        cls.bad_keyring.write_bytes(
            _run(
                [
                    "/usr/bin/gpg",
                    "--batch",
                    "--homedir",
                    str(cls.bad_home),
                    "--export",
                    cls.bad_fingerprint,
                ]
            ).stdout
        )
        os.chmod(cls.good_keyring, 0o644)
        os.chmod(cls.bad_keyring, 0o644)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.key_temp.cleanup()

    @staticmethod
    def _generate_key(home: Path, user_id: str) -> str:
        _run(
            [
                "/usr/bin/gpg",
                "--batch",
                "--homedir",
                str(home),
                "--passphrase",
                "",
                "--quick-generate-key",
                user_id,
                "rsa2048",
                "sign",
                "0",
            ]
        )
        listing = _run(
            [
                "/usr/bin/gpg",
                "--batch",
                "--homedir",
                str(home),
                "--with-colons",
                "--list-keys",
            ]
        ).stdout.decode()
        fingerprints = [
            line.split(":")[9]
            for line in listing.splitlines()
            if line.startswith("fpr:") and len(line.split(":")) > 9
        ]
        if len(fingerprints) != 1:
            raise AssertionError("test-only GPG key fingerprint unavailable")
        return fingerprints[0]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p39-p0b-packaging-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self._write("scripts/maintenance/phase39_p0b/worker.py", b"# fixed source\n")
        self._write(
            "app/services/orchestration/worker_identity.py", b"# worker identity\n"
        )
        self._write("start.sh", b"#!/bin/bash\nexit 0\n", mode=0o755)
        self._write("requirements.txt", b"celery==5.4.0\n")
        self.source_tree = release_manager._git_tree_id(self.source)
        self.commit_object = (
            f"tree {self.source_tree}\n"
            "author Phase39 Test <phase39-test@example.invalid> 1792510000 +0000\n"
            "committer Phase39 Test <phase39-test@example.invalid> 1792510000 +0000\n\n"
            "temporary signed package fixture\n"
        ).encode()
        self.source_head = hashlib.sha1(
            b"commit "
            + str(len(self.commit_object)).encode()
            + b"\0"
            + self.commit_object
        ).hexdigest()
        self.commit_object_b64 = base64.b64encode(self.commit_object).decode("ascii")
        self.release_id = release_packaging.derive_release_id(
            self.source, source_head=self.source_head
        )
        self.version = release_packaging.package_version(self.release_id)
        self.package = self._build()

    def _write(self, relative: str, contents: bytes, *, mode: int = 0o644) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        os.chmod(path, mode)
        return path

    def _build(
        self, release_id: str | None = None, output_name: str = "packages"
    ) -> Path:
        if release_id is None:
            release_id = release_packaging.derive_release_id(
                self.source, source_head=self.source_head
            )
        return release_packaging.build_deb(
            self.source,
            self.base / output_name,
            release_id=release_id,
            source_head=self.source_head,
            source_tree=self.source_tree,
            commit_object_b64=self.commit_object_b64,
            source_delta_sha256=EMPTY_DELTA,
            source_date_epoch=1792510000,
        )

    def _publish(
        self,
        *,
        repository_name: str = "repo",
        origin: str = release_packaging.ORIGIN,
        valid_until: dt.datetime | None = None,
    ) -> Path:
        return release_packaging.publish_repository(
            self.base / repository_name,
            self.package,
            signing_key_fingerprint=self.good_fingerprint,
            gnupghome=self.good_home,
            origin=origin,
            date=NOW,
            valid_until=valid_until or NOW + dt.timedelta(days=2),
        )

    def _verify_repo(
        self,
        repo: Path,
        *,
        keyring: Path | None = None,
        fingerprint: str | None = None,
        package: str | None = None,
        version: str | None = None,
        release_id: str | None = None,
        architecture: str = "arm64",
        origin: str = release_packaging.ORIGIN,
        now: dt.datetime = NOW,
    ) -> dict:
        return release_packaging.verify_repository(
            repo,
            keyring=keyring or self.good_keyring,
            expected_fingerprint=fingerprint or self.good_fingerprint,
            expected_origin=origin,
            expected_package=package or release_packaging.package_name(self.release_id),
            expected_version=version or self.version,
            expected_release_id=release_id or self.release_id,
            expected_architecture=architecture,
            now=now,
        )

    def _raw_extract(self, package: Path, label: str) -> tuple[Path, Path]:
        root = self.base / (label + "-raw")
        result = subprocess.run(
            ["/usr/bin/dpkg-deb", "--raw-extract", str(package), str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode:
            self.fail(result.stderr.decode(errors="replace"))
        return root, root / "DEBIAN"

    def _make_parent_writable(self, root: Path, target: Path) -> None:
        parent = target.parent
        while parent != root.parent:
            parent.chmod(0o755)
            if parent == root:
                break
            parent = parent.parent

    def _rebuild_raw(self, root: Path, label: str) -> Path:
        target = self.base / f"{label}.deb"
        _run(
            [
                "/usr/bin/dpkg-deb",
                "--build",
                "--root-owner-group",
                "--uniform-compression",
                "-Zgzip",
                "-z9",
                str(root),
                str(target),
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "TZ": "UTC",
                "SOURCE_DATE_EPOCH": "1792510000",
            },
        )
        return target

    def _resign(self, repo: Path) -> None:
        release = repo / "dists/noble/Release"
        data = release.read_bytes()
        inrelease = release.with_name("InRelease")
        inrelease.unlink()
        _run(
            [
                "/usr/bin/gpg",
                "--batch",
                "--no-tty",
                "--yes",
                "--homedir",
                str(self.good_home),
                "--local-user",
                self.good_fingerprint,
                "--digest-algo",
                "SHA256",
                "--clearsign",
                "--output",
                str(inrelease),
                str(release),
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "LANG": "C",
                "TZ": "UTC",
                "GNUPGHOME": str(self.good_home),
            },
        )
        self.assertTrue(data)

    def test_correct_signed_candidate_fixture_is_not_activatable(self) -> None:
        result = release_packaging.verify_package_archive(
            self.package,
            expected_release_id=self.release_id,
            expected_package_version=self.version,
            expected_source_head=self.source_head,
            expected_source_tree=self.source_tree,
        )
        self.assertEqual(
            result["package"], release_packaging.package_name(self.release_id)
        )
        self.assertEqual(result["activation_status"], "NOT_ACTIVATABLE")
        repo = self._publish()
        checked = self._verify_repo(repo)
        self.assertEqual(checked["signature"], "VALID")
        self.assertEqual(checked["activation_status"], "NOT_ACTIVATABLE")
        with self.assertRaisesRegex(
            release_entrypoint.EntryPointError, "release_package_not_activatable"
        ):
            release_entrypoint.validate_package_activation_manifest(
                result["manifest"], release_id=self.release_id
            )

    def test_post_unpack_integrity_verifier_accepts_only_the_complete_fixture(
        self,
    ) -> None:
        root, _ = self._raw_extract(self.package, "installed-fixture")
        release_root = (
            root / "usr/lib/orchestrator-maintenance/phase39-p0b" / self.release_id
        )
        expected_dpkg_status = f"install ok installed\t{self.version}\tarm64\n".encode()
        with patch.object(
            release_packaging,
            "_run",
            return_value=release_packaging.subprocess.CompletedProcess(
                [], 0, expected_dpkg_status, b""
            ),
        ) as run_command:
            checked = release_packaging.verify_installed_package_release(
                release_root,
                expected_package=release_packaging.package_name(self.release_id),
                expected_version=self.version,
                expected_source_head=self.source_head,
                expected_source_tree=self.source_tree,
                expected_installation_parent=release_root.parent,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
        self.assertEqual(run_command.call_args.args[0][0], "/usr/bin/dpkg-query")
        self.assertEqual(checked["status"], "RELEASE_INSTALLED_NOT_APPROVED")
        self.assertEqual(checked["activation_status"], "NOT_ACTIVATABLE")

    def test_build_is_byte_reproducible(self) -> None:
        first = self._build(self.release_id, "build-one")
        second = self._build(self.release_id, "build-two")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_wrong_source_head_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "wrong_source_head"
        ):
            release_packaging.verify_package_archive(
                self.package, expected_source_head="f" * 40
            )

    def test_wrong_source_tree_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "wrong_source_tree"
        ):
            release_packaging.verify_package_archive(
                self.package, expected_source_tree="e" * 40
            )

    def test_wrong_architecture_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "unapproved_package_architecture"
        ):
            release_packaging.verify_package_archive(
                self.package, expected_architecture="amd64"
            )

    def test_wrong_package_version_is_rejected(self) -> None:
        other_release_id = "r1-" + "a" * 16 + "-" + "b" * 16
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "wrong_package_version"
        ):
            release_packaging.verify_package_archive(
                self.package,
                expected_package_version=release_packaging.package_version(
                    other_release_id
                ),
            )

    def test_wrong_release_id_is_rejected(self) -> None:
        other_release_id = "r1-" + "a" * 16 + "-" + "b" * 16
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "wrong_release_id"
        ):
            release_packaging.verify_package_archive(
                self.package, expected_release_id=other_release_id
            )

    def test_duplicate_release_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "release_id_already_packaged"
        ):
            self._build(self.release_id)

    def test_changed_payload_cannot_claim_committed_source_tree(self) -> None:
        target = self.source / "app/services/orchestration/worker_identity.py"
        target.write_bytes(b"# changed source bytes\n")
        with self.assertRaisesRegex(
            release_packaging.PackagingError,
            "source_payload_does_not_match_committed_tree",
        ):
            self._build(self.release_id, "changed-payload")

    def test_nonempty_worktree_delta_cannot_be_marked_publishable(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError,
            "dirty_source_candidate_not_publishable",
        ):
            release_packaging.build_deb(
                self.source,
                self.base / "dirty-package",
                release_id=self.release_id,
                source_head=self.source_head,
                source_tree=self.source_tree,
                commit_object_b64=self.commit_object_b64,
                source_delta_sha256=hashlib.sha256(b"tracked edit").hexdigest(),
                source_date_epoch=1792510000,
            )

    def test_manifest_flag_cannot_relabel_a_dirty_source_snapshot(self) -> None:
        root, _ = self._raw_extract(self.package, "dirty-source-manifest")
        manifest_path = (
            root
            / "usr/lib/orchestrator-maintenance/phase39-p0b"
            / self.release_id
            / release_packaging.MANIFEST_NAME
        )
        manifest = release_packaging.json.loads(manifest_path.read_text())
        manifest["source"]["snapshot_kind"] = "dirty-worktree"
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(release_packaging.canonical_json(manifest))
        manifest_path.chmod(0o444)
        altered = self._rebuild_raw(root, "dirty-source-manifest")
        with self.assertRaisesRegex(
            release_packaging.PackagingError,
            "package_source_snapshot_not_publishable",
        ):
            release_packaging.verify_package_archive(altered)

    def test_altered_package_payload_is_rejected(self) -> None:
        root, _ = self._raw_extract(self.package, "altered-payload")
        target = (
            root
            / "usr/lib/orchestrator-maintenance/phase39-p0b"
            / self.release_id
            / "app/services/orchestration/worker_identity.py"
        )
        self._make_parent_writable(root, target)
        target.chmod(0o644)
        target.write_bytes(b"# altered payload\n")
        target.chmod(0o444)
        altered = self._rebuild_raw(root, "altered-payload")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "package_payload_hash_or_mode_mismatch"
        ):
            release_packaging.verify_package_archive(altered)

    def test_missing_release_manifest_is_rejected(self) -> None:
        root, _ = self._raw_extract(self.package, "missing-manifest")
        target = (
            root
            / "usr/lib/orchestrator-maintenance/phase39-p0b"
            / self.release_id
            / release_packaging.MANIFEST_NAME
        )
        self._make_parent_writable(root, target)
        target.unlink()
        altered = self._rebuild_raw(root, "missing-manifest")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "release_manifest_missing"
        ):
            release_packaging.verify_package_archive(altered)

    def test_writable_executable_file_is_rejected(self) -> None:
        root, _ = self._raw_extract(self.package, "writable-exec")
        target = (
            root
            / "usr/lib/orchestrator-maintenance/phase39-p0b"
            / self.release_id
            / "start.sh"
        )
        self._make_parent_writable(root, target)
        os.chmod(target, 0o777)
        altered = self._rebuild_raw(root, "writable-exec")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "package_file_path_or_mode_invalid"
        ):
            release_packaging.verify_package_archive(altered)

    def test_symlink_escape_is_rejected(self) -> None:
        (self.source / "escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(
            release_packaging.PackagingError,
            "source_symlink_or_special_directory|source_file_type_or_link_invalid",
        ):
            self._build(output_name="symlink-package")

    def test_hardlink_substitution_is_rejected(self) -> None:
        os.link(
            self.source / "requirements.txt",
            self.source / "requirements-hardlink.txt",
        )
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "source_file_type_or_link_invalid"
        ):
            self._build(output_name="hardlink-package")

    def test_unexpected_maintainer_script_is_rejected(self) -> None:
        root, control = self._raw_extract(self.package, "unexpected-script")
        (control / "postinst").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(control / "postinst", 0o755)
        altered = self._rebuild_raw(root, "unexpected-script")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "unexpected_maintainer_script"
        ):
            release_packaging.verify_package_archive(altered)

    def test_service_activation_attempt_is_rejected(self) -> None:
        root, control = self._raw_extract(self.package, "service-script")
        script = control / "postinst"
        script.write_text(
            "#!/bin/sh\nsystemctl start orchestrator-worker.service\n", encoding="utf-8"
        )
        os.chmod(script, 0o755)
        altered = self._rebuild_raw(root, "service-script")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "unexpected_maintainer_script"
        ):
            release_packaging.verify_package_archive(altered)

    def test_unsigned_repository_metadata_is_rejected(self) -> None:
        repo = self._publish()
        (repo / "dists/noble/InRelease").unlink()
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "signed_inrelease_missing"
        ):
            self._verify_repo(repo)

    def test_wrong_signing_key_is_rejected(self) -> None:
        repo = self._publish()
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "inrelease_signature_invalid"
        ):
            self._verify_repo(
                repo, keyring=self.bad_keyring, fingerprint=self.bad_fingerprint
            )

    def test_wrong_repository_origin_is_rejected(self) -> None:
        repo = self._publish()
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "repository_origin_mismatch"
        ):
            self._verify_repo(repo, origin="Unapproved-Origin")

    def test_altered_package_hash_is_rejected(self) -> None:
        repo = self._publish()
        package = next((repo / "pool").rglob("*.deb"))
        package.write_bytes(package.read_bytes() + b"altered")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "repository_package_hash_mismatch"
        ):
            self._verify_repo(repo)

    def test_unapproved_package_version_is_rejected(self) -> None:
        repo = self._publish()
        other_release_id = "r1-" + "a" * 16 + "-" + "b" * 16
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "unapproved_package_version"
        ):
            self._verify_repo(
                repo, version=release_packaging.package_version(other_release_id)
            )

    def test_missing_package_hash_is_rejected(self) -> None:
        repo = self._publish()
        index = repo / "dists/noble/main/binary-arm64/Packages"
        index.write_bytes(
            re.sub(rb"(?m)^SHA256: [0-9a-f]{64}\n", b"", index.read_bytes())
        )
        release = repo / "dists/noble/Release"
        fields = release_packaging._parse_release(release.read_bytes())
        date = release_packaging._parse_utc(fields["Date"], "date")
        expiry = release_packaging._parse_utc(fields["Valid-Until"], "expiry")
        release.write_bytes(
            release_packaging._release_metadata(
                origin=fields["Origin"],
                date=date,
                valid_until=expiry,
                packages=index.read_bytes(),
            )
        )
        self._resign(repo)
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "packages_index_fields_invalid"
        ):
            self._verify_repo(repo)

    def test_expired_repository_metadata_is_rejected(self) -> None:
        repo = self._publish()
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "repository_metadata_expired"
        ):
            self._verify_repo(repo, now=NOW + dt.timedelta(days=3))

    def test_partial_package_fixture_is_rejected(self) -> None:
        root, _ = self._raw_extract(self.package, "partial-package")
        target = (
            root
            / "usr/lib/orchestrator-maintenance/phase39-p0b"
            / self.release_id
            / "app/services/orchestration/worker_identity.py"
        )
        self._make_parent_writable(root, target)
        target.unlink()
        partial = self._rebuild_raw(root, "partial-package")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "package_payload_file_missing"
        ):
            release_packaging.verify_package_archive(partial)

    def test_apt_signed_by_and_pin_policy_are_explicit(self) -> None:
        source = release_packaging.apt_source_line(
            "https://apt.example.invalid/orchestrator",
            "/etc/apt/keyrings/orchestrator-p39-p0b.gpg",
        )
        pin = release_packaging.apt_pin_preferences(
            release_packaging.package_name(self.release_id)
        )
        self.assertIn(
            "arch=arm64 signed-by=/etc/apt/keyrings/orchestrator-p39-p0b.gpg", source
        )
        self.assertIn(f"release o={release_packaging.ORIGIN},n=noble,c=main", pin)
        self.assertIn(
            f"Package: {release_packaging.package_name(self.release_id)}", pin
        )

    def test_apt_get_update_uses_only_the_disposable_signed_by_fixture(self) -> None:
        repo = self._publish()
        os.chmod(self.base, 0o755)
        keyring = self.base / "apt-test-only.gpg"
        shutil.copyfile(self.good_keyring, keyring)
        os.chmod(keyring, 0o644)
        config = self.base / "apt-config"
        lists = self.base / "apt-lists"
        cache = self.base / "apt-cache"
        for path in (lists / "partial", cache / "archives" / "partial"):
            path.mkdir(parents=True)
            os.chmod(path, 0o755)
        status = self.base / "status"
        status.write_bytes(b"")
        sources = self.base / "sources.list"
        sources.write_text(
            release_packaging.apt_source_line(repo.as_uri(), str(keyring)) + "\n",
            encoding="utf-8",
        )
        preferences = self.base / "preferences"
        preferences.write_text(
            release_packaging.apt_pin_preferences(
                release_packaging.package_name(self.release_id)
            ),
            encoding="utf-8",
        )
        config.write_text("", encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}
        common = [
            "-o",
            f"Dir::Etc::main={config}",
            "-o",
            "Dir::Etc::parts=-",
            "-o",
            f"Dir::Etc::sourcelist={sources}",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            f"Dir::Etc::preferences={preferences}",
            "-o",
            "Dir::Etc::preferencesparts=-",
            "-o",
            f"Dir::State::lists={lists}/",
            "-o",
            f"Dir::State::status={status}",
            "-o",
            f"Dir::Cache={cache}/",
            "-o",
            "APT::Architecture=arm64",
            "-o",
            "Acquire::Languages=none",
            "-o",
            "APT::Get::List-Cleanup=0",
            "-o",
            "Debug::NoLocking=1",
        ]
        update = _run(["/usr/bin/apt-get", *common, "update"], env=env)
        self.assertIn(b"Reading package lists", update.stdout)
        policy = _run(
            [
                "/usr/bin/apt-cache",
                *common,
                "policy",
                release_packaging.package_name(self.release_id),
            ],
            env=env,
        ).stdout.decode()
        self.assertIn(f"Candidate: {self.version}", policy)
        self.assertIn("file:", policy)
        self.assertIn(f"{self.version} 1001", policy)


class PublishableSourceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p39-p0b-source-snapshot-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self._write("app/main.py", b"# committed app source\n")
        self._write(
            "scripts/maintenance/phase39_p0b/entrypoint.sh",
            b"#!/bin/sh\nexit 0\n",
            0o755,
        )
        self._git("init", "-q")
        self._git("add", "--all")
        self._git("commit", "-q", "-m", "clean source fixture")
        self.head = self._git("rev-parse", "HEAD").decode().strip()

    def _write(self, relative: str, contents: bytes, mode: int = 0o644) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        os.chmod(path, mode)
        return path

    def _git(self, *args: str) -> bytes:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.base),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "P0B Test",
            "GIT_AUTHOR_EMAIL": "p0b-test@example.invalid",
            "GIT_COMMITTER_NAME": "P0B Test",
            "GIT_COMMITTER_EMAIL": "p0b-test@example.invalid",
            "GIT_AUTHOR_DATE": "@1792510000 +0000",
            "GIT_COMMITTER_DATE": "@1792510000 +0000",
        }
        return _run(["/usr/bin/git", "-C", str(self.repo), *args], env=env).stdout

    def _build(self, *, expected_head: str | None = None) -> Path:
        return release_packaging.build_from_checkout(
            self.repo,
            self.base / "out",
            expected_source_head=expected_head or self.head,
            release_id="auto",
        )

    def test_clean_committed_git_archive_builds_exact_candidate(self) -> None:
        package = self._build()
        verified = release_packaging.verify_package_archive(
            package, expected_source_head=self.head
        )
        source = verified["manifest"]["source"]
        self.assertEqual(source["head"], self.head)
        self.assertEqual(
            source["snapshot_kind"], release_packaging.SOURCE_SNAPSHOT_KIND
        )
        self.assertEqual(
            source["tracked_worktree_delta_sha256"],
            release_packaging.EMPTY_SOURCE_DELTA_SHA256,
        )
        self.assertEqual(verified["activation_status"], "NOT_ACTIVATABLE")

    def test_dirty_tracked_source_rejected_for_publishable_build(self) -> None:
        (self.repo / "app/main.py").write_bytes(b"# uncommitted edit\n")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "publishable_source_worktree_dirty"
        ):
            self._build()

    def test_untracked_source_injection_rejected(self) -> None:
        self._write("app/injected.py", b"# untracked source\n")
        with self.assertRaisesRegex(
            release_packaging.PackagingError, "publishable_source_worktree_dirty"
        ):
            self._build()

    def test_explicit_source_head_must_match_checkout_commit(self) -> None:
        with self.assertRaisesRegex(
            release_packaging.PackagingError,
            "checkout_head_does_not_match_explicit_source",
        ):
            self._build(expected_head="0" * 40)


if __name__ == "__main__":
    unittest.main()
