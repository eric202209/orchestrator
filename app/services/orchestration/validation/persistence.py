"""Persistence helpers for validation results."""

import json
from typing import Optional

from sqlalchemy.orm import Session

from app.models import TaskCheckpoint
from ..types import ValidationVerdict


def persist_validation_result(
    db: Session,
    *,
    task_id: int,
    session_id: Optional[int],
    stage: str,
    verdict: ValidationVerdict,
    step_number: Optional[int] = None,
) -> None:
    db.add(
        TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            checkpoint_type=f"validation_{stage}",
            step_number=step_number,
            description=f"{stage}:{verdict.status}",
            state_snapshot=json.dumps(verdict.to_dict()),
        )
    )
