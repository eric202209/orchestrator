from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from celery.exceptions import DuplicateNodenameWarning

from scripts.maintenance.stage_r import task_snapshot_adapter as adapter
from scripts.maintenance.stage_r.preflight import _snapshot_task_status

WORKER = "celery@stage-r-host"
NOW = datetime(2026, 10, 21, 1, 2, 3, tzinfo=timezone.utc)


def _task(task_id: str = "task-123", name: str = "app.tasks.worker.execute") -> dict:
    return {
        "id": task_id,
        "name": name,
        "args": ["SECRET_CANARY_ARGUMENT"],
        "kwargs": {"credential": "SECRET_CANARY_KWARG"},
        "payload": "SECRET_CANARY_PAYLOAD",
    }


class FakeInspect:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.active_safe_flags: list[bool] = []

    def _response(self, category: str) -> object:
        self.calls.append(category)
        value = self.responses[category]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return value

    def active(self, safe: bool = False) -> object:
        self.active_safe_flags.append(safe)
        return self._response("active")

    def reserved(self) -> object:
        return self._response("reserved")

    def scheduled(self) -> object:
        return self._response("scheduled")


def _empty_responses() -> dict[str, object]:
    return {category: {WORKER: []} for category in adapter.CATEGORIES}


def _collect(
    responses: dict[str, object] | None = None,
    *,
    expected_nodes: tuple[str, ...] = (WORKER,),
    census: adapter.ProcessCensus | None = None,
    monotonic=None,
) -> tuple[adapter.CollectionResult, FakeInspect]:
    inspector = FakeInspect(responses or _empty_responses())
    calls = 0

    def census_provider() -> adapter.ProcessCensus:
        nonlocal calls
        calls += 1
        if census is not None:
            return census
        return adapter.ProcessCensus(True, expected_nodes)

    def inspect_factory(timeout: float) -> FakeInspect:
        if timeout != adapter.CONTROL_TIMEOUT_SECONDS:
            raise AssertionError("incorrect bounded timeout")
        return inspector

    result = adapter.collect_snapshot(
        expected_worker_nodes=expected_nodes,
        topology_confirmation_ref="topology-record-39",
        control_traffic_approved=True,
        inspect_factory=inspect_factory,
        process_census_provider=census_provider,
        utc_now=lambda: NOW,
        monotonic=monotonic or (lambda: 10.0 + calls),
    )
    return result, inspector


class StageRTaskSnapshotAdapterTests(unittest.TestCase):
    def test_one_expected_worker_with_empty_categories(self) -> None:
        result, inspector = _collect()

        self.assertEqual(inspector.calls, list(adapter.CATEGORIES))
        self.assertEqual(inspector.active_safe_flags, [True])
        self.assertEqual(
            result.snapshot,
            {
                "snapshot_schema_version": 1,
                "captured_at": "2026-10-21T01:02:03.000000Z",
                "active": [],
                "reserved": [],
                "scheduled": [],
            },
        )
        self.assertEqual(
            [
                call["completion_status"]
                for call in result.evidence["observed_worker_set_per_call"]
            ],
            ["COMPLETE", "COMPLETE", "COMPLETE"],
        )

    def test_active_task_is_sanitized(self) -> None:
        responses = _empty_responses()
        responses["active"] = {WORKER: [_task()]}

        result, _ = _collect(responses)

        row = result.snapshot["active"][0]
        self.assertEqual(row["worker_node"], WORKER)
        self.assertEqual(
            row["task_name_sha256"],
            hashlib.sha256(b"app.tasks.worker.execute").hexdigest(),
        )
        self.assertIn("task_id_sha256", row)
        self.assertNotIn("args", row)
        self.assertNotIn("kwargs", row)

    def test_reserved_task_is_sanitized(self) -> None:
        responses = _empty_responses()
        responses["reserved"] = {WORKER: [_task("reserved-id")]}

        result, _ = _collect(responses)

        self.assertEqual(len(result.snapshot["reserved"]), 1)
        self.assertEqual(result.snapshot["reserved"][0]["worker_node"], WORKER)

    def test_scheduled_task_uses_only_nested_request_identity(self) -> None:
        responses = _empty_responses()
        responses["scheduled"] = {
            WORKER: [
                {
                    "eta": "2026-10-21T01:10:00Z",
                    "priority": 3,
                    "request": _task("scheduled-id"),
                }
            ]
        }

        result, _ = _collect(responses)

        row = result.snapshot["scheduled"][0]
        self.assertEqual(row["worker_node"], WORKER)
        self.assertNotIn("eta", row)
        self.assertNotIn("priority", row)
        self.assertNotIn("payload", row)

    def test_missing_worker_reply_fails_closed(self) -> None:
        responses = _empty_responses()
        responses["active"] = {}

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "missing_worker_response"
        ):
            _collect(responses)

    def test_extra_worker_reply_fails_closed(self) -> None:
        responses = _empty_responses()
        responses["active"] = {WORKER: [], "celery@unexpected": []}

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "unexpected_worker_response"
        ):
            _collect(responses)

    def test_duplicate_worker_identity_warning_fails_closed(self) -> None:
        def duplicate_reply() -> dict[str, list]:
            warnings.warn(DuplicateNodenameWarning("duplicate node"))
            return {WORKER: []}

        responses = _empty_responses()
        responses["active"] = duplicate_reply

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "duplicate_worker_identity"
        ):
            _collect(responses)

    def test_duplicate_local_worker_identity_fails_before_control(self) -> None:
        inspector = FakeInspect(_empty_responses())

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "duplicate_local_worker_identity"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(
                    True, (WORKER, WORKER)
                ),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )
        self.assertEqual(inspector.calls, [])

    def test_timeout_fails_without_retry(self) -> None:
        responses = _empty_responses()
        responses["reserved"] = TimeoutError("SECRET_CANARY_TIMEOUT_DETAIL")
        inspector = FakeInspect(responses)

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "remote_control_timeout"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(True, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )
        self.assertEqual(inspector.calls, ["active", "reserved"])

    def test_malformed_response_fails_closed(self) -> None:
        responses = _empty_responses()
        responses["active"] = ["not", "a", "mapping"]

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "malformed_worker_response"
        ):
            _collect(responses)

    def test_missing_category_fails_closed(self) -> None:
        class MissingScheduled:
            def active(self, safe: bool = False):
                return {WORKER: []}

            def reserved(self):
                return {WORKER: []}

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "missing_category"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: MissingScheduled(),
                process_census_provider=lambda: adapter.ProcessCensus(True, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )

    def test_non_list_category_fails_closed(self) -> None:
        responses = _empty_responses()
        responses["active"] = {WORKER: "empty"}

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "non_list_category_response"
        ):
            _collect(responses)

    def test_partial_worker_response_fails_closed(self) -> None:
        second = "celery@stage-r-host-2"
        responses = _empty_responses()
        responses["active"] = {WORKER: []}

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "partial_worker_response"
        ):
            _collect(responses, expected_nodes=(WORKER, second))

    def test_excessive_collection_duration_fails_closed(self) -> None:
        class FakeClock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = FakeClock()
        inspector = FakeInspect(_empty_responses())
        original_active = inspector.active

        def slow_active(safe: bool = False) -> object:
            result = original_active(safe=safe)
            clock.value = adapter.MAX_COLLECTION_DURATION_SECONDS + 1
            return result

        inspector.active = slow_active  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "excessive_collection_duration"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(True, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=clock,
            )

    def test_secret_canaries_are_not_persisted(self) -> None:
        responses = _empty_responses()
        responses["active"] = {WORKER: [_task()]}
        result, _ = _collect(responses)

        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path, evidence_path = adapter.write_output_bundle(
                Path(temporary) / "snapshot-output", result.snapshot, result.evidence
            )
            persisted = snapshot_path.read_bytes() + evidence_path.read_bytes()

        for canary in (
            b"SECRET_CANARY_ARGUMENT",
            b"SECRET_CANARY_KWARG",
            b"SECRET_CANARY_PAYLOAD",
        ):
            self.assertNotIn(canary, persisted)

    def test_output_symlink_is_rejected(self) -> None:
        result, _ = _collect()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            output = root / "output-link"
            output.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(
                adapter.SnapshotCollectionError, "output_symlink_rejected"
            ):
                adapter.write_output_bundle(output, result.snapshot, result.evidence)

    def test_existing_output_is_refused(self) -> None:
        result, _ = _collect()

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing"
            output.mkdir()
            with self.assertRaisesRegex(
                adapter.SnapshotCollectionError, "output_exists"
            ):
                adapter.write_output_bundle(output, result.snapshot, result.evidence)

    def test_atomic_output_publishes_private_bundle_only_when_complete(self) -> None:
        result, _ = _collect()
        original_write = adapter._write_private_file

        def fail_on_evidence(directory_fd: int, name: str, payload: bytes) -> None:
            if name == "task-snapshot-evidence.json":
                raise adapter.SnapshotCollectionError("synthetic_write_failure")
            original_write(directory_fd, name, payload)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "atomic-output"
            with patch.object(
                adapter, "_write_private_file", side_effect=fail_on_evidence
            ):
                with self.assertRaisesRegex(
                    adapter.SnapshotCollectionError, "synthetic_write_failure"
                ):
                    adapter.write_output_bundle(
                        output, result.snapshot, result.evidence
                    )
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])

            snapshot_path, evidence_path = adapter.write_output_bundle(
                output, result.snapshot, result.evidence
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o600)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(
                evidence["snapshot_sha256"],
                hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            )

    def test_missing_process_census_is_inconclusive(self) -> None:
        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "process_census_incomplete"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: FakeInspect(_empty_responses()),
                process_census_provider=lambda: adapter.ProcessCensus(False, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )

    def test_unexpected_local_worker_fails_before_control(self) -> None:
        inspector = FakeInspect(_empty_responses())

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "unexpected_local_worker_master"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(
                    True, (WORKER, "celery@unexpected")
                ),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )
        self.assertEqual(inspector.calls, [])

    def test_snapshot_matches_existing_preflight_parser(self) -> None:
        result, _ = _collect()

        parsed = _snapshot_task_status(
            result.snapshot,
            now=datetime(2026, 10, 21, 1, 2, 3, tzinfo=timezone.utc),
        )

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["counts"], {"active": 0, "reserved": 0, "scheduled": 0})

    def test_missing_topology_confirmation_is_inconclusive(self) -> None:
        inspector = FakeInspect(_empty_responses())

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "broker_topology_unverified"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref=None,
                control_traffic_approved=True,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(True, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )
        self.assertEqual(inspector.calls, [])

    def test_control_traffic_requires_explicit_approval(self) -> None:
        inspector = FakeInspect(_empty_responses())

        with self.assertRaisesRegex(
            adapter.SnapshotCollectionError, "redis_control_approval_required"
        ):
            adapter.collect_snapshot(
                expected_worker_nodes=(WORKER,),
                topology_confirmation_ref="topology-record-39",
                control_traffic_approved=False,
                inspect_factory=lambda _timeout: inspector,
                process_census_provider=lambda: adapter.ProcessCensus(True, (WORKER,)),
                utc_now=lambda: NOW,
                monotonic=lambda: 1.0,
            )
        self.assertEqual(inspector.calls, [])


if __name__ == "__main__":
    unittest.main()
