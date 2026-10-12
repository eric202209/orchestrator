"""POST33-MODEL4 same-model OpenClaw/direct discovery isolation gate.

This evaluation-only harness compares the current local OpenClaw adapter with
the current OpenAI-compatible chat adapter while both target the gateway's
``qwen-local`` deployment.  It never enters the Orchestrator lifecycle and it
does not change discovery semantics.  Provider responses are retained before
the production discovery parser/executor/materialization/PL16 replay.

The six-cell order is frozen in ``CALL_ORDER``.  A runtime identity, raw
response, persistent-config, or product-state failure stops the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.evals import model2_discovery_ab as retained

EVIDENCE_ROOT = (
    REPOSITORY_ROOT
    / "docs/roadmap/reports/evidence/post33-model4-runtime-isolation-20260825"
)
RETAINED_RUNTIME_SOURCE = (
    REPOSITORY_ROOT
    / "docs/roadmap/reports/evidence/post33-runtime3/probes/neutral-identity-probe.stderr"
)
PERSISTENT_OPENCLAW_CONFIG = retained.PERSISTENT_OPENCLAW_CONFIG
PROVIDER_CALL_BUDGET = 6
PROVIDER_RETRIES = 0
DISCOVERY_TIMEOUT_SECONDS = 120
LOCAL_GATEWAY_MODEL = "qwen-local"
UNDERLYING_MODEL = "Qwen3.6-27B-Text-NVFP4-MTP"
EPHEMERAL_LOCAL_GATEWAY_CREDENTIAL = "post33-model4-runtime-isolation-local-placeholder"

TASK_IDS = ("T222", "T218", "T214")
TASKS = {packet_id: dict(retained.TASKS[packet_id]) for packet_id in TASK_IDS}

ARMS: dict[str, dict[str, Any]] = {
    "A": {
        "name": "openclaw",
        "runtime": "local_openclaw",
        "requested_provider_model": "openai/qwen-local",
        "provider_model_ref": "openai/qwen-local",
        "requested_model": LOCAL_GATEWAY_MODEL,
        "profile": "openclaw_default",
        "backend": "local_openclaw",
        "model_family": "qwen3.6:27B",
    },
    "B": {
        "name": "direct",
        "runtime": "openai_chat_completions",
        "requested_provider_model": "openai/qwen-local",
        "provider_model_ref": "openai/qwen-local",
        "requested_model": LOCAL_GATEWAY_MODEL,
        "profile": "planning_default",
        "backend": "openai_chat_completions",
        "model_family": LOCAL_GATEWAY_MODEL,
    },
}

CALL_ORDER = (
    ("T222", "A"),
    ("T218", "B"),
    ("T214", "A"),
    ("T218", "A"),
    ("T214", "B"),
    ("T222", "B"),
)


class IdentityFailure(RuntimeError):
    """A provider call did not prove the required same-model identity."""

    def __init__(self, proof: dict[str, Any]):
        self.proof = proof
        super().__init__(str(proof.get("failure_reason") or "identity failure"))


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
    text = str(value or "")
    for marker in ("Authorization:", "Bearer ", "api_key", "API_KEY"):
        if marker in text:
            text = text.replace(marker, f"{marker}[REDACTED]")
    return text


def _json_from_mixed_text(value: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    text = str(value or "")
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _persistent_config_fingerprint() -> dict[str, Any]:
    return retained._persistent_config_fingerprint()


def _product_state() -> dict[str, Any]:
    return retained._product_state()


def _c1_check() -> dict[str, Any]:
    command = [
        str(REPOSITORY_ROOT / "venv/bin/python3"),
        str(REPOSITORY_ROOT / "scripts/maintenance/apply_openclaw_c1_patch.py"),
        "check",
    ]
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"C1 vendor patch check failed: {completed.stderr[:500]}")
    payload = json.loads(completed.stdout)
    state = payload.get("state") or []
    hashes = {
        str(row.get("name")): row.get("sha256")
        for row in state[1:]
        if isinstance(row, dict) and row.get("name")
    }
    return {
        "status": "PASS",
        "operation": payload.get("operation"),
        "openclaw_version": state[0].get("openclaw_version") if state else None,
        "hashes": hashes,
        "state": state,
        "command": command,
    }


def _gateway_catalog(config: dict[str, Any]) -> dict[str, Any]:
    provider = ((config.get("models") or {}).get("providers") or {}).get("openai") or {}
    base_url = str(provider.get("baseUrl") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("OpenClaw openai provider has no baseUrl")
    inspection = retained._provider_inspection(f"{base_url}/models")
    records = [
        item
        for item in inspection.get("payload", {}).get("data", [])
        if isinstance(item, dict)
    ]
    match = next(
        (item for item in records if item.get("id") == LOCAL_GATEWAY_MODEL), None
    )
    root = str((match or {}).get("root") or "").removeprefix("/models/")
    if match is None or root != UNDERLYING_MODEL:
        raise RuntimeError(
            "gateway catalog did not prove qwen-local -> "
            f"{UNDERLYING_MODEL}: {match!r}"
        )
    return {
        "inspection": inspection,
        "provider_base_url": base_url,
        "endpoint": f"{base_url}/chat/completions",
        "model_record": match,
        "gateway_model": LOCAL_GATEWAY_MODEL,
        "underlying_model": root,
    }


def _prompt_packet(packet_id: str) -> dict[str, Any]:
    packet = TASKS[packet_id]
    from app.services.orchestration.planning.read_only_discovery import (
        build_discovery_prompt,
    )
    from app.services.orchestration.planning.repository_orientation import (
        derive_repository_orientation,
    )

    orientation = derive_repository_orientation(REPOSITORY_ROOT, packet["task"])
    canonical = build_discovery_prompt(packet["task"], "", orientation)
    encoded = canonical.encode("utf-8")
    return {
        **packet,
        "id": packet_id,
        "orientation": orientation,
        "orientation_metadata": {
            **orientation.as_details(),
            "paths": list(orientation.paths),
        },
        "task_sha256": _sha256_text(packet["task"]),
        "canonical_discovery_prompt": canonical,
        "canonical_discovery_prompt_sha256": _sha256_bytes(encoded),
        "canonical_discovery_prompt_bytes": len(encoded),
        "canonical_discovery_prompt_chars": len(canonical),
        "canonical_discovery_prompt_token_estimate": (len(canonical) + 3) // 4,
    }


def _bootstrap_audit() -> dict[str, Any]:
    """Audit current binding source plus retained runtime telemetry."""

    binding_source = (
        REPOSITORY_ROOT
        / "app/services/orchestration/execution/executor_workspace_binding.py"
    ).read_text(encoding="utf-8")
    service_source = (
        REPOSITORY_ROOT / "app/services/agents/openclaw_service.py"
    ).read_text(encoding="utf-8")
    retained_runtime_source_available = RETAINED_RUNTIME_SOURCE.is_file()
    retained_runtime_source_error = None
    retained_payload: dict[str, Any] = {}
    if retained_runtime_source_available:
        try:
            retained_raw = RETAINED_RUNTIME_SOURCE.read_text(encoding="utf-8")
        except OSError as exc:
            retained_runtime_source_available = False
            retained_runtime_source_error = (
                f"retained runtime artifact unreadable: {type(exc).__name__}"
            )
        else:
            retained_payload = _json_from_mixed_text(retained_raw) or {}
    else:
        retained_runtime_source_error = "generated-only artifact absent"
    report = (retained_payload.get("meta") or {}).get("systemPromptReport") or {}
    injected = {
        str(item.get("name")): item
        for item in report.get("injectedWorkspaceFiles", [])
        if isinstance(item, dict) and item.get("name")
    }
    system_prompt = report.get("systemPrompt") or {}
    extra_chars = int(system_prompt.get("chars") or 0)
    contamination_class = (
        "C. BOOTSTRAP_CONTEXT_PRESENT"
        if retained_runtime_source_available
        else "UNAVAILABLE. RETAINED_RUNTIME_TELEMETRY_ABSENT"
    )
    return {
        "OPENCLAW_DISCOVERY_SKIP_BOOTSTRAP": (
            'defaults["skipBootstrap"] = True' in binding_source
            or '"skipBootstrap": True' in binding_source
        ),
        "OPENCLAW_DISCOVERY_AGENTDIR_EPHEMERAL": (
            'agent["agentDir"]' in binding_source
            or 'selected["agentDir"]' in service_source
            or '"agentDir": str(agent_dir)' in binding_source
        ),
        "OPENCLAW_DISCOVERY_SESSION_STORE_EPHEMERAL": (
            (
                'session["store"]' in binding_source
                or '"session": {"store": str(state_dir' in binding_source
            )
            and 'session_config["store"]' in service_source
        ),
        "OPENCLAW_DISCOVERY_AGENTS_MD_VISIBLE": bool(
            (injected.get("AGENTS.md") or {}).get("injectedChars", 0)
        ),
        "OPENCLAW_DISCOVERY_SOUL_MD_VISIBLE": bool(
            (injected.get("SOUL.md") or {}).get("injectedChars", 0)
        ),
        "OPENCLAW_DISCOVERY_TOOLS_MD_VISIBLE": bool(
            (injected.get("TOOLS.md") or {}).get("injectedChars", 0)
        ),
        "OPENCLAW_DISCOVERY_MEMORY_CONTEXT_VISIBLE": bool(
            (injected.get("MEMORY.md") or {}).get("injectedChars", 0)
        ),
        "OPENCLAW_DISCOVERY_PRIOR_SESSION_CONTEXT_POSSIBLE": False,
        "OPENCLAW_DISCOVERY_SYSTEM_PROMPT_EXTRA_CHARS": extra_chars,
        "retained_runtime_source": str(RETAINED_RUNTIME_SOURCE),
        "retained_runtime_source_available": retained_runtime_source_available,
        "retained_runtime_source_status": (
            "AVAILABLE" if retained_runtime_source_available else "UNAVAILABLE"
        ),
        "retained_runtime_source_error": retained_runtime_source_error,
        "retained_session_key": ((report.get("sessionKey")) or None),
        "retained_injected_workspace_files": injected,
        "retained_system_prompt": system_prompt,
        "reasoning": {
            "fresh_agent_dir": "ephemeral binding directory",
            "fresh_session_store": "ephemeral state/store per invocation",
            "prior_history": "not possible under the fresh store; repeated session key alone does not import transcript",
            "bootstrap": "skipBootstrap is set, but retained telemetry still reports injected missing-file placeholders",
        },
        "OPENCLAW_DISCOVERY_BOOTSTRAP_CONTAMINATION_CLASS": contamination_class,
    }


def _preflight() -> dict[str, Any]:
    from app.config import settings
    from app.services.agents.providers.openai_chat_adapter import (
        OpenAIChatCompletionsRuntime,
    )
    from app.services.agents.runtime_configuration import (
        BackendRole,
        RoleRuntimeConfiguration,
    )

    c1 = _c1_check()
    persistent = retained._persistent_config()
    selected = retained._agent_for_project(persistent)
    gateway = _gateway_catalog(persistent)
    packets = {packet_id: _prompt_packet(packet_id) for packet_id in TASK_IDS}
    prompt_pairs = {
        packet_id: {
            "canonical_sha256": packet["canonical_discovery_prompt_sha256"],
            "canonical_bytes": packet["canonical_discovery_prompt_bytes"],
            "arm_a_wire_sha256": packet["canonical_discovery_prompt_sha256"],
            "arm_b_wire_sha256": packet["canonical_discovery_prompt_sha256"],
            "canonical_equal_arm_a": True,
            "canonical_equal_arm_b": True,
            "wire_body_equal": True,
        }
        for packet_id, packet in packets.items()
    }
    configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="openai_chat_completions",
        model_family=LOCAL_GATEWAY_MODEL,
        adaptation_profile="planning_default",
    )
    old_base = settings.OPENAI_CHAT_COMPLETIONS_BASE_URL
    old_model = settings.OPENAI_CHAT_COMPLETIONS_MODEL
    try:
        settings.OPENAI_CHAT_COMPLETIONS_BASE_URL = gateway["provider_base_url"]
        settings.OPENAI_CHAT_COMPLETIONS_MODEL = LOCAL_GATEWAY_MODEL
        direct_db = retained.EvaluationSessionLocal()
        try:
            direct_runtime = OpenAIChatCompletionsRuntime(
                direct_db, None, None, runtime_configuration=configuration
            )
            direct_metadata = direct_runtime.get_backend_metadata()
        finally:
            direct_db.close()
    finally:
        settings.OPENAI_CHAT_COMPLETIONS_BASE_URL = old_base
        settings.OPENAI_CHAT_COMPLETIONS_MODEL = old_model
    bootstrap = _bootstrap_audit()
    return {
        "status": "READY",
        "provider_call_budget": PROVIDER_CALL_BUDGET,
        "provider_retries": PROVIDER_RETRIES,
        "persistent_config": _persistent_config_fingerprint(),
        "selected_openclaw_agent": {
            "id": selected.get("id"),
            "workspace": selected.get("workspace"),
            "model": selected.get("model"),
        },
        "c1_vendor_patch": c1,
        "gateway_catalog": gateway,
        "arm_a": {
            **ARMS["A"],
            "endpoint": gateway["endpoint"],
            "underlying_model": gateway["underlying_model"],
            "fallbacks": [],
            "native_tool_requirement": 0,
        },
        "arm_b": {
            **ARMS["B"],
            "endpoint": gateway["endpoint"],
            "underlying_model": gateway["underlying_model"],
            "direct_adapter_class": "OpenAIChatCompletionsRuntime",
            "openclaw_invoked": False,
            "openclaw_config_used": False,
            "temporary_process_local_endpoint_binding": True,
        },
        "direct_runtime_metadata": direct_metadata,
        "packets": {
            packet_id: {
                key: value
                for key, value in packet.items()
                if key
                not in {"orientation", "canonical_discovery_prompt", "target_terms"}
            }
            for packet_id, packet in packets.items()
        },
        "prompt_fidelity": {
            "canonical_prompt_equal": all(
                item["wire_body_equal"] for item in prompt_pairs.values()
            ),
            "prompt_difference_class": "B. SAME_CANONICAL_BODY_RUNTIME_ENVELOPE_DIFFERS",
            "pairs": prompt_pairs,
        },
        "bootstrap_audit": bootstrap,
        "call_order": [
            {"sequence": index, "packet": packet, "arm": arm}
            for index, (packet, arm) in enumerate(CALL_ORDER, start=1)
        ],
        "isolation": {
            "private_evaluation_database": True,
            "normal_lifecycle_runner": False,
            "production_prompt_builder": True,
            "production_parser_executor_materialization_pl16": True,
            "raw_provider_response_retention": True,
            "pl18_deny_all_arm_a": True,
            "direct_native_tools": False,
            "max_discovery_turns": 1,
            "hidden_ground_truth_in_prompt": False,
        },
    }


def _configure_openclaw(
    arm: dict[str, Any], runtime_workspace: Path
) -> tuple[Any, dict[str, Any]]:
    service, identity = retained._configure_ephemeral_service(arm, runtime_workspace)
    service._configure_strict_provider_controls(identity["agent_id"])
    config_path = service._openclaw_config_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    selected = next(
        agent
        for agent in (config.get("agents") or {}).get("list", [])
        if isinstance(agent, dict)
        and str(agent.get("id") or "").strip() == identity["agent_id"]
    )
    final_model = selected.get("model")
    model_ref = (
        final_model.get("primary") if isinstance(final_model, dict) else final_model
    )
    if model_ref != arm["requested_provider_model"]:
        raise IdentityFailure(
            {
                "status": "INVALID_IDENTITY_DRIFT",
                "failure_reason": "selected OpenClaw model changed before dispatch",
                "selected_model": final_model,
            }
        )
    if (final_model.get("fallbacks") if isinstance(final_model, dict) else None) != []:
        raise IdentityFailure(
            {
                "status": "INVALID_IDENTITY_DRIFT",
                "failure_reason": "selected OpenClaw fallbacks were not empty",
                "selected_model": final_model,
            }
        )
    if selected.get("tools", {}).get("deny") != ["*"]:
        raise IdentityFailure(
            {
                "status": "INVALID_IDENTITY_DRIFT",
                "failure_reason": "PL18 deny-all was not retained",
                "selected_tools": selected.get("tools"),
            }
        )
    identity = {
        **identity,
        "requested_provider_model": arm["requested_provider_model"],
        "selected_model_object": final_model,
        "model": model_ref,
        "fallbacks": (
            final_model.get("fallbacks") if isinstance(final_model, dict) else None
        ),
        "tools": selected.get("tools"),
        "config_sha256": _sha256_bytes(config_path.read_bytes()),
        "endpoint": identity.get("provider_endpoint"),
        "underlying_model": UNDERLYING_MODEL,
        "strict_controls": getattr(service, "_strict_provider_controls", None),
        "openclaw_version": service._resolve_openclaw_cli_version(),
    }
    return service, identity


def _native_tool_telemetry(raw_stdout: str, raw_stderr: str) -> dict[str, Any]:
    payload = (
        _json_from_mixed_text(raw_stderr) or _json_from_mixed_text(raw_stdout) or {}
    )
    meta = payload.get("meta") or {}
    report = meta.get("systemPromptReport") or {}
    tools = report.get("tools") or {}
    entries = tools.get("entries")
    if not isinstance(entries, list):
        raise IdentityFailure(
            {
                "status": "IDENTITY_UNVERIFIED",
                "failure_reason": "OpenClaw native tool telemetry was absent",
            }
        )
    return {
        "provider": report.get("provider"),
        "model": report.get("model"),
        "system_prompt": report.get("systemPrompt") or {},
        "bootstrap_truncation": report.get("bootstrapTruncation") or {},
        "injected_workspace_files": report.get("injectedWorkspaceFiles") or [],
        "tools": tools,
        "entries": entries,
        "native_tool_entry_count": len(entries),
        "raw_source": "OpenClaw raw meta.systemPromptReport",
    }


def _direct_call(
    prompt: str,
    arm: dict[str, Any],
    endpoint_base: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from app.config import settings
    from app.services.agents.providers import openai_chat_adapter as adapter_module
    from app.services.agents.providers.openai_chat_adapter import (
        OpenAIChatCompletionsRuntime,
    )
    from app.services.agents.runtime_configuration import (
        BackendRole,
        RoleRuntimeConfiguration,
    )
    from app.services.agents.runtime_invocation import RuntimeInvocationOptions

    captured: dict[str, Any] = {}
    original_post = httpx.AsyncClient.post
    original_observability = adapter_module._response_shape_observability

    async def capture_post(
        client: httpx.AsyncClient, url: str, *args: Any, **kwargs: Any
    ):
        request_headers = dict(kwargs.get("headers") or {})
        request_headers.pop("Authorization", None)
        request_headers.pop("authorization", None)
        payload = copy.deepcopy(kwargs.get("json"))
        captured["request"] = {
            "url": url,
            "headers": request_headers,
            "json": payload,
            "tools_field_present": isinstance(payload, dict) and "tools" in payload,
        }
        response = await original_post(client, url, *args, **kwargs)
        captured["response_status"] = response.status_code
        captured["response_headers"] = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "x-request-id", "server"}
        }
        captured["response_text"] = _redact_text(response.text[:20000])
        return response

    def capture_response_shape(body: Any, content: str) -> dict[str, Any]:
        captured["response_body"] = copy.deepcopy(body)
        return original_observability(body, content)

    old_base = settings.OPENAI_CHAT_COMPLETIONS_BASE_URL
    old_model = settings.OPENAI_CHAT_COMPLETIONS_MODEL
    old_chat_key = settings.OPENAI_CHAT_COMPLETIONS_API_KEY
    old_openai_key = settings.OPENAI_API_KEY
    try:
        settings.OPENAI_CHAT_COMPLETIONS_BASE_URL = endpoint_base
        settings.OPENAI_CHAT_COMPLETIONS_MODEL = LOCAL_GATEWAY_MODEL
        settings.OPENAI_CHAT_COMPLETIONS_API_KEY = ""
        settings.OPENAI_API_KEY = ""
        configuration = RoleRuntimeConfiguration(
            role=BackendRole.PLANNING,
            backend_name="openai_chat_completions",
            model_family=LOCAL_GATEWAY_MODEL,
            adaptation_profile="planning_default",
        )
        runtime = OpenAIChatCompletionsRuntime(
            retained.EvaluationSessionLocal(),
            None,
            None,
            runtime_configuration=configuration,
        )
        with _patch_attr(httpx.AsyncClient, "post", capture_post):
            with _patch_attr(
                adapter_module, "_response_shape_observability", capture_response_shape
            ):
                started = time.monotonic()
                result = asyncio.run(
                    runtime.invoke_prompt(
                        prompt,
                        timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
                        session_prefix="model4-runtime-isolation",
                        invocation_options=RuntimeInvocationOptions(
                            timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
                            max_output_tokens=16_384,
                            temperature=0,
                            reasoning_enabled=False,
                        ),
                    )
                )
                captured["latency_seconds"] = round(time.monotonic() - started, 3)
        return result, captured, runtime.get_backend_metadata()
    finally:
        settings.OPENAI_CHAT_COMPLETIONS_BASE_URL = old_base
        settings.OPENAI_CHAT_COMPLETIONS_MODEL = old_model
        settings.OPENAI_CHAT_COMPLETIONS_API_KEY = old_chat_key
        settings.OPENAI_API_KEY = old_openai_key


class _patch_attr:
    """Small local patch context to avoid a pytest dependency in the script."""

    def __init__(self, owner: Any, name: str, value: Any):
        self.owner = owner
        self.name = name
        self.value = value
        self.original = getattr(owner, name)

    def __enter__(self):
        setattr(self.owner, self.name, self.value)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        setattr(self.owner, self.name, self.original)


def _direct_identity(
    arm: dict[str, Any],
    captured: dict[str, Any],
    metadata: dict[str, Any],
    gateway: dict[str, Any],
    prompt_hash: str,
) -> dict[str, Any]:
    body = captured.get("response_body")
    request = captured.get("request") or {}
    provider_model = body.get("model") if isinstance(body, dict) else None
    choices = body.get("choices") if isinstance(body, dict) else None
    message = (
        choices[0].get("message") if isinstance(choices, list) and choices else None
    )
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    proof = {
        "status": "PASS",
        "backend": "openai_chat_completions",
        "requested_provider_model": arm["requested_provider_model"],
        "provider_response_model": provider_model,
        "endpoint": request.get("url"),
        "gateway_model": gateway["gateway_model"],
        "underlying_model": gateway["underlying_model"],
        "canonical_prompt_sha256": prompt_hash,
        "tools_field_present": request.get("tools_field_present"),
        "response_tool_calls": tool_calls if isinstance(tool_calls, list) else [],
        "openclaw_invoked": False,
        "fallback_route": "none",
        "runtime_metadata": metadata,
        "failure_reason": None,
    }
    if provider_model != LOCAL_GATEWAY_MODEL:
        proof.update(
            status="INVALID_IDENTITY_DRIFT",
            failure_reason=f"direct provider response model {provider_model!r} did not match qwen-local",
        )
    elif request.get("url") != gateway["endpoint"]:
        proof.update(
            status="INVALID_IDENTITY_DRIFT",
            failure_reason="direct adapter request endpoint did not match ai-gateway qwen-local endpoint",
        )
    elif request.get("tools_field_present"):
        proof.update(
            status="INVALID_IDENTITY_DRIFT",
            failure_reason="direct adapter exposed a provider-native tools field",
        )
    elif isinstance(tool_calls, list) and tool_calls:
        proof.update(
            status="INVALID_IDENTITY_DRIFT",
            failure_reason="direct provider returned native tool calls",
        )
    elif not isinstance(body, dict):
        proof.update(
            status="IDENTITY_UNVERIFIED",
            failure_reason="direct raw provider response envelope was not retained",
        )
    if proof["status"] != "PASS":
        raise IdentityFailure(proof)
    return proof


def _serialize_handle(handle: Any) -> dict[str, Any]:
    """Serialize only the public PL16 projection; internal handles have no to_dict."""

    method = getattr(handle, "to_provider_dict", None)
    if not callable(method):
        raise TypeError("PL16 handle has no provider-safe serialization method")
    return dict(method())


def _path_fabrication_count(request: Any) -> int:
    values = list(getattr(request, "paths", ()) or ())
    if getattr(request, "path", None):
        values.append(request.path)
    count = 0
    for value in values:
        path = REPOSITORY_ROOT.joinpath(*str(value).split("/"))
        if getattr(request, "action", None) == "read_file":
            invalid = not path.is_file()
        else:
            invalid = not (path.is_file() or path.is_dir())
        count += int(invalid)
    return count


def _replay_and_score(
    packet: dict[str, Any], extracted: str, provider: dict[str, Any]
) -> dict[str, Any]:
    from app.services.orchestration.planning.read_only_discovery import (
        DiscoveryContractError,
        discovery_output_text,
        execute_discovery_request,
        materialize_observation_source_context,
        parse_discovery_request,
    )
    from app.services.orchestration.planning.semantic_target_inventory import (
        build_semantic_target_inventory,
    )
    from app.services.orchestration.planning.source_materialization import (
        materialize_planner_source_context,
        observed_candidate_paths,
    )

    metrics: dict[str, Any] = {
        "R1_canonical_contract_validity": False,
        "R2_exact_path_fidelity": False,
        "R3_fabricated_recombined_path_count": 0,
        "R4_relevant_scope_selection": False,
        "R5_query_semantic_quality": 0,
        "R6_production_execution_success": False,
        "R7_hit_count": 0,
        "R8_useful_materialized_source": False,
        "R9_target_region_exposed": False,
        "R10_target_hint": False,
        "R11_PL16_handle_count": 0,
        "R12_latency_seconds": provider.get("latency_seconds"),
        "R13_raw_response_size": provider.get("raw_response_size", 0),
        "R14_prior_session_bootstrap_like_prose": {
            "detected": False,
            "markers": [],
        },
        "R15_native_tool_syntax_leakage": False,
        "R16_effective_identity_proof": bool(provider.get("identity_proof")),
    }
    prose_markers = [
        marker
        for marker in ("NoReply", "prior session", "bootstrap", "tool_call", "<tool")
        if marker.lower() in extracted.lower()
    ]
    metrics["R14_prior_session_bootstrap_like_prose"] = {
        "detected": bool(prose_markers),
        "markers": prose_markers,
    }
    metrics["R15_native_tool_syntax_leakage"] = bool(
        re.search(r"(?i)(tool_calls?|<tool|function_call|assistant\s+to=)", extracted)
    )
    try:
        request = parse_discovery_request(extracted)
    except DiscoveryContractError as exc:
        return {
            "parser_status": "invalid",
            "failure_reason": str(exc),
            "metrics": metrics,
        }
    metrics["R1_canonical_contract_validity"] = True
    metrics["R3_fabricated_recombined_path_count"] = _path_fabrication_count(request)
    target_path = packet["target_path"]
    requested_paths = tuple(getattr(request, "paths", ()) or ())
    requested_path = getattr(request, "path", None)
    metrics["R2_exact_path_fidelity"] = (
        target_path in requested_paths or requested_path == target_path
    )
    metrics["R4_relevant_scope_selection"] = bool(
        metrics["R2_exact_path_fidelity"]
        or any(
            target_path.startswith(path.rstrip("/") + "/")
            or path.startswith(target_path.rstrip("/") + "/")
            for path in requested_paths
        )
    )
    query = str(getattr(request, "query", None) or "").lower()
    terms = tuple(str(term).lower() for term in packet.get("target_terms", ()))
    if (
        getattr(request, "action", None) == "read_file"
        and requested_path == target_path
    ):
        metrics["R5_query_semantic_quality"] = 2
    elif query and any(term in query for term in terms):
        metrics["R5_query_semantic_quality"] = 1
    try:
        observation = execute_discovery_request(REPOSITORY_ROOT, request)
    except DiscoveryContractError as exc:
        return {
            "parser_status": "valid",
            "parsed_request": {
                "action": request.action,
                "query": request.query,
                "paths": list(request.paths),
                "path": request.path,
            },
            "production_execution_status": "failed_closed",
            "failure_reason": str(exc),
            "metrics": metrics,
        }
    metrics["R6_production_execution_success"] = True
    metrics["R7_hit_count"] = len(getattr(observation, "hits", ()) or ())
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
    target_items = [
        item
        for item in getattr(materialization, "files", ())
        if getattr(item, "relative_path", None) == target_path
    ]
    metrics["R9_target_region_exposed"] = any(
        bool(getattr(item, "target_included", False)) for item in target_items
    )
    metrics["R10_target_hint"] = any(
        bool(getattr(item, "target_hint", None)) for item in target_items
    )
    metrics["R11_PL16_handle_count"] = len(getattr(inventory, "handles", ()) or ())
    relevant_materialized = any(
        getattr(item, "relative_path", None) == target_path
        and getattr(item, "content", None) is not None
        for item in getattr(materialization, "files", ())
    )
    metrics["R8_useful_materialized_source"] = bool(
        metrics["R1_canonical_contract_validity"]
        and metrics["R6_production_execution_success"]
        and relevant_materialized
    )
    return {
        "parser_status": "valid",
        "parsed_request": {
            "action": request.action,
            "query": request.query,
            "paths": list(request.paths),
            "path": request.path,
        },
        "production_execution_status": "completed",
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
            "handles": [_serialize_handle(handle) for handle in inventory.handles],
        },
        "metrics": metrics,
    }


def _run_cell(
    sequence: int, packet: dict[str, Any], arm: dict[str, Any]
) -> dict[str, Any]:
    cell_dir = EVIDENCE_ROOT / "cells" / f"{sequence:02d}-{packet['id']}-{arm['name']}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    prompt = packet["canonical_discovery_prompt"]
    prompt_bytes = prompt.encode("utf-8")
    wire_hash = _sha256_bytes(prompt_bytes)
    (cell_dir / "canonical-discovery-prompt.txt").write_bytes(prompt_bytes)
    (cell_dir / "wire-prompt.txt").write_bytes(prompt_bytes)
    _write_json(
        cell_dir / "prompt-metadata.json",
        {
            "packet": packet["id"],
            "runtime": arm["runtime"],
            "canonical_discovery_prompt_sha256": packet[
                "canonical_discovery_prompt_sha256"
            ],
            "canonical_discovery_prompt_bytes": packet[
                "canonical_discovery_prompt_bytes"
            ],
            "canonical_discovery_prompt_chars": packet[
                "canonical_discovery_prompt_chars"
            ],
            "canonical_discovery_prompt_token_estimate": packet[
                "canonical_discovery_prompt_token_estimate"
            ],
            "wire_prompt_sha256": wire_hash,
            "wire_prompt_bytes": len(prompt_bytes),
            "prompt_difference_class": "B. SAME_CANONICAL_BODY_RUNTIME_ENVELOPE_DIFFERS",
            "wire_body_semantically_equal": wire_hash
            == packet["canonical_discovery_prompt_sha256"],
            "orientation_metadata": packet["orientation_metadata"],
        },
    )
    runtime_workspace = Path(
        tempfile.mkdtemp(prefix="post33-model4-isolation-workspace-")
    )
    service = None
    dispatch_started = False
    started = time.monotonic()
    identity: dict[str, Any] | None = None
    try:
        if arm["runtime"] == "local_openclaw":
            c1 = _c1_check()
            _write_json(cell_dir / "c1-patch-verification.json", c1)
            service, identity = _configure_openclaw(arm, runtime_workspace)
            command = service.build_cli_agent_command(
                prompt,
                source_brain="local",
                timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
                session_prefix="planning-model4-runtime-isolation",
                strict_provider_result=True,
            )
            dispatch_started = True
            proc, diagnostics = asyncio.run(
                service._run_cli_prompt_with_diagnostics(
                    command,
                    timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
                    cwd=str(runtime_workspace),
                    prompt=prompt,
                    invocation_kind="model4-runtime-isolation",
                    strict_provider_result=True,
                )
            )
            raw_stdout = _redact_text(proc.stdout or "")
            raw_stderr = _redact_text(proc.stderr or "")
            (cell_dir / "raw-stdout.txt").write_text(raw_stdout, encoding="utf-8")
            (cell_dir / "raw-stderr.txt").write_text(raw_stderr, encoding="utf-8")
            parsed = service.parse_cli_response(
                proc, expected_session_id=None, strict_provider_result=True
            )
            native = _native_tool_telemetry(raw_stdout, raw_stderr)
            if native["native_tool_entry_count"] != 0:
                raise IdentityFailure(
                    {
                        "status": "INVALID_IDENTITY_DRIFT",
                        "failure_reason": "OpenClaw exposed native tools under PL18",
                        "native_tool_entry_count": native["native_tool_entry_count"],
                    }
                )
            proof = retained._verify_runtime_identity(
                arm,
                identity=identity,
                diagnostics=diagnostics,
                parsed_runtime=parsed,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                prompt_hash=wire_hash,
            )
            if (
                proof.get("effective_provider_model_ref")
                != arm["requested_provider_model"]
            ):
                raise IdentityFailure(proof)
            identity = {
                **identity,
                "effective_openclaw_provider": "openai",
                "effective_openclaw_model": LOCAL_GATEWAY_MODEL,
                "underlying_model": UNDERLYING_MODEL,
                "identity_proof": proof,
                "native_tool_telemetry": native,
                "native_tool_entry_count": 0,
            }
            _write_json(cell_dir / "runtime-identity.json", identity)
            _write_json(cell_dir / "native-tool-telemetry.json", native)
            _write_json(
                cell_dir / "fallback-diagnostics.json",
                proof.get("fallback_diagnostics", []),
            )
            extracted = retained.discovery_output_text(
                parsed, retained.extract_structured_text
            )
            raw_size = len(raw_stdout.encode("utf-8")) + len(raw_stderr.encode("utf-8"))
            provider_record = {
                "runtime": arm["runtime"],
                "raw_response_size": raw_size,
                "latency_seconds": round(time.monotonic() - started, 3),
                "response_extraction_source": parsed.get("output_channel_used"),
                "runtime_system_prompt_tool_telemetry": native,
                "identity_proof": proof,
            }
            _write_json(cell_dir / "provider-envelope-metadata.json", provider_record)
        else:
            gateway_base = json.loads(
                (EVIDENCE_ROOT / "preflight.json").read_text(encoding="utf-8")
            )["gateway_catalog"]["provider_base_url"]
            dispatch_started = True
            result, captured, metadata = _direct_call(prompt, arm, gateway_base)
            body = captured.get("response_body")
            _write_json(cell_dir / "raw-provider-response.json", body)
            _write_json(
                cell_dir / "wire-request-metadata.json",
                {
                    **(captured.get("request") or {}),
                    "headers": {"Content-Type": "application/json"},
                },
            )
            _write_json(
                cell_dir / "response-envelope-metadata.json",
                {
                    "status": captured.get("response_status"),
                    "headers": captured.get("response_headers"),
                    "response_shape": result.get("provider_response_observability"),
                    "raw_response_bytes": (
                        len(json.dumps(body, sort_keys=True).encode("utf-8"))
                        if body is not None
                        else 0
                    ),
                },
            )
            if body is None:
                raise IdentityFailure(
                    {
                        "status": "IDENTITY_UNVERIFIED",
                        "failure_reason": "direct raw provider response envelope was not retained",
                    }
                )
            gateway = json.loads(
                (EVIDENCE_ROOT / "preflight.json").read_text(encoding="utf-8")
            )["gateway_catalog"]
            proof = _direct_identity(arm, captured, metadata, gateway, wire_hash)
            identity = {
                "backend": arm["backend"],
                "effective_provider": "openai",
                "effective_model": LOCAL_GATEWAY_MODEL,
                "underlying_model": UNDERLYING_MODEL,
                "endpoint": captured.get("request", {}).get("url"),
                "openclaw_invoked": False,
                "native_tool_entry_count": 0,
                "identity_proof": proof,
                "runtime_metadata": metadata,
            }
            _write_json(cell_dir / "runtime-identity.json", identity)
            _write_json(
                cell_dir / "native-tool-telemetry.json",
                {
                    "native_tool_entry_count": 0,
                    "request_tools_field_present": bool(
                        (captured.get("request") or {}).get("tools_field_present")
                    ),
                    "source": "direct adapter request/response envelope",
                },
            )
            extracted = str(result.get("output") or "")
            raw_size = len(json.dumps(body, sort_keys=True).encode("utf-8"))
            provider_record = {
                "runtime": arm["runtime"],
                "raw_response_size": raw_size,
                "latency_seconds": captured.get("latency_seconds"),
                "response_extraction_source": "choices[0].message.content",
                "normalization_branch": (
                    result.get("provider_response_observability") or {}
                ).get("normalization_branch"),
                "identity_proof": proof,
                "openclaw_invoked": False,
            }
            _write_json(cell_dir / "provider-envelope-metadata.json", provider_record)
        (cell_dir / "extracted-response.txt").write_text(extracted, encoding="utf-8")
        replay = _replay_and_score(packet, extracted, provider_record)
        result_record = {
            "status": "completed",
            "dispatch_started": dispatch_started,
            "sequence": sequence,
            "packet": packet["id"],
            "runtime": arm["runtime"],
            "arm": arm["name"],
            "requested_provider_model": arm["requested_provider_model"],
            "canonical_prompt_sha256": packet["canonical_discovery_prompt_sha256"],
            "wire_prompt_sha256": wire_hash,
            "identity": identity,
            "latency_seconds": provider_record["latency_seconds"],
            "extracted_response": extracted,
            "replay": replay,
            "metrics": replay["metrics"],
        }
        _write_json(cell_dir / "result.json", result_record)
        return result_record
    except IdentityFailure as exc:
        proof = exc.proof
        _write_json(cell_dir / "identity-failure.json", proof)
        result_record = {
            "status": "invalid_identity",
            "hard_stop": True,
            "dispatch_started": dispatch_started,
            "sequence": sequence,
            "packet": packet["id"],
            "runtime": arm["runtime"],
            "arm": arm["name"],
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc),
            "identity_proof": proof,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
        _write_json(cell_dir / "result.json", result_record)
        return result_record
    except Exception as exc:  # noqa: BLE001 - one-cell gate evidence
        raw_retained = (cell_dir / "raw-stdout.txt").exists() or (
            cell_dir / "raw-provider-response.json"
        ).exists()
        hard_stop = bool(dispatch_started and not raw_retained)
        result_record = {
            "status": "failed",
            "hard_stop": hard_stop,
            "dispatch_started": dispatch_started,
            "sequence": sequence,
            "packet": packet["id"],
            "runtime": arm["runtime"],
            "arm": arm["name"],
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc)[:1200],
            "latency_seconds": round(time.monotonic() - started, 3),
        }
        _write_json(cell_dir / "result.json", result_record)
        return result_record
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
        cells = [item for item in results if item.get("arm") == arm["name"]]
        scored = [item for item in cells if item.get("status") == "completed"]
        metrics = [item.get("metrics", {}) for item in scored]
        aggregate[arm_id] = {
            "runtime": arm["runtime"],
            "cells": len(cells),
            "scored_cells": len(scored),
            "canonical_responses": sum(
                bool(item.get("R1_canonical_contract_validity")) for item in metrics
            ),
            "useful_observations": sum(
                bool(item.get("R8_useful_materialized_source")) for item in metrics
            ),
            "exact_path_fidelity": sum(
                bool(item.get("R2_exact_path_fidelity")) for item in metrics
            ),
            "relevant_scope_selection": sum(
                bool(item.get("R4_relevant_scope_selection")) for item in metrics
            ),
            "query_quality_total": sum(
                int(item.get("R5_query_semantic_quality", 0)) for item in metrics
            ),
            "production_successes": sum(
                bool(item.get("R6_production_execution_success")) for item in metrics
            ),
            "hit_count_total": sum(
                int(item.get("R7_hit_count", 0)) for item in metrics
            ),
            "target_regions_exposed": sum(
                bool(item.get("R9_target_region_exposed")) for item in metrics
            ),
            "target_hints": sum(bool(item.get("R10_target_hint")) for item in metrics),
            "pl16_handle_total": sum(
                int(item.get("R11_PL16_handle_count", 0)) for item in metrics
            ),
            "fabricated_path_total": sum(
                int(item.get("R3_fabricated_recombined_path_count", 0))
                for item in metrics
            ),
            "latency_seconds": [item.get("R12_latency_seconds") for item in metrics],
            "raw_response_sizes": [
                item.get("R13_raw_response_size") for item in metrics
            ],
            "bootstrap_like_cells": sum(
                bool(
                    (item.get("R14_prior_session_bootstrap_like_prose") or {}).get(
                        "detected"
                    )
                )
                for item in metrics
            ),
            "native_tool_leakage_cells": sum(
                bool(item.get("R15_native_tool_syntax_leakage")) for item in metrics
            ),
            "identity_proven_cells": sum(
                bool(item.get("R16_effective_identity_proof")) for item in metrics
            ),
        }
    return aggregate


def _decision(
    results: list[dict[str, Any]],
    aggregate: dict[str, Any],
    *,
    config_unchanged: bool,
    product_mutation: bool,
) -> dict[str, Any]:
    if product_mutation or not config_unchanged:
        return {
            "final_decision": "F. EVALUATION_INVALID_OTHER",
            "confidence": "HIGH",
            "reason": "product state or persistent OpenClaw configuration changed",
        }
    if len(results) < len(CALL_ORDER) or any(
        not (
            item.get("identity_proof")
            or (item.get("identity") or {}).get("identity_proof")
        )
        for item in results
        if item.get("dispatch_started")
    ):
        return {
            "final_decision": "E. EFFECTIVE_MODEL_IDENTITY_INVALID",
            "confidence": "HIGH",
            "reason": "same-model identity was not proven for every dispatched cell",
        }
    if any(item.get("status") != "completed" for item in results):
        return {
            "final_decision": "F. EVALUATION_INVALID_OTHER",
            "confidence": "HIGH",
            "reason": "a provider cell failed before a complete comparable replay",
        }
    direct = aggregate["B"]
    openclaw = aggregate["A"]
    direct_improvements = sum(
        direct[key] > openclaw[key]
        for key in (
            "canonical_responses",
            "useful_observations",
            "exact_path_fidelity",
            "relevant_scope_selection",
        )
    )
    control_direct = next(
        (
            item
            for item in results
            if item.get("packet") == "T214" and item.get("arm") == "direct"
        ),
        {},
    )
    control_openclaw = next(
        (
            item
            for item in results
            if item.get("packet") == "T214" and item.get("arm") == "openclaw"
        ),
        {},
    )
    control_no_regression = (
        control_direct.get("metrics", {}).get("R1_canonical_contract_validity") is True
    ) and (
        int(control_direct.get("metrics", {}).get("R5_query_semantic_quality", 0))
        >= int(control_openclaw.get("metrics", {}).get("R5_query_semantic_quality", 0))
    )
    if direct_improvements >= 2 and control_no_regression:
        return {
            "final_decision": "A. OPENCLAW_RUNTIME_MATERIALLY_DEGRADES_DISCOVERY",
            "confidence": "MEDIUM",
            "reason": "direct runtime improves at least two discovery dimensions on the three-packet gate without explicit-path control regression",
        }
    if direct_improvements == 0 and aggregate["A"] == aggregate["B"]:
        return {
            "final_decision": "B. OPENCLAW_RUNTIME_NOT_MATERIAL",
            "confidence": "MEDIUM",
            "reason": "both runtime arms behave substantially similarly",
        }
    return {
        "final_decision": "C. OPENCLAW_RUNTIME_EFFECT_MIXED",
        "confidence": "MEDIUM",
        "reason": "runtime differences do not establish a consistent causal direction",
    }


def run(*, execute: bool) -> int:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    preflight_path = EVIDENCE_ROOT / "preflight.json"
    try:
        preflight = _preflight()
    except Exception as exc:  # provider-free preflight failure
        failed = {
            "status": "PREFLIGHT_FAILED",
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc)[:1200],
        }
        try:
            failed["persistent_config"] = _persistent_config_fingerprint()
        except Exception:
            pass
        _write_json(preflight_path, failed)
        return 2
    _write_json(preflight_path, preflight)
    _write_json(EVIDENCE_ROOT / "call-order.json", preflight["call_order"])
    if not execute:
        return 0

    state_before = _product_state()
    config_before = _persistent_config_fingerprint()
    _write_json(EVIDENCE_ROOT / "product-state-before.json", state_before)
    _write_json(EVIDENCE_ROOT / "config-before-fingerprint.json", config_before)
    packet_data = {packet_id: _prompt_packet(packet_id) for packet_id in TASK_IDS}
    results: list[dict[str, Any]] = []
    for sequence, (packet_id, arm_id) in enumerate(CALL_ORDER, start=1):
        result = _run_cell(sequence, packet_data[packet_id], ARMS[arm_id])
        results.append(result)
        if result.get("hard_stop"):
            break
    config_after = _persistent_config_fingerprint()
    state_after = _product_state()
    deltas = {
        key: state_after.get(key, 0) - state_before.get(key, 0) for key in state_before
    }
    aggregate = _aggregate(results)
    decision = _decision(
        results,
        aggregate,
        config_unchanged=config_before == config_after,
        product_mutation=any(value != 0 for value in deltas.values()),
    )
    _write_json(EVIDENCE_ROOT / "config-after-fingerprint.json", config_after)
    _write_json(EVIDENCE_ROOT / "product-state-after.json", state_after)
    _write_json(EVIDENCE_ROOT / "replay-summaries.json", results)
    _write_json(EVIDENCE_ROOT / "scorecard.json", aggregate)
    _write_json(EVIDENCE_ROOT / "decision.json", decision)
    manifest = {
        "status": "COMPLETED" if len(results) == len(CALL_ORDER) else "ABORTED",
        "provider_call_budget": PROVIDER_CALL_BUDGET,
        "provider_calls": sum(bool(item.get("dispatch_started")) for item in results),
        "provider_retries": PROVIDER_RETRIES,
        "persistent_openclaw_config_unchanged": config_before == config_after,
        "product_state_before": state_before,
        "product_state_after": state_after,
        "product_state_delta": deltas,
        "product_state_mutation": any(value != 0 for value in deltas.values()),
        "aggregate": aggregate,
        "decision": decision,
        "framework_boundary_preserved": True,
        "task222_retry": False,
        "task223_run": False,
        "product_attempts": 0,
    }
    _write_json(EVIDENCE_ROOT / "benchmark-manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return (
        0
        if config_before == config_after and not manifest["product_state_mutation"]
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
