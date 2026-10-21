"""Offline fixture tests for bounded Phase 39 P0B metadata resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from scripts.maintenance.phase39_p0b.worker_metadata_resolution import (
    AUTHORIZED_HOST,
    AUTHORIZED_ORIGIN,
    BoundedPyPIMetadataClient,
    DirectRequirement,
    MetadataOnlyResolver,
    MetadataResolutionError,
    MetadataUnavailable,
    build_document,
    canonical_digest,
    canonical_json_bytes,
    evaluate_requirement_marker,
    load_authoritative_inputs,
    merge_attempt_history,
    target_wheel_tags,
    validate_resolution_document,
    _parse_wheel_tags,
    _wp2a_tag_supported,
)


ROOT = Path(__file__).resolve().parents[2]
P0B = ROOT / "scripts" / "maintenance" / "phase39_p0b"
CONFIG_PATH = P0B / "worker-metadata-resolution-config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
TARGET = CONFIG["target"]
MARKERS = TARGET["marker_environment"]


def wheel_file(
    name: str,
    version: str,
    *,
    tag: str = "py3-none-any",
    sha256: str | None = None,
    requires_python: str | None = None,
    yanked: bool = False,
) -> dict[str, Any]:
    filename = f"{name.replace('-', '_')}-{version}-{tag}.whl"
    record: dict[str, Any] = {
        "filename": filename,
        "packagetype": "bdist_wheel",
        "digests": {"sha256": sha256 or hashlib.sha256(filename.encode()).hexdigest()},
        "requires_python": requires_python,
        "upload_time_iso_8601": "2026-10-21T12:00:00Z",
        "yanked": yanked,
        # This URL is deliberately present as index metadata. The resolver must
        # never request or follow it.
        "url": "https://files.pythonhosted.org/packages/fixture/" + filename,
    }
    if sha256 == "MISSING":
        record["digests"] = {}
    return record


def metadata_payload(
    name: str,
    version: str,
    *,
    requires_dist: list[str] | None = None,
    requires_python: str | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    actual_files = files or [wheel_file(name, version)]
    return {
        "info": {
            "name": name,
            "version": version,
            "metadata_version": "2.4",
            "requires_python": requires_python,
            "requires_dist": requires_dist,
            "provides_extra": [],
            "license": "MIT",
            "license_expression": None,
            "license_files": [],
            "classifiers": ["License :: OSI Approved :: MIT License"],
        },
        "urls": actual_files,
    }


class FixtureMetadataClient:
    def __init__(self, packages: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.packages = packages
        self.evidence: list[dict[str, Any]] = []
        self.redirect_attempts: list[dict[str, Any]] = []
        self.request_urls: list[str] = []

    def fetch_json(self, name: str, version: str | None = None):
        name = name.lower().replace("_", "-")
        if version is None:
            payload = self.packages[name]["project"]
            url = f"{AUTHORIZED_ORIGIN}/pypi/{name}/json"
        else:
            payload = self.packages[name]["versions"][version]
            url = f"{AUTHORIZED_ORIGIN}/pypi/{name}/{version}/json"
        self.request_urls.append(url)
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        evidence = {
            "url": url,
            "host": AUTHORIZED_HOST,
            "method": "GET",
            "response_status": 200,
            "retrieved_at_utc": "2026-10-21T18:00:00Z",
            "response_bytes": len(canonical_json_bytes(payload)),
            "response_sha256": digest,
            "content_type": "application/json",
            "redirect_location": None,
            "redirect_followed": False,
            "error": None,
        }
        self.evidence.append(evidence)
        return copy.deepcopy(payload), evidence


def fixture_index(
    name: str, releases: dict[str, tuple[list[str] | None, str | None, bool]]
) -> dict[str, dict[str, Any]]:
    release_files: dict[str, list[dict[str, Any]]] = {}
    version_payloads: dict[str, dict[str, Any]] = {}
    for version, (requires_dist, requires_python, yanked) in releases.items():
        file = wheel_file(name, version, requires_python=requires_python, yanked=yanked)
        release_files[version] = [file]
        version_payloads[version] = metadata_payload(
            name,
            version,
            requires_dist=requires_dist,
            requires_python=requires_python,
            files=[file],
        )
    latest = max(
        releases, key=lambda value: __import__("packaging").version.Version(value)
    )
    return {
        "project": {
            "info": version_payloads[latest]["info"],
            "urls": version_payloads[latest]["urls"],
            "releases": release_files,
        },
        "versions": version_payloads,
    }


def resolver_for(
    packages: dict[str, dict[str, dict[str, Any]]],
) -> tuple[MetadataOnlyResolver, FixtureMetadataClient]:
    client = FixtureMetadataClient(packages)
    return (
        MetadataOnlyResolver(target=TARGET, marker_environment=MARKERS, client=client),
        client,
    )


class MetadataOnlyDependencyResolutionTests(unittest.TestCase):
    def test_deterministic_selection_prefers_highest_stable_candidate(self) -> None:
        resolver, _client = resolver_for(
            {
                "sample": fixture_index(
                    "sample",
                    {
                        "1.0.0": ([], None, False),
                        "2.0.0": ([], None, False),
                        "3.0.0rc1": ([], None, False),
                    },
                )
            }
        )
        result = resolver.resolve(
            [DirectRequirement("sample", ">=1", (), "sample>=1", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        self.assertEqual(result["packages"][0]["version"], "2.0.0")

    def test_source_digest_binding_rejects_modified_authoritative_input(self) -> None:
        changed = copy.deepcopy(CONFIG)
        changed["source_bindings"][
            "scripts/maintenance/phase39_p0b/worker-requirements.in"
        ] = ("0" * 64)
        with self.assertRaisesRegex(
            MetadataResolutionError, "source_binding_configuration_mismatch"
        ):
            load_authoritative_inputs(ROOT, changed)

    def test_bcrypt_override_is_separate_from_authoritative_requirement(self) -> None:
        direct, _inputs, _bindings = load_authoritative_inputs(ROOT, CONFIG)
        bcrypt = next(item for item in direct if item.name == "bcrypt")
        tzdata = next(item for item in direct if item.name == "tzdata")
        self.assertEqual(bcrypt.specifier, "")
        self.assertEqual(bcrypt.original, "bcrypt")
        self.assertEqual(CONFIG["candidate_overrides"][0]["specifier"], "==5.0.0")
        self.assertIs(
            CONFIG["candidate_overrides"][0]["source_requirement_unchanged"], True
        )
        self.assertEqual(tzdata.specifier, "")
        self.assertIsNone(CONFIG.get("direct_requirement_removals"))

    def test_target_marker_evaluation_and_parent_extra_propagation(self) -> None:
        active, decisions = evaluate_requirement_marker(
            'extra == "redis" and python_version >= "3.12"',
            MARKERS,
            ["", "redis"],
        )
        self.assertTrue(active)
        self.assertEqual(
            decisions,
            [
                {"extra": "", "result": False},
                {"extra": "redis", "result": True},
            ],
        )
        with self.assertRaisesRegex(
            MetadataResolutionError, "target_marker_environment_incomplete"
        ):
            evaluate_requirement_marker('platform_release == "6.8"', MARKERS, [""])

    def test_extras_propagate_transitively_even_when_parent_sorts_after_child(
        self,
    ) -> None:
        packages = {
            "z-parent": fixture_index(
                "z-parent",
                {
                    "1.0": (
                        [
                            'featuredep>=1; extra == "feature"',
                            "a-child[fast]>=1",
                        ],
                        None,
                        False,
                    )
                },
            ),
            "a-child": fixture_index(
                "a-child",
                {"1.0": (['fastdep>=1; extra == "fast"'], None, False)},
            ),
            "featuredep": fixture_index("featuredep", {"1.0": ([], None, False)}),
            "fastdep": fixture_index("fastdep", {"1.0": ([], None, False)}),
            "unused": fixture_index("unused", {"1.0": ([], None, False)}),
        }
        resolver, _client = resolver_for(packages)
        result = resolver.resolve(
            [DirectRequirement("z-parent", "", ("feature",), "z-parent[feature]", 1)],
            {},
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        names = {item["name"] for item in result["packages"]}
        self.assertEqual(names, {"z-parent", "a-child", "featuredep", "fastdep"})
        child = next(item for item in result["packages"] if item["name"] == "a-child")
        self.assertEqual(child["requested_extras"], ["fast"])
        fast_edge = next(
            item
            for item in result["dependency_evaluations"]
            if item["parent"] == "a-child"
        )
        self.assertTrue(fast_edge["active"])
        self.assertEqual(
            fast_edge["marker_evaluations"],
            [{"extra": "", "result": False}, {"extra": "fast", "result": True}],
        )

    def test_requires_python_rejects_latest_candidate_and_selects_compatible_wheel(
        self,
    ) -> None:
        resolver, _client = resolver_for(
            {
                "sample": fixture_index(
                    "sample",
                    {
                        "1.0": ([], ">=3.12", False),
                        "2.0": ([], ">=3.13", False),
                    },
                )
            }
        )
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        self.assertEqual(result["packages"][0]["version"], "1.0")
        self.assertTrue(
            any(
                row["reason"] == "REQUIRES_PYTHON_REJECTED"
                for row in resolver.candidate_rejections
            )
        )

    def test_target_wheel_compatibility_enforces_arch_abi_and_glibc(self) -> None:
        self.assertEqual(
            target_wheel_tags(
                "crypto-1.0-cp312-cp312-manylinux_2_39_aarch64.whl", TARGET
            ),
            ["cp312-cp312-manylinux_2_39_aarch64"],
        )
        self.assertEqual(
            target_wheel_tags("crypto-1.0-cp36-abi3-manylinux2014_aarch64.whl", TARGET),
            ["cp36-abi3-manylinux2014_aarch64"],
        )
        self.assertEqual(
            target_wheel_tags("sample-1.0-py3-none-any.whl", TARGET),
            ["py3-none-any"],
        )
        universal_python_2_3 = "tzdata-2026.5-py2.py3-none-any.whl"
        self.assertEqual(
            target_wheel_tags(universal_python_2_3, TARGET), ["py3-none-any"]
        )
        self.assertFalse(
            all(
                _wp2a_tag_supported(str(tag))
                for tag in _parse_wheel_tags(universal_python_2_3)
            )
        )
        for filename in (
            "crypto-1.0-cp312-cp312-manylinux_2_40_aarch64.whl",
            "crypto-1.0-cp312-cp312-manylinux_2_39_x86_64.whl",
            "crypto-1.0-cp311-cp311-manylinux_2_39_aarch64.whl",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(target_wheel_tags(filename, TARGET), [])

    def test_yanked_candidate_is_rejected(self) -> None:
        resolver, _client = resolver_for(
            {"sample": fixture_index("sample", {"1.0": ([], None, True)})}
        )
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_INCOMPLETE")
        self.assertEqual(
            result["unresolved"][0]["classification"], "RESOLUTION_CONFLICT"
        )
        self.assertTrue(
            any(
                row["file_rejection_counts"]["yanked_files"]
                for row in resolver.candidate_rejections
            )
        )

    def test_missing_target_wheel_is_classified_explicitly(self) -> None:
        index = fixture_index("sample", {"1.0": ([], None, False)})
        x86 = wheel_file("sample", "1.0")
        x86["filename"] = "sample-1.0-cp312-cp312-manylinux_2_39_x86_64.whl"
        index["project"]["releases"]["1.0"] = [x86]
        index["project"]["urls"] = [x86]
        index["versions"]["1.0"]["urls"] = [x86]
        resolver, _client = resolver_for({"sample": index})
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_INCOMPLETE")
        self.assertEqual(
            result["unresolved"][0]["classification"], "NO_COMPATIBLE_TARGET_WHEEL"
        )

    def test_missing_dependency_metadata_is_not_treated_as_empty_closure(self) -> None:
        packages = fixture_index("sample", {"1.0": ([], None, False)})
        del packages["project"]["info"]["requires_dist"]
        resolver, _client = resolver_for({"sample": packages})
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_INCOMPLETE")
        self.assertEqual(
            result["unresolved"][0]["classification"], "METADATA_UNAVAILABLE"
        )

    def test_metadata_direct_url_dependency_is_rejected(self) -> None:
        resolver, client = resolver_for(
            {
                "sample": fixture_index(
                    "sample",
                    {
                        "1.0": (
                            [
                                "foreign @ https://files.pythonhosted.org/packages/foreign.whl"
                            ],
                            None,
                            False,
                        )
                    },
                ),
                "foreign": fixture_index("foreign", {"1.0": ([], None, False)}),
            }
        )
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_INCOMPLETE")
        self.assertEqual(
            result["unresolved"][0]["classification"], "METADATA_UNAVAILABLE"
        )
        self.assertFalse(any("foreign" in url for url in client.request_urls))

    def test_explicit_null_requires_dist_is_recorded_as_empty(self) -> None:
        packages = fixture_index("sample", {"1.0": ([], None, False)})
        packages["project"]["info"]["requires_dist"] = None
        packages["versions"]["1.0"]["info"]["requires_dist"] = None
        resolver, _client = resolver_for({"sample": packages})
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        self.assertEqual(result["packages"][0]["requires_dist"], [])
        self.assertIsNone(result["packages"][0]["requires_dist_metadata_value"])
        self.assertEqual(
            result["packages"][0]["requires_dist_interpretation"],
            "PYPI_JSON_NULL_MEANS_NO_REQUIRES_DIST_FIELDS",
        )

    def test_missing_index_advertised_wheel_hash_blocks_candidate(self) -> None:
        packages = fixture_index("sample", {"1.0": ([], None, False)})
        for payload in [packages["project"], packages["versions"]["1.0"]]:
            payload["urls"][0]["digests"] = {}
        packages["project"]["releases"]["1.0"][0]["digests"] = {}
        resolver, _client = resolver_for({"sample": packages})
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_INCOMPLETE")
        self.assertEqual(
            result["unresolved"][0]["classification"], "METADATA_UNAVAILABLE"
        )

    def test_backtracking_records_conflict_and_resolves_shared_constraint(self) -> None:
        packages = {
            "alpha": fixture_index(
                "alpha",
                {
                    "1.0": (["shared>=1,<3"], None, False),
                    "2.0": (["shared<2"], None, False),
                },
            ),
            "beta": fixture_index("beta", {"1.0": (["shared>=2"], None, False)}),
            "shared": fixture_index(
                "shared", {"1.0": ([], None, False), "2.0": ([], None, False)}
            ),
        }
        resolver, _client = resolver_for(packages)
        result = resolver.resolve(
            [
                DirectRequirement("alpha", "", (), "alpha", 1),
                DirectRequirement("beta", "", (), "beta", 2),
            ],
            {},
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        versions = {item["name"]: item["version"] for item in result["packages"]}
        self.assertEqual(versions, {"alpha": "1.0", "beta": "1.0", "shared": "2.0"})
        self.assertTrue(
            any(
                item["version"] == "2.0"
                for item in resolver.candidate_rejections
                if item["name"] == "alpha"
            )
        )

    def test_canonical_unresolved_ledger_reproduces_and_rejects_incomplete_graph(
        self,
    ) -> None:
        direct, inputs, bindings = load_authoritative_inputs(ROOT, CONFIG)
        from scripts.maintenance.phase39_p0b.worker_metadata_resolution import (
            MetadataOnlyResolver,
        )

        resolver, _client = resolver_for({})
        resolution = {
            "status": "METADATA_RESOLUTION_INCOMPLETE",
            "failure": "fixture:METADATA_UNAVAILABLE",
            "packages": [],
            "dependency_evaluations": [],
            "dependency_edges": [],
            "unresolved": [
                {"classification": "METADATA_UNAVAILABLE", "detail": "fixture"}
            ],
        }
        config_sha = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
        first = build_document(
            config=CONFIG,
            inputs=inputs,
            source_bindings=bindings,
            direct=direct,
            resolution=resolution,
            resolver=resolver,
            config_sha256=config_sha,
        )
        second = build_document(
            config=CONFIG,
            inputs=inputs,
            source_bindings=bindings,
            direct=direct,
            resolution=resolution,
            resolver=resolver,
            config_sha256=config_sha,
        )
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(first["manifest_sha256"], canonical_digest(first))
        merged_a = merge_attempt_history(first, [first, first])
        merged_b = merge_attempt_history(first, [first, first])
        self.assertEqual(canonical_json_bytes(merged_a), canonical_json_bytes(merged_b))
        self.assertEqual(len(merged_a["resolution_attempt_history"]), 3)
        incomplete = copy.deepcopy(first)
        incomplete["decision"] = "METADATA_RESOLUTION_COMPLETE"
        incomplete["unresolved"] = []
        incomplete["manifest_sha256"] = canonical_digest(incomplete)
        with self.assertRaisesRegex(
            MetadataResolutionError, "incomplete_graph_marked_complete"
        ):
            validate_resolution_document(incomplete)

    def test_document_rejects_missing_candidate_hash(self) -> None:
        direct, inputs, bindings = load_authoritative_inputs(ROOT, CONFIG)
        resolver, _client = resolver_for({})
        document = build_document(
            config=CONFIG,
            inputs=inputs,
            source_bindings=bindings,
            direct=direct,
            resolution={
                "status": "METADATA_RESOLUTION_INCOMPLETE",
                "failure": "fixture",
                "packages": [],
                "dependency_evaluations": [],
                "dependency_edges": [],
                "unresolved": [
                    {"classification": "METADATA_UNAVAILABLE", "detail": "fixture"}
                ],
            },
            resolver=resolver,
            config_sha256=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        )
        bad = copy.deepcopy(document)
        bad["packages"] = [
            {
                "name": "sample",
                "version": "1.0",
                "requires_python": None,
                "requires_dist": [],
                "distribution_metadata": {
                    "identity_sha256": "a" * 64,
                    "response_sha256": "b" * 64,
                    "endpoint": AUTHORIZED_ORIGIN + "/pypi/sample/json",
                },
                "project_listing": {
                    "endpoint": AUTHORIZED_ORIGIN + "/pypi/sample/json",
                    "response_sha256": "b" * 64,
                },
                "wheel": {
                    "filename": "sample-1.0-py3-none-any.whl",
                    "sha256": None,
                    "index_advertised_sha256": None,
                    "tags": ["py3-none-any"],
                    "requires_python": None,
                    "yanked": False,
                    "artifact_url_requested": False,
                    "artifact_bytes_acquired": False,
                },
                "yanked": False,
            }
        ]
        bad["manifest_sha256"] = canonical_digest(bad)
        with self.assertRaisesRegex(
            MetadataResolutionError, "candidate_wheel_hash_or_filename_missing"
        ):
            validate_resolution_document(bad)

    def test_unauthorized_hosts_redirects_and_artifact_paths_are_rejected(self) -> None:
        for target in (
            "https://files.pythonhosted.org/packages/sample.whl",
            "/packages/sample.whl",
            "/pypi/sample/json?redirect=https://files.pythonhosted.org",
        ):
            with self.subTest(target=target), self.assertRaises(
                MetadataResolutionError
            ):
                BoundedPyPIMetadataClient.validate_request_target(target)

        class Response:
            status = 302

            def getheader(self, name: str):
                return {
                    "Location": "https://files.pythonhosted.org/packages/sample.whl",
                    "Content-Type": "text/plain",
                }.get(name)

            def read(self, limit: int):
                return b"redirect"

        class Connection:
            def __init__(self, host: str, **kwargs: Any) -> None:
                self.host = host
                self.requested: list[tuple[str, str]] = []
                self.closed = False

            def request(
                self, method: str, target: str, headers: dict[str, str]
            ) -> None:
                self.requested.append((method, target))

            def getresponse(self):
                return Response()

            def close(self) -> None:
                self.closed = True

        connections: list[Connection] = []

        def factory(host: str, **kwargs: Any) -> Connection:
            connection = Connection(host, **kwargs)
            connections.append(connection)
            return connection

        client = BoundedPyPIMetadataClient(connection_factory=factory)
        with self.assertRaisesRegex(MetadataUnavailable, "redirect_rejected"):
            client.fetch_json("sample")
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].host, AUTHORIZED_HOST)
        self.assertEqual(connections[0].requested, [("GET", "/pypi/sample/json")])
        self.assertTrue(connections[0].closed)
        self.assertEqual(len(client.redirect_attempts), 1)
        self.assertFalse(client.redirect_attempts[0]["followed"])

    def test_resolution_never_requests_artifact_urls_or_acquires_wheel_bytes(
        self,
    ) -> None:
        resolver, client = resolver_for(
            {"sample": fixture_index("sample", {"1.0": ([], None, False)})}
        )
        result = resolver.resolve(
            [DirectRequirement("sample", "", (), "sample", 1)], {}
        )
        self.assertEqual(result["status"], "METADATA_RESOLUTION_COMPLETE")
        self.assertTrue(
            all(
                url.startswith(AUTHORIZED_ORIGIN + "/pypi/")
                for url in client.request_urls
            )
        )
        self.assertFalse(
            any("files.pythonhosted.org" in url for url in client.request_urls)
        )
        self.assertFalse(result["packages"][0]["wheel"]["artifact_bytes_acquired"])
        self.assertNotIn("url", result["packages"][0]["wheel"])

    def test_production_lock_promotion_is_rejected(self) -> None:
        direct, inputs, bindings = load_authoritative_inputs(ROOT, CONFIG)
        resolver, _client = resolver_for({})
        document = build_document(
            config=CONFIG,
            inputs=inputs,
            source_bindings=bindings,
            direct=direct,
            resolution={
                "status": "METADATA_RESOLUTION_INCOMPLETE",
                "failure": "fixture",
                "packages": [],
                "dependency_evaluations": [],
                "dependency_edges": [],
                "unresolved": [
                    {"classification": "METADATA_UNAVAILABLE", "detail": "fixture"}
                ],
            },
            resolver=resolver,
            config_sha256=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        )
        promoted = copy.deepcopy(document)
        promoted["production_lock"] = True
        promoted["manifest_sha256"] = canonical_digest(promoted)
        with self.assertRaisesRegex(
            MetadataResolutionError, "production_lock_promotion_forbidden"
        ):
            validate_resolution_document(promoted)

    def test_unapproved_contact_in_resolution_evidence_is_rejected(self) -> None:
        direct, inputs, bindings = load_authoritative_inputs(ROOT, CONFIG)
        resolver, _client = resolver_for({})
        document = build_document(
            config=CONFIG,
            inputs=inputs,
            source_bindings=bindings,
            direct=direct,
            resolution={
                "status": "METADATA_RESOLUTION_INCOMPLETE",
                "failure": "fixture",
                "packages": [],
                "dependency_evaluations": [],
                "dependency_edges": [],
                "unresolved": [
                    {"classification": "METADATA_UNAVAILABLE", "detail": "fixture"}
                ],
            },
            resolver=resolver,
            config_sha256=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        )
        bad = copy.deepcopy(document)
        bad["network_evidence"]["hosts_contacted"] = ["files.pythonhosted.org"]
        bad["manifest_sha256"] = canonical_digest(bad)
        with self.assertRaisesRegex(
            MetadataResolutionError, "unauthorized_host_contacted"
        ):
            validate_resolution_document(bad)


if __name__ == "__main__":
    unittest.main()
