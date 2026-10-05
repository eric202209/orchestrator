"""POST33-MODEL2 isolated provider-bound discovery evaluation.

This is an evaluation-only seam.  It never enters the Orchestrator lifecycle:
the OpenClaw service is constructed without a database/session/task, its log
sink is replaced with an in-memory no-op, and every provider invocation uses a
private ephemeral OpenClaw config/state directory.  Prompt construction,
response extraction, discovery parsing, observation, materialization, and PL16
inventory are the production functions.

The script intentionally has no retry path.  The fixed nine-cell order is part
of the evidence contract.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from urllib.request import Request, urlopen

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.database import SessionLocal
from app.models import (
    Base,
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskDispatchIntent,
    PlanningSession,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.model_adaptation import render_prompt_for_profile
from app.services.model_adaptation.schemas import PromptEnvelope
from app.services.orchestration.planning.read_only_discovery import (
    build_discovery_prompt,
    discovery_output_text,
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.repository_orientation import (
    derive_repository_orientation,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
    observed_candidate_paths,
)
from app.services.orchestration.validation.parsing import extract_structured_text


EVIDENCE_ROOT = REPOSITORY_ROOT / "docs/roadmap/reports/evidence/post33-model2"
PERSISTENT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
PROVIDER_CALL_BUDGET = 9
DISCOVERY_TIMEOUT_SECONDS = 120
EPHEMERAL_LOCAL_GATEWAY_CREDENTIAL = "post33-runtime1-local-placeholder"

# The evaluation seam must not depend on the application database being
# initialized, and it must never create or mutate product rows.  The service
# constructor performs read-only session lookups even when no lifecycle IDs
# are supplied, so provide it a private in-memory schema instead of the
# production SessionLocal binding.
_EVALUATION_DB_ENGINE = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}
)
Base.metadata.create_all(bind=_EVALUATION_DB_ENGINE)
EvaluationSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=_EVALUATION_DB_ENGINE
)

TASKS: dict[str, dict[str, Any]] = {
    "T222": {
        "task": (
            "Restore zero-percent visibility for failure-only tool metrics\n\n"
            "Our tool usage analytics currently omits a tool from the success-rate "
            "mapping when every recorded invocation fails. Update the existing backend "
            "behavior so every observed tool has a success-rate entry, including 0% "
            "when there are no successes, while preserving execution counts, totals, "
            "and existing successful-tool percentages. Add focused regression coverage "
            "for a failure-only tool and keep the change limited to the existing "
            "analytics path. Verify with the focused test suite."
        ),
        "target_path": "app/services/tasks/tool_tracking.py",
        "target_terms": ("success_rates", "success-rate", "tool_success"),
        "shape": "natural-language/no-explicit-path",
    },
    "T218": {
        "task": (
            "Fix scheduled task timezone handling\n\n"
            "Fix scheduled task execution so ISO-8601 timestamps with Z or explicit "
            "UTC offsets are compared consistently, preserving retry-before-work "
            "behavior, and add focused regression coverage. Keep the scope to existing "
            "maintenance code and focused tests; do not change unrelated lifecycle "
            "behavior."
        ),
        "target_path": "app/tasks/maintenance.py",
        "target_terms": ("timezone", "UTC", "ISO-8601", "retry"),
        "shape": "natural-language/no-explicit-path",
    },
    "T214": {
        "task": (
            "Improve source-import context handling for unreadable files\n\n"
            "In app/services/project/source_imports.py, make _safe_read_text gracefully "
            "return an empty string when a file disappears or cannot be opened between "
            "discovery and reading."
        ),
        "target_path": "app/services/project/source_imports.py",
        "target_terms": ("_safe_read_text", "source_imports", "empty string"),
        "shape": "explicit-path-and-symbol",
    },
}

ARMS: dict[str, dict[str, Any]] = {
    "A": {
        "name": "baseline",
        "requested_model": "qwen3.6:27B",
        "provider_model_ref": "openai/qwen-local",
        "provider": "openai-compatible-ai-gateway",
        "profile": "openclaw_default",
        "backend": "local_openclaw",
        "model_family": "qwen3.6:27B",
    },
    "B": {
        "name": "candidate",
        "requested_model": "qwen3-coder:30b",
        "provider_model_ref": "ollama/qwen3-coder:30b",
        "provider": "ollama",
        "profile": "openclaw_default",
        "backend": "local_openclaw",
        "model_family": "qwen3-coder:30b",
    },
    "C": {
        "name": "profile_control",
        "requested_model": "qwen3.6:27B",
        "provider_model_ref": "openai/qwen-local",
        "provider": "openai-compatible-ai-gateway",
        "profile": "qwen_compact_json",
        "backend": "local_openclaw",
        "model_family": "qwen3.6:27B",
    },
}

CALL_ORDER = (
    ("T222", "A"),
    ("T218", "B"),
    ("T214", "C"),
    ("T218", "A"),
    ("T214", "B"),
    ("T222", "C"),
    ("T214", "A"),
    ("T222", "B"),
    ("T218", "C"),
)


class IdentityDriftError(RuntimeError):
    """Raised when a provider call cannot prove the requested runtime identity."""

    def __init__(self, proof: dict[str, Any]):
        self.proof = proof
        super().__init__(proof["failure_reason"])


def _fallback_diagnostics(raw_text: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"model fallback decision: decision=(?P<decision>\S+) "
        r"requested=(?P<requested>\S+) candidate=(?P<candidate>\S+) "
        r"reason=(?P<reason>\S+)(?: next=(?P<next>\S+))?"
    )
    return [match.groupdict(default="none") for match in pattern.finditer(raw_text)]


def _runtime_provider_model(
    raw_text: str, parsed_runtime: dict[str, Any], diagnostics: dict[str, Any]
) -> str | None:
    agent_meta = re.search(
        r'"agentMeta"\s*:\s*\{.*?"provider"\s*:\s*"([^"]+)"'
        r'.*?"model"\s*:\s*"([^"]+)"',
        raw_text,
        re.DOTALL,
    )
    if agent_meta:
        return f"{agent_meta.group(1)}/{agent_meta.group(2)}"
    provider = parsed_runtime.get("provider") or diagnostics.get("provider")
    model = parsed_runtime.get("model") or diagnostics.get("model")
    if provider and model:
        return f"{provider}/{model}"
    return None


def _verify_runtime_identity(
    arm: dict[str, Any],
    *,
    identity: dict[str, Any],
    diagnostics: dict[str, Any],
    parsed_runtime: dict[str, Any],
    raw_stdout: str,
    raw_stderr: str,
    prompt_hash: str,
) -> dict[str, Any]:
    """Prove provider identity immediately after a generation call.

    The proof is deliberately based on runtime diagnostics/raw provider output,
    not on the harness's requested arm metadata.  Missing or contradictory
    runtime identity fails closed before response extraction or scoring.
    """

    raw_text = f"{raw_stdout}\n{raw_stderr}"
    fallback_diagnostics = _fallback_diagnostics(raw_text)
    effective_ref = None
    if fallback_diagnostics:
        effective_ref = fallback_diagnostics[-1]["candidate"]
    else:
        effective_ref = _runtime_provider_model(raw_text, parsed_runtime, diagnostics)
    requested_ref = arm["provider_model_ref"]
    generation_error_signals = [
        bool(re.search(r"\bisError\s*=\s*true", raw_text)),
        bool(re.search(r'"isError"\s*:\s*true', raw_text)),
        "rawError=" in raw_text,
        bool(parsed_runtime.get("error")),
    ]
    generation_success = not any(generation_error_signals)
    proof: dict[str, Any] = {
        "status": "PASS",
        "requested_agent": identity.get("agent_id"),
        "requested_provider_model_ref": requested_ref,
        "effective_provider_model_ref": effective_ref,
        "provider_endpoint": identity.get("provider_endpoint"),
        "profile": identity.get("profile"),
        "config_fingerprint": identity.get("config_sha256"),
        "prompt_hash": prompt_hash,
        "openclaw_runtime_diagnostics": diagnostics,
        "fallback_diagnostics": fallback_diagnostics,
        "response_extraction_source": parsed_runtime.get("output_channel_used"),
        "tool_telemetry": identity.get("tools"),
        "generation_success": generation_success,
        "failure_reason": None,
    }
    if not generation_success:
        proof.update(
            {
                "status": "IDENTITY_UNVERIFIED",
                "failure_reason": (
                    "OpenClaw exposed provider/model metadata but the generation "
                    "ended with an error; effective identity is not valid evidence"
                ),
            }
        )
    elif fallback_diagnostics:
        proof.update(
            {
                "status": "INVALID_IDENTITY_DRIFT",
                "failure_reason": (
                    "OpenClaw emitted a model fallback decision for the requested "
                    f"identity {requested_ref}"
                ),
            }
        )
    elif not effective_ref:
        proof.update(
            {
                "status": "IDENTITY_UNVERIFIED",
                "failure_reason": (
                    "OpenClaw runtime output did not expose an effective provider "
                    "and model identity"
                ),
            }
        )
    elif effective_ref != requested_ref:
        proof.update(
            {
                "status": "INVALID_IDENTITY_DRIFT",
                "failure_reason": (
                    f"effective identity {effective_ref} did not match "
                    f"requested identity {requested_ref}"
                ),
            }
        )
    elif not identity.get("provider_endpoint"):
        proof.update(
            {
                "status": "IDENTITY_UNVERIFIED",
                "failure_reason": "provider endpoint was not captured",
            }
        )
    if proof["status"] != "PASS":
        raise IdentityDriftError(proof)
    return proof


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _redact_text(value: str) -> str:
    redacted = value or ""
    for marker in ("Authorization:", "Bearer ", "api_key", "API_KEY"):
        if marker in redacted:
            redacted = redacted.replace(marker, f"{marker}[REDACTED]")
    return redacted


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "tree": run("rev-parse", "HEAD^{tree}"),
        "parent": run("rev-parse", "HEAD^1"),
        "origin_main": run("rev-parse", "origin/main"),
        "branch": run("branch", "--show-current"),
        "worktrees": run("worktree", "list", "--porcelain"),
        "status": run("status", "--short", "--untracked-files=all"),
    }


def _persistent_config_fingerprint() -> dict[str, Any]:
    raw = PERSISTENT_OPENCLAW_CONFIG.read_bytes()
    return {
        "path": str(PERSISTENT_OPENCLAW_CONFIG),
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
    }


def _persistent_config() -> dict[str, Any]:
    return json.loads(PERSISTENT_OPENCLAW_CONFIG.read_text(encoding="utf-8"))


def _provider_inspection(url: str, *, timeout: int = 5) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        payload = json.loads(body.decode("utf-8"))
    return {
        "url": url,
        "status": "ok",
        "latency_seconds": round(time.monotonic() - started, 3),
        "payload": payload,
    }


def _agent_for_project(config: dict[str, Any]) -> dict[str, Any]:
    matches = [
        agent
        for agent in (config.get("agents") or {}).get("list", [])
        if isinstance(agent, dict)
        and str(agent.get("workspace") or "").strip() == str(REPOSITORY_ROOT.resolve())
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one OpenClaw agent for {REPOSITORY_ROOT}, found {len(matches)}"
        )
    return matches[0]


def _provider_catalogs(config: dict[str, Any]) -> dict[str, Any]:
    providers = (config.get("models") or {}).get("providers") or {}
    openai = providers.get("openai") or {}
    ollama = providers.get("ollama") or {}
    openai_url = str(openai.get("baseUrl") or "").rstrip("/") + "/models"
    ollama_url = str(ollama.get("baseUrl") or "").rstrip("/") + "/api/tags"
    inspections = {
        "openai_compatible": _provider_inspection(openai_url),
        "ollama": _provider_inspection(ollama_url),
    }
    openai_ids = {
        str(item.get("id"))
        for item in inspections["openai_compatible"]["payload"].get("data", [])
        if isinstance(item, dict)
    }
    ollama_ids = {
        str(item.get("name") or item.get("model"))
        for item in inspections["ollama"]["payload"].get("models", [])
        if isinstance(item, dict)
    }
    if "qwen-local" not in openai_ids:
        raise RuntimeError("ai-gateway did not advertise qwen-local")
    if "qwen3-coder:30b" not in ollama_ids:
        raise RuntimeError("Ollama did not advertise qwen3-coder:30b")
    return inspections


def _product_state() -> dict[str, Any]:
    classes = {
        "projects": Project,
        "sessions": SessionModel,
        "tasks": Task,
        "task_executions": TaskExecution,
        "planning_sessions": PlanningSession,
        "execution_plans": ExecutionPlan,
        "execution_tasks": ExecutionTask,
        "execution_task_attempts": ExecutionTaskAttempt,
        "execution_task_dispatch_intents": ExecutionTaskDispatchIntent,
    }
    db = SessionLocal()
    try:
        inspector = inspect(db.get_bind())
        return {
            name: (
                db.query(model).count()
                if inspector.has_table(model.__table__.name)
                else 0
            )
            for name, model in classes.items()
        }
    finally:
        db.close()


def _prompt_packet(packet_id: str) -> dict[str, Any]:
    packet = TASKS[packet_id]
    orientation = derive_repository_orientation(
        REPOSITORY_ROOT, packet["task"], explicit_paths=()
    )
    discovery_prompt = build_discovery_prompt(packet["task"], "", orientation)
    return {
        **packet,
        "orientation": orientation,
        "orientation_details": orientation.as_details(),
        "task_hash": _sha256_text(packet["task"]),
        "orientation_hash": _sha256_text(
            json.dumps(orientation.as_details(), sort_keys=True)
        ),
        "discovery_prompt": discovery_prompt,
        "discovery_prompt_hash": _sha256_text(discovery_prompt),
        "discovery_prompt_bytes": len(discovery_prompt.encode("utf-8")),
    }


def _wire_prompt(packet: dict[str, Any], arm: dict[str, Any]) -> str:
    canonical = packet["discovery_prompt"]
    if arm["profile"] == "openclaw_default":
        return canonical
    envelope = PromptEnvelope(
        objective="Execute one bounded read-only discovery action.",
        execution_mode="read_only_discovery",
        instructions=[
            "Return only the canonical discovery JSON described in the prompt body.",
        ],
        context={},
        expected_output='one JSON object with action "search_text", "read_file", or "stop"',
        prompt_body=canonical,
    )
    return render_prompt_for_profile(arm["profile"], envelope)


def _preflight() -> dict[str, Any]:
    persistent = _persistent_config()
    selected = _agent_for_project(persistent)
    catalogs = _provider_catalogs(persistent)
    command_db = EvaluationSessionLocal()
    command_service = OpenClawSessionService(
        command_db,
        None,
        None,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.PLANNING,
            backend_name="local_openclaw",
            model_family="qwen3.6:27B",
            adaptation_profile="openclaw_default",
        ),
    )
    cli_command = command_service._resolve_openclaw_command()
    cli_version = command_service._resolve_openclaw_cli_version()
    command_db.close()
    packets = {packet_id: _prompt_packet(packet_id) for packet_id in TASKS}
    packet_manifest = {
        packet_id: {
            key: value
            for key, value in packet.items()
            if key
            not in {
                "orientation",
                "discovery_prompt",
                "target_terms",
            }
        }
        for packet_id, packet in packets.items()
    }
    return {
        "status": "READY",
        "provider_call_budget": PROVIDER_CALL_BUDGET,
        "persistent_config": _persistent_config_fingerprint(),
        "selected_agent": {
            "id": selected.get("id"),
            "workspace": selected.get("workspace"),
            "model": selected.get("model"),
        },
        "cli_command": cli_command,
        "openclaw_version": cli_version,
        "thinking_default": persistent.get("agents", {})
        .get("defaults", {})
        .get("thinkingDefault"),
        "provider_inspection": catalogs,
        "model_identity_plan": {
            arm_id: {
                "requested_model": arm["requested_model"],
                "provider_model_ref": arm["provider_model_ref"],
                "provider": arm["provider"],
                "profile": arm["profile"],
            }
            for arm_id, arm in ARMS.items()
        },
        "packets": packet_manifest,
        "call_order": [
            {"sequence": index, "packet": packet, "arm": arm}
            for index, (packet, arm) in enumerate(CALL_ORDER, start=1)
        ],
        "isolation": {
            "database_object": None,
            "normal_lifecycle_runner": False,
            "ephemeral_openclaw_config": True,
            "ephemeral_openclaw_state": True,
            "production_prompt_builder": True,
            "production_parser_executor_materialization_pl16": True,
            "raw_response_retention": True,
            "pl18_deny_all": True,
            "max_discovery_turns": 1,
        },
    }


def _configure_ephemeral_service(
    arm: dict[str, Any],
    runtime_workspace: Path,
    *,
    inject_ephemeral_local_credential: bool = True,
    disable_fallback: bool = True,
) -> tuple[OpenClawSessionService, dict[str, Any]]:
    configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name=arm["backend"],
        # The runtime binding resolves the explicit provider/model reference
        # against the ephemeral catalog.  The historical unqualified A/C model
        # label is no longer present in the operator catalog.
        model_family=arm["provider_model_ref"],
        adaptation_profile=arm["profile"],
    )
    service_db = EvaluationSessionLocal()
    service = OpenClawSessionService(
        service_db,
        None,
        None,
        runtime_configuration=configuration,
    )
    service._log_entry = lambda *args, **kwargs: None
    runner_id = "post33-model2-runtime-runner"
    template_dir = tempfile.TemporaryDirectory(
        dir=runtime_workspace.parent, prefix="post33-model2-runner-"
    )
    runner_workspace = Path(template_dir.name) / "runner"
    runner_workspace.mkdir(parents=True)
    template_config_path = Path(template_dir.name) / "openclaw.json"
    template_config = json.loads(PERSISTENT_OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    template_config.setdefault("agents", {}).setdefault("list", []).append(
        {"id": runner_id, "workspace": str(runner_workspace)}
    )
    template_config_path.write_text(
        json.dumps(template_config, indent=2), encoding="utf-8"
    )
    service._openclaw_config_path = lambda: (
        getattr(service, "_openclaw_config_path_override", None) or template_config_path
    )
    context = SimpleNamespace(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=REPOSITORY_ROOT.resolve(),
        project_id=None,
        task_execution_id=None,
        runtime_root=runtime_workspace.parent,
        is_sandboxed=True,
    )
    service.bind_runtime_workspace(context, runner_agent_id=runner_id)
    config_path = service._openclaw_config_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    selected = next(
        agent
        for agent in (config.get("agents") or {}).get("list", [])
        if isinstance(agent, dict)
        and str(agent.get("id") or "").strip() == service._workspace_binding.agent_id
    )
    if disable_fallback:
        selected["model"] = {
            "primary": arm["provider_model_ref"],
            "fallbacks": [],
        }
    else:
        selected["model"] = arm["provider_model_ref"]
    provider_name = arm["provider_model_ref"].split("/", 1)[0]
    providers = (config.setdefault("models", {})).setdefault("providers", {})
    provider_config = providers.get(provider_name)
    if (
        inject_ephemeral_local_credential
        and provider_name == "openai"
        and isinstance(provider_config, dict)
    ):
        provider_config["apiKey"] = EPHEMERAL_LOCAL_GATEWAY_CREDENTIAL
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    service._apply_discovery_tool_suppression("PLANNING_DISCOVERY")
    final_config = json.loads(config_path.read_text(encoding="utf-8"))
    final_selected = next(
        agent
        for agent in (final_config.get("agents") or {}).get("list", [])
        if isinstance(agent, dict)
        and str(agent.get("id") or "").strip() == service._workspace_binding.agent_id
    )
    if final_selected.get("tools", {}).get("deny") != ["*"]:
        raise RuntimeError(
            "PL18 deny-all suppression was not present in ephemeral config"
        )
    final_model = final_selected.get("model")
    final_model_ref = (
        final_model.get("primary") if isinstance(final_model, dict) else final_model
    )
    if final_model_ref != arm["provider_model_ref"]:
        raise RuntimeError("ephemeral selected-agent model override was not retained")
    final_fallbacks = (
        final_model.get("fallbacks") if isinstance(final_model, dict) else None
    )
    service._evaluation_db = service_db
    original_release = service.release_runtime_workspace_binding

    def release() -> None:
        original_release()
        template_dir.cleanup()

    service.release_runtime_workspace_binding = release
    return service, {
        "agent_id": service._workspace_binding.agent_id,
        "config_path": str(config_path),
        "config_sha256": _sha256_bytes(config_path.read_bytes()),
        "model": final_model_ref,
        "fallbacks": final_fallbacks,
        "provider_endpoint": (
            provider_config.get("baseUrl")
            if isinstance(provider_config, dict)
            else None
        ),
        "profile": arm["profile"],
        "ephemeral_credential_source": (
            "ephemeral_config_placeholder"
            if inject_ephemeral_local_credential and provider_name == "openai"
            else "none"
        ),
        "tools": final_selected.get("tools"),
        "environment": dict(service._workspace_binding.environment),
        "runtime_workspace": str(runtime_workspace),
        "persistent_config": _persistent_config_fingerprint(),
    }


def _score(
    packet: dict[str, Any],
    request: Any,
    observation: Any,
    materialization: Any,
    inventory: Any,
) -> dict[str, Any]:
    target_path = packet["target_path"]
    requested_paths = tuple(getattr(request, "paths", ()) or ())
    target_requested = target_path in requested_paths or any(
        target_path.startswith(path.rstrip("/") + "/") for path in requested_paths
    )
    exact_path = target_path in requested_paths
    relevant_path = target_requested
    if exact_path:
        d2 = 2
    elif relevant_path:
        d2 = 1
    else:
        d2 = 0
    d3 = 2 if exact_path else 1 if relevant_path else 0
    action = getattr(request, "action", "")
    if action == "search_text":
        hit_paths = {hit.path for hit in getattr(observation, "hits", ())}
        target_hits = target_path in hit_paths
        d4 = 2 if target_hits and getattr(observation, "hits", ()) else 1
    elif action == "read_file":
        content = str(getattr(observation, "content", "") or "")
        d4 = 2 if target_path == getattr(request, "path", None) and content else 1
    else:
        d4 = 0
    handle_paths = {handle.path for handle in getattr(inventory, "handles", ())}
    target_hint = any(
        getattr(item, "relative_path", None) == target_path
        and getattr(item, "target_hint", None)
        for item in getattr(materialization, "files", ())
    )
    target_materialized = any(
        getattr(item, "relative_path", None) == target_path
        and getattr(item, "content", None) is not None
        for item in getattr(materialization, "files", ())
    )
    if target_materialized and (target_hint or target_path in handle_paths):
        d5 = 3
    elif target_materialized:
        d5 = 2
    elif getattr(observation, "status", "") in {"completed", "no_matches", "stopped"}:
        d5 = 1
    else:
        d5 = 0
    return {
        "D1_contract_validity": True,
        "D2_path_fidelity": d2,
        "D3_scope_semantic_quality": d3,
        "D4_query_quality": d4,
        "D5_observation_utility": d5,
        "D6_safety": "PASS_FAIL_CLOSED_PRESERVED",
        "D5_raw": {
            "target_materialized": target_materialized,
            "target_hint": target_hint,
            "target_handle": target_path in handle_paths,
            "hit_count": len(getattr(observation, "hits", ()) or ()),
            "materialized_path_count": len(
                getattr(observation, "materialization_paths", lambda: ())()
            ),
            "pl16_handle_count": len(getattr(inventory, "handles", ()) or ()),
        },
    }


def _run_cell(
    sequence: int,
    packet: dict[str, Any],
    arm: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    wire_prompt = _wire_prompt(packet, arm)
    cell_dir = output_dir / f"{sequence:02d}-{packet['id']}-{arm['name']}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        cell_dir / "prompt-metadata.json",
        {
            "task_hash": packet["task_hash"],
            "orientation_hash": packet["orientation_hash"],
            "discovery_prompt_hash": packet["discovery_prompt_hash"],
            "discovery_prompt_bytes": packet["discovery_prompt_bytes"],
            "wire_prompt_hash": _sha256_text(wire_prompt),
            "wire_prompt_bytes": len(wire_prompt.encode("utf-8")),
            "profile": arm["profile"],
            "profile_prompt_envelope_changed": wire_prompt
            != packet["discovery_prompt"],
        },
    )
    (cell_dir / "discovery-prompt.txt").write_text(
        packet["discovery_prompt"], encoding="utf-8"
    )
    (cell_dir / "wire-prompt.txt").write_text(wire_prompt, encoding="utf-8")

    service: OpenClawSessionService | None = None
    dispatch_started = False
    started = time.monotonic()
    runtime_workspace = Path(tempfile.mkdtemp(prefix="post33-model2-workspace-"))
    try:
        service, identity = _configure_ephemeral_service(arm, runtime_workspace)
        command = service.build_cli_agent_command(
            wire_prompt,
            source_brain="local",
            timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
            session_prefix="model2-discovery",
            strict_provider_result=False,
        )
        dispatch_started = True
        proc, diagnostics = asyncio.run(
            service._run_cli_prompt_with_diagnostics(
                command,
                timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
                cwd=str(runtime_workspace),
                prompt=wire_prompt,
                invocation_kind="model2-discovery",
                strict_provider_result=False,
            )
        )
        raw_stdout = _redact_text(proc.stdout or "")
        raw_stderr = _redact_text(proc.stderr or "")
        (cell_dir / "raw-provider.stdout").write_text(raw_stdout, encoding="utf-8")
        (cell_dir / "raw-provider.stderr").write_text(raw_stderr, encoding="utf-8")
        parsed_runtime = service.parse_cli_response(
            proc, expected_session_id=None, strict_provider_result=False
        )
        identity_proof = _verify_runtime_identity(
            arm,
            identity=identity,
            diagnostics=diagnostics,
            parsed_runtime=parsed_runtime,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            prompt_hash=_sha256_text(wire_prompt),
        )
        extracted = discovery_output_text(parsed_runtime, extract_structured_text)
        (cell_dir / "extracted-response.txt").write_text(extracted, encoding="utf-8")
        result: dict[str, Any] = {
            "status": "completed",
            "dispatch_started": dispatch_started,
            "sequence": sequence,
            "packet": packet["id"],
            "arm": arm["name"],
            "requested_model": arm["requested_model"],
            "provider_model_ref": arm["provider_model_ref"],
            "provider": arm["provider"],
            "profile": arm["profile"],
            "identity": identity,
            "identity_proof": identity_proof,
            "latency_seconds": round(time.monotonic() - started, 3),
            "runtime_diagnostics": diagnostics,
            "provider_result": {
                key: value for key, value in parsed_runtime.items() if key != "output"
            },
            "extracted_response": extracted,
        }
        reported_model = str(
            (diagnostics or {}).get("model")
            or (diagnostics or {}).get("model_family")
            or parsed_runtime.get("model")
            or ""
        ).strip()
        result["reported_model"] = reported_model or None
        if reported_model and reported_model.lower() not in {
            arm["requested_model"].lower(),
            arm["provider_model_ref"].lower(),
            "qwen-local",
            "qwen3-coder:30b",
        }:
            result["model_identity_drift"] = True
            raise RuntimeError(
                f"provider reported model {reported_model!r} for {arm['provider_model_ref']!r}"
            )
        request = parse_discovery_request(extracted)
        observation = execute_discovery_request(REPOSITORY_ROOT, request)
        materialization = materialize_observation_source_context(
            project_dir=REPOSITORY_ROOT,
            prompt=packet["task"],
            planner_contract=None,
            observation=observation,
            materialize=materialize_planner_source_context,
            source_cache={},
        )
        inventory = build_semantic_target_inventory(
            materialization,
            additional_candidate_paths=observed_candidate_paths(observation),
        )
        score = _score(packet, request, observation, materialization, inventory)
        result.update(
            {
                "parser_status": "valid",
                "parsed_action": request.action,
                "requested_paths": list(request.paths),
                "query": request.query,
                "requested_path": request.path,
                "observation": {
                    "action": observation.action,
                    "status": observation.status,
                    "paths": list(observation.paths),
                    "hit_count": len(observation.hits),
                    "hits": [hit.__dict__ for hit in observation.hits],
                    "content_bytes": len((observation.content or "").encode("utf-8")),
                    "truncated": observation.truncated,
                    "reason": observation.reason,
                },
                "materialized_sources": [
                    {
                        "path": item.relative_path,
                        "status": item.status,
                        "content_bytes": len((item.content or "").encode("utf-8")),
                        "target_hint": item.target_hint,
                        "target_included": item.target_included,
                        "target_match_count": item.target_match_count,
                    }
                    for item in materialization.files
                ],
                "pl16": {
                    "handle_count": len(inventory.handles),
                    "handles": [handle.to_dict() for handle in inventory.handles],
                },
                "score": score,
            }
        )
        _write_json(cell_dir / "result.json", result)
        return result
    except Exception as exc:  # one failed generation consumes this cell
        result = {
            "status": "failed",
            "dispatch_started": dispatch_started,
            "sequence": sequence,
            "packet": packet["id"],
            "arm": arm["name"],
            "requested_model": arm["requested_model"],
            "provider_model_ref": arm["provider_model_ref"],
            "provider": arm["provider"],
            "profile": arm["profile"],
            "latency_seconds": round(time.monotonic() - started, 3),
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc)[:1000],
        }
        if isinstance(exc, IdentityDriftError):
            result.update(
                {
                    "model_identity_drift": exc.proof["status"]
                    == "INVALID_IDENTITY_DRIFT",
                    "identity_failure": True,
                    "identity_proof": exc.proof,
                    "comparison_valid": False,
                }
            )
        _write_json(cell_dir / "result.json", result)
        return result
    finally:
        if service is not None:
            service.release_runtime_workspace_binding()
            evaluation_db = getattr(service, "_evaluation_db", None)
            if evaluation_db is not None:
                evaluation_db.close()
        try:
            runtime_workspace.rmdir()
        except OSError:
            pass


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    for arm_id, arm in ARMS.items():
        cells = [result for result in results if result.get("arm") == arm["name"]]
        valid = [result for result in cells if result.get("status") == "completed"]
        scores = [result.get("score", {}) for result in valid]
        aggregate[arm_id] = {
            "arm": arm["name"],
            "cells": len(cells),
            "valid_cells": len(valid),
            "contract_valid_cells": sum(
                score.get("D1_contract_validity") is True for score in scores
            ),
            "safety_preserved_cells": sum(
                score.get("D6_safety") == "PASS_FAIL_CLOSED_PRESERVED"
                for score in scores
            ),
            "useful_observations": sum(
                score.get("D5_observation_utility", 0) >= 2 for score in scores
            ),
            "pl16_handle_total": sum(
                score.get("D5_raw", {}).get("pl16_handle_count", 0) for score in scores
            ),
            "scores": {
                metric: sum(score.get(metric, 0) for score in scores)
                for metric in (
                    "D2_path_fidelity",
                    "D3_scope_semantic_quality",
                    "D4_query_quality",
                    "D5_observation_utility",
                )
            },
        }
    return aggregate


def run(*, execute: bool) -> int:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    preflight_path = EVIDENCE_ROOT / "preflight.json"
    try:
        preflight = _preflight()
    except Exception as exc:
        failed = {
            "status": "PREFLIGHT_FAILED",
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc)[:1000],
            "persistent_config": _persistent_config_fingerprint(),
        }
        _write_json(preflight_path, failed)
        return 2
    _write_json(preflight_path, preflight)
    _write_json(EVIDENCE_ROOT / "call-order.json", preflight["call_order"])
    if not execute:
        return 0

    state_before = _product_state()
    _write_json(EVIDENCE_ROOT / "product-state-before.json", state_before)
    config_before = _persistent_config_fingerprint()
    _write_json(EVIDENCE_ROOT / "config-before-fingerprint.json", config_before)
    packet_data = {packet_id: _prompt_packet(packet_id) for packet_id in TASKS}
    results: list[dict[str, Any]] = []
    identity_drift = False
    aborted = False
    for sequence, (packet_id, arm_id) in enumerate(CALL_ORDER, start=1):
        if aborted:
            break
        result = _run_cell(
            sequence,
            packet_data[packet_id] | {"id": packet_id},
            ARMS[arm_id],
            EVIDENCE_ROOT / "cells",
        )
        results.append(result)
        if result.get("model_identity_drift") or result.get("identity_failure"):
            identity_drift = bool(result.get("model_identity_drift"))
            aborted = True
    config_after = _persistent_config_fingerprint()
    state_after = _product_state()
    _write_json(EVIDENCE_ROOT / "config-after-fingerprint.json", config_after)
    _write_json(EVIDENCE_ROOT / "product-state-after.json", state_after)
    deltas = {
        key: state_after.get(key, 0) - state_before.get(key, 0) for key in state_before
    }
    _write_json(EVIDENCE_ROOT / "replay-summaries.json", results)
    aggregate = _aggregate(results)
    _write_json(EVIDENCE_ROOT / "scorecard.json", aggregate)
    manifest = {
        "status": "ABORTED" if aborted else "COMPLETED",
        "provider_calls": sum(
            bool(result.get("dispatch_started")) for result in results
        ),
        "provider_retries": 0,
        "identity_drift_detected": identity_drift,
        "persistent_openclaw_config_unchanged": config_before == config_after,
        "product_state_before": state_before,
        "product_state_after": state_after,
        "product_state_delta": deltas,
        "product_state_mutation": any(value != 0 for value in deltas.values()),
        "framework_boundary_preserved": True,
        "aggregate": aggregate,
    }
    _write_json(EVIDENCE_ROOT / "benchmark-manifest.json", manifest)
    return (
        0
        if manifest["persistent_openclaw_config_unchanged"]
        and not manifest["product_state_mutation"]
        else 3
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.preflight == args.execute:
        parser.error("choose exactly one of --preflight or --execute")
    return run(execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
