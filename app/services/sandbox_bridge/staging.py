"""Private per-request staging and bounded artifact collection."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

MAX_INPUT_BYTES = 64 * 1024
MAX_ARTIFACTS = 8
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_ARTIFACT_TOTAL = 4 * 1024 * 1024


class StagingRejected(ValueError):
    pass


def validate_artifact_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise StagingRejected("invalid_artifact_path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise StagingRejected("invalid_artifact_path")
    return path


class StagedWorkspace:
    def __init__(
        self,
        root: str | Path,
        code: str,
        snapshot: list[tuple[PurePosixPath, bytes]] | None = None,
    ):
        """Stage ``input.py`` (v2) or pytest arguments plus a snapshot (v3).

        ``snapshot`` is the already validated output of
        ``contract.decode_workspace_snapshot``. It is written read-only under
        ``src/``; nothing staged here is ever copied back to the Worker.
        """

        self.snapshot = snapshot
        raw = code.encode("utf-8")
        if not raw or len(raw) > MAX_INPUT_BYTES:
            raise StagingRejected("invalid_input_size")
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            self.root.is_symlink()
            or not self.root.is_dir()
            or self.root.resolve() != self.root.absolute()
        ):
            raise StagingRejected("unsafe_staging_root")
        if self.root.stat().st_uid != os.geteuid():
            raise StagingRejected("unsafe_staging_owner")
        os.chmod(self.root, 0o700)
        self.path = Path(tempfile.mkdtemp(prefix="request-", dir=self.root))
        try:
            self._initialize(raw)
        except BaseException:
            shutil.rmtree(self.path, ignore_errors=True)
            raise

    @staticmethod
    def _write_readonly(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)

    def _initialize(self, raw: bytes) -> None:
        # The Host-only 0700 parent contains the 0755 bind root. Only the
        # artifact directory is writable by the fixed sandbox UID; its sticky
        # bit prevents replacing entries owned by another identity.
        os.chmod(self.path, 0o755)
        if self.snapshot is not None:
            self._initialize_snapshot(raw)
            return
        self.input_path = self.path / "input.py"
        # Docker executes as uid/gid 65534; the source must be readable by that
        # identity while remaining immutable inside the read-only bind root.
        fd = os.open(
            self.input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444
        )
        try:
            os.write(fd, raw)
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)
        self.output_path = self.path / "output"
        self.output_path.mkdir(mode=0o1777)
        os.chmod(self.output_path, 0o1777)

    def _initialize_snapshot(self, raw: bytes) -> None:
        # v3: the whole bind is mounted read-only; the sandbox writes only to
        # its own tmpfs. Directories are created fresh here, so no component
        # can be a pre-existing link.
        self.input_path = self.path / "args.json"
        self._write_readonly(self.input_path, raw)
        self.source_path = self.path / "src"
        self.source_path.mkdir(mode=0o755)
        created = {self.source_path}
        for rel, data in self.snapshot or []:
            target = self.source_path.joinpath(*rel.parts)
            for parent in reversed(target.parents):
                if parent in created or self.source_path not in (
                    parent,
                    *parent.parents,
                ):
                    continue
                parent.mkdir(mode=0o755)
                created.add(parent)
            self._write_readonly(target, data)
        for directory in sorted(created, key=lambda d: len(d.parts), reverse=True):
            os.chmod(directory, 0o555)
        self.output_path = None

    def collect(self) -> tuple[list[dict[str, object]], list[bytes]]:
        manifest: list[dict[str, object]] = []
        contents: list[bytes] = []
        total = 0
        root_fd = os.open(
            self.output_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            for name in sorted(os.listdir(root_fd)):
                rel = validate_artifact_path(name)
                st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise StagingRejected("unsafe_artifact_entry")
                if st.st_size > MAX_ARTIFACT_BYTES or len(manifest) >= MAX_ARTIFACTS:
                    raise StagingRejected("artifact_limit_exceeded")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (
                        st.st_dev,
                        st.st_ino,
                    ) or opened.st_nlink != 1:
                        raise StagingRejected("artifact_changed_during_read")
                    chunks = bytearray()
                    while len(chunks) <= MAX_ARTIFACT_BYTES:
                        part = os.read(
                            fd, min(65536, MAX_ARTIFACT_BYTES + 1 - len(chunks))
                        )
                        if not part:
                            break
                        chunks.extend(part)
                finally:
                    os.close(fd)
                data = bytes(chunks)
                total += len(data)
                if len(data) > MAX_ARTIFACT_BYTES or total > MAX_ARTIFACT_TOTAL:
                    raise StagingRejected("artifact_limit_exceeded")
                manifest.append(
                    {
                        "path": str(rel),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                contents.append(data)
        finally:
            os.close(root_fd)
        return manifest, contents

    def cleanup(self) -> None:
        if self.snapshot is not None:
            # Read-only snapshot directories must be writable to be removed.
            for directory, _dirs, _files in os.walk(self.path):
                os.chmod(directory, 0o700)
        shutil.rmtree(self.path, ignore_errors=False)
