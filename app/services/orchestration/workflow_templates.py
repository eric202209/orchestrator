"""Workflow template loader and review-policy override helpers.

Templates live as YAML files in docs/workflow-templates/. Adding a new template
requires only a new YAML file — no code change.

Templates configure review policy and allowed ops per task shape. The planner
receives no new rules from templates; only the workflow profile and policy change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "docs" / "workflow-templates"
)

# Condition name → evaluator(warning_flags: set[str]) → bool
_AUTO_PROMOTE_CONDITIONS: Dict[str, Any] = {
    "no_deletes": lambda wf: "deleted_files" not in wf,
    "no_dependency_changes": lambda wf: "dependency_files_changed" not in wf,
    "no_config_changes": lambda wf: "config_files_changed" not in wf,
    "no_secret_paths": lambda wf: "secret_path_write" not in wf,
    "no_high_risk_commands": lambda wf: "security_high_risk_command" not in wf,
    "no_large_change_sets": lambda wf: "more_than_10_changed_files" not in wf,
}

_HOLD_CONDITIONS: Dict[str, Any] = {
    "has_deletes": lambda wf: "deleted_files" in wf,
    "config_change": lambda wf: "config_files_changed" in wf,
    "dependency_change": lambda wf: "dependency_files_changed" in wf,
    "secret_path_write": lambda wf: "secret_path_write" in wf,
    "high_risk_command": lambda wf: "security_high_risk_command" in wf,
    "large_change_set": lambda wf: "more_than_10_changed_files" in wf,
    "always": lambda wf: True,
}

_VALID_AUTO_PROMOTE_CONDITIONS: frozenset = frozenset(_AUTO_PROMOTE_CONDITIONS)
_VALID_HOLD_CONDITIONS: frozenset = frozenset(_HOLD_CONDITIONS)


@dataclass
class WorkflowTemplate:
    id: str
    display_name: str
    workflow_profile: str
    review_policy: Dict[str, List[str]]
    allowed_ops: List[str]
    verification: str
    auto_promote_eligible: bool
    risk_flags: List[str] = field(default_factory=list)

    def evaluate_review_policy(self, warning_flags: Set[str]) -> Dict[str, Any]:
        """Return a policy signal dict for use in decide_change_set_review.

        Returns:
          {
            "forced_hold": bool,        # hold_if condition triggered
            "auto_promote_ok": bool,    # all auto_promote_if conditions met
            "triggered_hold_conditions": list[str],
            "failed_auto_promote_conditions": list[str],
          }
        """
        # auto_promote_eligible: false forces hold regardless of conditions.
        if not self.auto_promote_eligible:
            return {
                "forced_hold": True,
                "auto_promote_ok": False,
                "triggered_hold_conditions": ["auto_promote_eligible_false"],
                "failed_auto_promote_conditions": [],
            }
        # Unknown condition in hold_if → fail-closed (treat as triggered).
        # Unknown condition in auto_promote_if → fail-closed (treat as failed).
        triggered_hold = [
            cond
            for cond in self.review_policy.get("hold_if", [])
            if _HOLD_CONDITIONS.get(cond, lambda _: True)(warning_flags)
        ]
        failed_promote = [
            cond
            for cond in self.review_policy.get("auto_promote_if", [])
            if not _AUTO_PROMOTE_CONDITIONS.get(cond, lambda _: False)(warning_flags)
        ]
        return {
            "forced_hold": bool(triggered_hold),
            "auto_promote_ok": not failed_promote,
            "triggered_hold_conditions": triggered_hold,
            "failed_auto_promote_conditions": failed_promote,
        }


class WorkflowTemplateLoader:
    """Loads and exposes workflow templates from the YAML directory."""

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        self._templates: Dict[str, WorkflowTemplate] = {}
        self._load(template_dir or _DEFAULT_TEMPLATE_DIR)

    def _load(self, template_dir: Path) -> None:
        if not template_dir.is_dir():
            logger.warning("workflow-templates dir missing: %s", template_dir)
            return
        try:
            import yaml  # type: ignore[import]
        except ImportError:
            logger.warning("PyYAML not installed; workflow templates disabled")
            return

        for path in sorted(template_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
                tmpl = WorkflowTemplate(
                    id=str(data["id"]),
                    display_name=str(data.get("display_name", data["id"])),
                    workflow_profile=str(data.get("workflow_profile", "default")),
                    review_policy={
                        "auto_promote_if": list(
                            data.get("review_policy", {}).get("auto_promote_if", [])
                        ),
                        "hold_if": list(
                            data.get("review_policy", {}).get("hold_if", [])
                        ),
                    },
                    allowed_ops=list(data.get("allowed_ops", [])),
                    verification=str(data.get("verification", "mutation")),
                    auto_promote_eligible=bool(data.get("auto_promote_eligible", True)),
                    risk_flags=list(data.get("risk_flags", [])),
                )
                unknown_ap = (
                    set(tmpl.review_policy.get("auto_promote_if", []))
                    - _VALID_AUTO_PROMOTE_CONDITIONS
                )
                unknown_hold = (
                    set(tmpl.review_policy.get("hold_if", [])) - _VALID_HOLD_CONDITIONS
                )
                if unknown_ap or unknown_hold:
                    logger.warning(
                        "Template %s has unknown condition names (fail-closed): "
                        "auto_promote_if=%s hold_if=%s",
                        tmpl.id,
                        unknown_ap,
                        unknown_hold,
                    )
                self._templates[tmpl.id] = tmpl
            except Exception as exc:
                logger.error("Failed to load template %s: %s", path.name, exc)

        logger.info("Loaded %d workflow templates", len(self._templates))

    def get(self, template_id: str) -> Optional[WorkflowTemplate]:
        return self._templates.get(template_id)

    def list(self) -> List[WorkflowTemplate]:
        return list(self._templates.values())

    def known_ids(self) -> Set[str]:
        return set(self._templates)


# Module-level singleton — loaded once on first import.
_loader: Optional[WorkflowTemplateLoader] = None


def get_template_loader() -> WorkflowTemplateLoader:
    global _loader
    if _loader is None:
        _loader = WorkflowTemplateLoader()
    return _loader


def get_template(template_id: str) -> Optional[WorkflowTemplate]:
    return get_template_loader().get(template_id)


def list_templates() -> List[WorkflowTemplate]:
    return get_template_loader().list()


def known_template_ids() -> Set[str]:
    return get_template_loader().known_ids()
