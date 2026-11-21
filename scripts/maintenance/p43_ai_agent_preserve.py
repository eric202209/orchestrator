#!/usr/bin/env python3
"""P43-PRODUCT-22-C1A: bounded ai-agent container-layer preservation. Run inside ai-agent as root.

  inventory                                   classify paths by mount backing; sizes only, never contents
  backup DEST [--allow-live-claude]           archive MUST_PRESERVE into a new private DEST on a persistent mount
  verify DEST                                 archive/manifest digests and member safety
  rehearse DEST                               extract into a private scratch dir under DEST, compare, remove scratch
  check DEST                                  read-only: compare the live container against the manifest
  restore DEST --operator-approved [--root R] extract into a recreated container, then compare

Secondary to the primary `docker commit` preservation (see the PRODUCT-22 C1A addendum).
Never prints file contents. Never follows symlinks. Stdlib only; run with /usr/bin/python3 -I.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

# A. Must preserve: irreplaceable state that lives only in the container writable layer.
MUST_PRESERVE = (
    "root/.claude",
    "root/.claude.json",
    "root/.codex",
    "root/.config",
    "root/.gnupg",
    "root/.gitconfig",
    "root/.bash_history",
    "root/.orchestrator",
    "root/.local/share/pki",
    "root/p43-product-07-*",
    "var/lib/redis/dump.rdb",
)
# Reconstructable subtrees inside MUST_PRESERVE roots (re-downloaded by Codex on demand).
EXCLUDE = (
    "root/.codex/packages",
    "root/.codex/cache",
    "root/.codex/tmp",
    "root/.codex/.tmp",
    "root/.codex/log",
)
# B/C. Inventory only: reconstructable or ephemeral, never archived by this tool.
RECONSTRUCTABLE = (
    "root/.npm",
    "root/.cache",
    "root/.local/share/Trash",
    "usr/lib/node_modules",
    "usr/local/bin",
)
EPHEMERAL_FS = {
    "tmpfs",
    "proc",
    "sysfs",
    "devpts",
    "mqueue",
    "cgroup",
    "cgroup2",
    "shm",
}
ARCHIVE, MANIFEST, SUMS = "preserve.tar", "manifest.json", "SHA256SUMS"
CHUNK = 1 << 20


class PreserveError(Exception):
    pass


# --- mount interpretation -------------------------------------------------------------------


def _unescape(field: str) -> str:
    return (
        field.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def parse_mountinfo(text: str) -> list[dict]:
    mounts = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        pre, post = line.split(" - ", 1)
        f = pre.split()
        mounts.append(
            {
                "root": _unescape(f[3]),
                "point": _unescape(f[4]),
                "fstype": post.split()[0],
            }
        )
    return mounts


def classify(path: str, mounts: list[dict]) -> str:
    """Backing of an absolute container path, from the deepest covering mount."""
    path = os.path.normpath(path)
    best = None
    for m in mounts:
        p = m["point"]
        if path == p or path.startswith(p.rstrip("/") + "/"):
            if best is None or len(p) >= len(best["point"]):
                best = m
    if best is None:
        return "UNKNOWN"
    if best["fstype"] in EPHEMERAL_FS or best["root"].startswith(
        "/var/lib/docker/containers/"
    ):
        return "EPHEMERAL"  # tmpfs, or Docker-managed per-container files (hosts, resolv.conf)
    if best["fstype"] == "overlay":
        return "CONTAINER_LAYER" if best["point"] == "/" else "UNKNOWN"
    if best["root"].startswith("/var/lib/docker/volumes/"):
        return "PERSISTENT_VOLUME"
    return "PERSISTENT_BIND"


def _mounts() -> list[dict]:
    with open("/proc/self/mountinfo") as f:
        return parse_mountinfo(f.read())


# --- consistency preconditions --------------------------------------------------------------


def redis_consistency(info: str | None) -> tuple[bool, str]:
    """dump.rdb is written via temp file + rename, so a copy is a complete snapshot as of
    the last save. It is current only when no writes happened since and no BGSAVE is running.
    """
    if info is None:
        return True, "redis not reachable: dump.rdb quiescent"
    kv = dict(
        line.split(":", 1)
        for line in info.splitlines()
        if ":" in line and not line.startswith("#")
    )
    kv = {k.strip(): v.strip() for k, v in kv.items()}
    if kv.get("rdb_bgsave_in_progress", "0") != "0":
        return False, "BGSAVE in progress"
    if kv.get("aof_enabled", "0") != "0":
        return False, "AOF enabled: archive appendonly files too (not supported here)"
    changes = kv.get("rdb_changes_since_last_save")
    if changes is None:
        return False, "rdb_changes_since_last_save unavailable"
    if changes != "0":
        return (
            False,
            f"{changes} writes since last save: run `redis-cli BGSAVE` (operator) and retry",
        )
    return True, "rdb_changes_since_last_save:0"


def _redis_info() -> str | None:
    try:
        out = subprocess.run(
            ["redis-cli", "INFO", "persistence"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 and "rdb_" in out.stdout else None


def live_tools(proc_root: str = "/proc") -> set[str]:
    """Names of running tools whose state is being archived (SQLite/JSONL writers)."""
    found = set()
    for pid in os.listdir(proc_root):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        head = argv[:2]  # direct binary, or `node <script>`
        names = {os.path.basename(a) for a in head}
        joined = " ".join(head)
        if "claude" in names or "@anthropic-ai/claude-code" in joined:
            found.add("claude")
        if "codex" in names or "@openai/codex" in joined:
            found.add("codex")
    return found


# --- path selection -------------------------------------------------------------------------


def select_roots(source_root: str) -> tuple[list[str], list[str]]:
    present, missing = [], []
    for pattern in MUST_PRESERVE:
        parent, leaf = os.path.split(pattern)
        if any(c in leaf for c in "*?["):
            base = os.path.join(source_root, parent)
            names = (
                sorted(fnmatch.filter(os.listdir(base), leaf))
                if os.path.isdir(base)
                else []
            )
            present += [os.path.join(parent, n) for n in names]
        elif os.path.lexists(os.path.join(source_root, pattern)):
            present.append(pattern)
        else:
            missing.append(pattern)
    return present, missing


def _excluded(rel: str) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in EXCLUDE)


def _walk(source_root: str, rel: str):
    """Yield relative paths depth-first without following symlinks or crossing excluded trees."""
    if _excluded(rel):
        return
    yield rel
    full = os.path.join(source_root, rel)
    if stat.S_ISDIR(os.lstat(full).st_mode):
        for entry in sorted(os.scandir(full), key=lambda e: e.name):
            yield from _walk(source_root, os.path.join(rel, entry.name))


def _under(rel: str, roots: list[str]) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r in roots)


# --- archive --------------------------------------------------------------------------------


class _Hashing:
    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _private(path, flags):
    return os.open(path, flags, 0o600)


def _private_new_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.islink(parent) or not os.path.isdir(parent):
        raise PreserveError(f"destination parent must be a real directory: {parent}")
    os.mkdir(path, 0o700)  # fails if it already exists: never overwrite a backup
    os.chmod(path, 0o700)


def backup(
    dest: str,
    *,
    source_root: str = "/",
    mounts: list[dict] | None = None,
    redis_info=_redis_info,
    tools=live_tools,
    allow_live_claude: bool = False,
) -> dict:
    mounts = _mounts() if mounts is None else mounts
    backing = classify(os.path.abspath(dest), mounts)
    if backing not in ("PERSISTENT_BIND", "PERSISTENT_VOLUME"):
        raise PreserveError(
            f"destination is {backing}: it would not survive recreation"
        )
    running = tools()
    if "codex" in running:
        raise PreserveError(
            "codex is running: exit Codex so its SQLite state is quiescent"
        )
    if "claude" in running and not allow_live_claude:
        raise PreserveError(
            "claude is running: exit Claude Code (or --allow-live-claude for a rehearsal)"
        )
    roots, missing = select_roots(source_root)
    if "var/lib/redis/dump.rdb" in roots:
        ok, why = redis_consistency(redis_info())
        if not ok:
            raise PreserveError(f"redis snapshot not consistent: {why}")
    _private_new_dir(dest)
    entries, skipped, external = [], 0, 0
    old_umask = os.umask(0o077)
    try:
        with tarfile.open(
            os.path.join(dest, ARCHIVE), "w", format=tarfile.PAX_FORMAT
        ) as tar:
            for root in roots:
                for rel in _walk(source_root, root):
                    full = os.path.join(source_root, rel)
                    st = os.lstat(full)
                    e = {
                        "path": rel,
                        "mode": stat.S_IMODE(st.st_mode),
                        "uid": st.st_uid,
                        "gid": st.st_gid,
                    }
                    if stat.S_ISREG(st.st_mode):
                        with open(
                            full,
                            "rb",
                            opener=lambda p, fl: os.open(p, fl | os.O_NOFOLLOW),
                        ) as f:
                            tar.inodes = {}  # store hard links as independent files
                            ti = tar.gettarinfo(arcname=rel, fileobj=f)
                            hf = _Hashing(f)
                            tar.addfile(ti, hf)
                        e.update(
                            type="file",
                            size=ti.size,
                            mtime=int(ti.mtime),
                            sha256=hf.h.hexdigest(),
                        )
                    elif stat.S_ISDIR(st.st_mode):
                        tar.addfile(tar.gettarinfo(full, arcname=rel))
                        e.update(type="dir")
                    elif stat.S_ISLNK(st.st_mode):
                        target = os.readlink(full)
                        tar.addfile(tar.gettarinfo(full, arcname=rel))
                        resolved = os.path.normpath(
                            os.path.join(os.path.dirname(rel), target)
                        )
                        ext = os.path.isabs(target) or not _under(resolved, roots)
                        external += ext
                        e.update(type="symlink", target=target, external=ext)
                    else:
                        skipped += 1  # sockets, fifos, devices: runtime-only
                        continue
                    entries.append(e)
        manifest = {
            "schema": "p43-c1a-preserve-v1",
            "roots": roots,
            "missing": missing,
            "exclude": list(EXCLUDE),
            "live_claude": "claude" in running,
            "entries": entries,
        }
        with open(os.path.join(dest, MANIFEST), "w") as f:
            json.dump(manifest, f, sort_keys=True)
        with open(os.path.join(dest, SUMS), "w") as f:
            for name in (ARCHIVE, MANIFEST):
                f.write(f"{_sha256(os.path.join(dest, name))}  {name}\n")
    finally:
        os.umask(old_umask)
    return _summary(manifest, skipped=skipped, external_symlinks=external)


def _summary(manifest: dict, **extra) -> dict:
    files = [e for e in manifest["entries"] if e["type"] == "file"]
    return {
        "roots": manifest["roots"],
        "missing": manifest["missing"],
        "entries": len(manifest["entries"]),
        "files": len(files),
        "bytes": sum(e["size"] for e in files),
        "live_claude": manifest["live_claude"],
        **extra,
    }


def _load(dest: str) -> dict:
    with open(os.path.join(dest, SUMS)) as f:
        sums = dict(
            reversed(line.rstrip("\n").split("  ", 1)) for line in f if line.strip()
        )
    if set(sums) != {ARCHIVE, MANIFEST}:
        raise PreserveError("SHA256SUMS incomplete")
    for name, digest in sums.items():
        if _sha256(os.path.join(dest, name)) != digest:
            raise PreserveError(f"digest mismatch: {name}")
    with open(os.path.join(dest, MANIFEST)) as f:
        return json.load(f)


def _check_members(members: list[tarfile.TarInfo], manifest: dict) -> None:
    roots = manifest["roots"]
    links = set()
    for m in members:
        name = m.name
        parts = name.split("/")
        if (
            os.path.isabs(name)
            or ".." in parts
            or "" in parts
            or not _under(name, roots)
        ):
            raise PreserveError(f"unsafe member path: {name!r}")
        if not (m.isreg() or m.isdir() or m.issym()):
            raise PreserveError(f"unsupported member type: {name!r}")
        if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
            raise PreserveError(f"member below a symlink: {name!r}")
        if m.issym():
            links.add(name)
    if [m.name for m in members] != [e["path"] for e in manifest["entries"]]:
        raise PreserveError("archive members do not match manifest")


def verify(dest: str) -> dict:
    manifest = _load(dest)
    with tarfile.open(os.path.join(dest, ARCHIVE)) as tar:
        _check_members(tar.getmembers(), manifest)
    return _summary(manifest)


def compare(target_root: str, manifest: dict) -> list[str]:
    """Metadata and content mismatches between an extracted tree and the manifest."""
    bad = []
    for e in manifest["entries"]:
        full = os.path.join(target_root, e["path"])
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            bad.append(f"missing {e['path']}")
            continue
        kind = {stat.S_IFREG: "file", stat.S_IFDIR: "dir", stat.S_IFLNK: "symlink"}.get(
            stat.S_IFMT(st.st_mode)
        )
        got = {"type": kind, "uid": st.st_uid, "gid": st.st_gid}
        if kind != "symlink":
            got["mode"] = stat.S_IMODE(st.st_mode)
        if kind == "file":
            got.update(size=st.st_size, mtime=int(st.st_mtime), sha256=_sha256(full))
        if kind == "symlink":
            got["target"] = os.readlink(full)
        want = {k: e[k] for k in got if k in e}
        if {k: got[k] for k in want} != want:
            bad.append(f"metadata {e['path']}")
    return bad


def _extract(dest: str, target_root: str, manifest: dict) -> None:
    with tarfile.open(os.path.join(dest, ARCHIVE)) as tar:
        members = tar.getmembers()
        _check_members(members, manifest)
        # Members were validated above; keep exact modes/owners (tar_filter would clear bits).
        tar.extractall(
            target_root, members=members, numeric_owner=True, filter=lambda m, _p: m
        )


def rehearse(dest: str) -> dict:
    manifest = _load(dest)
    scratch = tempfile.mkdtemp(prefix="rehearsal-", dir=dest)  # 0700, private to root
    try:
        _extract(dest, scratch, manifest)
        bad = compare(scratch, manifest)
        mode = stat.S_IMODE(os.lstat(scratch).st_mode)
    finally:
        shutil.rmtree(scratch)
    result = {**_summary(manifest), "mismatches": len(bad), "scratch_mode": oct(mode)}
    result["rehearsal"] = "PASS" if not bad and mode == 0o700 else "FAIL"
    with open(os.path.join(dest, "rehearsal.json"), "w", opener=_private) as f:
        json.dump({**result, "first_mismatches": bad[:20]}, f, sort_keys=True)
    return result


def check(dest: str, *, target_root: str = "/") -> dict:
    """Read-only: compare a live (e.g. recreated) container against the backup manifest."""
    manifest = _load(dest)
    bad = compare(target_root, manifest)
    return {
        **_summary(manifest),
        "mismatches": len(bad),
        "first_mismatches": bad[:20],
        "check": "PASS" if not bad else "FAIL",
    }


def restore(dest: str, *, target_root: str = "/", redis_info=_redis_info) -> dict:
    manifest = _load(dest)
    for root in manifest["roots"]:
        if os.path.lexists(os.path.join(target_root, root)):
            raise PreserveError(
                f"refusing to overwrite existing {root}: move it aside first"
            )
        parent = os.path.join(target_root, os.path.dirname(root))
        if os.path.realpath(parent) != os.path.normpath(os.path.abspath(parent)):
            raise PreserveError(
                f"restore parent traverses a symlink: {os.path.dirname(root)}"
            )
    if "var/lib/redis/dump.rdb" in manifest["roots"] and redis_info() is not None:
        raise PreserveError(
            "redis is running: stop it first or it will overwrite dump.rdb on shutdown"
        )
    _extract(dest, target_root, manifest)
    bad = compare(target_root, manifest)
    return {
        **_summary(manifest),
        "mismatches": len(bad),
        "restore": "PASS" if not bad else "FAIL",
    }


def _du(path: str) -> int | None:
    out = subprocess.run(["du", "-sxb", path], capture_output=True, text=True)
    return int(out.stdout.split()[0]) if out.returncode == 0 and out.stdout else None


def inventory(source_root: str = "/") -> list[dict]:
    mounts = _mounts()
    rows = []
    roots, missing = select_roots(source_root)
    for cat, rels in (
        ("A_MUST_PRESERVE", roots),
        ("B_RECONSTRUCTABLE", RECONSTRUCTABLE),
    ):
        for rel in rels:
            full = os.path.join(source_root, rel)
            if os.path.lexists(full):
                rows.append(
                    {
                        "path": "/" + rel,
                        "category": cat,
                        "backing": classify(full, mounts),
                        "bytes": _du(full),
                    }
                )
    rows += [
        {"path": "/" + m, "category": "A_MUST_PRESERVE", "backing": "ABSENT"}
        for m in missing
    ]
    for m in mounts:
        if m["fstype"] not in EPHEMERAL_FS and m["point"] != "/":
            rows.append(
                {
                    "path": m["point"],
                    "category": "MOUNT",
                    "backing": classify(m["point"], mounts),
                }
            )
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    b = sub.add_parser("backup")
    b.add_argument("dest")
    b.add_argument("--allow-live-claude", action="store_true")
    for name in ("verify", "rehearse", "check"):
        sub.add_parser(name).add_argument("dest")
    r = sub.add_parser("restore")
    r.add_argument("dest")
    r.add_argument("--operator-approved", action="store_true")
    r.add_argument("--root", default="/")
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("must run as root inside ai-agent", file=sys.stderr)
        return 1
    try:
        if a.cmd == "inventory":
            out = inventory()
        elif a.cmd == "backup":
            out = backup(a.dest, allow_live_claude=a.allow_live_claude)
        elif a.cmd == "verify":
            out = verify(a.dest)
        elif a.cmd == "rehearse":
            out = rehearse(a.dest)
        elif a.cmd == "check":
            out = check(a.dest)
        else:
            if not a.operator_approved:
                print("refusing: requires --operator-approved", file=sys.stderr)
                return 64
            out = restore(a.dest, target_root=a.root)
    except (PreserveError, OSError, tarfile.TarError) as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1, sort_keys=True))
    failed = isinstance(out, dict) and "FAIL" in (
        out.get("rehearsal"),
        out.get("restore"),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
