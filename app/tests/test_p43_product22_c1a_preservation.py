"""P43-PRODUCT-22-C1A: ai-agent preservation tool and Host bootstrap source-integrity gate."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import pytest

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / "scripts" / "maintenance"
HOST_SCRIPT = MAINT / "p43_sandbox_bridge_host.sh"
_spec = importlib.util.spec_from_file_location(
    "p43_ai_agent_preserve", MAINT / "p43_ai_agent_preserve.py"
)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

root_only = pytest.mark.skipif(os.geteuid() != 0, reason="needs root to set owners")

# Shape of the real ai-agent /proc/self/mountinfo (2026-11-21), trimmed.
MOUNTINFO = """\
1 0 0:1 / / rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y/diff
2 1 0:2 / /proc rw - proc proc rw
3 1 0:3 / /dev/shm rw - tmpfs shm rw
4 1 8:2 /home/u/persist/config /root/.openclaw rw - ext4 /dev/nvme0n1p2 rw
5 4 8:2 /home/u/persist/workspace /root/.openclaw/workspace rw - ext4 /dev/nvme0n1p2 rw
6 1 8:2 /var/lib/docker/containers/abc/hosts /etc/hosts rw - ext4 /dev/nvme0n1p2 rw
7 1 8:2 /var/lib/docker/volumes/v1/_data /data rw - ext4 /dev/nvme0n1p2 rw
8 1 8:2 /home/u/with\\040space /mnt/sp rw - ext4 /dev/nvme0n1p2 rw
"""


def test_mount_inventory_interpretation():
    m = pp.parse_mountinfo(MOUNTINFO)
    assert pp.classify("/root/.claude", m) == "CONTAINER_LAYER"
    assert pp.classify("/var/lib/redis/dump.rdb", m) == "CONTAINER_LAYER"
    assert pp.classify("/root/.openclaw/backups/x", m) == "PERSISTENT_BIND"
    assert pp.classify("/root/.openclaw/workspace/vault", m) == "PERSISTENT_BIND"
    assert (
        pp.classify("/root/.openclawx", m) == "CONTAINER_LAYER"
    )  # no prefix confusion
    assert pp.classify("/data/a", m) == "PERSISTENT_VOLUME"
    assert pp.classify("/dev/shm/a", m) == "EPHEMERAL"
    assert pp.classify("/etc/hosts", m) == "EPHEMERAL"
    assert pp.classify("/mnt/sp/f", m) == "PERSISTENT_BIND"
    assert pp.classify("/x", []) == "UNKNOWN"


def _persistent(tmp_path):
    return [
        {"root": "/", "point": "/", "fstype": "overlay"},
        {
            "root": "/home/u/persist",
            "point": str(tmp_path / "persist"),
            "fstype": "ext4",
        },
    ]


def _tree(tmp_path) -> Path:
    src = tmp_path / "src"
    for d in (
        "root/.claude/projects/p",
        "root/.codex/sessions",
        "root/.codex/packages/big",
        "root/.npm/_cacache",
        "root/p43-product-07-logs-a",
        "var/lib/redis",
    ):
        (src / d).mkdir(parents=True)
    (src / "root/.claude/projects/p/m.md").write_text("memory\n")
    (src / "root/.claude.json").write_text("{}\n")
    os.chmod(src / "root/.claude.json", 0o600)
    (src / "root/.codex/auth.json").write_text("not-a-real-secret\n")
    os.chmod(src / "root/.codex/auth.json", 0o600)
    (src / "root/.codex/sessions/s.jsonl").write_bytes(os.urandom(3000))
    (src / "root/.codex/packages/big/bin").write_text("reconstructable\n")
    (src / "root/.npm/_cacache/x").write_text("cache\n")
    (src / "root/p43-product-07-logs-a/log.txt").write_text("evidence\n")
    os.chmod(src / "root/p43-product-07-logs-a", 0o777)
    (src / "var/lib/redis/dump.rdb").write_bytes(b"REDIS0011")
    outside = tmp_path / "outside-secret"
    outside.write_text("OUTSIDE-CONTENT-MUST-NOT-BE-ARCHIVED\n")
    (src / "root/.claude/ext").symlink_to(outside)
    (src / "root/.claude/rel").symlink_to("projects/p/m.md")
    (src / "root/.claude/dirlink").symlink_to(tmp_path)  # never descended
    (tmp_path / "persist").mkdir()
    return src


def _backup(tmp_path, src, **kw):
    kw.setdefault("redis_info", lambda: None)
    kw.setdefault("tools", lambda: set())
    return pp.backup(
        str(tmp_path / "persist/b"),
        source_root=str(src),
        mounts=_persistent(tmp_path),
        **kw,
    )


def test_backup_path_selection(tmp_path):
    src = _tree(tmp_path)
    summary = _backup(tmp_path, src)
    manifest = json.loads((tmp_path / "persist/b/manifest.json").read_text())
    paths = {e["path"] for e in manifest["entries"]}
    assert "root/p43-product-07-logs-a/log.txt" in paths  # glob root
    assert "root/.codex/auth.json" in paths and "root/.codex/sessions/s.jsonl" in paths
    assert not any(p.startswith(("root/.codex/packages", "root/.npm")) for p in paths)
    assert (
        "root/.gnupg" in summary["missing"] and "root/.gnupg" not in manifest["roots"]
    )
    assert not any(p.startswith("root/.claude/dirlink/") for p in paths)
    assert (
        summary["external_symlinks"] == 2
    )  # absolute ext + dirlink, stored as links only
    rel = next(e for e in manifest["entries"] if e["path"] == "root/.claude/rel")
    assert rel == {
        **rel,
        "type": "symlink",
        "target": "projects/p/m.md",
        "external": False,
    }


def test_backup_refuses_unsafe_destinations(tmp_path):
    src = _tree(tmp_path)
    layer = [{"root": "/", "point": "/", "fstype": "overlay"}]
    with pytest.raises(pp.PreserveError, match="CONTAINER_LAYER"):
        pp.backup(
            str(tmp_path / "persist/b"),
            source_root=str(src),
            mounts=layer,
            redis_info=lambda: None,
            tools=lambda: set(),
        )
    (tmp_path / "persist/b").mkdir()
    with pytest.raises(FileExistsError):  # never overwrites an existing backup
        _backup(tmp_path, src)
    (tmp_path / "real").mkdir()
    (tmp_path / "persist/link").symlink_to(tmp_path / "real")
    with pytest.raises(pp.PreserveError, match="real directory"):
        pp.backup(
            str(tmp_path / "persist/link/b"),
            source_root=str(src),
            mounts=_persistent(tmp_path),
            redis_info=lambda: None,
            tools=lambda: set(),
        )


@pytest.mark.parametrize(
    "running,allow,error",
    [({"codex"}, True, "codex is running"), ({"claude"}, False, "claude is running")],
)
def test_backup_refuses_live_writers(tmp_path, running, allow, error):
    src = _tree(tmp_path)
    with pytest.raises(pp.PreserveError, match=error):
        _backup(tmp_path, src, tools=lambda: running, allow_live_claude=allow)
    assert not (tmp_path / "persist/b").exists()


def test_live_claude_allowed_only_as_marked_rehearsal(tmp_path):
    src = _tree(tmp_path)
    assert _backup(tmp_path, src, tools=lambda: {"claude"}, allow_live_claude=True)[
        "live_claude"
    ]


def test_live_tool_detection(tmp_path):
    for pid, argv in {
        "11": [b"claude"],
        "12": [b"node", b"/usr/bin/codex"],
        "13": [b"bash"],
    }.items():
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (tmp_path / "self").mkdir()
    assert pp.live_tools(str(tmp_path)) == {"claude", "codex"}


@pytest.mark.parametrize(
    "info,ok",
    [
        (None, True),
        (
            "# Persistence\r\nrdb_changes_since_last_save:0\r\nrdb_bgsave_in_progress:0\r\naof_enabled:0\r\n",
            True,
        ),
        ("rdb_changes_since_last_save:3\nrdb_bgsave_in_progress:0\n", False),
        ("rdb_changes_since_last_save:0\nrdb_bgsave_in_progress:1\n", False),
        ("rdb_changes_since_last_save:0\naof_enabled:1\n", False),
        ("loading:0\n", False),
    ],
)
def test_redis_consistency_preconditions(info, ok):
    assert pp.redis_consistency(info)[0] is ok


def test_backup_refuses_inconsistent_redis(tmp_path):
    src = _tree(tmp_path)
    with pytest.raises(pp.PreserveError, match="writes since last save"):
        _backup(tmp_path, src, redis_info=lambda: "rdb_changes_since_last_save:5\n")


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        out[str(p)] = (
            st.st_mode,
            st.st_uid,
            st.st_mtime_ns,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None,
        )
    return out


@root_only
def test_roundtrip_rehearsal_preserves_metadata_and_is_private(tmp_path):
    src = _tree(tmp_path)
    os.chown(src / "var/lib/redis/dump.rdb", 1234, 2345)
    os.chmod(src / "var/lib/redis/dump.rdb", 0o660)
    os.lchown(src / "root/.claude/rel", 1234, 1234)
    before = _snapshot(src)
    _backup(tmp_path, src)
    dest = tmp_path / "persist/b"
    assert {p.name: oct(p.stat().st_mode & 0o777) for p in dest.iterdir()} == {
        "preserve.tar": "0o600",
        "manifest.json": "0o600",
        "SHA256SUMS": "0o600",
    }
    assert oct(dest.stat().st_mode & 0o777) == "0o700"
    assert b"OUTSIDE-CONTENT" not in (dest / "preserve.tar").read_bytes()
    assert pp.verify(str(dest))["files"] == 6
    result = pp.rehearse(str(dest))
    assert result["rehearsal"] == "PASS" and result["mismatches"] == 0
    assert result["scratch_mode"] == "0o700"
    assert not list(dest.glob("rehearsal-*"))  # scratch removed
    assert _snapshot(src) == before  # source untouched
    assert json.loads((dest / "rehearsal.json").read_text())["first_mismatches"] == []
    assert oct((dest / "rehearsal.json").stat().st_mode & 0o777) == "0o600"


def test_compare_detects_metadata_and_content_drift(tmp_path):
    src = _tree(tmp_path)
    _backup(tmp_path, src)
    manifest = json.loads((tmp_path / "persist/b/manifest.json").read_text())
    assert pp.compare(str(src), manifest) == []
    os.chmod(src / "root/.claude.json", 0o644)
    (src / "root/.codex/auth.json").write_text("not-a-real-secreT\n")
    (src / "root/.claude/rel").unlink()
    (src / "root/.claude/rel").symlink_to("elsewhere")
    os.remove(src / "var/lib/redis/dump.rdb")
    assert sorted(pp.compare(str(src), manifest)) == [
        "metadata root/.claude.json",
        "metadata root/.claude/rel",
        "metadata root/.codex/auth.json",
        "missing var/lib/redis/dump.rdb",
    ]


def test_check_reports_missing_state_read_only(tmp_path):
    src = _tree(tmp_path)
    _backup(tmp_path, src)
    dest = str(tmp_path / "persist/b")
    assert pp.check(dest, target_root=str(src))["check"] == "PASS"
    os.remove(src / "root/.codex/auth.json")
    before = _snapshot(src)
    result = pp.check(dest, target_root=str(src))
    assert result["check"] == "FAIL"
    assert result["first_mismatches"] == ["missing root/.codex/auth.json"]
    assert _snapshot(src) == before


@pytest.mark.parametrize("target", ["preserve.tar", "manifest.json"])
def test_tampered_backup_fails_integrity(tmp_path, target):
    src = _tree(tmp_path)
    _backup(tmp_path, src)
    p = tmp_path / "persist/b" / target
    data = bytearray(p.read_bytes())
    data[len(data) // 3] ^= 1
    p.write_bytes(bytes(data))
    with pytest.raises(pp.PreserveError, match=f"digest mismatch: {re.escape(target)}"):
        pp.verify(str(tmp_path / "persist/b"))


def _evil_backup(dest: Path, members: list[tuple[str, str, str]], roots: list[str]):
    dest.mkdir(mode=0o700)
    with tarfile.open(dest / "preserve.tar", "w") as tar:
        for name, kind, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
                tar.addfile(ti)
            elif kind == "dev":
                ti.type = tarfile.CHRTYPE
                tar.addfile(ti)
            else:
                ti.size = len(payload)
                tar.addfile(ti, io.BytesIO(payload.encode()))
    entries = [{"path": n, "type": "file"} for n, _, _ in members]
    (dest / "manifest.json").write_text(
        json.dumps(
            {"roots": roots, "missing": [], "live_claude": False, "entries": entries}
        )
    )
    with open(dest / "SHA256SUMS", "w") as f:
        for n in ("preserve.tar", "manifest.json"):
            f.write(f"{pp._sha256(str(dest / n))}  {n}\n")


@pytest.mark.parametrize(
    "members,error",
    [
        ([("root/.claude/../../escape", "file", "x")], "unsafe member path"),
        ([("/root/.claude/abs", "file", "x")], "unsafe member path"),
        (
            [("etc/cron.d/x", "file", "x")],
            "unsafe member path",
        ),  # outside approved roots
        (
            [("root/.claude/l", "sym", "/etc"), ("root/.claude/l/passwd", "file", "x")],
            "member below a symlink",
        ),
        ([("root/.claude/dev", "dev", "")], "unsupported member type"),
    ],
)
def test_rehearsal_refuses_unsafe_archive_without_writing(tmp_path, members, error):
    dest = tmp_path / "b"
    _evil_backup(dest, members, ["root/.claude"])
    with pytest.raises(pp.PreserveError, match=error):
        pp.rehearse(str(dest))
    assert sorted(p.name for p in dest.iterdir()) == [
        "SHA256SUMS",
        "manifest.json",
        "preserve.tar",
    ]
    assert not (tmp_path / "escape").exists()


def test_restore_requires_approval():
    out = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            str(MAINT / "p43_ai_agent_preserve.py"),
            "restore",
            "/nonexistent",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == (64 if os.geteuid() == 0 else 1)


@root_only
def test_restore_boundaries(tmp_path):
    src = _tree(tmp_path)
    _backup(tmp_path, src)
    dest = str(tmp_path / "persist/b")
    target = tmp_path / "new"
    (target / "root/.claude").mkdir(parents=True)
    with pytest.raises(
        pp.PreserveError, match="refusing to overwrite existing root/.claude"
    ):
        pp.restore(dest, target_root=str(target), redis_info=lambda: None)
    shutil.rmtree(target / "root/.claude")
    (target / "var/lib").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (target / "var/lib/redis").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(pp.PreserveError, match="traverses a symlink"):
        pp.restore(dest, target_root=str(target), redis_info=lambda: None)
    (target / "var/lib/redis").unlink()
    (target / "var/lib/redis").mkdir()
    with pytest.raises(pp.PreserveError, match="redis is running"):
        pp.restore(
            dest,
            target_root=str(target),
            redis_info=lambda: "rdb_changes_since_last_save:0",
        )
    assert not (target / "var/lib/redis/dump.rdb").exists()
    assert (
        pp.restore(dest, target_root=str(target), redis_info=lambda: None)["restore"]
        == "PASS"
    )


# --- Host bootstrap source integrity --------------------------------------------------------

BUNDLE = [
    "scripts/maintenance/p43_sandbox_bridge_host.sh",
    "scripts/maintenance/p43-sandbox-bridge.service",
    *(
        f"app/services/sandbox_bridge/{m}"
        for m in (
            "__init__.py",
            "contract.py",
            "docker_executor.py",
            "host_service.py",
            "ipc_contract.py",
            "ledger.py",
            "staging.py",
            "transport.py",
        )
    ),
]


def _trusted_copy(tmp_path) -> Path:
    copy = tmp_path / "trusted"
    for rel in BUNDLE:
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, copy / rel)
        os.chmod(copy / rel, 0o755 if rel.endswith(".sh") else 0o644)
    for d in [
        copy,
        *{(copy / r).parent for r in BUNDLE},
        (copy / "app"),
        (copy / "app/services"),
    ]:
        os.chmod(d, 0o755)
    return copy


def _source_check(tmp_path, copy: Path, docker_body: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text("#!/bin/sh\n" + docker_body + "\n")
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", str(copy / BUNDLE[0]), "source-check"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )


@root_only
def test_source_check_passes_only_for_root_only_copy_outside_ai_agent_mounts(tmp_path):
    os.chmod(tmp_path, 0o700)
    copy = _trusted_copy(tmp_path)
    out = _source_check(
        tmp_path, copy, "printf '/home/u/persist/workspace\\n/home/u/persist/config\\n'"
    )
    assert (
        out.returncode == 0 and "SOURCE_CHECK=PASS failures=0" in out.stdout
    ), out.stdout

    covering = _source_check(tmp_path, copy, f"printf '{tmp_path}\\n'")
    assert (
        covering.returncode == 1
        and f"inside ai-agent mount {tmp_path}" in covering.stdout
    )

    unavailable = _source_check(tmp_path, copy, "exit 1")
    assert unavailable.returncode == 1 and "cannot prove" in unavailable.stdout


@root_only
@pytest.mark.parametrize(
    "tamper", ["group_writable_file", "world_writable_dir", "symlink", "non_root_owner"]
)
def test_source_check_rejects_mutable_sources(tmp_path, tamper):
    os.chmod(tmp_path, 0o700)
    copy = _trusted_copy(tmp_path)
    module = copy / "app/services/sandbox_bridge/transport.py"
    if tamper == "group_writable_file":
        os.chmod(module, 0o664)
    elif tamper == "world_writable_dir":
        os.chmod(module.parent, 0o777)
    elif tamper == "symlink":
        module.unlink()
        module.symlink_to(REPO / "app/services/sandbox_bridge/transport.py")
    else:
        os.chown(module, 1000, 0)
    out = _source_check(tmp_path, copy, "printf '/home/u/persist/workspace\\n'")
    assert out.returncode == 1 and "SOURCE_CHECK=FAIL" in out.stdout, out.stdout


def test_source_check_gates_preflight_and_install():
    text = HOST_SCRIPT.read_text()
    preflight = re.search(r"^preflight\(\) \{\n(.*?)^\}", text, re.S | re.M).group(1)
    install = re.search(r"^install_host\(\) \{\n(.*?)^\}", text, re.S | re.M).group(1)
    assert preflight.lstrip().startswith("source_check\n")
    lines = [ln.strip() for ln in install.splitlines()]
    assert (
        lines.index('need_approval "${1:-}"')
        < lines.index("preflight")
        < next(
            i
            for i, ln in enumerate(lines)
            if ln.startswith("useradd") or "useradd" in ln
        )
    )


def test_mutable_workspace_fails_source_check_closed():
    # Run from the real (ai-agent-writable) checkout: must never report PASS.
    out = subprocess.run(
        ["bash", str(HOST_SCRIPT), "source-check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 1 and "SOURCE_CHECK=FAIL" in out.stdout
