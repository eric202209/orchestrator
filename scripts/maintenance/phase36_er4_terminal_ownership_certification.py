#!/usr/bin/env python3
"""PHASE36-MAINT-LA1-ER4 provider-free live infrastructure certification.

Proves the repaired terminal Planning ownership transfer across the real
database, Redis broker, Celery worker loop, FailureCoordinator and lifecycle
transitions, with no provider invocation:

    terminal      injected discovery terminal Planning result
                  -> FailureCoordinator -> logical failure -> generation fence
                  -> no continuation -> Celery FAILURE -> physical release
    recoverable   injected retryable Planning failure
                  -> recovering -> retry_pending -> durable marker BEFORE
                  publication; the publication is then deliberately lost
    br2           the lost-publication graph stays retry_pending with a valid
                  marker and the unchanged BR2 reconciler recognizes it
    stale         a stale delivery for an older generation cannot terminalize,
                  recover, or clear a successor

Isolation: a disposable synthetic Project/Session/Task under a temporary
workspace (never a ProductRoot), a dedicated broker database and a dedicated
worker process, so the production worker never sees these messages.  The
worker module registers exactly one task and the harness asserts the registered
task set before publishing anything.

Run with:
    venv/bin/python scripts/maintenance/phase36_er4_terminal_ownership_certification.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CERT_BROKER_DB = 11
ORCHESTRATION_TASK_NAME = "app.tasks.worker.execute_orchestration_task"
CERT_QUEUE = "er4_certification"
WORKER_MODULE = "scripts.maintenance.phase36_er4_certification_worker"
EVIDENCE_MESSAGE = "ER4 certification terminal handoff"


def _log(step: str, **fields) -> None:
    print(json.dumps({"step": step, **fields}, default=str), flush=True)


def _isolated_broker_url() -> str:
    base = os.environ.get("CELERY_BROKER_URL") or "redis://localhost:6379/0"
    return f"{base.rsplit('/', 1)[0]}/{CERT_BROKER_DB}"


class Certification:
    def __init__(self, *, keep: bool = False):
        self.keep = keep
        self.workspace = Path(tempfile.mkdtemp(prefix="er4-certification-"))
        self.project_ids: list[int] = []
        self.session_ids: list[int] = []
        self.worker: subprocess.Popen | None = None
        self.results: dict[str, object] = {}

    # -- infrastructure ---------------------------------------------------

    def start_worker(self) -> None:
        env = dict(os.environ)
        env["CELERY_BROKER_URL"] = _isolated_broker_url()
        env["CELERY_RESULT_BACKEND"] = _isolated_broker_url()
        env["PYTHONPATH"] = str(REPO_ROOT)
        self.worker = subprocess.Popen(
            [
                str(REPO_ROOT / "venv" / "bin" / "celery"),
                "-A",
                WORKER_MODULE,
                "worker",
                "--concurrency=1",
                "--loglevel=info",
                "-Q",
                CERT_QUEUE,
                "-n",
                "er4-certification@%h",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.time() + 90
        from scripts.maintenance.phase36_er4_certification_worker import app

        while time.time() < deadline:
            if self.worker.poll() is not None:
                raise RuntimeError(
                    f"certification worker exited: {self.worker.stdout.read()[-3000:]}"
                )
            replies = app.control.ping(timeout=2) or []
            if any("er4-certification" in list(reply)[0] for reply in replies if reply):
                self._assert_registered_task_set(app)
                _log("worker_ready", replies=replies)
                return
            time.sleep(1)
        raise RuntimeError("certification worker did not become ready")

    def _assert_registered_task_set(self, app) -> None:
        """Fail closed if the worker could run real orchestration work."""

        registered = app.control.inspect(timeout=10).registered() or {}
        names = sorted({name for names in registered.values() for name in names})
        production = [
            name
            for name in names
            if name.startswith("app.tasks.") and name != ORCHESTRATION_TASK_NAME
        ]
        digest = hashlib.sha256("\n".join(names).encode()).hexdigest()
        self.results["registered_tasks"] = names
        self.results["registered_task_set_sha256"] = digest
        _log("worker_registered_tasks", tasks=names, sha256=digest)
        if production:
            raise RuntimeError(
                f"certification worker registered production tasks: {production}"
            )

    def stop_worker(self) -> None:
        if self.worker is None:
            return
        try:
            os.killpg(os.getpgid(self.worker.pid), signal.SIGTERM)
            self.worker.wait(timeout=30)
        except Exception:
            try:
                os.killpg(os.getpgid(self.worker.pid), signal.SIGKILL)
            except Exception:
                pass
        _log("worker_stopped")

    # -- synthetic fixtures ----------------------------------------------

    def seed(self, db, label: str):
        from app.models import (
            Project,
            Session as SessionModel,
            SessionTask,
            Task,
            TaskExecution,
            TaskStatus,
        )

        root = self.workspace / label
        root.mkdir(parents=True, exist_ok=True)
        project = Project(
            name=f"ER4 certification {label} {int(time.time() * 1000)}",
            workspace_path=str(root),
        )
        session = SessionModel(
            project=project,
            name=f"ER4 certification session {label}",
            status="running",
            execution_mode="manual",
            is_active=True,
            instance_id=f"er4-cert-{label}-generation-1",
        )
        task = Task(
            project=project,
            title=f"ER4 certification task {label}",
            description="provider-free terminal ownership transfer certification",
            status=TaskStatus.RUNNING,
            task_subfolder=f"task-er4-{label}",
            workspace_status="isolated",
            plan_position=1,
        )
        link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
        execution = TaskExecution(
            session=session,
            task=task,
            attempt_number=1,
            status=TaskStatus.RUNNING,
            worker_pid=os.getpid(),
            worker_hostname=socket.gethostname(),
            worker_process_start_identity=f"er4-cert-{label}-owner",
            heartbeat_at=datetime.now(UTC),
        )
        db.add_all([project, session, task, link, execution])
        db.commit()
        for row in (project, session, task, link, execution):
            db.refresh(row)
        self.project_ids.append(project.id)
        self.session_ids.append(session.id)
        _log(
            "seeded",
            label=label,
            project_id=project.id,
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            instance_id=session.instance_id,
        )
        return project, session, task, link, execution

    # -- delivery ---------------------------------------------------------

    def publish(self, *, session, task, execution, mode, expected_instance_id=None):
        from scripts.maintenance.phase36_er4_certification_worker import app

        async_result = app.send_task(
            ORCHESTRATION_TASK_NAME,
            kwargs={
                "session_id": session.id,
                "task_id": task.id,
                "task_execution_id": execution.id,
                "expected_session_instance_id": (
                    expected_instance_id
                    if expected_instance_id is not None
                    else session.instance_id
                ),
                "mode": mode,
                "failure_reason": (
                    "discovery_output_not_json"
                    if mode == "terminal"
                    else "planning_json_error: retryable planning failure"
                ),
            },
            queue=CERT_QUEUE,
        )
        _log("published", mode=mode, celery_task_id=async_result.id)
        return async_result

    @staticmethod
    def _await_result(async_result, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if async_result.ready():
                return async_result.state
            time.sleep(1)
        raise RuntimeError("certification delivery never completed")

    def _evidence(self, db, session_id):
        from app.models import LogEntry

        db.rollback()
        rows = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session_id,
                LogEntry.message == EVIDENCE_MESSAGE,
            )
            .all()
        )
        payloads = []
        for row in rows:
            try:
                payloads.append(json.loads(row.log_metadata or "{}"))
            except ValueError:
                continue
        return payloads

    # -- scenarios --------------------------------------------------------

    def certify_terminal(self, db):
        """Section 32: terminal discovery result reaches logical closure."""

        from app.models import Session as SessionModel, TaskExecution, TaskStatus
        from app.services.orchestration.lifecycle.authority import (
            derive_lifecycle_authority,
        )

        _p, session, task, _link, execution = self.seed(db, "terminal")
        original_instance = session.instance_id
        execution_id = execution.id
        session_id = session.id

        result = self.publish(
            session=session, task=task, execution=execution, mode="terminal"
        )
        state = self._await_result(result)

        db.rollback()
        session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        execution = (
            db.query(TaskExecution).filter(TaskExecution.id == execution_id).one()
        )
        authority = derive_lifecycle_authority(
            db, session, task_id=task.id, latest_task_execution=execution
        )
        evidence = self._evidence(db, session_id)
        facts = {
            "celery_state": state,
            "session_status": session.status,
            "session_is_active": session.is_active,
            "generation_fenced": session.instance_id != original_instance,
            "continuation_task_id": session.continuation_task_id,
            "task_execution_status": str(execution.status),
            "logical_terminal": authority.logical_terminal,
            "quiescent": authority.quiescent,
            "continuation_pending": authority.continuation_pending,
            "worker_pid": execution.worker_pid,
            "worker_hostname": execution.worker_hostname,
            "worker_process_start_identity": execution.worker_process_start_identity,
            "heartbeat_at": execution.heartbeat_at,
            "provider_calls": sum(int(p.get("provider_calls", 0)) for p in evidence),
            "fc_invocations": len(evidence),
            "intermediate": evidence[0].get("intermediate") if evidence else None,
        }
        _log("terminal_case", **facts)
        assert state == "FAILURE", facts
        assert session.status == "failed", facts
        assert session.is_active is False, facts
        assert facts["generation_fenced"] is True, facts
        assert session.continuation_task_id is None, facts
        assert execution.status == TaskStatus.FAILED, facts
        assert authority.logical_terminal is True, facts
        assert authority.quiescent is True, facts
        assert authority.continuation_pending is False, facts
        assert execution.worker_pid is None, facts
        assert execution.worker_hostname is None, facts
        assert execution.worker_process_start_identity is None, facts
        assert execution.heartbeat_at is None, facts
        assert facts["provider_calls"] == 0, facts
        assert facts["fc_invocations"] == 1, facts
        self.results["terminal"] = facts
        return facts

    def certify_recoverable_and_br2(self, db):
        """Sections 33/34: durable marker before publication, then BR2."""

        from app.models import Session as SessionModel
        from app.services.orchestration.lifecycle import continuation_recovery as cr
        from app.services.orchestration.lifecycle.authority import (
            derive_lifecycle_authority,
        )
        from app.services.orchestration.lifecycle.transitions import (
            resolve_continuation_identity,
        )

        _p, session, task, _link, execution = self.seed(db, "recoverable")
        session_id = session.id

        result = self.publish(
            session=session, task=task, execution=execution, mode="recoverable"
        )
        state = self._await_result(result)

        db.rollback()
        session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        authority = derive_lifecycle_authority(db, session, task_id=task.id)
        identity = resolve_continuation_identity(db, session)
        evidence = self._evidence(db, session_id)
        facts = {
            "celery_state": state,
            "session_status": session.status,
            "continuation_kind": session.continuation_kind,
            "continuation_task_id": session.continuation_task_id,
            "continuation_retry_count": session.continuation_retry_count,
            "logical_terminal": authority.logical_terminal,
            "continuation_pending": authority.continuation_pending,
            "quiescent": authority.quiescent,
            "durable_identity": bool(identity),
            "provider_calls": sum(int(p.get("provider_calls", 0)) for p in evidence),
        }
        _log("recoverable_case", **facts)
        assert session.status == "retry_pending", facts
        assert identity is not None, facts
        assert authority.continuation_pending is True, facts
        assert authority.logical_terminal is False, facts
        assert facts["provider_calls"] == 0, facts
        self.results["recoverable"] = facts

        # Section 34: the unchanged BR2 reconciler recognizes the graph.
        probe = cr.inspect_continuation_delivery(identity)
        published: list[dict] = []

        def _record_publish(_db, published_identity):
            published.append(
                {
                    "session_id": published_identity.session_id,
                    "instance_id": published_identity.instance_id,
                    "continuation_task_id": published_identity.continuation_task_id,
                    "continuation_kind": published_identity.continuation_kind,
                    "task_execution_id": published_identity.task_execution_id,
                    "retry_count": published_identity.retry_count,
                }
            )
            return "er4-cert-br2-recognized"

        # Make the marker due for the reconciler without changing BR2 policy.
        session.continuation_retry_eta = datetime.now(UTC) - timedelta(seconds=600)
        db.commit()
        decision = cr.reconcile_session_continuation(
            db, session, publish=_record_publish
        )
        db.rollback()
        session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        after = derive_lifecycle_authority(db, session, task_id=task.id)
        br2 = {
            "delivery_present": probe.present,
            "outcome": decision.outcome,
            "published": published,
            "session_status_after": session.status,
            "logical_terminal_after": after.logical_terminal,
            "continuation_pending_after": after.continuation_pending,
        }
        _log("br2_case", **br2)
        assert probe.present is False, br2
        assert decision.outcome == cr.REPUBLISHED, br2
        assert len(published) == 1, br2
        assert published[0]["instance_id"] == identity.instance_id, br2
        assert session.status == "retry_pending", br2
        assert after.logical_terminal is False, br2
        self.results["br2"] = br2
        return facts, br2

    def certify_stale_generation(self, db):
        """Section 35: a stale delivery cannot touch a successor."""

        from app.models import (
            Session as SessionModel,
            TaskExecution,
            TaskStatus,
        )

        _p, session, task, _link, execution = self.seed(db, "stale")
        session_id = session.id
        stale_instance = session.instance_id
        stale_execution_id = execution.id

        # A successor generation and attempt exist before the stale delivery.
        session.instance_id = "er4-cert-stale-generation-2"
        session.status = "running"
        session.is_active = True
        session.continuation_task_id = None
        successor = TaskExecution(
            session=session,
            task=task,
            attempt_number=2,
            status=TaskStatus.RUNNING,
            worker_pid=999001,
            worker_hostname="er4-successor-host",
            worker_process_start_identity="er4-successor-process",
            heartbeat_at=datetime.now(UTC),
        )
        db.add(successor)
        db.commit()
        db.refresh(successor)
        successor_id = successor.id
        successor_instance = session.instance_id

        stale_execution = (
            db.query(TaskExecution).filter(TaskExecution.id == stale_execution_id).one()
        )
        result = self.publish(
            session=session,
            task=task,
            execution=stale_execution,
            mode="terminal",
            expected_instance_id=stale_instance,
        )
        state = self._await_result(result)

        db.rollback()
        session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        successor = (
            db.query(TaskExecution).filter(TaskExecution.id == successor_id).one()
        )
        facts = {
            "celery_state": state,
            "stale_expected_instance_id": stale_instance,
            "session_status": session.status,
            "session_instance_id": session.instance_id,
            "continuation_task_id": session.continuation_task_id,
            "successor_status": str(successor.status),
            "successor_worker_pid": successor.worker_pid,
            "successor_worker_hostname": successor.worker_hostname,
            "successor_process_identity": successor.worker_process_start_identity,
        }
        _log("stale_generation_case", **facts)
        assert session.status == "running", facts
        assert session.instance_id == successor_instance, facts
        assert session.continuation_task_id is None, facts
        assert successor.status == TaskStatus.RUNNING, facts
        assert successor.worker_pid == 999001, facts
        assert successor.worker_hostname == "er4-successor-host", facts
        assert successor.worker_process_start_identity == "er4-successor-process", facts
        self.results["stale_generation"] = facts
        return facts

    # -- cleanup ----------------------------------------------------------

    def cleanup(self, db) -> dict:
        from app.models import (
            LogEntry,
            SessionTask,
            Session as SessionModel,
            Task,
            TaskExecution,
            TaskExecutionChangeSet,
        )

        if self.keep:
            _log("cleanup_skipped", keep=True)
            return {"skipped": True}

        removed: dict[str, int] = {}
        db.rollback()
        for session_id in self.session_ids:
            removed["log_entries"] = removed.get("log_entries", 0) + (
                db.query(LogEntry)
                .filter(LogEntry.session_id == session_id)
                .delete(synchronize_session=False)
            )
            execution_ids = [
                row.id
                for row in db.query(TaskExecution.id)
                .filter(TaskExecution.session_id == session_id)
                .all()
            ]
            if execution_ids:
                removed["change_sets"] = removed.get("change_sets", 0) + (
                    db.query(TaskExecutionChangeSet)
                    .filter(TaskExecutionChangeSet.task_execution_id.in_(execution_ids))
                    .delete(synchronize_session=False)
                )
            removed["task_executions"] = removed.get("task_executions", 0) + (
                db.query(TaskExecution)
                .filter(TaskExecution.session_id == session_id)
                .delete(synchronize_session=False)
            )
            removed["session_tasks"] = removed.get("session_tasks", 0) + (
                db.query(SessionTask)
                .filter(SessionTask.session_id == session_id)
                .delete(synchronize_session=False)
            )
            removed["sessions"] = removed.get("sessions", 0) + (
                db.query(SessionModel)
                .filter(SessionModel.id == session_id)
                .delete(synchronize_session=False)
            )
        for project_id in self.project_ids:
            removed["tasks"] = removed.get("tasks", 0) + (
                db.query(Task)
                .filter(Task.project_id == project_id)
                .delete(synchronize_session=False)
            )
        db.commit()
        removed["projects"] = sum(
            _delete_project_row(project_id) for project_id in self.project_ids
        )
        shutil.rmtree(self.workspace, ignore_errors=True)
        removed["workspace_removed"] = not self.workspace.exists()
        try:
            import redis

            redis.from_url(_isolated_broker_url()).flushdb()
            removed["certification_broker_flushed"] = True
        except Exception as exc:  # noqa: BLE001
            removed["certification_broker_flushed"] = f"failed: {exc}"
        _log("cleanup", **removed)
        return removed

    def verify_cleanup(self, db) -> dict:
        from app.models import (
            Project,
            Session as SessionModel,
            TaskExecution,
        )

        db.rollback()
        remaining = {
            "sessions": db.query(SessionModel)
            .filter(SessionModel.id.in_(self.session_ids or [-1]))
            .count(),
            "projects": db.query(Project)
            .filter(Project.id.in_(self.project_ids or [-1]))
            .count(),
            "task_executions": db.query(TaskExecution)
            .filter(TaskExecution.session_id.in_(self.session_ids or [-1]))
            .count(),
        }
        _log("cleanup_verification", **remaining)
        assert remaining == {"sessions": 0, "projects": 0, "task_executions": 0}
        return remaining


def _delete_project_row(project_id: int) -> int:
    """Remove the synthetic Project row directly.

    This database carries a legacy trigger that references a table dropped by
    an earlier migration, so the ORM delete cannot run.  The synthetic project
    is disposable certification scaffolding and is removed on a raw connection.
    """

    import sqlite3

    from app.config import settings

    path = str(settings.DATABASE_URL).replace("sqlite:///", "").replace("sqlite://", "")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        cursor = connection.execute("delete from projects where id = ?", (project_id,))
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="retain synthetic records")
    args = parser.parse_args()

    os.environ["CELERY_BROKER_URL"] = _isolated_broker_url()
    os.environ["CELERY_RESULT_BACKEND"] = os.environ["CELERY_BROKER_URL"]

    from app.database import get_db_session

    certification = Certification(keep=args.keep)
    db = get_db_session()
    status = 1
    try:
        _log("broker", url=os.environ["CELERY_BROKER_URL"])
        certification.start_worker()
        certification.certify_terminal(db)
        certification.certify_recoverable_and_br2(db)
        certification.certify_stale_generation(db)
        _log("certification", result="PASS", **certification.results)
        status = 0
    except Exception as exc:  # noqa: BLE001
        _log("certification", result="FAIL", error=str(exc))
        raise
    finally:
        certification.stop_worker()
        try:
            certification.cleanup(db)
            if not args.keep:
                certification.verify_cleanup(db)
        finally:
            db.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
