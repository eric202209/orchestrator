#!/usr/bin/env python3
"""PHASE36-MAINT-LA1-BR2 provider-free live infrastructure certification.

Proves the E8 reconciler across the real database/Redis/Celery/worker boundary
without any provider invocation:

    durable retry_pending marker, delivery intentionally never published
        -> the real reconciler detects it
        -> the real broker carries the restored delivery
        -> a real Celery worker consumes it and the strict claim wins once
        -> the Session progresses to running
    then: operator generation rotation prevents stale reconciliation.

Isolation: a disposable synthetic Project/Session/Task under a temporary
workspace (never a ProductRoot), and a dedicated broker database plus a
dedicated worker process, so the production worker never sees these messages.
The worker module registers a claim-only task under the production task name,
so the real broker, message headers, worker loop and strict E2 claim all run
while no orchestration or provider work is started.

Run with:
    venv/bin/python scripts/maintenance/phase36_br2_continuation_delivery_certification.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CERT_BROKER_DB = 9
ORCHESTRATION_TASK_NAME_LOCAL = "app.tasks.worker.execute_orchestration_task"
CERT_QUEUE = "br2_certification"
WORKER_MODULE = "scripts.maintenance.phase36_br2_certification_worker"


def _isolated_broker_url() -> str:
    from app.config import settings

    base = str(settings.CELERY_BROKER_URL).rsplit("/", 1)[0]
    return f"{base}/{CERT_BROKER_DB}"


def _log(step: str, **fields) -> None:
    print(json.dumps({"step": step, **fields}, default=str), flush=True)


class Certification:
    def __init__(self, *, keep: bool = False):
        self.keep = keep
        self.workspace = Path(tempfile.mkdtemp(prefix="br2-certification-"))
        self.created: dict[str, int] = {}
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
                "br2-certification@%h",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.time() + 60
        from scripts.maintenance.phase36_br2_certification_worker import app

        while time.time() < deadline:
            if self.worker.poll() is not None:
                raise RuntimeError(
                    f"certification worker exited: {self.worker.stdout.read()[-2000:]}"
                )
            replies = app.control.ping(timeout=2) or []
            if any("br2-certification" in list(reply)[0] for reply in replies if reply):
                self._assert_no_production_tasks(app)
                _log("worker_ready", replies=replies)
                return
            time.sleep(1)
        raise RuntimeError("certification worker did not become ready")

    @staticmethod
    def _assert_no_production_tasks(app) -> None:
        """Fail closed if the worker could run real orchestration work."""

        registered = app.control.inspect(timeout=5).registered() or {}
        names = sorted({name for names in registered.values() for name in names})
        production = [
            name
            for name in names
            if name.startswith("app.tasks.") and name != ORCHESTRATION_TASK_NAME_LOCAL
        ]
        _log("worker_registered_tasks", tasks=names)
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

    def seed(self, db):
        from app.models import (
            Project,
            Session as SessionModel,
            SessionTask,
            Task,
            TaskExecution,
            TaskStatus,
        )

        project = Project(
            name=f"BR2 certification {int(time.time())}",
            workspace_path=str(self.workspace),
        )
        session = SessionModel(
            project=project,
            name="BR2 certification session",
            status="running",
            execution_mode="manual",
            is_active=True,
        )
        task = Task(
            project=project,
            title="BR2 certification task",
            description="provider-free lost-delivery certification",
            status=TaskStatus.RUNNING,
            task_subfolder="task-br2-cert",
            workspace_status="isolated",
        )
        link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
        execution = TaskExecution(
            session=session, task=task, attempt_number=1, status=TaskStatus.RUNNING
        )
        db.add_all([project, session, task, link, execution])
        db.commit()
        for row in (project, session, task, link, execution):
            db.refresh(row)
        self.created = {
            "project_id": project.id,
            "session_id": session.id,
            "task_id": task.id,
        }
        _log("seeded", **self.created)
        return project, session, task, execution

    def strand(self, db, session, execution):
        """Commit the durable marker and deliberately never publish it."""

        from app.services.orchestration.lifecycle.transitions import (
            enter_recovering,
            schedule_continuation,
        )

        enter_recovering(
            db,
            session,
            task_execution=execution,
            continuation_kind="celery_retry",
            retry_count=0,
            failure_reason="certification: attempt failed",
            commit=True,
        )
        identity = schedule_continuation(
            db,
            session,
            continuation_kind="celery_retry",
            retry_count=1,
            # Already past due; the publication that should follow is skipped.
            retry_eta=datetime.now(timezone.utc) - timedelta(seconds=300),
            commit=True,
        )
        db.refresh(session)
        _log(
            "stranded",
            session_status=session.status,
            continuation_kind=session.continuation_kind,
            retry_count=session.continuation_retry_count,
            instance_id=identity.instance_id,
            task_execution_id=identity.task_execution_id,
        )
        return identity

    # -- assertions -------------------------------------------------------

    def assert_nonterminal(self, db, session, label: str):
        from app.services.orchestration.lifecycle.authority import (
            derive_lifecycle_authority,
        )

        db.refresh(session)
        authority = derive_lifecycle_authority(db, session)
        state = {
            "status": session.status,
            "continuation_pending": authority.continuation_pending,
            "logical_terminal": authority.logical_terminal,
            "quiescent": authority.quiescent,
        }
        _log(label, **state)
        assert session.status == "retry_pending", state
        assert authority.continuation_pending is True, state
        assert authority.logical_terminal is False, state
        return state

    # -- scenarios --------------------------------------------------------

    def certify_republish_and_claim(self, db, session, identity):
        from app.services.orchestration.lifecycle import continuation_recovery as cr

        self.assert_nonterminal(db, session, "before_reconciliation")

        probed = cr.inspect_continuation_delivery(identity)
        _log("delivery_probe", present=probed.present, error=probed.error)
        assert probed.present is False, "a stranded marker must have no live delivery"

        decision = cr.reconcile_session_continuation(
            db, session, publish=self._publish_to_certification_queue
        )
        _log("reconciliation", **decision.as_evidence())
        assert decision.outcome == cr.REPUBLISHED, decision.as_evidence()
        self.results["republished_celery_task_id"] = decision.details.get(
            "celery_task_id"
        )

        claim = self._await_claim(db, session)
        _log("worker_claim", **claim)
        assert claim["status"] == "running", claim
        assert claim["claim_count"] == 1, claim
        assert claim["active_executions"] == 1, claim
        self.results["claim"] = claim
        return claim

    def certify_stale_generation(self, db):
        """A rotated generation must make the old candidate unreconcilable."""

        from app.services.orchestration.lifecycle import continuation_recovery as cr
        from app.services.orchestration.lifecycle.transitions import (
            claim_continuation,
            enter_recovering,
            revoke_autonomous_continuation,
            schedule_continuation,
        )
        from app.models import (
            Session as SessionModel,
            TaskExecution,
            TaskStatus,
        )

        session = (
            db.query(SessionModel)
            .filter(SessionModel.id == self.created["session_id"])
            .one()
        )
        execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session.id,
                TaskExecution.status == TaskStatus.RUNNING,
            )
            .order_by(TaskExecution.id.desc())
            .first()
        )
        enter_recovering(
            db,
            session,
            task_execution=execution,
            continuation_kind="celery_retry",
            retry_count=1,
            failure_reason="certification: second attempt failed",
            commit=True,
        )
        stale = schedule_continuation(
            db,
            session,
            continuation_kind="celery_retry",
            retry_count=2,
            retry_eta=datetime.now(timezone.utc) - timedelta(seconds=300),
            commit=True,
        )

        # Operator pause rotates the generation before reconciliation runs.
        revoke_autonomous_continuation(
            db, session, resulting_status="paused", reason="certification", commit=True
        )
        db.refresh(session)

        published: list = []
        decision = cr.reconcile_session_continuation(
            db,
            session,
            publish=lambda _db, _identity: published.append(_identity),
        )
        rejected = claim_continuation(db, stale)
        outcome = {
            "session_status": session.status,
            "rotated_instance_id": session.instance_id,
            "stale_instance_id": stale.instance_id,
            "reconciliation_outcome": decision.outcome,
            "published_count": len(published),
            "stale_claim_accepted": rejected.accepted,
            "stale_claim_reason": rejected.reason,
        }
        _log("stale_generation", **outcome)
        assert published == [], outcome
        assert decision.outcome in {
            cr.INVALID_CONTINUATION,
            cr.STALE_GENERATION,
            cr.NOT_ELIGIBLE,
        }, outcome
        assert rejected.accepted is False, outcome
        assert session.instance_id != stale.instance_id, outcome
        self.results["stale_generation"] = outcome
        return outcome

    # -- helpers ----------------------------------------------------------

    def _publish_to_certification_queue(self, db, identity):
        """Publish the real message shape onto the isolated certification queue."""

        from app.services.orchestration.lifecycle.continuation_recovery import (
            ORCHESTRATION_TASK_NAME,
            _celery_delivery_retries,
        )
        from app.services.session.session_runtime_service import (
            DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
            build_task_execution_prompt,
        )
        from app.models import Task

        task = db.query(Task).filter(Task.id == identity.continuation_task_id).one()
        from scripts.maintenance.phase36_br2_certification_worker import app

        result = app.send_task(
            ORCHESTRATION_TASK_NAME,
            kwargs={
                "session_id": identity.session_id,
                "task_id": identity.continuation_task_id,
                "prompt": build_task_execution_prompt(task),
                "timeout_seconds": DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
                "expected_session_instance_id": identity.instance_id,
                "task_execution_id": identity.task_execution_id,
                "continuation_task_id": identity.continuation_task_id,
                "continuation_kind": identity.continuation_kind,
                "continuation_retry_count": identity.retry_count,
            },
            retries=_celery_delivery_retries(identity),
            queue=CERT_QUEUE,
        )
        return result.id

    def _await_claim(self, db, session, timeout: float = 90.0) -> dict:
        from app.models import LogEntry, TaskExecution, TaskStatus

        deadline = time.time() + timeout
        while time.time() < deadline:
            db.rollback()
            db.refresh(session)
            if session.status == "running":
                break
            time.sleep(1)
        db.rollback()
        claims = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session.id,
                LogEntry.message == "BR2 certification strict claim",
            )
            .all()
        )
        accepted = []
        for row in claims:
            try:
                accepted.append(json.loads(row.log_metadata or "{}"))
            except ValueError:
                continue
        active = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session.id,
                TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .count()
        )
        return {
            "status": session.status,
            "claim_count": sum(1 for row in accepted if row.get("accepted")),
            "claim_reasons": [row.get("reason") for row in accepted],
            "active_executions": active,
            "instance_id": session.instance_id,
        }

    # -- cleanup ----------------------------------------------------------

    def cleanup(self, db) -> dict:
        from app.models import (
            LogEntry,
            Project,
            Session as SessionModel,
            SessionTask,
            Task,
            TaskExecution,
            TaskExecutionChangeSet,
        )

        _ = Project
        removed = {}
        session_id = self.created.get("session_id")
        project_id = self.created.get("project_id")
        if self.keep or session_id is None:
            _log("cleanup_skipped", keep=self.keep)
            return {"skipped": True}
        removed["log_entries"] = (
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
            removed["change_sets"] = (
                db.query(TaskExecutionChangeSet)
                .filter(TaskExecutionChangeSet.task_execution_id.in_(execution_ids))
                .delete(synchronize_session=False)
            )
        removed["task_executions"] = (
            db.query(TaskExecution)
            .filter(TaskExecution.session_id == session_id)
            .delete(synchronize_session=False)
        )
        removed["session_tasks"] = (
            db.query(SessionTask)
            .filter(SessionTask.session_id == session_id)
            .delete(synchronize_session=False)
        )
        removed["sessions"] = (
            db.query(SessionModel)
            .filter(SessionModel.id == session_id)
            .delete(synchronize_session=False)
        )
        removed["tasks"] = (
            db.query(Task)
            .filter(Task.project_id == project_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        removed["projects"] = _delete_project_row(project_id)
        shutil.rmtree(self.workspace, ignore_errors=True)
        try:
            import redis

            redis.from_url(_isolated_broker_url()).flushdb()
            removed["certification_broker_flushed"] = True
        except Exception as exc:  # noqa: BLE001
            removed["certification_broker_flushed"] = f"failed: {exc}"
        _log("cleanup", **removed)
        return removed


def _delete_project_row(project_id: int) -> int:
    """Remove the synthetic Project row directly.

    This database carries a legacy trigger that references a table dropped by an
    earlier migration, so the ORM delete cannot run. The synthetic project is
    disposable certification scaffolding and is removed on a raw connection.
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

    os.environ["CELERY_BROKER_URL"] = _isolated_broker_url_from_env()
    os.environ["CELERY_RESULT_BACKEND"] = os.environ["CELERY_BROKER_URL"]

    from app.database import get_db_session

    certification = Certification(keep=args.keep)
    db = get_db_session()
    status = 1
    try:
        _log("broker", url=os.environ["CELERY_BROKER_URL"])
        certification.start_worker()
        _project, session, _task, execution = certification.seed(db)
        identity = certification.strand(db, session, execution)
        certification.certify_republish_and_claim(db, session, identity)
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
        finally:
            db.close()
    return status


def _isolated_broker_url_from_env() -> str:
    base = os.environ.get("CELERY_BROKER_URL") or "redis://localhost:6379/0"
    return f"{base.rsplit('/', 1)[0]}/{CERT_BROKER_DB}"


if __name__ == "__main__":
    raise SystemExit(main())
