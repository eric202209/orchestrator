"""Prospective treatment manifest and drift detector (§19, §20).

RR1 creates and validates the *mechanism*.  It does not freeze an authorized
Stratum-2 treatment: a manifest produced here is a template whose final freeze
happens only at preregistration/authorization.

Drift rules:

``MATCH``
    Every treatment-relevant field was readable and equals the frozen value.
``DRIFT``
    A readable field differs from the frozen value.
``UNVERIFIABLE``
    A field could not be read, on either side.  Missing evidence is never
    ``MATCH``.

Both ``DRIFT`` and ``UNVERIFIABLE`` prevent launch.  The manifest is never
auto-updated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.services.research.rr1 import RR1_HARNESS_VERSION

MANIFEST_SCHEMA_VERSION = "rr1-treatment-manifest/1.0"

MATCH = "MATCH"
DRIFT = "DRIFT"
UNVERIFIABLE = "UNVERIFIABLE"

#: Sentinel stored when a field could not be read at capture time.
UNREADABLE = "__RR1_UNREADABLE__"

#: Every field here is treatment-relevant: a difference or an unreadable value
#: blocks launch.
TREATMENT_FIELDS: tuple[str, ...] = (
    "orchestrator_head",
    "orchestrator_tree",
    "lifecycle_authority_hash",
    "lifecycle_transitions_hash",
    "br1_admission_hash",
    "br2_reconciliation_hash",
    "pc1_hash",
    "planner_hash",
    "planning_policy_hash",
    "provider_routing",
    "provider_role_resolution",
    "timeouts",
    "retry_limits",
    "grounding_budgets",
    "sb1_values",
    "completion_repair_configuration",
    "capture_configuration",
    "harness_version",
    "oracle_hashes",
    "task_manifest_hashes",
)


@dataclass(frozen=True)
class FieldComparison:
    field: str
    verdict: str
    frozen: Any = None
    observed: Any = None
    detail: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "verdict": self.verdict,
            "frozen": self.frozen,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DriftReport:
    verdict: str
    launch_permitted: bool
    comparisons: tuple[FieldComparison, ...]
    compared_at: str

    @property
    def drifted_fields(self) -> tuple[str, ...]:
        return tuple(c.field for c in self.comparisons if c.verdict == DRIFT)

    @property
    def unverifiable_fields(self) -> tuple[str, ...]:
        return tuple(c.field for c in self.comparisons if c.verdict == UNVERIFIABLE)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "launch_permitted": self.launch_permitted,
            "drifted_fields": list(self.drifted_fields),
            "unverifiable_fields": list(self.unverifiable_fields),
            "compared_at": self.compared_at,
            "comparisons": [c.as_evidence() for c in self.comparisons],
        }


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return UNREADABLE
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return UNREADABLE
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return UNREADABLE
    if result.returncode != 0:
        return UNREADABLE
    return result.stdout.strip() or UNREADABLE


def _settings_values(names: tuple[str, ...]) -> dict[str, Any]:
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001
        return {name: UNREADABLE for name in names}
    values: dict[str, Any] = {}
    for name in names:
        value = getattr(settings, name, UNREADABLE)
        values[name] = value if _jsonable(value) else str(value)
    return values


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def capture_treatment_manifest(
    *,
    repo_root: str | Path,
    db: DbSession | None = None,
    oracle_paths: tuple[str | Path, ...] = (),
    task_manifest_paths: tuple[str | Path, ...] = (),
    capture_configuration: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Build a machine-readable prospective treatment manifest.

    This freezes the *mechanism*, not an authorized cohort.  Any value that
    cannot be read is stored as :data:`UNREADABLE` so the drift detector later
    reports ``UNVERIFIABLE`` instead of a false ``MATCH``.
    """

    root = Path(repo_root)
    from app.services.research.rr1.completion_repair_config import (
        capture_completion_repair_configuration,
    )
    from app.services.research.rr1.lanes import lane_inventory_evidence

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "label": label,
        "captured_at": datetime.now(UTC).isoformat(),
        "frozen_as_authorized_cohort": False,
        "orchestrator_head": _git(root, "rev-parse", "HEAD"),
        "orchestrator_tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "lifecycle_authority_hash": _sha256_file(
            root / "app/services/orchestration/lifecycle/authority.py"
        ),
        "lifecycle_transitions_hash": _sha256_file(
            root / "app/services/orchestration/lifecycle/transitions.py"
        ),
        "br1_admission_hash": _sha256_file(
            root / "app/services/orchestration/lifecycle/transitions.py"
        ),
        "br2_reconciliation_hash": _sha256_file(
            root / "app/services/orchestration/lifecycle/continuation_recovery.py"
        ),
        "pc1_hash": _sha256_file(
            root / "app/services/orchestration/planning/behavioral_repair_contract.py"
        ),
        "planner_hash": _sha256_file(
            root / "app/services/orchestration/planning/planner.py"
        ),
        "planning_policy_hash": _sha256_file(
            root / "app/services/orchestration/policy.py"
        ),
        "harness_version": RR1_HARNESS_VERSION,
        "capture_configuration": dict(capture_configuration or {})
        or lane_inventory_evidence(),
        "completion_repair_configuration": (
            capture_completion_repair_configuration(db).as_evidence()
        ),
    }

    manifest["provider_routing"] = _settings_values(
        (
            "AGENT_BACKEND",
            "AGENT_MODEL",
            "PLANNING_BACKEND",
            "EXECUTION_BACKEND",
            "REPAIR_BACKEND",
            "DEBUG_REPAIR_BACKEND",
            "COMPLETION_REPAIR_BACKEND",
            "PLANNER_MODEL",
            "EXECUTION_MODEL",
            "PLANNING_REPAIR_MODEL",
            "COMPLETION_REPAIR_MODEL",
            "DEBUG_REPAIR_MODEL",
            "OPENAI_BASE_URL",
            "OPENAI_CHAT_COMPLETIONS_BASE_URL",
            "OPENAI_CHAT_COMPLETIONS_MODEL",
            "OLLAMA_BASE_URL",
            "LOW_RESOURCE_SINGLE_MODEL",
        )
    )
    manifest["provider_role_resolution"] = _role_resolution(db)
    manifest["timeouts"] = _timeouts()
    manifest["retry_limits"] = _retry_limits()
    manifest["grounding_budgets"] = _settings_values(
        (
            "ENABLE_TYPED_GROUNDING_COORDINATOR",
            "TYPED_GROUNDING_MAX_STEPS",
            "TYPED_GROUNDING_MAX_PROVIDER_REQUESTS",
        )
    )
    # SB1 is a code-level source-grounding budget contract, not a settings
    # value: it is frozen by hashing the modules that declare it.
    manifest["sb1_values"] = {
        "post_plan_source_grounding_hash": _sha256_file(
            root / "app/services/orchestration/phases/post_plan_source_grounding.py"
        ),
        "grounding_consumer_hash": _sha256_file(
            root / "app/services/orchestration/planning/grounding/consumer.py"
        ),
    }
    manifest["oracle_hashes"] = {
        str(path): _sha256_file(Path(path)) for path in oracle_paths
    }
    manifest["task_manifest_hashes"] = {
        str(path): _sha256_file(Path(path)) for path in task_manifest_paths
    }
    return manifest


def _role_resolution(db: DbSession | None) -> dict[str, Any]:
    if db is None:
        return {"status": UNREADABLE, "reason": "no_database_context"}
    try:
        from app.services.agents.agent_runtime import (
            BackendRole,
            RuntimeCapabilityError,
            resolve_runtime_configuration,
        )
    except Exception:  # noqa: BLE001
        return {"status": UNREADABLE, "reason": "import_failed"}
    resolution: dict[str, Any] = {}
    for role in BackendRole:
        try:
            resolution[role.value] = resolve_runtime_configuration(db, role).to_dict()
        except RuntimeCapabilityError as exc:
            if (
                role is BackendRole.COMPLETION_REPAIR
                and exc.code == "provider_model_unavailable"
            ):
                # Completion repair is an optional lane and is intentionally
                # unconfigured on the CI/default profile. That is readable
                # configuration, not missing evidence; preserve the state in
                # the manifest without making the whole manifest unverifiable.
                resolution[role.value] = {
                    "status": "UNCONFIGURED",
                    "reason": str(exc),
                }
            else:
                resolution[role.value] = {
                    "status": UNREADABLE,
                    "reason": f"{type(exc).__name__}",
                }
        except Exception as exc:  # noqa: BLE001
            resolution[role.value] = {
                "status": UNREADABLE,
                "reason": f"{type(exc).__name__}",
            }
    return resolution


def _retry_limits() -> dict[str, Any]:
    """Freeze the retry budgets that actually bound a run."""

    values: dict[str, Any] = {}
    try:
        from app.services.orchestration.lifecycle.worker_capacity import (
            BACKEND_CAPACITY_RETRY_MAX_RETRIES,
        )

        values["BACKEND_CAPACITY_RETRY_MAX_RETRIES"] = (
            BACKEND_CAPACITY_RETRY_MAX_RETRIES
        )
    except Exception:  # noqa: BLE001
        values["BACKEND_CAPACITY_RETRY_MAX_RETRIES"] = UNREADABLE
    values.update(
        _settings_values(
            ("MAX_PLAN_STEPS", "CONTINUATION_RECONCILIATION_MAX_CANDIDATES")
        )
    )
    return values


def _timeouts() -> dict[str, Any]:
    values: dict[str, Any] = {}
    try:
        from app.services.orchestration import policy

        for name in dir(policy):
            if name.endswith("TIMEOUT_SECONDS") and name.isupper():
                values[name] = getattr(policy, name)
    except Exception:  # noqa: BLE001
        return {"status": UNREADABLE, "reason": "policy_unreadable"}
    return values or {"status": UNREADABLE, "reason": "no_timeout_constants"}


def _contains_unreadable(value: Any) -> bool:
    if value == UNREADABLE:
        return True
    if isinstance(value, dict):
        return any(_contains_unreadable(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unreadable(item) for item in value)
    return False


def compare_to_manifest(
    frozen: dict[str, Any],
    observed: dict[str, Any],
    *,
    fields: tuple[str, ...] = TREATMENT_FIELDS,
) -> DriftReport:
    """Compare current state against a frozen manifest.  Never auto-updates."""

    comparisons: list[FieldComparison] = []
    for name in fields:
        if name not in frozen or name not in observed:
            comparisons.append(
                FieldComparison(
                    field=name,
                    verdict=UNVERIFIABLE,
                    frozen=frozen.get(name),
                    observed=observed.get(name),
                    detail="field_absent_from_manifest_or_observation",
                )
            )
            continue
        frozen_value = frozen[name]
        observed_value = observed[name]
        if _contains_unreadable(frozen_value) or _contains_unreadable(observed_value):
            comparisons.append(
                FieldComparison(
                    field=name,
                    verdict=UNVERIFIABLE,
                    frozen=frozen_value,
                    observed=observed_value,
                    detail="unreadable_value_present",
                )
            )
            continue
        if _canonical(frozen_value) == _canonical(observed_value):
            comparisons.append(
                FieldComparison(
                    field=name,
                    verdict=MATCH,
                    frozen=frozen_value,
                    observed=observed_value,
                )
            )
        else:
            comparisons.append(
                FieldComparison(
                    field=name,
                    verdict=DRIFT,
                    frozen=frozen_value,
                    observed=observed_value,
                    detail="value_differs_from_frozen_manifest",
                )
            )

    if any(c.verdict == DRIFT for c in comparisons):
        verdict = DRIFT
    elif any(c.verdict == UNVERIFIABLE for c in comparisons):
        verdict = UNVERIFIABLE
    else:
        verdict = MATCH
    return DriftReport(
        verdict=verdict,
        launch_permitted=verdict == MATCH,
        comparisons=tuple(comparisons),
        compared_at=datetime.now(UTC).isoformat(),
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    """Persist a manifest with shared-workspace permissions."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o775)
    target.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(target, 0o664)
    return target
