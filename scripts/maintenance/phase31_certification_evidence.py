#!/usr/bin/env python3
"""Phase 31B certification evidence pipeline.

Writes the artifacts the Phase 31 evidence program requires
(`docs/roadmap/workflow/phase31/phase31-evidence-program.md` Sections 1-2),
under `docs/roadmap/reports/evidence/phase31<letter>-<slug>/`, in the exact
naming shape that document specifies. It invents no new record shape: the
session preamble/closing records, scenario evidence record, and operator
notes are the shapes that document already defines; this module just
serializes them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def openclaw_json_checksum(path: Optional[Path] = None) -> Optional[str]:
    target = path or Path("/root/.openclaw/openclaw.json")
    if not target.exists():
        return None
    return hashlib.sha256(target.read_bytes()).hexdigest()


class CertificationEvidenceSession:
    """One certification session's evidence directory and writers.

    `evidence_dir` follows the naming convention exactly:
    `docs/roadmap/reports/evidence/phase31<letter>-<slug>/`.
    """

    def __init__(self, phase_letter: str, slug: str, session_number: int) -> None:
        self.evidence_dir = (
            REPO_ROOT
            / "docs/roadmap/reports/evidence"
            / f"phase31{phase_letter}-{slug}"
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.chmod(0o775)
        self.session_number = session_number
        self._notes_path = (
            self.evidence_dir / f"session-{session_number}-operator-notes.md"
        )
        if not self._notes_path.exists():
            self._notes_path.write_text(
                f"# Session {session_number} operator notes\n\n", encoding="utf-8"
            )
            self._notes_path.chmod(0o664)

    def _write_json(self, filename: str, payload: dict[str, Any]) -> Path:
        path = self.evidence_dir / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o664)
        return path

    def note(self, message: str) -> None:
        with self._notes_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {now_iso()} {message}\n")

    def write_preamble(
        self,
        *,
        f10_result: dict[str, Any],
        f11_result: dict[str, Any],
        environment_baseline: dict[str, Any],
        operator_identity: str,
        declared_scenario_set: list[str],
        dispatch_budget: int,
    ) -> Path:
        payload = {
            "session_number": self.session_number,
            "start_timestamp": now_iso(),
            "operator_identity": operator_identity,
            "declared_scenario_set": declared_scenario_set,
            "dispatch_budget": dispatch_budget,
            "f10_result": f10_result,
            "f11_result": f11_result,
            "environment_baseline": environment_baseline,
        }
        self.note(
            f"session start: scenarios={declared_scenario_set} "
            f"f10_passed={f10_result.get('passed')} f11_passed={f11_result.get('passed')}"
        )
        return self._write_json(f"session-{self.session_number}-preamble.json", payload)

    def write_closing(
        self,
        *,
        f11_result: dict[str, Any],
        environment_baseline: dict[str, Any],
        residue_comparison: dict[str, Any],
    ) -> Path:
        payload = {
            "session_number": self.session_number,
            "end_timestamp": now_iso(),
            "closing_f11_result": f11_result,
            "environment_baseline_at_close": environment_baseline,
            "residue_comparison": residue_comparison,
        }
        self.note(f"session end: f11_passed={f11_result.get('passed')}")
        return self._write_json(f"session-{self.session_number}-closing.json", payload)

    def write_scenario_record(
        self,
        *,
        scenario_id: str,
        run: int,
        contract: Any,
        facts: Any,
        result: Any,
        provider_identity: Optional[str],
        pre_hash: Optional[str],
        post_hash: Optional[str],
        timings: dict[str, Any],
        repair_telemetry: list[dict[str, Any]],
        event_journal_pointer: Optional[str],
    ) -> Path:
        payload = {
            "scenario_id": scenario_id,
            "run": run,
            "declared_contract": (
                asdict(contract) if is_dataclass(contract) else contract
            ),
            "captured_facts": (asdict(facts) if is_dataclass(facts) else facts),
            "outcome_class": (
                result.outcome_class.value
                if hasattr(result.outcome_class, "value")
                else result.outcome_class
            ),
            "result": result.to_dict() if hasattr(result, "to_dict") else result,
            "provider_identity": provider_identity,
            "pre_run_content_hash": pre_hash,
            "post_run_content_hash": post_hash,
            "timings": timings,
            "repair_telemetry": repair_telemetry,
            "event_journal_pointer": event_journal_pointer,
            "captured_at": now_iso(),
        }
        self.note(
            f"scenario {scenario_id} r{run}: " f"outcome={payload['outcome_class']}"
        )
        return self._write_json(f"{scenario_id.lower()}-r{run}.json", payload)

    def write_replay_result(
        self, *, scenario_id: str, run: int, replay_comparison: dict[str, Any]
    ) -> Path:
        payload = {
            "scenario_id": scenario_id,
            "run": run,
            "replayed_at": now_iso(),
            "match": replay_comparison.get("match"),
            "differences": replay_comparison.get("differences", []),
        }
        self.note(f"replay {scenario_id} r{run}: match={payload['match']}")
        return self._write_json(f"{scenario_id.lower()}-r{run}-replay.json", payload)

    def write_certification_summary(self, payload: dict[str, Any]) -> Path:
        return self._write_json(
            f"session-{self.session_number}-certification-summary.json", payload
        )
