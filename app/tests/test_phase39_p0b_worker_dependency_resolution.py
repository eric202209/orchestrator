"""Offline source-only regressions for Phase 39 P0B WP2B1 inputs."""

from __future__ import annotations

import builtins
import json
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b.worker_dependency_resolution import (
    WorkerDependencyInventoryError,
    build_import_graph,
    canonical_json_bytes,
    discover_worker_roots,
    evaluate_target_marker,
    map_import_roots_to_distributions,
    parse_requirement_source,
    source_digest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PHASE39_ROOT = REPOSITORY_ROOT / "scripts/maintenance/phase39_p0b"


class WorkerDependencyResolutionTests(unittest.TestCase):
    def _fixture(self, root: Path, *, autodiscovered: bool = False) -> None:
        package = root / "app/tasks"
        package.mkdir(parents=True)
        (root / "app/__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (root / "app/celery_app.py").write_text(
            "from celery import Celery\n"
            "celery_app = Celery('x', include=['app.tasks.worker', 'app.tasks.maintenance'])\n"
            "celery_app.autodiscover_tasks(['app.tasks'])\n",
            encoding="utf-8",
        )
        (package / "worker.py").write_text(
            "from app.tasks.worker_support import run_task\n", encoding="utf-8"
        )
        (package / "maintenance.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "worker_support.py").write_text(
            "def run_task(): pass\n", encoding="utf-8"
        )
        if autodiscovered:
            (package / "tasks.py").write_text("VALUE = 2\n", encoding="utf-8")

    def test_worker_import_root_discovery_reads_celery_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            discovered = discover_worker_roots(root)
        self.assertEqual(discovered.celery_application, "app.celery_app")
        self.assertEqual(
            discovered.explicit_task_modules,
            ("app.tasks.worker", "app.tasks.maintenance"),
        )
        self.assertEqual(discovered.autodiscovery_candidates, ("app.tasks.tasks",))
        self.assertEqual(discovered.existing_autodiscovery_modules, ())

    def test_explicit_and_autodiscovered_task_modules_enter_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root, autodiscovered=True)
            graph = build_import_graph(root)
        self.assertIn("app.tasks.worker", graph.local_modules)
        self.assertIn("app.tasks.maintenance", graph.local_modules)
        self.assertIn("app.tasks.tasks", graph.local_modules)
        self.assertIn("app.tasks.worker_support", graph.local_modules)

    def test_production_dynamic_import_targets_are_classified(self) -> None:
        graph = build_import_graph(REPOSITORY_ROOT)
        self.assertEqual(len(graph.dynamic_imports), 2)
        self.assertTrue(all(site.static_targets for site in graph.dynamic_imports))
        self.assertTrue(
            all(
                site.classification == "STATIC_TARGETS"
                for site in graph.dynamic_imports
            )
        )
        self.assertTrue(
            all(
                target in graph.local_modules
                for site in graph.dynamic_imports
                for target in site.static_targets
            )
        )

    def test_unbounded_dynamic_import_is_reported_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            (root / "app/tasks/worker.py").write_text(
                "import importlib\n"
                "def load(plugin_name):\n"
                "    return importlib.import_module(plugin_name)\n",
                encoding="utf-8",
            )
            graph = build_import_graph(root)
        self.assertEqual(len(graph.dynamic_imports), 1)
        self.assertEqual(graph.dynamic_imports[0].classification, "UNRESOLVED")

    def test_known_third_party_import_roots_map_to_declarations(self) -> None:
        graph = build_import_graph(REPOSITORY_ROOT)
        mapped = map_import_roots_to_distributions(graph.third_party_imports)
        self.assertEqual(mapped["fastapi"], "fastapi")
        self.assertEqual(mapped["sqlalchemy"], "sqlalchemy")
        self.assertEqual(mapped["bcrypt"], "bcrypt")
        self.assertEqual(mapped["tzdata"], "tzdata")

    def test_required_declarations_are_present_in_worker_source(self) -> None:
        ledger = json.loads(
            (PHASE39_ROOT / "worker-dependency-classification.json").read_bytes()
        )
        source = (PHASE39_ROOT / "worker-requirements.in").read_bytes()
        declarations = parse_requirement_source(source)
        by_name = {item["name"]: item for item in declarations}
        for item in ledger["classifications"]:
            if item["classification"] not in {
                "REQUIRED_WORKER",
                "CONDITIONALLY_REQUIRED",
            }:
                continue
            self.assertIn(item["name"], by_name)
            if item["original_declaration"] is not None:
                self.assertEqual(
                    by_name[item["name"]]["original_declaration"],
                    item["original_declaration"],
                )
        self.assertIn("fastapi", by_name)  # API-shaped dependency is Worker-reachable.
        self.assertIn("celery", by_name)

    def test_development_and_worker_external_only_declarations_are_excluded(
        self,
    ) -> None:
        ledger = json.loads(
            (PHASE39_ROOT / "worker-dependency-classification.json").read_bytes()
        )
        declarations = parse_requirement_source(
            (PHASE39_ROOT / "worker-requirements.in").read_bytes()
        )
        included = {item["name"] for item in declarations}
        excluded = {
            item["name"]
            for item in ledger["classifications"]
            if item["classification"] == "NOT_REQUIRED_FOR_WORKER"
        }
        self.assertTrue(
            {"pytest", "pytest-asyncio", "pytest-cov", "black", "flake8"} <= excluded
        )
        self.assertTrue(
            {"uvicorn", "python-multipart", "requests", "gitpython", "passlib"}
            <= excluded
        )
        self.assertIn("bcrypt", included)
        self.assertFalse(included & excluded)

    def test_conditional_requirements_remain_explicit_in_source(self) -> None:
        ledger = json.loads(
            (PHASE39_ROOT / "worker-dependency-classification.json").read_bytes()
        )
        declarations = parse_requirement_source(
            (PHASE39_ROOT / "worker-requirements.in").read_bytes()
        )
        included = {item["name"] for item in declarations}
        conditional = {
            item["name"]
            for item in ledger["classifications"]
            if item["classification"] == "CONDITIONALLY_REQUIRED"
        }
        self.assertTrue({"langfuse", "pyyaml", "tzdata"} <= conditional)
        self.assertTrue(conditional <= included)

    def test_unknown_third_party_import_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            WorkerDependencyInventoryError, "third_party_import_unmapped"
        ):
            map_import_roots_to_distributions({"unreviewed_provider_sdk"})

    def test_source_digest_and_canonical_input_serialization_are_deterministic(
        self,
    ) -> None:
        source = (PHASE39_ROOT / "worker-requirements.in").read_bytes()
        self.assertEqual(source_digest(source), source_digest(source))
        self.assertEqual(
            canonical_json_bytes({"target": "cp312", "schema": 1}),
            canonical_json_bytes({"schema": 1, "target": "cp312"}),
        )
        inputs = json.loads(
            (PHASE39_ROOT / "worker-resolution-inputs.json").read_bytes()
        )
        by_path = {
            item["path"]: item["sha256"] for item in inputs["source_declarations"]
        }
        self.assertEqual(
            by_path["scripts/maintenance/phase39_p0b/worker-requirements.in"],
            source_digest(source),
        )

    def test_evidence_documents_are_canonical_and_digest_bound(self) -> None:
        graph_path = PHASE39_ROOT / "worker-import-graph.json"
        graph_bytes = graph_path.read_bytes()
        self.assertEqual(
            graph_bytes,
            canonical_json_bytes(build_import_graph(REPOSITORY_ROOT).as_dict()),
        )
        ledger_path = PHASE39_ROOT / "worker-dependency-classification.json"
        ledger_bytes = ledger_path.read_bytes()
        ledger = json.loads(ledger_bytes)
        self.assertEqual(ledger_bytes, canonical_json_bytes(ledger))
        self.assertEqual(
            ledger["worker_import_graph"]["sha256"], source_digest(graph_bytes)
        )
        inputs_path = PHASE39_ROOT / "worker-resolution-inputs.json"
        inputs_bytes = inputs_path.read_bytes()
        inputs = json.loads(inputs_bytes)
        self.assertEqual(inputs_bytes, canonical_json_bytes(inputs))
        sources = {
            item["path"]: item["sha256"] for item in inputs["source_declarations"]
        }
        for relative_path, expected_digest in sources.items():
            self.assertEqual(
                expected_digest,
                source_digest((REPOSITORY_ROOT / relative_path).read_bytes()),
                relative_path,
            )
        self.assertEqual(
            sources[
                "scripts/maintenance/phase39_p0b/worker-dependency-classification.json"
            ],
            source_digest(ledger_bytes),
        )
        self.assertEqual(
            sources["scripts/maintenance/phase39_p0b/worker-import-graph.json"],
            source_digest(graph_bytes),
        )

    def test_target_marker_evaluation_fails_closed_for_unknown_target_fields(
        self,
    ) -> None:
        self.assertIs(
            evaluate_target_marker(
                "python_version >= '3.12' and sys_platform == 'linux'"
            ),
            True,
        )
        self.assertIs(
            evaluate_target_marker("python_full_version >= '3.12.1'"),
            None,
        )
        inputs = json.loads(
            (PHASE39_ROOT / "worker-resolution-inputs.json").read_bytes()
        )
        self.assertEqual(inputs["target"]["platform"]["architecture"], "aarch64")
        self.assertEqual(inputs["target"]["python"]["abi"], "cp312")

    def test_inventory_has_no_network_install_or_application_import_side_effects(
        self,
    ) -> None:
        original_import = builtins.__import__
        with (
            patch.object(
                socket, "create_connection", side_effect=AssertionError("network")
            ) as connect,
            patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("network")
            ) as urlopen,
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("subprocess")
            ) as popen,
            patch.object(
                subprocess, "run", side_effect=AssertionError("subprocess")
            ) as run,
            patch.object(
                builtins,
                "__import__",
                side_effect=lambda name, *args, **kwargs: (
                    (_ for _ in ()).throw(AssertionError("application import"))
                    if name.startswith("app.")
                    else original_import(name, *args, **kwargs)
                ),
            ),
        ):
            graph = build_import_graph(REPOSITORY_ROOT)
            parse_requirement_source(
                (PHASE39_ROOT / "worker-requirements.in").read_bytes()
            )
        self.assertGreater(len(graph.local_modules), 300)
        connect.assert_not_called()
        urlopen.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
