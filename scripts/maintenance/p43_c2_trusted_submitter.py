"""P43-PRODUCT-22-C2-C3B trusted one-shot submitter for the disposable C2 slice.

Runs once, inside a disposable container built from the reviewed Worker image,
against the fresh Worker-owned SQLite database. It never starts a Worker, never
claims or completes a TaskExecution, never receives the C2 Bridge key/socket,
and never calls a model provider.

    python3 -m scripts.maintenance.p43_c2_trusted_submitter init
    python3 -m scripts.maintenance.p43_c2_trusted_submitter submit [--control C]
    python3 -m scripts.maintenance.p43_c2_trusted_submitter observe [--control C]

The plan is fixed in this module and is persisted through the existing
``TaskCreate.steps`` field. The Worker reuses a stored plan only after the
existing normalization and ``ValidatorService.validate_plan`` acceptance. No
plan is read from arguments, files, or the environment.

P43-PRODUCT-23B adds two trusted controls beside the PRODUCT-22 ``exit-check``:

* ``reference``: the frozen PRODUCT-18 U1 reference control. Its plan is
  derived from the frozen evaluation material mounted read-only at
  ``U1_MATERIAL_ROOT``, accepted only after every byte matches the frozen
  manifest whose sha256 is pinned here. No reference content lives in this
  module; the hidden suite is never mounted into this container.
* ``probe``: a fixed containment probe (Sandbox network, read-only snapshot,
  writable ``/tmp``, identity) followed by a step that must time out.

Each control has its own Project, workspace, Task, Session and TaskExecution
and may be submitted once per disposable database, only while no other
TaskExecution is in flight. The frozen naive control is not submitted: the
product rejects it at completion, which enters provider-dependent repair. It
is judged by the independent evaluation only (``verdict``), like PRODUCT-18.

P43-PRODUCT-23C adds the ``model`` control: the frozen U1 task text and
baseline project files only, submitted with no stored Plan through the normal
full-lifecycle path (Discovery and Planning admission, manual mode). Its
material mount holds exactly the pinned manifest, task and baseline
(``MODEL_PARTS``); reference, naive, hidden and oracle material never reach
this container, the Worker, or the provider.

Host-side, with only the standard library (``python3 -I <this file> ...``):

    verify-material ROOT      verify the staged evaluation material
    verdict --control C ...   integrity + oracle + hidden verdict for C
    contamination EVAL RELAY  scan recorded provider requests (supplementary)
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

FIXED_COMMAND = "python3 -c 'import sys; sys.exit(0)'"
FIXED_PLAN: tuple[dict[str, Any], ...] = (
    {
        "step_number": 1,
        "description": "Run the bounded C2 sandbox exit check.",
        "commands": [FIXED_COMMAND],
        "verification": None,
        "rollback": None,
        "expected_files": [],
    },
)
PROJECT_NAME = "p43-c2-c3b-disposable"
TASK_TITLE = "C2 sandbox exit check"
TASK_DESCRIPTION = f"Run the bounded C2 sandbox exit check: {FIXED_COMMAND}"
# The existing validation profile that admits a command-only step.
EXECUTION_PROFILE = "test_only"
SUBMITTER_EMAIL = "c2-c3b-submitter@disposable.invalid"
NODE_IDENTITY_BASE = "orchestrator-worker-c2-c1"
ORCHESTRATION_TIMEOUT_SECONDS = 300

# Bridge, Docker, and C1B material the submitter must never be able to see.
FORBIDDEN_PATHS = (
    "/run/p43-sandbox-bridge-c2/launcher.sock",
    "/run/secrets/p43-sandbox-bridge-c2.key",
    "/run/p43-sandbox-bridge/launcher.sock",
    "/run/secrets/p43-sandbox-bridge.key",
    "/var/run/docker.sock",
)
FORBIDDEN_ENV = (
    "SANDBOX_BRIDGE_SOCKET",
    "SANDBOX_BRIDGE_KEY_FILE",
    "HOST_BRIDGE_SOCKET",
    "HOST_BRIDGE_KEY_FILE",
    "DOCKER_HOST",
)
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

# --- P43-PRODUCT-23B trusted controls ---------------------------------------

EXIT_CHECK = "exit-check"
MODEL = "model"
CONTROLS = (EXIT_CHECK, "reference", "probe", MODEL)
CONTROL_PROJECTS = {
    "reference": "p43-c2-u1-reference",
    "probe": "p43-c2-probe",
    MODEL: "p43-c2-u1-model",
}
# Frozen PRODUCT-18 identities (freeze.json). Only hashes live here.
U1_MANIFEST_SHA256 = "26965b02fe87e35de753267d0b0ee5741c0e555e1e019180a913224543b91f86"
U2_MANIFEST_SHA256 = "314142190e698daf19b1b2f3e8d7e64629c9c7644953cb22442458569ee1bd3d"
ORACLE_SHA256 = "b28ea4b22e26942b3e8462db62c2f4d790a4dd293d00e5a228a1b3e09687ecdc"
HIDDEN_SUITE_SHA256 = "59fd0656331c9515e757949fd130961ccb1d1aed2b9d0a497f36d035da1d87df"
# Staged layout (the PRODUCT-18 evidence tree, relative to its parent).
U1_DIR = "p43-product-18/u1"
U2_DIR = "p43-product-18/u2"
ORACLE_PATH = "p43-product-02/oracle.py"
HIDDEN_SUITE_PATH = "p43-product-18/tools/hidden_suite.py"
# Read-only bind into the submitter container only (never the Worker).
U1_MATERIAL_ROOT = "/run/p43-u1-material"
U2_MATERIAL_ROOT = "/run/p43-u2-material"
SUBMIT_PARTS = ("baseline", "reference")
# The model control's mount: manifest, task and baseline only.
MODEL_PARTS = ("baseline",)
# P43-PRODUCT-23C: the evaluation window (the relay enforces the deadline).
MODEL_TIMEOUT_SECONDS = 1200
EVALUATION_PARTS = ("baseline", "reference", "naive", "hidden", "case", "tools")


@dataclass(frozen=True)
class FrozenCase:
    """The immutable identity and labels for one frozen evaluation case."""

    id: str
    manifest_sha256: str
    directory: str
    material_root: str
    reference_project: str
    model_project: str
    reference_title: str
    model_title: str
    reference_session: str
    model_session: str
    evaluation_controls: tuple[str, ...]


CASE_DESCRIPTORS = MappingProxyType(
    {
        "U1": FrozenCase(
            id="U1",
            manifest_sha256=U1_MANIFEST_SHA256,
            directory=U1_DIR,
            material_root=U1_MATERIAL_ROOT,
            reference_project="p43-c2-u1-reference",
            model_project="p43-c2-u1-model",
            reference_title="U1 trusted reference control",
            model_title="U1 rangekit task",
            reference_session="P43-PRODUCT-23B U1 reference control",
            model_session="P43-PRODUCT-23C U1 model evaluation",
            evaluation_controls=("reference", "naive"),
        ),
        "U2": FrozenCase(
            id="U2",
            manifest_sha256=U2_MANIFEST_SHA256,
            directory=U2_DIR,
            material_root=U2_MATERIAL_ROOT,
            reference_project="p43-c2-u2-reference",
            model_project="p43-c2-u2-model",
            reference_title="U2 trusted reference control",
            model_title="U2 ledgerlite task",
            reference_session="P43-PRODUCT-24A U2 reference control",
            model_session="P43-PRODUCT-24B U2 model evaluation",
            evaluation_controls=("reference", "naive-symptom", "naive-partial"),
        ),
    }
)
DEFAULT_CASE_ID = "U1"


def frozen_case(case_id: str = DEFAULT_CASE_ID) -> FrozenCase:
    """Return one explicitly supported case, refusing unknown selectors."""

    try:
        return CASE_DESCRIPTORS[case_id]
    except (KeyError, TypeError):
        raise SubmitterRefused(f"unknown_case:{case_id}") from None


def case_material_root(case_id: str = DEFAULT_CASE_ID) -> Path:
    case = frozen_case(case_id)
    # The U1 constant is retained as a test seam and as the compatibility path
    # used by the PRODUCT-23B/23C procedures. U2 has the same explicit seam.
    return Path(U1_MATERIAL_ROOT if case.id == "U1" else U2_MATERIAL_ROOT)


def case_info(case_id: str = DEFAULT_CASE_ID) -> dict[str, Any]:
    case = frozen_case(case_id)
    return {**asdict(case), "material_root": str(case_material_root(case.id))}


PROBE_TASK_DESCRIPTION = (
    "Run the fixed C2 containment probe: "
    "python -m pytest tests/test_c2_containment_probe.py -q, then "
    "python -m pytest tests/test_c2_timeout_probe.py -q (must time out)."
)
# Trusted probe sources, seeded into the probe Project workspace. They run
# only inside the Sandbox (uid 65534, cwd /ws/src, read-only bind, /tmp tmpfs).
PROBE_FILES = {
    "tests/test_c2_containment_probe.py": (
        '"""P43-PRODUCT-23B fixed Sandbox containment probe."""\n'
        "import os\n"
        "import socket\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "def test_network_is_denied():\n"
        '    assert sorted(os.listdir("/sys/class/net")) == ["lo"]\n'
        '    for address in (("1.1.1.1", 443), ("172.17.0.1", 80)):\n'
        "        with pytest.raises(OSError):\n"
        "            socket.create_connection(address, timeout=2).close()\n"
        "    with pytest.raises(OSError):\n"
        '        socket.getaddrinfo("model-provider", 11434)\n'
        "\n"
        "\n"
        "def test_snapshot_and_root_are_read_only():\n"
        '    assert os.getcwd() == "/ws/src"\n'
        "    for path in (\n"
        '        "probe-write.txt",\n'
        '        "tests/probe-write.txt",\n'
        '        "/ws/probe-write.txt",\n'
        '        "/probe-write.txt",\n'
        "    ):\n"
        "        with pytest.raises(OSError):\n"
        '            open(path, "x").close()\n'
        "    with pytest.raises(OSError):\n"
        '        open("tests/test_c2_containment_probe.py", "a").close()\n'
        "\n"
        "\n"
        "def test_only_tmp_is_writable():\n"
        '    assert os.environ["TMPDIR"] == "/tmp"\n'
        '    path = os.path.join("/tmp", "probe-write.txt")\n'
        '    with open(path, "w") as handle:\n'
        '        handle.write("ok")\n'
        "    with open(path) as handle:\n"
        '        assert handle.read() == "ok"\n'
        "\n"
        "\n"
        "def test_identity_and_environment_are_fixed():\n"
        "    assert (os.getuid(), os.getgid()) == (65534, 65534)\n"
        '    names = {name for name in os.environ if not name.startswith("PYTEST_")}\n'
        '    assert names == {"HOME", "TMPDIR", "PATH", "LC_CTYPE"}\n'
        '    for path in ("/var/run/docker.sock", "/run/docker.sock", "/run/secrets"):\n'
        "        assert not os.path.exists(path)\n"
    ),
    "tests/test_c2_timeout_probe.py": (
        '"""P43-PRODUCT-23B: must exceed the C2 Sandbox timeout cap."""\n'
        "import time\n"
        "\n"
        "\n"
        "def test_exceeds_the_sandbox_timeout():\n"
        "    time.sleep(60)\n"
    ),
}
PROBE_PLAN: tuple[dict[str, Any], ...] = (
    {
        "step_number": 1,
        "description": "Run the fixed Sandbox containment probe.",
        "commands": ["python -m pytest tests/test_c2_containment_probe.py -q"],
        "verification": None,
        "rollback": None,
        "expected_files": [],
    },
    {
        "step_number": 2,
        "description": "Run the probe that must exceed the Sandbox timeout.",
        "commands": ["python -m pytest tests/test_c2_timeout_probe.py -q"],
        "verification": None,
        "rollback": None,
        "expected_files": [],
    },
)
PROBE_TIMEOUT_MARKER = "Sandbox verification timeout"
# Test-environment mechanisms a candidate must never add or change.
TEST_ENVIRONMENT_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "sitecustomize.py",
        "usercustomize.py",
    }
)


class SubmitterRefused(RuntimeError):
    """The environment is not the exact disposable C2 submitter contract."""


def _redis_database(url: str) -> str | None:
    if not url.startswith(("redis://", "rediss://")) or "/" not in url[8:]:
        return None
    return url.rsplit("/", 1)[1].split("?", 1)[0]


def refusal_reasons(
    environ: Mapping[str, str],
    *,
    path_exists: Callable[[str], bool] = os.path.lexists,
) -> list[str]:
    """Every reason this process may not act as the disposable submitter."""

    from app.config import DEFAULT_DATABASE_URL
    from app.services.orchestration.worker_boundary import (
        DISPOSABLE_WORKER_QUEUE,
        disposable_c2_publication_admitted,
    )

    def value(name: str) -> str:
        return str(environ.get(name, "") or "").strip()

    reasons: list[str] = []
    if not disposable_c2_publication_admitted(
        environ,
        queue_name=value("CELERY_QUEUE_NAME"),
        database_url=value("DATABASE_URL"),
        production_database_url=DEFAULT_DATABASE_URL,
    ):
        reasons.append("disposable_c2_configuration_required")
    if value("WORKER_QUEUE_NAME") != DISPOSABLE_WORKER_QUEUE:
        reasons.append("disposable_worker_queue_required")
    if value("EXECUTION_ENABLED").lower() != "false":
        reasons.append("execution_enabled_must_be_false")
    if value("CONTROLLED_WORKER_IDENTITY_REQUIRED").lower() != "true":
        reasons.append("controlled_worker_identity_required")
    instance = value("WORKER_INSTANCE_ID")
    if not instance.startswith("c2-c1-") or value(
        "CONTROLLED_WORKER_NODE_IDENTITY"
    ) != (f"{NODE_IDENTITY_BASE}@{instance}"):
        reasons.append("controlled_worker_node_identity_mismatch")
    for name in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        if _redis_database(value(name)) in {None, "", "0", "1"}:
            reasons.append(f"{name.lower()}_must_use_disposable_redis_database")
        # The credential travels as CELERY_BROKER_PASSWORD; a credential-bearing
        # URL would also fail the Worker configuration fingerprint.
        if "@" in value(name):
            reasons.append(f"{name.lower()}_must_not_carry_credentials")
    for name in FORBIDDEN_ENV:
        if value(name):
            reasons.append(f"{name.lower()}_must_be_absent")
    for path in FORBIDDEN_PATHS:
        if path_exists(path):
            reasons.append(f"forbidden_material_visible:{path}")
    return reasons


def require_disposable_environment(environ: Mapping[str, str] | None = None) -> None:
    environ = os.environ if environ is None else environ
    reasons = refusal_reasons(environ)
    if not reasons:
        # Settings may also read a .env file; the effective values must agree.
        from app.config import settings

        if settings.EXECUTION_ENABLED:
            reasons.append("effective_execution_enabled_must_be_false")
        if settings.DATABASE_URL != environ.get("DATABASE_URL"):
            reasons.append("effective_database_url_mismatch")
    if reasons:
        raise SubmitterRefused(",".join(reasons))


def _database_file(environ: Mapping[str, str]) -> Path:
    return Path(str(environ["DATABASE_URL"]).removeprefix("sqlite:///"))


def init_database(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Create the fresh disposable schema with the existing ``init_db``."""

    environ = os.environ if environ is None else environ
    require_disposable_environment(environ)
    database = _database_file(environ)
    if os.path.lexists(database):
        raise SubmitterRefused("database_already_exists")
    from app.database import init_db

    init_db()
    return {"initialized": str(database)}


def _fixed_plan_json() -> str:
    return json.dumps([dict(step) for step in FIXED_PLAN])


def _plan_commands(steps: str | None) -> list[list[str]] | None:
    """Per-step commands of the persisted plan (the Worker adds status fields)."""

    try:
        plan = json.loads(steps or "null")
        return [list(step.get("commands") or []) for step in plan]
    except (TypeError, ValueError, AttributeError):
        return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(rel: str) -> str:
    path = PurePosixPath(rel)
    if (
        not rel
        or path.is_absolute()
        or any(p in ("", ".", "..") for p in rel.split("/"))
    ):
        raise SubmitterRefused(f"material_path_invalid:{rel}")
    return rel


def _material_files(
    manifest: Mapping[str, Any],
    parts: tuple[str, ...],
    case_id: str = DEFAULT_CASE_ID,
) -> dict:
    """Every staged file for ``parts`` with its frozen sha256."""

    case = frozen_case(case_id)
    case_dir = case.directory
    files = {
        f"{case_dir}/manifest.json": case.manifest_sha256,
        f"{case_dir}/task.txt": manifest["task_sha256"],
    }
    if "baseline" in parts:
        for rel, digest in manifest["baseline_files"].items():
            files[f"{case_dir}/baseline/{_safe_relative(rel)}"] = digest
    control_names = []
    if "reference" in parts:
        control_names.append("reference")
    if "naive" in parts:
        control_names.extend(
            name for name in case.evaluation_controls if name != "reference"
        )
    for name in control_names:
        for rel, digest in manifest["controls"][name].items():
            files[f"{case_dir}/controls/{name}/{_safe_relative(rel)}"] = digest
    if "hidden" in parts:
        for rel, digest in manifest["hidden_files"].items():
            files[f"{case_dir}/hidden/{_safe_relative(rel)}"] = digest
    if "case" in parts:
        files[f"{case_dir}/case.json"] = manifest["expectations_sha256"]
    if "tools" in parts:
        files[ORACLE_PATH] = ORACLE_SHA256
        files[HIDDEN_SUITE_PATH] = HIDDEN_SUITE_SHA256
    return files


def _read_pinned_manifest(root: Path, case_id: str = DEFAULT_CASE_ID) -> dict[str, Any]:
    case = frozen_case(case_id)
    path = root / case.directory / "manifest.json"
    label = case.id.lower()
    if path.is_symlink() or not path.is_file():
        raise SubmitterRefused(f"{label}_manifest_missing")
    data = path.read_bytes()
    if _sha256_bytes(data) != case.manifest_sha256:
        raise SubmitterRefused(f"{label}_manifest_sha256_mismatch")
    manifest = json.loads(data)
    if manifest.get("id") != case.id:
        raise SubmitterRefused(f"{label}_manifest_case_mismatch")
    return manifest


def verify_case_material(
    root: str | Path,
    parts: tuple[str, ...],
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Verify exactly one selected case's frozen files: no link or extra file."""

    root = Path(root)
    manifest = _read_pinned_manifest(root, case_id)
    expected = _material_files(manifest, parts, case_id)
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SubmitterRefused(f"material_symlink:{rel}")
        if path.is_file():
            actual[rel] = _sha256_bytes(path.read_bytes())
        elif not path.is_dir():
            raise SubmitterRefused(f"material_special_file:{rel}")
    if actual != expected:
        unexpected = sorted(set(actual) - set(expected))
        differing = sorted(r for r in expected if actual.get(r) != expected[r])
        raise SubmitterRefused(
            f"{case_id.lower()}_material_mismatch:unexpected={unexpected[:5]},differing={differing[:5]}"
        )
    return manifest


def verify_u1_material(root: str | Path, parts: tuple[str, ...]) -> dict[str, Any]:
    """Compatibility wrapper for the original PRODUCT-23B/23C U1 path."""

    return verify_case_material(root, parts, "U1")


def stage_material(
    source: str | Path,
    destination: str | Path,
    parts: tuple[str, ...],
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Copy exactly the frozen files for ``parts`` and verify the copy."""

    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination):
        raise SubmitterRefused("material_destination_exists")
    manifest = _read_pinned_manifest(source, case_id)
    files = _material_files(manifest, parts, case_id)
    for rel, digest in sorted(files.items()):
        origin = source / rel
        if origin.is_symlink() or not origin.is_file():
            raise SubmitterRefused(f"material_missing:{rel}")
        data = origin.read_bytes()
        if _sha256_bytes(data) != digest:
            raise SubmitterRefused(f"material_hash_mismatch:{rel}")
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)
        # Root-owned on the Host; not 0444, because the oracle copies the
        # baseline with its mode and then overwrites candidate files.
        os.chmod(target, 0o644)
    for directory in sorted({destination, *destination.rglob("*")}):
        if directory.is_dir():
            os.chmod(directory, 0o755)
    verify_case_material(destination, parts, case_id)
    return {
        "case_id": case_id,
        "manifest_sha256": frozen_case(case_id).manifest_sha256,
        "staged": str(destination),
        "parts": list(parts),
        "files": len(files),
    }


def _is_test_file(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def reference_plan(
    material_root: str | Path, case_id: str = DEFAULT_CASE_ID
) -> list[dict[str, Any]]:
    """The selected frozen reference control as existing structured ops."""

    case = frozen_case(case_id)
    root = Path(material_root)
    manifest = verify_case_material(root, SUBMIT_PARTS, case.id)
    control = root / case.directory / "controls" / "reference"
    change = json.loads((control / "manifest.json").read_text(encoding="utf-8"))
    if change.get("deleted_files"):
        raise SubmitterRefused("reference_control_deletes_files")
    copied = [_safe_relative(rel) for rel in change["copied_files"]]
    sources = [rel for rel in copied if not _is_test_file(rel)]
    tests = [rel for rel in copied if _is_test_file(rel)]

    def ops(paths: list[str]) -> list[dict[str, str]]:
        return [
            {
                "op": "write_file",
                "path": rel,
                "content": (control / "files" / rel).read_text(encoding="utf-8"),
            }
            for rel in paths
        ]

    def step(number, description, operations, targets, expected):
        command = f"python -m pytest {' '.join(targets)} -q"
        entry = {
            "step_number": number,
            "description": description,
            "commands": [command],
            "verification": command,
            "rollback": None,
            "expected_files": expected,
        }
        if operations:
            entry["ops"] = operations
        return entry

    return [
        step(
            1,
            "Apply the trusted reference source change.",
            ops(sources),
            manifest["existing_tests"],
            sources,
        ),
        step(
            2, "Add the trusted reference regression tests.", ops(tests), tests, tests
        ),
        step(
            3,
            "Run the independent acceptance contract.",
            [],
            [manifest["acceptance"]],
            [],
        ),
    ]


def u1_reference_plan(material_root: str | Path) -> list[dict[str, Any]]:
    """Compatibility wrapper for the original PRODUCT-23B U1 control."""

    return reference_plan(material_root, "U1")


def _case_control_projects(case_id: str = DEFAULT_CASE_ID) -> dict[str, str]:
    case = frozen_case(case_id)
    return {
        "reference": case.reference_project,
        "probe": CONTROL_PROJECTS["probe"],
        MODEL: case.model_project,
    }


def _control_definition(control: str, case_id: str = DEFAULT_CASE_ID) -> dict[str, Any]:
    case = frozen_case(case_id)
    if control == EXIT_CHECK:
        return {
            "project": PROJECT_NAME,
            "project_description": "Disposable C2-C3B execution-path verification.",
            "title": TASK_TITLE,
            "description": TASK_DESCRIPTION,
            "profile": EXECUTION_PROFILE,
            "plan": [dict(step) for step in FIXED_PLAN],
            "session": "C2-C3B disposable execution",
            "seed": {},
        }
    if control == "reference":
        root = case_material_root(case.id)
        manifest = verify_case_material(root, SUBMIT_PARTS, case.id)
        baseline = root / case.directory / "baseline"
        return {
            "project": case.reference_project,
            "project_description": f"P43-PRODUCT-23B trusted {case.id} reference control.",
            "title": case.reference_title,
            "description": (root / case.directory / "task.txt").read_text(
                encoding="utf-8"
            ),
            "profile": "full_lifecycle",
            "plan": reference_plan(root, case.id),
            "session": case.reference_session,
            "seed": {
                rel: (baseline / rel).read_bytes() for rel in manifest["baseline_files"]
            },
        }
    if control == MODEL:
        root = case_material_root(case.id)
        # Exact verification: a mount holding any reference, naive, hidden or
        # case file is refused, not filtered.
        manifest = verify_case_material(root, MODEL_PARTS, case.id)
        baseline = root / case.directory / "baseline"
        return {
            "project": case.model_project,
            "project_description": f"P43-PRODUCT-24B {case.id} real-provider evaluation.",
            "title": case.model_title,
            "description": (root / case.directory / "task.txt").read_text(
                encoding="utf-8"
            ),
            "profile": "full_lifecycle",
            "plan": None,
            "session": case.model_session,
            "seed": {
                rel: (baseline / rel).read_bytes() for rel in manifest["baseline_files"]
            },
        }
    if control == "probe":
        return {
            "project": CONTROL_PROJECTS[control],
            "project_description": "P43-PRODUCT-23B containment probe.",
            "title": "C2 containment probe",
            "description": PROBE_TASK_DESCRIPTION,
            "profile": EXECUTION_PROFILE,
            "plan": [dict(step) for step in PROBE_PLAN],
            "session": "P43-PRODUCT-23B containment probe",
            "seed": {rel: text.encode("utf-8") for rel, text in PROBE_FILES.items()},
        }
    raise SubmitterRefused(f"unknown_control:{control}")


def _control_project_names(case_id: str = DEFAULT_CASE_ID) -> dict[str, str]:
    """Stored Project names (``create_project`` humanizes the slug)."""

    from app.services.project.name_formatter import humanize_display_name

    return {
        c: humanize_display_name(name)
        for c, name in _case_control_projects(case_id).items()
    }


def _control_guard(db: Any, control: str, case_id: str = DEFAULT_CASE_ID) -> Any:
    """One submission per control, never while another attempt is in flight."""

    from app.models import Project, TaskExecution, User

    if control == EXIT_CHECK:
        from app.models import Session as SessionModel, Task

        if any(
            db.query(model).count() for model in (User, Project, Task, SessionModel)
        ):
            raise SubmitterRefused("database_not_fresh")
        return None
    users = db.query(User).all()
    names = {project.name for project in db.query(Project).all()}
    known = _control_project_names(case_id)
    if any(user.email != SUBMITTER_EMAIL for user in users) or not names <= set(
        known.values()
    ):
        raise SubmitterRefused("database_not_disposable_controls")
    if known[control] in names:
        raise SubmitterRefused("control_already_submitted")
    for execution in db.query(TaskExecution).all():
        status = str(getattr(execution.status, "value", execution.status)).lower()
        if status not in TERMINAL_STATUSES:
            raise SubmitterRefused("control_in_progress")
    return users[0] if users else None


def _workspace_files(workspace: Path) -> dict[str, str]:
    """Project workspace content, without Orchestrator scaffolding."""

    from app.services.workspace.workspace_paths import is_hydration_excluded_path

    files: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace)
        if is_hydration_excluded_path(rel) or not path.is_file():
            continue
        files[rel.as_posix()] = _sha256_bytes(path.read_bytes())
    return files


def submit(
    db_factory: Callable[[], Any] | None = None,
    environ: Mapping[str, str] | None = None,
    control: str = EXIT_CHECK,
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Create one Project/Task/Session and queue it via ``queue_task_for_session``."""

    environ = os.environ if environ is None else environ
    require_disposable_environment(environ)
    case = frozen_case(case_id)
    if control not in CONTROLS:
        raise SubmitterRefused(f"unknown_control:{control}")
    from app.api.v1.endpoints.projects import create_project
    from app.api.v1.endpoints.sessions import create_session
    from app.api.v1.endpoints.tasks import create_task
    from app.models import Project, User
    from app.schemas import ProjectCreate, SessionCreate, TaskCreate
    from app.services.session.session_runtime_service import queue_task_for_session
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )

    if db_factory is None:
        from app.database import SessionLocal as db_factory
    db = db_factory()
    try:
        user = _control_guard(db, control, case.id)
        definition = _control_definition(control, case.id)
        if user is None:
            # Unusable password: this row only owns the disposable Projects.
            user = User(email=SUBMITTER_EMAIL, hashed_password="!", is_active=True)
            db.add(user)
            db.commit()
            db.refresh(user)
        project = create_project(
            ProjectCreate(
                name=definition["project"],
                description=definition["project_description"],
                workspace_path=definition["project"],
            ),
            db=db,
            current_user=user,
        )
        seeded: dict[str, str] = {}
        if definition["seed"]:
            workspace = resolve_project_workspace_path(
                db.get(Project, project.id).workspace_path, project.name, db=db
            )
            for rel, data in sorted(definition["seed"].items()):
                target = Path(workspace) / _safe_relative(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(data)
                seeded[rel] = _sha256_bytes(data)
        task = create_task(
            TaskCreate(
                project_id=project.id,
                title=definition["title"],
                description=definition["description"],
                execution_profile=definition["profile"],
                steps=(
                    None
                    if definition["plan"] is None
                    else json.dumps(definition["plan"])
                ),
            ),
            db=db,
            current_user=user,
        )
        session = create_session(
            SessionCreate(
                project_id=project.id,
                name=definition["session"],
                description=(
                    TASK_DESCRIPTION if control == EXIT_CHECK else definition["title"]
                ),
                execution_mode="manual",
            ),
            db=db,
            current_user=user,
        )
        queued = queue_task_for_session(
            db,
            session,
            task.id,
            timeout_seconds=(
                MODEL_TIMEOUT_SECONDS
                if control == MODEL
                else ORCHESTRATION_TIMEOUT_SECONDS
            ),
        )
        result = {
            "project_id": project.id,
            "task_id": task.id,
            "session_id": session.id,
            "session_instance_id": session.instance_id,
            "task_execution_id": queued.get("task_execution_id"),
            "queue_status": queued.get("status"),
        }
        if control != EXIT_CHECK:
            result.update(
                case_id=case.id,
                case_manifest_sha256=case.manifest_sha256,
                control=control,
                seeded_files=seeded,
            )
        return result
    finally:
        db.close()


def _control_observation(
    db: Any, control: str, execution: Any, case_id: str = DEFAULT_CASE_ID
) -> dict[str, Any]:
    """Change Set, workspace and expected outcome for one trusted control."""

    case = frozen_case(case_id)
    from app.models import Project, Task, TaskExecutionChangeSet
    from app.services.workspace.changeset_service import ChangesetService
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )

    task = db.get(Task, execution.task_id)
    project = db.get(Project, task.project_id)
    if (
        control != EXIT_CHECK
        and project.name != _control_project_names(case.id)[control]
    ):
        raise SubmitterRefused("case_project_identity_mismatch")
    workspace = resolve_project_workspace_path(
        project.workspace_path, project.name, db=db
    )
    row = (
        db.query(TaskExecutionChangeSet)
        .filter_by(task_execution_id=execution.id)
        .one_or_none()
    )
    change_set = None
    if row is not None:
        directory = ChangesetService(db).change_set_dir_for_read(project, execution.id)
        change_set = {
            "disposition": row.disposition,
            "outcome": (row.review_decision or {}).get("outcome"),
            "added_files": list(row.added_files or []),
            "modified_files": list(row.modified_files or []),
            "deleted_files": list(row.deleted_files or []),
            "directory": str(directory),
            "artifact_present": (Path(directory) / "manifest.json").is_file(),
        }
    workspace_files = _workspace_files(Path(workspace))
    status = str(getattr(execution.status, "value", execution.status)).lower()
    error = task.error_message or ""
    if control in ("reference", MODEL):
        # The unchanged baseline is compared by ``verdict`` (frozen hashes).
        # For ``model`` this is "delivered and held", not an expectation.
        met = (
            status == "done"
            and change_set is not None
            and change_set["disposition"] == "captured"
            and change_set["outcome"] == "hold_for_review"
            and change_set["artifact_present"]
        )
    else:
        probe_seed = {
            rel: _sha256_bytes(text.encode("utf-8"))
            for rel, text in PROBE_FILES.items()
        }
        changed = change_set is None or any(
            change_set[key]
            for key in ("added_files", "modified_files", "deleted_files")
        )
        met = (
            status == "failed"
            and PROBE_TIMEOUT_MARKER in error
            and "step 2" in error
            and not changed
            and workspace_files == probe_seed
        )
    return {
        "case_id": case.id,
        "case_manifest_sha256": case.manifest_sha256,
        "control": control,
        "project_id": project.id,
        "project_workspace": str(workspace),
        "workspace_files": workspace_files,
        "change_set": change_set,
        "expected_outcome_met": bool(met),
    }


def _model_attempt_observation(task: Any, session: Any, executions: list) -> dict:
    """Retry, rerun and accepted-Plan facts for the model control."""

    steps = task.steps or ""
    return {
        "task_execution_count": len(executions),
        "task_executions": [
            {
                "id": row.id,
                "attempt_number": row.attempt_number,
                "status": str(getattr(row.status, "value", row.status)),
                "failure_category": row.failure_category,
            }
            for row in executions
        ],
        "accepted_plan_sha256": _sha256_bytes(steps.encode("utf-8")) if steps else None,
        "accepted_plan_step_count": len(_plan_commands(steps) or []),
        "session_continuation_kind": getattr(session, "continuation_kind", None),
        "session_continuation_retry_count": getattr(
            session, "continuation_retry_count", None
        ),
    }


def observe(
    db_factory: Callable[[], Any] | None = None,
    *,
    deadline_seconds: float = 600.0,
    poll_seconds: float = 2.0,
    control: str = EXIT_CHECK,
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Wait for the control's TaskExecution to reach a persisted terminal state."""

    from app.models import Project, Session as SessionModel, Task, TaskExecution

    if control not in CONTROLS:
        raise SubmitterRefused(f"unknown_control:{control}")
    case = frozen_case(case_id)
    if db_factory is None:
        from app.database import SessionLocal as db_factory
    stop_at = time.monotonic() + deadline_seconds
    while True:
        db = db_factory()
        try:
            query = db.query(TaskExecution)
            if control != EXIT_CHECK:
                project = (
                    db.query(Project)
                    .filter_by(name=_control_project_names(case.id)[control])
                    .one_or_none()
                )
                if project is None:
                    raise SubmitterRefused("control_not_submitted")
                query = query.join(Task, Task.id == TaskExecution.task_id).filter(
                    Task.project_id == project.id
                )
            executions = sorted(query.all(), key=lambda row: row.id)
            # A model run reports a second execution instead of hiding it.
            if len(executions) != 1 and not (control == MODEL and executions):
                raise SubmitterRefused(f"expected_one_task_execution:{len(executions)}")
            execution = executions[0]
            status = getattr(execution.status, "value", execution.status)
            if str(status).lower() in TERMINAL_STATUSES or time.monotonic() >= stop_at:
                task = db.get(Task, execution.task_id)
                session = db.get(SessionModel, execution.session_id)
                result = {
                    "case_id": case.id,
                    "case_manifest_sha256": case.manifest_sha256,
                    "terminal": str(status).lower() in TERMINAL_STATUSES,
                    "task_execution_id": execution.id,
                    "task_execution_status": str(status),
                    "attempt_number": execution.attempt_number,
                    "failure_category": execution.failure_category,
                    "task_id": execution.task_id,
                    "task_status": str(getattr(task.status, "value", task.status)),
                    "task_error_message": (task.error_message or "")[:500],
                    "task_plan_commands": _plan_commands(task.steps),
                    "session_id": execution.session_id,
                    "session_status": session.status,
                    "session_instance_id": session.instance_id,
                    "dispatch_session_instance_id": (
                        execution.dispatch_session_instance_id
                    ),
                    "dispatch_state": execution.dispatch_state,
                    "dispatch_celery_task_id": execution.dispatch_celery_task_id,
                    "dispatch_lease_fence": execution.dispatch_lease_fence,
                    "dispatch_refusal_reason": execution.dispatch_refusal_reason,
                    "worker_hostname": execution.worker_hostname,
                    "worker_pid": execution.worker_pid,
                    "worker_process_start_identity": (
                        execution.worker_process_start_identity
                    ),
                }
                if control == MODEL:
                    result.update(_model_attempt_observation(task, session, executions))
                if control != EXIT_CHECK:
                    result.update(_control_observation(db, control, execution, case.id))
                    result["expected_outcome_met"] = bool(
                        result["expected_outcome_met"]
                        and execution.attempt_number == 1
                        and db.query(TaskExecution)
                        .filter_by(task_id=execution.task_id)
                        .count()
                        == 1
                    )
                return result
        finally:
            db.close()
        time.sleep(poll_seconds)


# --- Host-side independent evaluation (standard library only) ---------------


def change_set_integrity(
    manifest: Mapping[str, Any],
    change_set_dir: str | Path,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect the complete Change Set against the frozen deliverable scope.

    Rejects deletions of protected tests, any test-environment mechanism
    (pytest configuration, ``conftest.py``, ``sitecustomize``, ``.pth``) and
    additions outside "new test file" or the existing package directories.
    With ``row`` (the product's held Change Set), additions, modifications and
    deletions must equal the captured artifact.
    """

    change_set_dir = Path(change_set_dir)
    artifact = json.loads((change_set_dir / "manifest.json").read_text("utf-8"))
    copied = set(artifact.get("copied_files") or [])
    deleted = set(artifact.get("deleted_files") or [])
    reasons: list[str] = []
    on_disk: set[str] = set()
    files_root = change_set_dir / "files"
    if files_root.exists():
        for path in files_root.rglob("*"):
            rel = path.relative_to(files_root).as_posix()
            if path.is_symlink():
                reasons.append(f"artifact_symlink:{rel}")
            elif path.is_file():
                on_disk.add(rel)
    if on_disk != copied:
        reasons.append("artifact_files_do_not_match_manifest")
    changed, removed = set(copied), set(deleted)
    if row is not None:
        row_changed = set(row.get("added_files") or []) | set(
            row.get("modified_files") or []
        )
        row_deleted = set(row.get("deleted_files") or [])
        if row_changed != copied or row_deleted != deleted:
            reasons.append("change_set_row_does_not_match_artifact")
        changed |= row_changed
        removed |= row_deleted
    baseline = set(manifest["baseline_files"])
    acceptance = manifest["acceptance"]
    protected = {acceptance, *manifest["existing_tests"]}
    test_dirs = {PurePosixPath(rel).parent for rel in protected}
    package_dirs = {
        PurePosixPath(rel).parent
        for rel in baseline
        if PurePosixPath(rel).name == "__init__.py"
    }
    for rel in sorted(removed):
        if rel in protected:
            reasons.append(f"protected_test_deleted:{rel}")
    for rel in sorted(changed):
        path = PurePosixPath(rel)
        if path.is_absolute() or ".." in path.parts:
            reasons.append(f"unsafe_path:{rel}")
        elif path.name in TEST_ENVIRONMENT_NAMES or path.suffix == ".pth":
            reasons.append(f"test_environment_modification:{rel}")
        elif rel == acceptance:
            reasons.append(f"acceptance_modified:{rel}")
        elif rel in baseline:
            continue
        elif not (
            (_is_test_file(rel) and path.parent in test_dirs)
            or (path.suffix == ".py" and path.parent in package_dirs)
        ):
            reasons.append(f"unauthorized_addition:{rel}")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "changed_files": sorted(changed),
        "deleted_files": sorted(removed),
    }


def evaluation_verdict(
    control: str,
    material_root: str | Path,
    change_set_dir: str | Path,
    oracle: Mapping[str, Any],
    hidden: Mapping[str, Any],
    observed: Mapping[str, Any] | None = None,
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Combine integrity, the P02 oracle and the hidden suite for ``control``.

    ``reference`` is the product-captured Change Set and needs the
    ``observe`` record; ``naive`` is the frozen control Change Set itself.
    The verdict is compared with the frozen PRODUCT-18 expectation.
    ``model`` (PRODUCT-23C) is the actual held model Change Set, judged on
    its own: it has no frozen expectation and is never replaced.
    """

    case_descriptor = frozen_case(case_id)
    root = Path(material_root)
    manifest = verify_case_material(root, EVALUATION_PARTS, case_descriptor.id)
    case = json.loads(
        (root / case_descriptor.directory / "case.json").read_text(encoding="utf-8")
    )
    if control != MODEL and control not in case.get("expectations", {}):
        raise SubmitterRefused(f"unknown_evaluation_control:{control}")
    expected = {} if control == MODEL else case["expectations"][control]
    row = None
    held = None
    if control in ("reference", MODEL):
        if not observed or observed.get("control") != control:
            raise SubmitterRefused(f"{control}_observation_required")
        if (
            observed.get("case_id") != case_descriptor.id
            or observed.get("case_manifest_sha256") != case_descriptor.manifest_sha256
        ):
            raise SubmitterRefused("case_observation_identity_mismatch")
        row = observed.get("change_set") or {}
        baseline_unchanged = observed.get("workspace_files") == dict(
            manifest["baseline_files"]
        )
        held = bool(observed.get("expected_outcome_met")) and baseline_unchanged
    integrity = change_set_integrity(manifest, change_set_dir, row)
    delivery = bool(
        integrity["ok"]
        and oracle.get("complete_delivery") is True
        and hidden.get("hidden_pass") is True
        and held is not False
    )
    observed_fields = {
        "case_id": case_descriptor.id,
        "case_manifest_sha256": case_descriptor.manifest_sha256,
        "functional_pass": oracle.get("functional_pass"),
        "preservation_pass": oracle.get("preservation_pass"),
        "acceptance_protected": oracle.get("acceptance_protected"),
        "deliverable": oracle.get("deliverable"),
        "oracle_complete_delivery": oracle.get("complete_delivery"),
        "hidden_pass": hidden.get("hidden_pass"),
        "complete_delivery_with_hidden": delivery,
    }
    mismatches = [
        f"{key}: expected {value!r}, observed {observed_fields.get(key)!r}"
        for key, value in expected.items()
        if observed_fields.get(key) != value
    ]
    if control == "reference" and not (integrity["ok"] and held):
        mismatches.append("reference_not_held_or_integrity_failed")
    if control == MODEL:
        if not integrity["ok"]:
            outcome = "INTEGRITY_FAILED"
        elif delivery:
            outcome = "COMPLETE_DELIVERY"
        else:
            outcome = "INCOMPLETE_DELIVERY"
        return {
            "case_id": case_descriptor.id,
            "case_manifest_sha256": case_descriptor.manifest_sha256,
            "control": control,
            "integrity": integrity,
            "product_held_and_baseline_unchanged": held,
            "observed": observed_fields,
            "verdict": outcome,
        }
    return {
        "case_id": case_descriptor.id,
        "case_manifest_sha256": case_descriptor.manifest_sha256,
        "control": control,
        "integrity": integrity,
        "product_held_and_baseline_unchanged": held,
        "observed": observed_fields,
        "expected": expected,
        "mismatches": mismatches,
        "verdict": "MATCHES_FROZEN_EXPECTATION" if not mismatches else "MISMATCH",
    }


CONTAMINATION_MIN_LINE_CHARS = 24


def _chat_text(payload: Any, key: str) -> str:
    """Concatenated message text of a chat request (``messages``) or response."""

    if not isinstance(payload, dict):
        return ""
    if key == "messages":
        messages = payload.get("messages") or []
    else:
        messages = [
            (choice or {}).get("message") or {}
            for choice in payload.get("choices") or []
            if isinstance(choice, dict)
        ]
    parts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
    return "\n".join(parts)


def provider_request_contamination(
    material_root: str | Path,
    relay_state_dir: str | Path,
    case_id: str = DEFAULT_CASE_ID,
) -> dict[str, Any]:
    """Supplementary evidence: evaluation-only lines in recorded requests.

    A marker is a stripped line of at least ``CONTAMINATION_MIN_LINE_CHARS``
    from a reference, naive, hidden, case or tool file that does not occur in
    the task text or baseline. A request hit is *explained* when an earlier
    provider response already contained the line, i.e. the model produced it
    itself. Unexplained hits need review; they are not a leakage verdict, and
    model output matching the reference is never leakage by itself. Only line
    digests are reported.
    """

    root = Path(material_root)
    case = frozen_case(case_id)
    manifest = verify_case_material(root, EVALUATION_PARTS, case.id)
    files = _material_files(manifest, EVALUATION_PARTS, case.id)
    visible = set(_material_files(manifest, MODEL_PARTS, case.id))
    allowed = "\n".join(
        (root / rel).read_text(encoding="utf-8", errors="replace")
        for rel in sorted(visible)
    )
    markers: dict[str, str] = {}
    for rel in sorted(set(files) - visible):
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if (
                len(stripped) >= CONTAMINATION_MIN_LINE_CHARS
                and stripped not in allowed
            ):
                markers.setdefault(stripped, rel)
    bodies = Path(relay_state_dir) / "bodies"
    requests = sorted(bodies.glob("*.request.json")) if bodies.is_dir() else []
    produced = ""
    hits: list[dict[str, Any]] = []
    for request in requests:
        sequence = request.name.split(".", 1)[0]
        text = _chat_text(json.loads(request.read_bytes()), "messages")
        for line, source in markers.items():
            if line in text:
                hits.append(
                    {
                        "sequence": sequence,
                        "source": source,
                        "line_sha256": _sha256_bytes(line.encode("utf-8")),
                        "explained_by_prior_response": line in produced,
                    }
                )
        response = request.with_name(f"{sequence}.response.json")
        if response.is_file() and not response.is_symlink():
            try:
                produced += "\n" + _chat_text(
                    json.loads(response.read_bytes()), "choices"
                )
            except ValueError:
                pass
    unexplained = [hit for hit in hits if not hit["explained_by_prior_response"]]
    return {
        "case_id": case.id,
        "case_manifest_sha256": case.manifest_sha256,
        "requests_scanned": len(requests),
        "marker_lines": len(markers),
        "hits": hits,
        "unexplained_hits": len(unexplained),
        "classification": (
            "REVIEW_REQUIRED" if unexplained else "NO_EVALUATION_MATERIAL_IN_REQUESTS"
        ),
    }


def _load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=(
            "init",
            "submit",
            "observe",
            "stage-material",
            "verify-material",
            "case-info",
            "verdict",
            "contamination",
        ),
    )
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--deadline-seconds", type=float, default=600.0)
    parser.add_argument("--control", default=EXIT_CHECK, choices=(*CONTROLS, "naive"))
    parser.add_argument("--parts", choices=("submit", "evaluation", "model"))
    parser.add_argument("--oracle")
    parser.add_argument("--hidden")
    parser.add_argument("--observed")
    parser.add_argument("--case", default=DEFAULT_CASE_ID)
    args = parser.parse_args(argv)
    # Paths are Host-side evaluation inputs only; nothing else takes input.
    arity = {
        "stage-material": 2,
        "verify-material": 1,
        "case-info": 0,
        "verdict": 2,
        "contamination": 2,
    }
    if len(args.paths) != arity.get(args.command, 0):
        parser.error(f"{args.command} takes {arity.get(args.command, 0)} path(s)")
    parts = {
        "submit": SUBMIT_PARTS,
        "evaluation": EVALUATION_PARTS,
        "model": MODEL_PARTS,
    }
    try:
        if args.command == "init":
            result = init_database()
        elif args.command == "case-info":
            result = case_info(args.case)
        elif args.command == "submit":
            result = submit(control=args.control, case_id=args.case)
        elif args.command == "observe":
            require_disposable_environment()
            result = observe(
                deadline_seconds=args.deadline_seconds,
                control=args.control,
                case_id=args.case,
            )
        elif args.command == "stage-material":
            source, destination = args.paths
            result = stage_material(source, destination, parts[args.parts], args.case)
        elif args.command == "verify-material":
            verify_case_material(args.paths[0], parts[args.parts], args.case)
            result = {
                "case_id": args.case,
                "manifest_sha256": frozen_case(args.case).manifest_sha256,
                "verified": args.paths[0],
                "parts": args.parts,
            }
        elif args.command == "contamination":
            result = provider_request_contamination(*args.paths, args.case)
        else:
            material, change_set = args.paths
            result = evaluation_verdict(
                args.control,
                material,
                change_set,
                _load_json(args.oracle),
                _load_json(args.hidden),
                _load_json(args.observed) if args.observed else None,
                args.case,
            )
    except (SubmitterRefused, ValueError) as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 78
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "observe":
        if args.control == MODEL:
            # An observation, not a pass/fail gate: terminal or not, it is
            # the evidence; the outcome is judged by ``verdict``.
            return 0 if result["terminal"] else 2
        if args.control != EXIT_CHECK:
            return 0 if result["expected_outcome_met"] else 1
        return 0 if result["task_execution_status"].lower() == "done" else 1
    if args.command == "verdict":
        if args.control == MODEL:
            return 0
        return 0 if result["verdict"] == "MATCHES_FROZEN_EXPECTATION" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
