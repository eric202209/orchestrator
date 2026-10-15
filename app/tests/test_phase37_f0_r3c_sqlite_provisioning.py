"""F0-R3C: SQLite state provisioning under CHOWN/SETUID/SETGID only.

The disposable container runs as root with only CAP_CHOWN, CAP_SETUID and
CAP_SETGID. Without CAP_FOWNER root cannot chmod a file it no longer owns, and
without CAP_FSETID chmod silently drops S_ISGID unless root is in the file's
group. These tests reproduce both with ``setpriv`` (no Docker) and run the
real ``provision_state`` from f0r1.sh against temporary paths.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

F0R1_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verification/f0r1.sh"
API_UID, WORKER_UID, SHARED_GID, REDIS_UID = 61001, 61002, 61003, 61004
REDUCED_CAPS = [
    "setpriv",
    "--inh-caps=-all",
    "--bounding-set=-all,+chown,+setuid,+setgid",
    "--",
]

needs_root_setpriv = pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("setpriv") is None,
    reason="requires root and setpriv to drop to CHOWN/SETUID/SETGID",
)


def _reduced_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*REDUCED_CAPS, "bash", "-c", "umask 0022\n" + script],
        capture_output=True,
        text=True,
        check=False,
    )


def _owner_mode(path: Path) -> str:
    st = path.stat()
    return f"{st.st_uid}:{st.st_gid} {st.st_mode & 0o7777:o}"


def _harness(tmp_path: Path, body: str) -> str:
    """f0r1.sh with container paths redirected to tmp_path and no main call."""

    text = F0R1_SCRIPT.read_text(encoding="utf-8")
    for name, value in {
        "REL_A": tmp_path / "release",
        "STATE": tmp_path / "state",
        "EVIDENCE": tmp_path / "evidence",
    }.items():
        text, count = re.subn(
            rf"^readonly {name}=\S+", f"readonly {name}={value}", text, flags=re.M
        )
        assert count == 1, name
    text, count = re.subn(r'^main "\$@"\n\Z', "", text, flags=re.M)
    assert count == 1
    return text + f"\nRESULTS_FILE={tmp_path / 'evidence/results.tsv'}\n" + body


def _fake_release(tmp_path: Path) -> None:
    (tmp_path / "release/app").mkdir(parents=True)
    (tmp_path / "release/app/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "release/venv/bin").mkdir(parents=True)
    (tmp_path / "release/venv/lib").mkdir()
    (tmp_path / "release/venv/bin/python").symlink_to(shutil.which("python3"))
    (tmp_path / "state").mkdir()
    (tmp_path / "evidence").mkdir()


@needs_root_setpriv
def test_reduced_caps_drop_fowner_and_fsetid():
    out = _reduced_bash("grep -E '^CapEff' /proc/self/status")
    assert out.stdout.split() == ["CapEff:", "00000000000000c1"]


@needs_root_setpriv
def test_previous_db_install_fails_like_live_run(tmp_path):
    """The pre-R3C line 725: install chowns to the API UID, then chmod EPERMs."""

    db = tmp_path / "orchestrator.db"
    out = _reduced_bash(f"install -m 0660 -o {API_UID} -g {SHARED_GID} /dev/null {db}")
    assert out.returncode == 1
    assert (
        f"install: cannot change permissions of '{db}': Operation not permitted"
        in out.stderr
    )
    # chown succeeded, chmod did not: the file is left given away and 0600.
    assert _owner_mode(db) == f"{API_UID}:{SHARED_GID} 600"


@needs_root_setpriv
def test_previous_shared_dir_install_silently_drops_setgid(tmp_path):
    """The pre-R3C 2770 dirs: exit 0, but S_ISGID is stripped (latent R4-AUX)."""

    target = tmp_path / "db"
    out = _reduced_bash(f"install -d -m 2770 -o 0 -g {SHARED_GID} {target}")
    assert out.returncode == 0, out.stderr
    assert _owner_mode(target) == f"0:{SHARED_GID} 770"


@needs_root_setpriv
def test_provision_state_succeeds_with_reduced_caps(tmp_path):
    _fake_release(tmp_path)
    out = _reduced_bash(_harness(tmp_path, "provision_state\necho F0R3C_DONE\n"))
    assert out.returncode == 0, out.stderr
    assert "F0R3C_DONE" in out.stdout

    state = tmp_path / "state"
    for name in (
        "db",
        "runtime",
        "checkpoints",
        "candidate-content",
        "projects",
        "beat",
    ):
        assert _owner_mode(state / name) == f"0:{SHARED_GID} 2770", name
    assert _owner_mode(state / "db/orchestrator.db") == f"{API_UID}:{SHARED_GID} 660"
    assert _owner_mode(state / "redis") == f"{REDIS_UID}:{REDIS_UID} 700"
    assert _owner_mode(state / "home-api") == f"{API_UID}:{API_UID} 700"
    assert _owner_mode(state / "home-worker") == f"{WORKER_UID}:{WORKER_UID} 700"
    rows = (tmp_path / "evidence/results.tsv").read_text(encoding="utf-8")
    assert "R4-PROVISION\tPASS\t" in rows
    assert "\tFAIL\t" not in rows


@needs_root_setpriv
def test_setgid_db_dir_gives_new_files_the_shared_group():
    """Recipe R: WAL/SHM created by any UID inherit the shared GID."""

    # pytest's tmp root is 0700; rehearsal UIDs need a traversable parent.
    tmp_path = Path(tempfile.mkdtemp(prefix="f0r3c-"))
    tmp_path.chmod(0o755)
    try:
        _assert_wal_inherits_shared_group(tmp_path)
    finally:
        shutil.rmtree(tmp_path)


def _assert_wal_inherits_shared_group(tmp_path: Path) -> None:
    _fake_release(tmp_path)
    out = _reduced_bash(_harness(tmp_path, "provision_state\n"))
    assert out.returncode == 0, out.stderr
    wal = tmp_path / "state/db/orchestrator.db-wal"
    out = _reduced_bash(
        f"setpriv --reuid={WORKER_UID} --regid={WORKER_UID} --groups={SHARED_GID} "
        f"-- sh -c ': >\"$1\"' _ {wal}"
    )
    assert out.returncode == 0, out.stderr
    assert wal.stat().st_gid == SHARED_GID


def test_verify_provisioned_fails_closed_on_mismatch(tmp_path):
    (tmp_path / "evidence").mkdir()
    target = tmp_path / "d"
    target.mkdir(mode=0o700)
    target.chmod(0o770)
    st = target.stat()
    body = (
        f"printf '{st.st_uid}:{st.st_gid} 2770 %s\\n' {target} | verify_provisioned\n"
        "echo rc=$?\n"
    )
    out = subprocess.run(
        ["bash", "-c", _harness(tmp_path, "set +e\n" + body)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=1" in out.stdout
    rows = (tmp_path / "evidence/results.tsv").read_text(encoding="utf-8")
    assert f"R4-PROVISION\tFAIL\t{target}={st.st_uid}:{st.st_gid} 770 want" in rows


def test_r2_capability_allowlist_unchanged():
    text = F0R1_SCRIPT.read_text(encoding="utf-8")
    assert "readonly ALLOWED_CAP_MASK=$((0xc1))" in text
    assert (
        "--cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \\\\" in text
    )
    for forbidden in ("--privileged", "seccomp=unconfined", "apparmor=unconfined"):
        assert forbidden not in text
    for cap in ("FOWNER", "FSETID", "DAC_OVERRIDE"):
        assert f"--cap-add {cap}" not in text


def test_provision_never_chmods_after_giving_a_file_away():
    body = F0R1_SCRIPT.read_text(encoding="utf-8").split("provision_state() {", 1)[1]
    body = body.split("\n}\n", 1)[0]
    # Regular files: install may only target root ownership; chown comes last.
    for line in body.splitlines():
        if re.search(r"\binstall\b(?!.*\s-d\b).*\s-o\s", line):
            assert "-o 0 -g 0" in line, line
