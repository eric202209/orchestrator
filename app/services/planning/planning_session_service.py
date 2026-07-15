"""Interactive planning session orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Plan,
    PlanningArtifact,
    PlanningMessage,
    PlanningSession,
    Project,
    Task,
)
from app.schemas import PlannerTaskCandidate
from app.services.agents.agent_runtime import (
    BackendRole,
    invoke_runtime_prompt,
    resolve_planning_runtime_configuration,
    runtime_reports_context_overflow,
)
from app.services.agents.interfaces import AgentRuntimeError
from app.services.model_adaptation import render_prompt_for_profile
from app.services.model_adaptation.schemas import PromptEnvelope
from app.services.planning.plan_commit_service import PlanCommitService
from app.services.planning.planner_service import PlannerService
from app.services.orchestration.prompt_optimization import optimize_prompt
from app.services.orchestration.policy import PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS
from app.services.observability.planning_identity import active_planning_identity

logger = logging.getLogger(__name__)


class StalePlanningOwnerError(RuntimeError):
    """A planning worker no longer owns the generation it was given."""

    def __init__(self, session_id: int, generation_id: str, reason: str):
        super().__init__("stale_owner")
        self.session_id = session_id
        self.generation_id = generation_id
        self.reason = reason


class PlanningSessionService:
    """Manage resumable planning conversations and final plan synthesis."""

    ACTIVE_STATUSES = {"active", "waiting_for_input"}
    MAX_QUESTIONS = 2
    PLANNING_SYNTHESIS_TIMEOUT_SECONDS = settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS
    REPLAN_SYNTHESIS_TIMEOUT_SECONDS = settings.REPLAN_SYNTHESIS_TIMEOUT_SECONDS
    SYNTHESIS_TRANSCRIPT_CHAR_BUDGET = 1800
    SYNTHESIS_PROMPT_CHAR_BUDGET = 4200
    SYNTHESIS_OUTPUT_CHAR_LIMIT = 100_000
    PROCESSING_LEASE_MINUTES = 10

    def __init__(self, db: Session):
        self.db = db

    def list_sessions(self, project_id: Optional[int] = None) -> list[PlanningSession]:
        query = self.db.query(PlanningSession).join(Project)
        query = query.filter(Project.deleted_at.is_(None))
        if project_id is not None:
            query = query.filter(PlanningSession.project_id == project_id)
        return query.order_by(
            PlanningSession.created_at.desc(), PlanningSession.id.desc()
        ).all()

    def get_session(self, session_id: int) -> PlanningSession:
        session = (
            self.db.query(PlanningSession)
            .join(Project)
            .filter(PlanningSession.id == session_id)
            .filter(Project.deleted_at.is_(None))
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Planning session not found")
        return session

    def start_session(
        self,
        project: Project,
        prompt: str,
        source_brain: str = "local",
        skip_clarification: bool = False,
    ) -> PlanningSession:
        existing = (
            self.db.query(PlanningSession)
            .filter(
                PlanningSession.project_id == project.id,
                PlanningSession.status.in_(tuple(self.ACTIVE_STATUSES)),
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail="This project already has an active planning session",
            )

        identity = active_planning_identity(self.db)
        session = PlanningSession(
            project_id=project.id,
            title=self._generate_title(prompt),
            prompt=prompt.strip(),
            status="active",
            source_brain=source_brain,
            **identity,
        )
        self.db.add(session)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail="This project already has an active planning session",
            ) from exc

        msg_metadata: dict = {"kind": "prompt"}
        if skip_clarification:
            msg_metadata["skip_clarification"] = True
        if skip_clarification and self._looks_like_replan_prompt(prompt):
            msg_metadata["replan_recovery"] = True
        self._add_message(session, "user", prompt.strip(), metadata=msg_metadata)
        self.db.commit()
        self.schedule_processing(session.id)
        self.db.refresh(session)
        return session

    def respond(self, session_id: int, response: str) -> PlanningSession:
        session = self.get_session(session_id)
        if session.status != "waiting_for_input" or not session.current_prompt_id:
            raise HTTPException(
                status_code=409, detail="Planning session is not waiting for input"
            )

        self._add_message(
            session,
            "user",
            response.strip(),
            prompt_id=session.current_prompt_id,
            metadata={"kind": "response"},
        )
        session.current_prompt_id = None
        session.status = "active"
        session.processing_token = None
        session.processing_started_at = None
        session.processing_task_id = None
        session.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.schedule_processing(session.id)
        self.db.refresh(session)
        return session

    def retry(self, session_id: int) -> PlanningSession:
        session = self.get_session(session_id)
        if session.status != "failed":
            raise HTTPException(
                status_code=409,
                detail="Only failed planning sessions can be retried",
            )
        session.status = "active"
        session.last_error = None
        session.processing_token = None
        session.processing_started_at = None
        session.processing_task_id = None
        session.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.schedule_processing(session.id)
        self.db.refresh(session)
        return session

    def cancel(self, session_id: int) -> PlanningSession:
        session = self.get_session(session_id)
        processing_task_id = session.processing_task_id
        if (
            session.status != "cancelled"
            or session.processing_token is not None
            or session.processing_started_at is not None
        ):
            if session.status not in {"completed", "cancelled"}:
                session.status = "cancelled"
            session.current_prompt_id = None
            # Invalidate the write authority before the response is observable.
            session.processing_token = None
            session.processing_started_at = None
            session.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(session)
            self._revoke_processing_task(processing_task_id)
        return session

    def delete_terminal_session(self, session_id: int) -> None:
        session = self.get_session(session_id)
        committed_task_ids = self._load_committed_task_ids(session)
        can_delete_uncommitted_plan = (
            session.status == "completed"
            and not committed_task_ids
            and session.committed_at is None
        )
        if (
            session.status not in {"failed", "cancelled"}
            and not can_delete_uncommitted_plan
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only failed, cancelled, or uncommitted completed planning "
                    "sessions can be deleted"
                ),
            )
        if session.processing_token:
            raise HTTPException(
                status_code=409,
                detail="Planning session cleanup pending while processing is owned",
            )
        self.db.delete(session)
        self.db.commit()

    def schedule_processing(
        self,
        session_id: int,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> Optional[dict[str, object]]:
        """Persist an owner before publishing a task and serialize all fences."""

        session = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session_id)
            .populate_existing()
            .first()
        )
        if not session or not session.generation_id:
            self.db.rollback()
            return self._stale_owner_result(
                session_id, generation_id or "", "missing_generation"
            )
        if generation_id is not None and session.generation_id != generation_id:
            self.db.rollback()
            return self._stale_owner_result(
                session_id, generation_id, "generation_mismatch"
            )
        generation_id = session.generation_id
        if session.status != "active" or session.current_prompt_id is not None:
            self.db.rollback()
            return None

        if session.processing_token is not None:
            if owner_token != session.processing_token:
                self.db.rollback()
                return None
            task_id = session.processing_task_id or str(uuid.uuid4())
            if session.processing_task_id is None:
                session.processing_task_id = task_id
                session.updated_at = datetime.now(timezone.utc)
        else:
            owner_token = owner_token or uuid.uuid4().hex
            task_id = session.processing_task_id or str(uuid.uuid4())
            session.processing_token = owner_token
            session.processing_started_at = datetime.now(timezone.utc)
            session.processing_task_id = task_id
            session.updated_at = datetime.now(timezone.utc)

        self.db.commit()

        if self._should_process_inline():
            try:
                self.process_session(
                    session_id,
                    generation_id,
                    owner_token,
                    processing_task_id=task_id,
                )
            finally:
                self.release_processing_task(
                    session_id, generation_id, owner_token, task_id
                )
            return None

        try:
            from app.tasks.planning_tasks import advance_planning_session

            advance_planning_session.apply_async(
                args=(session_id, generation_id, owner_token),
                task_id=task_id,
            )
        except Exception:
            logger.exception("Planning task publish failed for session %s", session_id)
            try:
                self.process_session(
                    session_id,
                    generation_id,
                    owner_token,
                    processing_task_id=task_id,
                )
            finally:
                self.release_processing_task(
                    session_id, generation_id, owner_token, task_id
                )
        return None

    def process_session(
        self,
        session_id: int,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
        *,
        processing_task_id: Optional[str] = None,
    ) -> Optional[PlanningSession | dict[str, object]]:
        # Direct synchronous callers predate the Celery contract.  They acquire
        # a current owner safely; Celery tasks must always supply all arguments.
        if generation_id is None or owner_token is None:
            prepared = self._prepare_direct_owner(session_id)
            if isinstance(prepared, dict):
                return prepared
            generation_id, owner_token = prepared

        claim = self._claim_session_for_processing(
            session_id, generation_id, owner_token
        )
        if isinstance(claim, dict):
            return claim
        if not claim:
            return None

        session = claim
        try:
            project = session.project
            self._advance_or_finalize(
                session, project, generation_id=generation_id, owner_token=owner_token
            )
            self._assert_owner(session.id, generation_id, owner_token)
            self._clear_processing_lease(session)
            self.db.commit()
            self.db.refresh(session)
            return session
        except StalePlanningOwnerError as exc:
            self.db.rollback()
            return self._stale_owner_result(
                exc.session_id, exc.generation_id, exc.reason
            )
        except HTTPException:
            try:
                self._assert_owner(session.id, generation_id, owner_token)
                self._clear_processing_lease(session)
                self.db.commit()
            except StalePlanningOwnerError as exc:
                self.db.rollback()
                return self._stale_owner_result(
                    exc.session_id, exc.generation_id, exc.reason
                )
            raise
        except Exception as exc:
            try:
                self._assert_owner(session.id, generation_id, owner_token)
                session.status = "failed"
                session.last_error = str(exc)
                session.current_prompt_id = None
                session.updated_at = datetime.now(timezone.utc)
                self._clear_processing_lease(session)
                self.db.commit()
                self.db.refresh(session)
                return session
            except StalePlanningOwnerError as stale:
                self.db.rollback()
                return self._stale_owner_result(
                    stale.session_id, stale.generation_id, stale.reason
                )

    def recover_active_sessions(self) -> list[int]:
        stale_before = datetime.now(timezone.utc) - timedelta(
            minutes=self.PROCESSING_LEASE_MINUTES
        )
        active_sessions = (
            self.db.query(PlanningSession)
            .join(Project)
            .filter(Project.deleted_at.is_(None))
            .filter(PlanningSession.status == "active")
            .filter(PlanningSession.current_prompt_id.is_(None))
            .filter(
                or_(
                    PlanningSession.processing_token.is_(None),
                    PlanningSession.processing_started_at.is_(None),
                    PlanningSession.processing_started_at < stale_before,
                )
            )
            .all()
        )
        session_details = [
            (session.id, session.generation_id) for session in active_sessions
        ]
        for session in active_sessions:
            session.processing_token = None
            session.processing_started_at = None
            session.processing_task_id = None
            session.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        for session_id, generation_id in session_details:
            self.schedule_processing(session_id)
        return [session_id for session_id, _ in session_details]

    def commit(
        self,
        session_id: int,
        selected_tasks: Optional[list[PlannerTaskCandidate]] = None,
        planner_markdown: Optional[str] = None,
    ) -> tuple[PlanningSession, Optional[Plan], list[Task]]:
        session = self.get_session(session_id)
        if not session.generation_id:
            raise HTTPException(
                status_code=409,
                detail="Planning session generation is unavailable; migration required",
            )
        if session.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="Planning session must be completed before commit",
            )

        committed_task_ids = self._load_committed_task_ids(session)
        if committed_task_ids:
            tasks = (
                self.db.query(Task)
                .filter(Task.id.in_(committed_task_ids))
                .order_by(Task.plan_position.asc(), Task.id.asc())
                .all()
            )
            return session, session.finalized_plan, tasks

        effective_markdown = (
            planner_markdown.strip() if planner_markdown is not None else None
        )
        if effective_markdown is None:
            effective_markdown = self._get_artifact_content(session, "planner_markdown")
        if not effective_markdown:
            raise HTTPException(
                status_code=422,
                detail="Planning session is missing final planner markdown",
            )

        task_candidates = (
            PlannerService.parse_markdown(effective_markdown)
            if selected_tasks is None
            else selected_tasks
        )
        included_tasks = [
            task
            for task in task_candidates
            if getattr(task, "include", True) and (task.title or "").strip()
        ]
        if not included_tasks:
            raise HTTPException(
                status_code=422,
                detail="At least one task must be selected for commit",
            )

        plan = session.finalized_plan
        if plan is None:
            title = session.title[:255]
            requirement = (
                self._get_artifact_content(session, "requirements") or session.prompt
            )
            plan = Plan(
                project_id=session.project_id,
                title=title,
                source_brain=session.source_brain,
                requirement=requirement,
                markdown=effective_markdown,
                status="draft",
            )
            self.db.add(plan)
            self.db.flush()
            session.finalized_plan_id = plan.id
        else:
            plan.markdown = effective_markdown

        if planner_markdown is not None:
            self._append_artifact_version(
                session,
                artifact_type="planner_markdown",
                filename="planner.md",
                content=effective_markdown,
            )

        committed_plan, tasks = PlanCommitService(self.db).create_plan_tasks(
            session.project,
            included_tasks,
            plan=plan,
            commit=False,
        )
        session.committed_at = datetime.now(timezone.utc)
        session.committed_task_ids = json.dumps([task.id for task in tasks])
        session.finalized_plan_id = (
            committed_plan.id if committed_plan else session.finalized_plan_id
        )
        self.db.flush()
        self.db.commit()
        self.db.refresh(session)
        if committed_plan:
            self.db.refresh(committed_plan)
        for task in tasks:
            self.db.refresh(task)
        return session, committed_plan, tasks

    def build_session_payload(self, session: PlanningSession) -> dict[str, Any]:
        return {
            "id": session.id,
            "project_id": session.project_id,
            "title": session.title,
            "prompt": session.prompt,
            "status": session.status,
            "source_brain": session.source_brain,
            "planning_backend": session.planning_backend,
            "planner_model": session.planner_model,
            "reasoning_profile": session.reasoning_profile,
            "configuration_fingerprint": session.configuration_fingerprint,
            "current_prompt_id": session.current_prompt_id,
            "finalized_plan_id": session.finalized_plan_id,
            "committed_at": session.committed_at,
            "completed_at": session.completed_at,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "last_error": session.last_error,
            "messages": session.messages,
            "artifacts": self._latest_artifacts(session),
            "tasks_preview": [
                PlannerTaskCandidate(
                    title=item.title,
                    description=item.description,
                    execution_profile=item.execution_profile,
                    workflow_stage=item.workflow_stage,
                    priority=item.priority,
                    plan_position=item.plan_position,
                    estimated_effort=item.estimated_effort,
                )
                for item in PlannerService.parse_markdown(
                    self._get_artifact_content(session, "planner_markdown") or ""
                )
            ],
            "committed_task_ids": self._load_committed_task_ids(session),
        }

    def _prepare_direct_owner(
        self, session_id: int
    ) -> tuple[str, str] | dict[str, object]:
        session = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session_id)
            .populate_existing()
            .first()
        )
        if not session or not session.generation_id:
            self.db.rollback()
            return self._stale_owner_result(session_id, "", "missing_generation")
        if session.processing_token:
            return session.generation_id, session.processing_token
        session.processing_token = uuid.uuid4().hex
        session.processing_started_at = datetime.now(timezone.utc)
        session.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        return session.generation_id, session.processing_token

    def _claim_session_for_processing(
        self, session_id: int, generation_id: str, owner_token: str
    ) -> Optional[PlanningSession | dict[str, object]]:
        stale_before = datetime.now(timezone.utc) - timedelta(
            minutes=self.PROCESSING_LEASE_MINUTES
        )
        session = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session_id)
            .with_for_update()
            .first()
        )
        if not session:
            self.db.rollback()
            return self._stale_owner_result(
                session_id, generation_id, "missing_session"
            )
        if session.generation_id != generation_id:
            self.db.rollback()
            return self._stale_owner_result(
                session_id, generation_id, "generation_mismatch"
            )
        if session.processing_token != owner_token:
            self.db.rollback()
            return self._stale_owner_result(session_id, generation_id, "owner_mismatch")
        if session.status != "active" or session.current_prompt_id is not None:
            self.db.rollback()
            return None
        started_at = session.processing_started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if started_at is not None and started_at < stale_before:
            # An expired owner must be replaced by recovery with a new token;
            # the expired task cannot reclaim itself.
            self.db.rollback()
            return self._stale_owner_result(session_id, generation_id, "lease_expired")
        return session

    @staticmethod
    def _clear_processing_lease(session: PlanningSession) -> None:
        session.processing_token = None
        session.processing_started_at = None
        session.updated_at = datetime.now(timezone.utc)

    def _assert_owner_if_present(
        self,
        session: PlanningSession,
        generation_id: Optional[str],
        owner_token: Optional[str],
    ) -> None:
        if generation_id is not None and owner_token is not None:
            self._assert_owner(session.id, generation_id, owner_token)

    def _assert_owner(
        self, session_id: int, generation_id: str, owner_token: str
    ) -> PlanningSession:
        with self.db.no_autoflush:
            current = (
                self.db.query(PlanningSession.id, PlanningSession.status)
                .filter(
                    PlanningSession.id == session_id,
                    PlanningSession.generation_id == generation_id,
                    PlanningSession.processing_token == owner_token,
                )
                .first()
            )
        if current is None:
            reason = "missing_session"
            with self.db.no_autoflush:
                by_id = (
                    self.db.query(
                        PlanningSession.generation_id, PlanningSession.processing_token
                    )
                    .populate_existing()
                    .filter(PlanningSession.id == session_id)
                    .first()
                )
            if by_id is not None:
                reason = (
                    "generation_mismatch"
                    if by_id[0] != generation_id
                    else "owner_mismatch"
                )
            raise StalePlanningOwnerError(session_id, generation_id, reason)
        if current[1] not in {"active", "waiting_for_input", "completed", "failed"}:
            raise StalePlanningOwnerError(session_id, generation_id, "terminal_state")
        return self.db.get(PlanningSession, session_id)

    @staticmethod
    def _stale_owner_result(
        session_id: int, generation_id: str, reason: str
    ) -> dict[str, object]:
        return {
            "status": "stale_owner",
            "session_id": session_id,
            "generation_id": generation_id,
            "reason": reason,
        }

    @staticmethod
    def _revoke_processing_task(task_id: Optional[str]) -> None:
        if not task_id:
            return
        try:
            from app.celery_app import celery_app

            celery_app.control.revoke(task_id, terminate=False)
        except Exception:
            logger.warning("Best-effort revoke failed for planning task %s", task_id)

    def release_processing_task(
        self,
        session_id: int,
        generation_id: str,
        owner_token: str,
        task_id: Optional[str],
    ) -> None:
        """Clear only this generation's observational Celery task ID."""

        if not task_id:
            return
        with self.db.no_autoflush:
            session = (
                self.db.query(PlanningSession)
                .filter(
                    PlanningSession.id == session_id,
                    PlanningSession.generation_id == generation_id,
                    PlanningSession.processing_task_id == task_id,
                )
                .populate_existing()
                .first()
            )
        if session is None:
            self.db.rollback()
            return
        # Token matching is required while the owner is still active.  After
        # terminalization/cancellation the token is intentionally NULL, so the
        # generation + task-id match remains the cleanup observation boundary.
        if session.processing_token not in {None, owner_token}:
            self.db.rollback()
            return
        session.processing_task_id = None
        session.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    @staticmethod
    def _should_process_inline() -> bool:
        return settings.INLINE_PLANNING

    @staticmethod
    def _looks_like_replan_prompt(prompt: str) -> bool:
        prompt_text = prompt or ""
        return (
            "## Failure Context" in prompt_text
            or "requires replanning" in prompt_text.lower()
        )

    def _is_replan_recovery_session(self, session: PlanningSession) -> bool:
        first_msg = session.messages[0] if session.messages else None
        metadata = getattr(first_msg, "metadata_json", None)
        if isinstance(metadata, dict) and metadata.get("replan_recovery"):
            return True
        return self._looks_like_replan_prompt(session.prompt or "")

    def _extract_replan_task_hint(self, prompt: str) -> tuple[str, str]:
        for line in (prompt or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            body = stripped[2:].strip()
            if not body:
                continue
            if ":" in body:
                title, detail = body.split(":", 1)
                title = title.strip(" -*")
                detail = detail.strip()
            else:
                title, detail = (
                    body.strip(" -*"),
                    "Review the failed execution context.",
                )
            if title:
                return title[:90], detail[:220]
        return "Recovered failed task", "Review the failed execution context."

    @staticmethod
    def _planner_field(text: str) -> str:
        return re.sub(r"[|\n\r]+", " ", text or "").strip()

    def _build_replan_recovery_artifacts(
        self, session: PlanningSession, project: Project
    ) -> dict[str, str]:
        failed_title, failure_detail = self._extract_replan_task_hint(session.prompt)
        safe_failed_title = self._planner_field(failed_title)
        safe_failure_detail = self._planner_field(failure_detail)
        project_name = self._planner_field(project.name or "Project")

        planner_markdown = "\n".join(
            [
                f"# Project: {project_name}",
                "",
                "## Task List",
                (
                    "- [ ] TASK_START: Diagnose recovered failure"
                    f" | Inspect the failure context for {safe_failed_title}: {safe_failure_detail}"
                    " | order=1 | P1 | effort=small | stage=diagnose | profile=review_only"
                ),
                (
                    "- [ ] TASK_START: Plan bounded recovery approach"
                    f" | Turn the failure context for {safe_failed_title} into a minimal repair plan without changing files"
                    " | order=2 | P1 | effort=small | stage=plan | profile=review_only"
                ),
                (
                    "- [ ] TASK_START: Apply targeted recovery fix"
                    f" | Implement the smallest safe fix for {safe_failed_title} based on the recovered failure context"
                    " | order=3 | P1 | effort=medium | stage=debug | profile=debug_only"
                ),
                (
                    "- [ ] TASK_START: Validate recovery path"
                    " | Run focused validation for the recovery fix and confirm the session can continue without repeating the failure"
                    " | order=4 | P1 | effort=small | stage=validate | profile=test_only"
                ),
                (
                    "- [ ] TASK_START: Review recovery outcome"
                    " | Audit the changed files, validation evidence, and remaining session risks after the recovery fix"
                    " | order=5 | P2 | effort=small | stage=complete | profile=review_only"
                ),
            ]
        )

        return {
            "requirements": (
                "# Requirements\n\n"
                "- Recover from the failed execution using the recorded failure summary.\n"
                "- Keep the recovery narrow and avoid unrelated project changes.\n"
                "- Verify the original failure mode no longer repeats."
            ),
            "design": (
                "# Design\n\n"
                "Use the deterministic recovery plan because model planning synthesis "
                "timed out or returned malformed output. The recovery stays scoped to "
                f"`{safe_failed_title}` and relies on focused diagnosis, a targeted fix, "
                "and validation before continuing."
            ),
            "implementation_plan": (
                "# Implementation Plan\n\n"
                "1. Inspect the failed task logs and current workspace state.\n"
                "2. Write down the smallest bounded recovery approach.\n"
                "3. Apply only the fix that addresses the recorded root cause.\n"
                "4. Run focused validation for the original failure mode.\n"
                "5. Review evidence and remaining risk before continuing."
            ),
            "planner_markdown": planner_markdown,
        }

    def _advance_or_finalize(
        self,
        session: PlanningSession,
        project: Project,
        *,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        # Skip Q&A entirely for sessions that carry full context (e.g. replan).
        first_msg = session.messages[0] if session.messages else None
        if (
            first_msg
            and isinstance(getattr(first_msg, "metadata_json", None), dict)
            and first_msg.metadata_json.get("skip_clarification")
        ):
            self._finalize_session(
                session,
                project,
                generation_id=generation_id,
                owner_token=owner_token,
            )
            return

        question_count = len([m for m in session.messages if m.role == "assistant"])
        try:
            decision = self._decide_clarification(
                session,
                project,
                generation_id=generation_id,
                owner_token=owner_token,
            )
        except TypeError as exc:
            # Preserve older test/in-process seams while the production worker
            # uses the explicit owner context.
            if "generation_id" not in str(exc) and "owner_token" not in str(exc):
                raise
            decision = self._decide_clarification(session, project)
        if decision["needs_clarification"] and question_count < self.MAX_QUESTIONS:
            question = decision["question"]
            prompt_id = f"prompt-{uuid.uuid4().hex[:12]}"
            self._assert_owner_if_present(session, generation_id, owner_token)
            session.status = "waiting_for_input"
            session.current_prompt_id = prompt_id
            session.updated_at = datetime.now(timezone.utc)
            self._add_message(
                session,
                "assistant",
                question,
                prompt_id=prompt_id,
                metadata={"kind": "clarifying_question"},
                generation_id=generation_id,
                owner_token=owner_token,
            )
            return

        self._finalize_session(
            session,
            project,
            generation_id=generation_id,
            owner_token=owner_token,
        )

    def _finalize_session(
        self,
        session: PlanningSession,
        project: Project,
        *,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        prompt = self._build_synthesis_prompt(session, project)
        is_replan_recovery = self._is_replan_recovery_session(session)
        timeout_seconds = (
            self.REPLAN_SYNTHESIS_TIMEOUT_SECONDS
            if is_replan_recovery
            else self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS
        )
        used_replan_fallback = False
        replan_fallback_error = ""
        result: dict[str, Any] = {}
        try:
            result = self._run_openclaw_with_fallback(
                prompt,
                source_brain=session.source_brain,
                timeout_seconds=timeout_seconds,
                project_id=project.id,
            )
            self._assert_owner_if_present(session, generation_id, owner_token)
            artifacts = self._parse_finalization_payload(result)
        except HTTPException:
            raise
        except Exception as first_exc:
            if is_replan_recovery:
                used_replan_fallback = True
                replan_fallback_error = str(first_exc)[:500]
                artifacts = self._build_replan_recovery_artifacts(session, project)
            else:
                # First attempt failed (e.g. OpenClaw returned empty output). Retry with compact prompt.
                try:
                    compact = self._build_compact_synthesis_prompt(prompt)
                    result = self._invoke_openclaw(
                        compact,
                        source_brain=session.source_brain,
                        timeout_seconds=timeout_seconds,
                        project_id=project.id,
                    )
                    self._assert_owner_if_present(session, generation_id, owner_token)
                    artifacts = self._parse_finalization_payload(result)
                except HTTPException:
                    raise
                except Exception as exc:
                    self._capture_finalization_parse_failure(
                        session,
                        result,
                        exc,
                        attempt="compact_retry",
                        first_attempt_error=str(first_exc),
                        generation_id=generation_id,
                        owner_token=owner_token,
                    )
                    self._assert_owner_if_present(session, generation_id, owner_token)
                    session.status = "failed"
                    session.last_error = str(exc)
                    session.current_prompt_id = None
                    session.updated_at = datetime.now(timezone.utc)
                    return

            if is_replan_recovery:
                self._add_message(
                    session,
                    "assistant",
                    (
                        "Planning model synthesis timed out or returned malformed output; "
                        "used deterministic replan markdown instead."
                    ),
                    metadata={
                        "kind": "replan_fallback",
                        "error": replan_fallback_error,
                    },
                    generation_id=generation_id,
                    owner_token=owner_token,
                )

        planner_markdown = artifacts.get("planner_markdown", "")
        parsed_tasks = PlannerService.parse_markdown(planner_markdown)
        if is_replan_recovery and self._replan_tasks_need_scope_fallback(parsed_tasks):
            artifacts = self._build_replan_recovery_artifacts(session, project)
            planner_markdown = artifacts.get("planner_markdown", "")
            parsed_tasks = PlannerService.parse_markdown(planner_markdown)
            self._add_message(
                session,
                "assistant",
                (
                    "Planning model produced full-lifecycle recovery tasks; used "
                    "deterministic scoped replan markdown instead."
                ),
                metadata={"kind": "replan_scope_fallback"},
                generation_id=generation_id,
                owner_token=owner_token,
            )
        if not planner_markdown or not parsed_tasks:
            self._assert_owner_if_present(session, generation_id, owner_token)
            session.status = "failed"
            session.last_error = (
                "Planning synthesis did not produce parseable planner markdown"
            )
            session.current_prompt_id = None
            session.updated_at = datetime.now(timezone.utc)
            return

        artifact_specs = {
            "requirements": ("requirements.md", artifacts.get("requirements", "")),
            "design": ("design.md", artifacts.get("design", "")),
            "implementation_plan": (
                "implementation_plan.md",
                artifacts.get("implementation_plan", ""),
            ),
            "planner_markdown": ("planner.md", planner_markdown),
        }
        for artifact_type, (filename, content) in artifact_specs.items():
            self._append_artifact_version(
                session,
                artifact_type=artifact_type,
                filename=filename,
                content=content.strip(),
                generation_id=generation_id,
                owner_token=owner_token,
            )

        self._add_message(
            session,
            "assistant",
            "Planning complete. Review the artifacts and task preview, then commit when ready.",
            metadata={"kind": "completion"},
            generation_id=generation_id,
            owner_token=owner_token,
        )
        self._assert_owner_if_present(session, generation_id, owner_token)
        session.status = "completed"
        session.current_prompt_id = None
        session.completed_at = datetime.now(timezone.utc)
        session.updated_at = datetime.now(timezone.utc)
        session.last_error = None

    @staticmethod
    def _replan_tasks_need_scope_fallback(tasks: list[Any]) -> bool:
        if not tasks:
            return False
        profiles = [
            getattr(task, "execution_profile", "full_lifecycle") for task in tasks
        ]
        scoped_profiles = {"review_only", "test_only", "debug_only"}
        return not any(profile in scoped_profiles for profile in profiles)

    def _run_openclaw(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Execute planning synthesis through the active backend runtime."""
        effective_timeout = int(
            timeout_seconds or self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS
        )
        try:
            return invoke_runtime_prompt(
                self.db,
                prompt,
                session_id=None,
                project_id=project_id,
                task_id=None,
                source_brain=source_brain,
                timeout_seconds=effective_timeout,
                no_output_timeout_seconds=max(
                    1,
                    min(effective_timeout, PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS),
                ),
                session_prefix="planning",
                role=BackendRole.PLANNING,
            )
        except AgentRuntimeError as exc:
            raise RuntimeError(str(exc))

    def _run_openclaw_with_fallback(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        result = self._invoke_openclaw(
            prompt,
            source_brain=source_brain,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
        )
        if not runtime_reports_context_overflow(
            self.db,
            result,
            role=BackendRole.PLANNING,
        ):
            return result

        compact_prompt = self._build_compact_synthesis_prompt(prompt)
        if compact_prompt == prompt:
            return result
        return self._invoke_openclaw(
            compact_prompt,
            source_brain=source_brain,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
        )

    def _invoke_openclaw(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Run planning synthesis while tolerating older monkeypatched helpers used in tests.
        """

        try:
            return self._run_openclaw(
                prompt,
                source_brain=source_brain,
                timeout_seconds=timeout_seconds,
                project_id=project_id,
            )
        except TypeError as exc:
            error_text = str(exc)
            unsupported = {
                name
                for name in ("source_brain", "timeout_seconds", "project_id")
                if name in error_text
            }
            if not unsupported:
                raise
            fallback_kwargs = {
                "source_brain": source_brain,
                "timeout_seconds": timeout_seconds,
            }
            fallback_kwargs = {
                key: value
                for key, value in fallback_kwargs.items()
                if key not in unsupported
            }
            try:
                return self._run_openclaw(prompt, **fallback_kwargs)
            except TypeError as second_exc:
                second_text = str(second_exc)
                if (
                    "source_brain" not in second_text
                    and "timeout_seconds" not in second_text
                ):
                    raise
                fallback_kwargs.pop("timeout_seconds", None)
                fallback_kwargs.pop("source_brain", None)
                return self._run_openclaw(prompt, **fallback_kwargs)

    def _parse_finalization_payload(self, result: dict[str, Any]) -> dict[str, str]:
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error") or "Planning synthesis failed")

        output_text = self._extract_output_text(
            result,
            context="finalization",
            allow_parse_fallback=False,
        )

        if not isinstance(output_text, str):
            raise RuntimeError("Planning synthesis returned unsupported output")

        cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", output_text.strip())
        self._ensure_output_size(cleaned, context="finalization")
        parsed = json.loads(cleaned)
        required_keys = {
            "requirements",
            "design",
            "implementation_plan",
            "planner_markdown",
        }
        if not isinstance(parsed, dict) or not required_keys.issubset(parsed):
            raise RuntimeError("Planning synthesis returned malformed artifact payload")
        return {key: str(parsed.get(key, "")).strip() for key in required_keys}

    def _capture_finalization_parse_failure(
        self,
        session: PlanningSession,
        result: dict[str, Any],
        exc: Exception,
        *,
        attempt: str,
        first_attempt_error: Optional[str] = None,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        output_text = self._extract_output_text(
            result,
            context="finalization_diagnostic",
            allow_parse_fallback=False,
        )
        if not isinstance(output_text, str):
            output_text = ""
        cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", output_text.strip())
        raw_digest = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
        metadata: dict[str, Any] = {
            "kind": "planning_synthesis_parse_failure",
            "prompt_phase": "planning_synthesis",
            "attempt": attempt,
            "backend": result.get("backend"),
            "model_family": result.get("model_family"),
            "status": result.get("status"),
            "response_chars": len(output_text),
            "cleaned_chars": len(cleaned),
            "raw_sha256": raw_digest,
            "parse_error": str(exc),
            "classification": self._classify_finalization_parse_failure(cleaned, exc),
            "raw_excerpt_head": self._redacted_diagnostic_excerpt(output_text[:1200]),
            "raw_excerpt_tail": self._redacted_diagnostic_excerpt(output_text[-1200:]),
        }
        if first_attempt_error:
            metadata["first_attempt_error"] = first_attempt_error[:500]
        if isinstance(exc, json.JSONDecodeError):
            metadata.update(
                {
                    "json_error_message": exc.msg,
                    "json_error_line": exc.lineno,
                    "json_error_column": exc.colno,
                    "json_error_position": exc.pos,
                }
            )
        self._append_artifact_version(
            session,
            artifact_type="planning_synthesis_parse_failure_diagnostic",
            filename="planning_synthesis_parse_failure.json",
            content=json.dumps(metadata, indent=2, sort_keys=True),
            generation_id=generation_id,
            owner_token=owner_token,
        )

    @staticmethod
    def _classify_finalization_parse_failure(
        cleaned_output: str, exc: Exception
    ) -> str:
        if not cleaned_output.strip():
            return "empty_output"
        if isinstance(exc, json.JSONDecodeError):
            lowered = str(exc).lower()
            near_end = exc.pos >= max(len(cleaned_output) - 5, 0)
            if near_end or "unterminated" in lowered:
                return "incomplete_or_truncated_json"
            return "malformed_json_syntax"
        return "malformed_artifact_payload"

    @staticmethod
    def _redacted_diagnostic_excerpt(text: str) -> str:
        redacted = re.sub(
            r"(?i)(api[_-]?key|token|secret|password)([\"'\s:=]+)([^\"'\s,}]+)",
            r"\1\2[REDACTED]",
            text,
        )
        return redacted

    def _build_synthesis_prompt(
        self, session: PlanningSession, project: Project
    ) -> str:
        transcript = self._build_condensed_transcript(session)
        project_description = self._trim_text(project.description or "", 280)
        project_rules = self._trim_text(project.project_rules or "", 280)
        prompt = self._render_adapted_prompt(
            objective="Create implementation-planning artifacts for a software project.",
            execution_mode="planning_synthesis",
            instructions=[
                "Return JSON only with exactly these keys: requirements, design, implementation_plan, planner_markdown.",
                "requirements must be markdown with goals, scope, constraints, and acceptance criteria.",
                "design must be markdown with architecture, interfaces, data flow, and risks.",
                "implementation_plan must be markdown with ordered steps and test strategy.",
                "planner_markdown must be markdown compatible with an Orchestrator task list using: ## Task List then - [ ] TASK_START: Title | Description | order=1 | P1 | effort=medium | profile=full_lifecycle.",
                "planner_markdown must contain between 3 and 8 concrete tasks.",
                "Prefer implementation detail grounded in the prompt and transcript.",
                "Do not include prose outside the JSON object.",
            ],
            context={
                "Project": project.name,
                "Project description": project_description or "None provided",
                "Project rules": project_rules or "None provided",
                "Planning prompt": self._trim_text(session.prompt, 600),
                "Conversation transcript": transcript,
            },
            expected_output="A JSON object with requirements, design, implementation_plan, and planner_markdown.",
        )
        return optimize_prompt(
            prompt,
            max_tokens=1400,
            hard_char_limit=self.SYNTHESIS_PROMPT_CHAR_BUDGET,
        )

    def _build_compact_synthesis_prompt(self, prompt: str) -> str:
        compact = optimize_prompt(prompt, max_tokens=700, hard_char_limit=2200)
        compact += (
            "\n\nKeep every artifact concise. Prefer short markdown sections and compact "
            "task descriptions.\n\n"
            "COMPACT RETRY OUTPUT CONTRACT:\n"
            "Return exactly one top-level JSON object. The first non-whitespace "
            "character must be { and the last non-whitespace character must be }.\n"
            "The object must contain exactly these artifact keys: requirements, "
            "design, implementation_plan, planner_markdown.\n"
            "Do not return a top-level array. Do not return step objects, "
            "task-plan arrays, implementation-plan arrays, or objects with "
            "top-level step/title/description fields.\n"
            "TASK_START lines are allowed only inside the planner_markdown string "
            "value. They must not appear as top-level array items or top-level "
            "step objects.\n"
            "Do not include prose outside the JSON object."
        )
        return compact

    def _heuristic_needs_clarification(self, session: PlanningSession) -> bool:
        responses = [m for m in session.messages if m.role == "user"][1:]
        if responses:
            combined = " ".join(message.content for message in responses)
            return len(combined.split()) < 8

        prompt = session.prompt.lower()
        if len(prompt.split()) < 10:
            return True
        strong_detail_markers = (
            "api",
            "database",
            "frontend",
            "backend",
            "auth",
            "mobile",
            "websocket",
            "test",
            "integration",
            "migration",
            "dashboard",
        )
        return sum(marker in prompt for marker in strong_detail_markers) < 2

    def _heuristic_next_question(self, session: PlanningSession) -> str:
        responses = [m for m in session.messages if m.role == "user"][1:]
        if not responses:
            return (
                "What outcome should this planning session optimize for, and are there any "
                "must-keep constraints around users, integrations, or rollout?"
            )
        return (
            "What acceptance criteria or implementation constraints would make this plan "
            "feel complete enough to execute safely?"
        )

    def _decide_clarification(
        self,
        session: PlanningSession,
        project: Project,
        *,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> dict[str, Any]:
        heuristic_question = self._heuristic_next_question(session)
        heuristic_needs = self._heuristic_needs_clarification(session)
        user_followups = [m for m in session.messages if m.role == "user"][1:]
        question_count = len([m for m in session.messages if m.role == "assistant"])
        if question_count >= self.MAX_QUESTIONS:
            return {"needs_clarification": False, "question": None}

        # Keep the first turn deterministic for obviously underspecified prompts so
        # planning sessions don't jump straight to completion when a runtime is live.
        if heuristic_needs and not user_followups:
            return {"needs_clarification": True, "question": heuristic_question}

        prompt = self._build_clarification_prompt(
            session,
            project,
            fallback_question=heuristic_question,
        )
        try:
            result = self._run_openclaw_with_fallback(
                prompt,
                source_brain=session.source_brain,
                project_id=project.id,
            )
            self._assert_owner_if_present(session, generation_id, owner_token)
            return self._parse_clarification_payload(
                result,
                fallback_needs=heuristic_needs,
                fallback_question=heuristic_question,
            )
        except Exception:
            return {
                "needs_clarification": heuristic_needs,
                "question": heuristic_question if heuristic_needs else None,
            }

    def _build_clarification_prompt(
        self,
        session: PlanningSession,
        project: Project,
        *,
        fallback_question: str,
    ) -> str:
        transcript = self._build_condensed_transcript(session)
        project_description = self._trim_text(project.description or "", 220)
        project_rules = self._trim_text(project.project_rules or "", 220)
        prompt = self._render_adapted_prompt(
            objective=(
                "Decide whether a planning conversation needs one more clarifying "
                "question before final plan synthesis."
            ),
            execution_mode="planning_clarification",
            instructions=[
                "Return JSON only with exactly these keys: needs_clarification, question.",
                "Set needs_clarification to true only if one more user answer would materially improve implementation safety or task quality.",
                "If needs_clarification is false, set question to an empty string.",
                "If needs_clarification is true, question must be a single concrete question under 30 words, focused on the most important missing constraint or acceptance criterion.",
                "Avoid repeating already-answered questions.",
                f"If uncertain, prefer this fallback question: {fallback_question}",
            ],
            context={
                "Project": project.name,
                "Project description": project_description or "None provided",
                "Project rules": project_rules or "None provided",
                "Planning prompt": self._trim_text(session.prompt, 500),
                "Conversation transcript": transcript,
            },
            expected_output=("A JSON object with needs_clarification and question."),
        )
        return optimize_prompt(prompt, max_tokens=500, hard_char_limit=2200)

    def _render_adapted_prompt(
        self,
        *,
        objective: str,
        execution_mode: str,
        instructions: list[str],
        context: dict[str, Any],
        expected_output: str,
    ) -> str:
        configuration = resolve_planning_runtime_configuration(self.db)
        envelope = PromptEnvelope(
            objective=objective,
            execution_mode=execution_mode,
            instructions=instructions,
            context=context,
            expected_output=expected_output,
        )
        return render_prompt_for_profile(configuration.adaptation_profile, envelope)

    def _parse_clarification_payload(
        self,
        result: dict[str, Any],
        *,
        fallback_needs: bool,
        fallback_question: str,
    ) -> dict[str, Any]:
        if result.get("status") == "failed":
            return {
                "needs_clarification": fallback_needs,
                "question": fallback_question if fallback_needs else None,
            }

        output_text = self._extract_output_text(
            result,
            context="clarification",
            allow_parse_fallback=True,
        )

        if not isinstance(output_text, str):
            return {
                "needs_clarification": fallback_needs,
                "question": fallback_question if fallback_needs else None,
            }

        cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", output_text.strip())
        self._ensure_output_size(cleaned, context="clarification")
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Planning clarification JSON parse failed: %s output_excerpt=%r",
                exc,
                cleaned[:1000],
            )
            return {
                "needs_clarification": fallback_needs,
                "question": fallback_question if fallback_needs else None,
            }

        if not isinstance(parsed, dict) or (
            "needs_clarification" not in parsed and "question" not in parsed
        ):
            return {
                "needs_clarification": fallback_needs,
                "question": fallback_question if fallback_needs else None,
            }

        needs_clarification = bool(parsed.get("needs_clarification"))
        question = str(parsed.get("question", "") or "").strip()
        if needs_clarification and not question:
            question = fallback_question
        if not needs_clarification:
            question = None
        return {
            "needs_clarification": needs_clarification,
            "question": question,
        }

    def _add_message(
        self,
        session: PlanningSession,
        role: str,
        content: str,
        *,
        prompt_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> PlanningMessage:
        self._assert_owner_if_present(session, generation_id, owner_token)
        message = PlanningMessage(
            planning_session_id=session.id,
            role=role,
            prompt_id=prompt_id,
            content=content.strip(),
            metadata_json=metadata,
        )
        self.db.add(message)
        session.messages.append(message)
        return message

    def _generate_title(self, prompt: str) -> str:
        normalized = " ".join((prompt or "").split())
        return normalized[:57] + "..." if len(normalized) > 60 else normalized

    def _build_condensed_transcript(self, session: PlanningSession) -> str:
        rendered: list[str] = []
        total_chars = 0
        for message in session.messages[-6:]:
            speaker = "Planner" if message.role == "assistant" else "User"
            line = f"{speaker}: {self._trim_text(message.content, 260)}"
            if (
                total_chars + len(line) > self.SYNTHESIS_TRANSCRIPT_CHAR_BUDGET
                and rendered
            ):
                break
            rendered.append(line)
            total_chars += len(line)
        if not rendered:
            return "No conversation transcript available."
        return "\n".join(rendered)

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 3].rstrip() + "..."

    def _get_artifact_content(
        self, session: PlanningSession, artifact_type: str
    ) -> Optional[str]:
        for artifact in self._latest_artifacts(session):
            if artifact.artifact_type == artifact_type:
                return artifact.content
        return None

    def _latest_artifacts(self, session: PlanningSession) -> list[PlanningArtifact]:
        latest = (
            self.db.query(PlanningArtifact)
            .filter(
                PlanningArtifact.planning_session_id == session.id,
                PlanningArtifact.is_latest.is_(True),
            )
            .order_by(PlanningArtifact.artifact_type.asc(), PlanningArtifact.id.desc())
            .all()
        )
        if latest:
            return latest

        fallback: dict[str, PlanningArtifact] = {}
        artifacts = (
            self.db.query(PlanningArtifact)
            .filter(PlanningArtifact.planning_session_id == session.id)
            .order_by(
                PlanningArtifact.artifact_type.asc(),
                PlanningArtifact.version.asc(),
                PlanningArtifact.id.asc(),
            )
            .all()
        )
        for artifact in artifacts:
            fallback[artifact.artifact_type] = artifact
        return list(fallback.values())

    def _append_artifact_version(
        self,
        session: PlanningSession,
        *,
        artifact_type: str,
        filename: str,
        content: str,
        generation_id: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        self._assert_owner_if_present(session, generation_id, owner_token)
        latest = next(
            (
                artifact
                for artifact in self._latest_artifacts(session)
                if artifact.artifact_type == artifact_type
            ),
            None,
        )
        next_version = 1
        if latest is not None:
            latest.is_latest = False
            next_version = (latest.version or 1) + 1
        artifact = PlanningArtifact(
            planning_session_id=session.id,
            artifact_type=artifact_type,
            filename=filename,
            content=content.strip(),
            version=next_version,
            is_latest=True,
        )
        self.db.add(artifact)
        session.artifacts.append(artifact)

    def _load_committed_task_ids(self, session: PlanningSession) -> list[int]:
        if not session.committed_task_ids:
            return []
        try:
            parsed = json.loads(session.committed_task_ids)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Planning session {session.id} has corrupted committed_task_ids"
            ) from exc
        if not isinstance(parsed, list):
            raise RuntimeError(
                f"Planning session {session.id} committed_task_ids must be a list"
            )
        task_ids: list[int] = []
        for item in parsed:
            if isinstance(item, int):
                task_ids.append(item)
            elif isinstance(item, str) and item.isdigit():
                task_ids.append(int(item))
            else:
                raise RuntimeError(
                    f"Planning session {session.id} committed_task_ids contains invalid value"
                )
        return task_ids

    def _extract_output_text(
        self,
        result: dict[str, Any],
        *,
        context: str,
        allow_parse_fallback: bool,
    ) -> Any:
        output_text = result.get("output", "")
        if not isinstance(output_text, str):
            return output_text
        self._ensure_output_size(output_text, context=context)
        try:
            parsed_output = json.loads(output_text)
        except json.JSONDecodeError as exc:
            if allow_parse_fallback:
                logger.warning(
                    "Planning %s envelope JSON parse failed: %s output_excerpt=%r",
                    context,
                    exc,
                    output_text[:1000],
                )
            return output_text
        if isinstance(parsed_output, dict) and "payloads" in parsed_output:
            payloads = parsed_output.get("payloads") or []
            if payloads and isinstance(payloads[0], dict):
                nested_text = payloads[0].get("text", output_text)
                if isinstance(nested_text, str):
                    self._ensure_output_size(nested_text, context=context)
                return nested_text
        return output_text

    def _ensure_output_size(self, output_text: str, *, context: str) -> None:
        if len(output_text or "") > self.SYNTHESIS_OUTPUT_CHAR_LIMIT:
            raise RuntimeError(
                f"Planning {context} returned oversized output "
                f"({len(output_text)} chars)"
            )
