"""Execution-capacity admission evidence for deployment gating.

Phase 22B-1X1. Provider reachability is not execution capacity: the R5
dogfood attempt passed every readiness check and then failed dispatch because
the only ``local_openclaw`` slot was held by a lease whose worker no longer
existed. Deployment admission therefore has to observe *usable* capacity,
after canonical slot reconciliation, and fail closed when ownership cannot be
established.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session as DbSession

from app.services.agents.backend_slot_reconciliation import (
    DECISION_AMBIGUOUS_RETAINED,
    DECISION_RELEASE_FAILED,
    reconcile_backend_slots,
)

CAPACITY_SCHEMA_VERSION = "backend-capacity-admission/1.0"

CAPACITY_OK = "capacity_available"
CAPACITY_UNAVAILABLE = "backend_capacity_unavailable"
CAPACITY_AMBIGUOUS = "backend_slot_ownership_ambiguous"
CAPACITY_RECONCILIATION_FAILED = "backend_slot_reconciliation_failed"
CAPACITY_UNGOVERNED = "capacity_ungoverned"


def evaluate_backend_capacity(
    db: DbSession,
    redis_client: Any,
    backend_id: str,
    max_slots: Optional[int],
    *,
    now: Optional[datetime] = None,
    reconcile: bool = True,
) -> Dict[str, Any]:
    """Return structured usable-capacity evidence for one backend.

    ``capacity_available`` is True only when reconciliation succeeded, no slot
    ownership was ambiguous, and at least one slot is free.
    """

    evaluated_at = (now or datetime.now(UTC)).isoformat()
    base: Dict[str, Any] = {
        "schema_version": CAPACITY_SCHEMA_VERSION,
        "backend_id": backend_id,
        "evaluated_at": evaluated_at,
        "max_slots": max_slots,
        "configured_capacity": max_slots,
        "active_valid_count": None,
        "stale_reconciled_count": 0,
        "ambiguous_count": 0,
        "available_count": None,
        "capacity_available": False,
        "status_code": CAPACITY_UNAVAILABLE,
        "reconciliation": None,
    }

    if max_slots is None:
        # Backends without a declared parallelism ceiling are not slot
        # governed; there is no lease that can block dispatch.
        base.update(
            {
                "capacity_available": True,
                "status_code": CAPACITY_UNGOVERNED,
                "active_valid_count": 0,
                "available_count": None,
            }
        )
        return base

    try:
        reconciliation = reconcile_backend_slots(
            db, redis_client, backend_id, now=now, dry_run=not reconcile
        )
    except Exception as exc:  # noqa: BLE001 - admission must fail closed
        base.update(
            {
                "status_code": CAPACITY_RECONCILIATION_FAILED,
                "error": str(exc)[:300],
            }
        )
        return base

    ambiguous = int(reconciliation["ambiguous"])
    release_failed = sum(
        1
        for decision in reconciliation["decisions"]
        if decision["decision"] == DECISION_RELEASE_FAILED
    )
    active_valid = sum(
        1
        for decision in reconciliation["decisions"]
        if decision["decision"]
        not in {DECISION_AMBIGUOUS_RETAINED, "RELEASED_STALE", DECISION_RELEASE_FAILED}
    )
    # Ambiguous and unreleasable members still occupy a slot.
    occupied = active_valid + ambiguous + release_failed
    available = max(0, int(max_slots) - occupied)

    base.update(
        {
            "active_valid_count": active_valid,
            "stale_reconciled_count": int(reconciliation["released_stale"]),
            "ambiguous_count": ambiguous,
            "release_failed_count": release_failed,
            "occupied_count": occupied,
            "available_count": available,
            "reconciliation": reconciliation,
        }
    )

    if release_failed:
        base["status_code"] = CAPACITY_RECONCILIATION_FAILED
        return base
    if ambiguous:
        base["status_code"] = CAPACITY_AMBIGUOUS
        return base
    if available <= 0:
        base["status_code"] = CAPACITY_UNAVAILABLE
        return base

    base["capacity_available"] = True
    base["status_code"] = CAPACITY_OK
    return base
