"""One-shot, operator-quiesced SQLite WAL bundle snapshot helper.

This helper deliberately never opens the source with SQLite.  A caller must
explicitly attest that all source writers are quiesced for the whole copy.
The copied database, WAL, and SHM files are then validated by fingerprint;
SQLite validation belongs on the destination copy only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


class SnapshotSafetyError(RuntimeError):
    """Raised when the source bundle cannot be copied safely."""


@dataclass(frozen=True)
class FileFingerprint:
    name: str
    size: int
    mtime_ns: int
    sha256: str


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _reject_symlink_ancestors(path: Path, *, label: str) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise SnapshotSafetyError(
                f"{label} path must not traverse a symlink: {ancestor}"
            )


def _fingerprint(path: Path) -> FileFingerprint:
    if path.is_symlink() or not path.is_file():
        raise SnapshotSafetyError(
            f"source or destination is not a regular file: {path}"
        )
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return FileFingerprint(
        name=path.name,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


def _bundle_paths(database: Path) -> tuple[Path, Path, Path]:
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    present = (database.exists(), wal.exists(), shm.exists())
    if present != (True, True, True):
        raise SnapshotSafetyError(
            "a WAL snapshot requires the database, -wal, and -shm files; "
            f"present={present} for {database}"
        )
    return database, wal, shm


def copy_quiesced_sqlite_bundle(
    source_database: Path,
    destination_database: Path,
    *,
    operator_quiesced: bool = False,
) -> dict[str, object]:
    """Copy a complete WAL bundle without opening the source in SQLite.

    The explicit quiescence assertion is intentional.  File copying is not a
    substitute for quiescence or an atomic storage snapshot when writers may
    run concurrently.  The source is fingerprinted before and after copying;
    any change rejects the result.  The destination is the only path that a
    later SQLite integrity check may open.
    """

    if not operator_quiesced:
        raise SnapshotSafetyError(
            "operator-controlled source quiescence is required before copying"
        )

    source_database = _absolute_path(Path(source_database))
    destination_database = _absolute_path(Path(destination_database))
    _reject_symlink_ancestors(source_database, label="source")
    _reject_symlink_ancestors(destination_database, label="destination")
    source_root = source_database.parent.resolve()
    destination_root = destination_database.resolve(strict=False)
    if destination_root == source_root or source_root in destination_root.parents:
        raise SnapshotSafetyError(
            "destination must be outside the protected source directory"
        )
    if destination_database.exists() or destination_database.is_symlink():
        raise SnapshotSafetyError(
            f"destination database already exists: {destination_database}"
        )

    source_paths = _bundle_paths(source_database)
    destination_paths = tuple(
        Path(f"{destination_database}{suffix}") for suffix in ("", "-wal", "-shm")
    )
    if any(path.exists() or path.is_symlink() for path in destination_paths):
        raise SnapshotSafetyError(
            f"destination bundle already exists: {destination_database}"
        )

    destination_database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(destination_database.parent.stat().st_mode) & 0o077:
        raise SnapshotSafetyError(
            "destination directory must be restricted to the operator"
        )
    before = tuple(_fingerprint(path) for path in source_paths)
    for source, destination in zip(source_paths, destination_paths):
        shutil.copy2(source, destination)
        destination.chmod(0o600)
    after = tuple(_fingerprint(path) for path in source_paths)
    if after != before:
        raise SnapshotSafetyError(
            "source bundle changed during copy; discard the destination and retry "
            "only after source quiescence is restored"
        )

    copied = tuple(_fingerprint(path) for path in destination_paths)
    if tuple(item.sha256 for item in copied) != tuple(item.sha256 for item in before):
        raise SnapshotSafetyError("destination bundle does not match source content")

    return {
        "source": str(source_database),
        "destination": str(destination_database),
        "source_unchanged": True,
        "files": [asdict(item) for item in before],
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--quiesced",
        action="store_true",
        help="assert that all source writers are quiesced for the entire copy",
    )
    args = parser.parse_args()
    try:
        result = copy_quiesced_sqlite_bundle(
            args.source,
            args.destination,
            operator_quiesced=args.quiesced,
        )
    except SnapshotSafetyError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
