"""Durable request admission ledger; uncertain admissions are never replayed."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
import threading
import hashlib
import os


class RequestLedger:
    # Fixed-size Bloom tombstones preserve replay rejection after bounded
    # terminal-row retention. False positives reject admission, never execute.
    BUCKETS = 256
    BITS = 1 << 18
    POSITIONS = 4

    def __init__(self, path: str | Path, *, retention: int = 10000):
        self.path, self.retention = str(path), retention
        self.lock = threading.Lock()
        parent = Path(self.path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            parent.is_symlink()
            or (parent.stat().st_mode & 0o077)
            or parent.stat().st_uid != os.geteuid()
        ):
            raise RuntimeError("unsafe_ledger_directory")
        ledger = Path(self.path)
        if ledger.is_symlink():
            raise RuntimeError("unsafe_ledger_file")
        fd = os.open(ledger, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sandbox_requests (request_id TEXT PRIMARY KEY, admitted REAL NOT NULL, status TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS sandbox_requests_admitted ON sandbox_requests(admitted)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS replay_bloom (bucket INTEGER PRIMARY KEY, bits BLOB NOT NULL)"
            )
            db.executemany(
                "INSERT OR IGNORE INTO replay_bloom VALUES (?, zeroblob(?))",
                ((i, self.BITS // 8) for i in range(self.BUCKETS)),
            )
            # A request left ACCEPTED by a previous Launcher has an uncertain
            # outcome. The Bloom tombstone blocks replay; record uncertainty.
            db.execute(
                "UPDATE sandbox_requests SET status='INTERNAL_ERROR' WHERE status='ACCEPTED'"
            )
            db.execute(
                "DELETE FROM sandbox_requests WHERE status != 'ACCEPTED' AND request_id IN (SELECT request_id FROM sandbox_requests WHERE status != 'ACCEPTED' ORDER BY admitted DESC LIMIT -1 OFFSET ?)",
                (self.retention,),
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def admit(self, request_id: str) -> bool:
        with self.lock, self._connect() as db:
            digest = hashlib.sha256(request_id.encode()).digest()
            bucket = digest[0]
            positions = [
                int.from_bytes(digest[i : i + 4], "big") % self.BITS
                for i in (1, 5, 9, 13)
            ]
            row = db.execute(
                "SELECT bits FROM replay_bloom WHERE bucket=?", (bucket,)
            ).fetchone()
            bits = bytearray(row[0])
            if all(bits[p // 8] & (1 << (p % 8)) for p in positions):
                return False
            for position in positions:
                bits[position // 8] |= 1 << (position % 8)
            db.execute(
                "UPDATE replay_bloom SET bits=? WHERE bucket=?", (bytes(bits), bucket)
            )
            try:
                db.execute(
                    "INSERT INTO sandbox_requests VALUES (?, ?, 'ACCEPTED')",
                    (request_id, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            db.execute(
                "DELETE FROM sandbox_requests WHERE status != 'ACCEPTED' AND request_id IN (SELECT request_id FROM sandbox_requests WHERE status != 'ACCEPTED' ORDER BY admitted DESC LIMIT -1 OFFSET ?)",
                (self.retention,),
            )
            return True

    def terminal(self, request_id: str, status: str) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE sandbox_requests SET status=? WHERE request_id=?",
                (status, request_id),
            )
            db.execute(
                "DELETE FROM sandbox_requests WHERE status != 'ACCEPTED' AND request_id IN (SELECT request_id FROM sandbox_requests WHERE status != 'ACCEPTED' ORDER BY admitted DESC LIMIT -1 OFFSET ?)",
                (self.retention,),
            )

    def status(self, request_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM sandbox_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            return row[0] if row else None
