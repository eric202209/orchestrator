from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from scripts.p42_v15a_snapshot import (
    SnapshotSafetyError,
    copy_quiesced_sqlite_bundle,
)


def _create_wal_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records(value) VALUES ('main-or-wal-row')")
        connection.execute("INSERT INTO records(value) VALUES ('second-row')")
        connection.commit()


def test_snapshot_requires_explicit_operator_quiescence(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_wal_fixture(source)

    with pytest.raises(SnapshotSafetyError, match="quiescence"):
        copy_quiesced_sqlite_bundle(source, tmp_path / "copy.db")


def test_wal_bundle_copy_preserves_source_and_reads_on_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "source.db"
    destination = tmp_path / "copy" / "database.db"
    _create_wal_fixture(source)
    source_files = tuple(Path(f"{source}{suffix}") for suffix in ("", "-wal", "-shm"))
    before = tuple(
        (path.stat().st_size, path.stat().st_mtime_ns) for path in source_files
    )

    result = copy_quiesced_sqlite_bundle(
        source,
        destination,
        operator_quiesced=True,
    )

    after = tuple(
        (path.stat().st_size, path.stat().st_mtime_ns) for path in source_files
    )
    assert result["source_unchanged"] is True
    assert before == after
    with sqlite3.connect(destination) as connection:
        count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert count == 2

    # Opening/validating the destination may update its SHM; it must not touch
    # the source bundle.
    assert after == tuple(
        (path.stat().st_size, path.stat().st_mtime_ns) for path in source_files
    )


def test_snapshot_rejects_destination_inside_source_or_symlinked_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "source.db"
    _create_wal_fixture(source)

    with pytest.raises(SnapshotSafetyError, match="outside the protected source"):
        copy_quiesced_sqlite_bundle(
            source,
            source.parent / "nested" / "database.db",
            operator_quiesced=True,
        )

    real_parent = tmp_path / "real-destination"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-destination"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(SnapshotSafetyError, match="must not traverse a symlink"):
        copy_quiesced_sqlite_bundle(
            source,
            linked_parent / "database.db",
            operator_quiesced=True,
        )


def test_snapshot_forces_restricted_destination_permissions(tmp_path: Path) -> None:
    source = tmp_path / "source" / "source.db"
    destination = tmp_path / "copy" / "database.db"
    _create_wal_fixture(source)
    for path in (source, Path(f"{source}-wal"), Path(f"{source}-shm")):
        path.chmod(0o644)

    copy_quiesced_sqlite_bundle(
        source,
        destination,
        operator_quiesced=True,
    )

    for path in (
        destination,
        Path(f"{destination}-wal"),
        Path(f"{destination}-shm"),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
