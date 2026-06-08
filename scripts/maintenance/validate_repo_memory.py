"""RepoMemory 10-task characterization harness.

Runs build_repo_memory() and write_repo_memory() on real and synthetic project
directories to verify detection correctness, stability, and hash invalidation.
No live model calls. No DB access. No prompt injection.

Usage:
    PYTHONPATH=. python3 scripts/maintenance/validate_repo_memory.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.orchestration.repo_memory import (  # noqa: E402
    build_repo_memory,
    load_repo_memory,
    write_repo_memory,
)

VAULT_PROJECTS = Path(__file__).resolve().parents[3]
ORCHESTRATOR = VAULT_PROJECTS / "orchestrator"
ORCHESTRATOR_FRONTEND = ORCHESTRATOR / "frontend"
GARDEN_MICROSITE = VAULT_PROJECTS / "garden-story-microsite"
CLAWMOBILE = VAULT_PROJECTS / "clawmobile"

# ── Result helpers ────────────────────────────────────────────────────────────


class _FakeLogger:
    def info(self, *a: Any, **kw: Any) -> None:
        pass

    def warning(self, *a: Any, **kw: Any) -> None:
        pass


def _run_task(label: str, project_dir: Path, *, check_stability: bool = True,
              check_invalidation_file: Optional[str] = None,
              invalidation_new_content: Optional[str] = None) -> dict:
    """Run one characterization task. Returns a result dict."""
    log = _FakeLogger()

    # First write.
    rm1 = write_repo_memory(project_dir, _logger=log)
    created = (project_dir / ".agent" / "repo_memory.json").exists()

    # Load (should cache-hit).
    loaded = load_repo_memory(project_dir)
    cache_hit = loaded is not None

    # Stability: second write should produce identical facts.
    stability_ok: Optional[bool] = None
    if check_stability and rm1 is not None:
        rm2 = build_repo_memory(project_dir)
        stability_ok = (
            rm1.project_type == rm2.project_type
            and rm1.package_manager == rm2.package_manager
            and rm1.source_root == rm2.source_root
            and rm1.test_root == rm2.test_root
            and rm1.test_command == rm2.test_command
        )

    # Hash invalidation check.
    invalidation_triggered: Optional[bool] = None
    if check_invalidation_file and rm1 is not None:
        target = project_dir / check_invalidation_file
        original = target.read_text(encoding="utf-8") if target.exists() else None
        try:
            target.write_text(
                invalidation_new_content or "# mutated by harness\n", encoding="utf-8"
            )
            after = load_repo_memory(project_dir)
            invalidation_triggered = after is None
        finally:
            # Restore original.
            if original is not None:
                target.write_text(original, encoding="utf-8")
            elif target.exists():
                target.unlink()
            # Re-populate so the file is clean for subsequent tasks.
            write_repo_memory(project_dir, _logger=log)

    result = {
        "task": label,
        "project_dir": str(project_dir),
        "file_created": created,
        "cache_hit_after_write": cache_hit,
        "stability_ok": stability_ok,
        "invalidation_triggered": invalidation_triggered,
    }
    if rm1 is not None:
        result.update(
            {
                "project_type": rm1.project_type,
                "package_manager": rm1.package_manager,
                "source_root": rm1.source_root,
                "test_root": rm1.test_root,
                "test_command": rm1.test_command,
                "build_command": rm1.build_command,
                "entry_points": rm1.entry_points,
                "known_config_files": rm1.known_config_files,
            }
        )
    else:
        result["error"] = "write_repo_memory returned None"
    return result


# ── Synthetic project factories ───────────────────────────────────────────────


def _make_pure_python(tmp: Path) -> None:
    (tmp / "requirements.txt").write_text("pytest\nfastapi\n")
    (tmp / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp / "app").mkdir()
    (tmp / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp / "app" / "tests").mkdir()
    (tmp / "app" / "tests" / "conftest.py").write_text("")


def _make_pure_node_npm(tmp: Path) -> None:
    (tmp / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "jest", "build": "vite build"}})
    )
    (tmp / "package-lock.json").write_text("{}")
    (tmp / "src").mkdir()
    (tmp / "src" / "index.ts").write_text("export const main = () => {};")
    (tmp / "index.html").write_text("<!DOCTYPE html><html></html>")


def _make_poetry_python(tmp: Path) -> None:
    (tmp / "pyproject.toml").write_text("[tool.poetry]\nname = 'mylib'\n\n[tool.pytest.ini_options]\n")
    (tmp / "poetry.lock").write_text("")
    (tmp / "src").mkdir()
    (tmp / "src" / "mylib.py").write_text("")
    (tmp / "tests").mkdir()


def _make_pure_node_yarn(tmp: Path) -> None:
    (tmp / "package.json").write_text(
        json.dumps({"name": "app", "scripts": {"test": "vitest"}})
    )
    (tmp / "yarn.lock").write_text("")
    (tmp / "src").mkdir()
    (tmp / "src" / "index.js").write_text("module.exports = {};")


def _make_static_html(tmp: Path) -> None:
    (tmp / "index.html").write_text("<!DOCTYPE html><html><body>Hello</body></html>")
    (tmp / "css").mkdir()
    (tmp / "css" / "style.css").write_text("body { margin: 0; }")


def _make_mixed_project(tmp: Path) -> None:
    (tmp / "requirements.txt").write_text("pytest\n")
    (tmp / "package.json").write_text(
        json.dumps({"name": "mixed", "scripts": {"build": "vite build"}})
    )
    (tmp / "app").mkdir()
    (tmp / "app" / "main.py").write_text("")
    (tmp / "Makefile").write_text("build:\n\tnpm run build\n")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    results = []
    failed = 0

    # ── Task 1: orchestrator (real Python+Node mixed project) ──────────────
    if ORCHESTRATOR.exists():
        results.append(
            _run_task(
                "T01_orchestrator_mixed",
                ORCHESTRATOR,
                check_invalidation_file="requirements.txt",
                invalidation_new_content="pytest\nfastapi\nhttpx\n# mutated\n",
            )
        )
    else:
        print(f"SKIP T01: {ORCHESTRATOR} not found", file=sys.stderr)

    # ── Task 2: orchestrator frontend (real Node project) ─────────────────
    if ORCHESTRATOR_FRONTEND.exists():
        results.append(_run_task("T02_orchestrator_frontend_node", ORCHESTRATOR_FRONTEND))
    else:
        print(f"SKIP T02: {ORCHESTRATOR_FRONTEND} not found", file=sys.stderr)

    # ── Task 3: garden-story-microsite (real static HTML project) ─────────
    if GARDEN_MICROSITE.exists():
        results.append(_run_task("T03_garden_microsite_static", GARDEN_MICROSITE))
    else:
        print(f"SKIP T03: {GARDEN_MICROSITE} not found", file=sys.stderr)

    # ── Task 4: clawmobile (real Android/Gradle project) ──────────────────
    if CLAWMOBILE.exists():
        results.append(_run_task("T04_clawmobile_android", CLAWMOBILE))
    else:
        print(f"SKIP T04: {CLAWMOBILE} not found", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="repo_mem_harness_") as tmpdir:
        base = Path(tmpdir)

        # ── Task 5: pure Python (requirements.txt + pytest.ini) ───────────
        d5 = base / "pure_python"
        d5.mkdir()
        _make_pure_python(d5)
        results.append(
            _run_task(
                "T05_pure_python_pip_pytest",
                d5,
                check_invalidation_file="requirements.txt",
                invalidation_new_content="pytest\nrequests\n",
            )
        )

        # ── Task 6: pure Node (npm + package-lock.json) ───────────────────
        d6 = base / "pure_node_npm"
        d6.mkdir()
        _make_pure_node_npm(d6)
        results.append(
            _run_task(
                "T06_pure_node_npm",
                d6,
                check_invalidation_file="package.json",
                invalidation_new_content=json.dumps({"name": "demo-v2", "scripts": {"test": "jest"}}),
            )
        )

        # ── Task 7: Poetry Python ─────────────────────────────────────────
        d7 = base / "poetry_python"
        d7.mkdir()
        _make_poetry_python(d7)
        results.append(_run_task("T07_poetry_python", d7))

        # ── Task 8: pure Node (yarn) ──────────────────────────────────────
        d8 = base / "pure_node_yarn"
        d8.mkdir()
        _make_pure_node_yarn(d8)
        results.append(_run_task("T08_pure_node_yarn", d8))

        # ── Task 9: static HTML (no package markers) ──────────────────────
        d9 = base / "static_html"
        d9.mkdir()
        _make_static_html(d9)
        results.append(_run_task("T09_static_html_no_markers", d9))

        # ── Task 10: mixed Python+Node with Makefile ──────────────────────
        d10 = base / "mixed_with_makefile"
        d10.mkdir()
        _make_mixed_project(d10)
        results.append(
            _run_task(
                "T10_mixed_python_node_makefile",
                d10,
                check_invalidation_file="Makefile",
                invalidation_new_content="build:\n\tnpm run build\ntest:\n\tpytest\n",
            )
        )

    # ── Print results ─────────────────────────────────────────────────────
    print("\n=== REPO MEMORY CHARACTERIZATION WINDOW — 10 tasks ===\n")
    passed = 0
    for r in results:
        ok = r.get("file_created") and r.get("cache_hit_after_write")
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        else:
            passed += 1
        print(f"[{status}] {r['task']}")
        print(f"  project_type      : {r.get('project_type')}")
        print(f"  package_manager   : {r.get('package_manager')}")
        print(f"  source_root       : {r.get('source_root')}")
        print(f"  test_root         : {r.get('test_root')}")
        print(f"  test_command      : {r.get('test_command')}")
        print(f"  build_command     : {r.get('build_command')}")
        print(f"  entry_points      : {r.get('entry_points')}")
        print(f"  known_config_files: {r.get('known_config_files')}")
        print(f"  file_created      : {r.get('file_created')}")
        print(f"  cache_hit         : {r.get('cache_hit_after_write')}")
        print(f"  stability_ok      : {r.get('stability_ok')}")
        print(f"  invalidation      : {r.get('invalidation_triggered')}")
        if "error" in r:
            print(f"  ERROR             : {r['error']}")
        print()

    print(f"=== SUMMARY: {passed}/{len(results)} passed, {failed} failed ===\n")

    # Output machine-readable JSON for report generation.
    output_path = (
        Path(__file__).resolve().parents[2]
        / "docs/roadmap/reports/maintenance"
        / "transition_to_project_aware_continuation_execution"
        / "slices_B_repo_memory"
        / "artifacts"
        / "repo_memory_characterization_results.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"Results written to: {output_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
