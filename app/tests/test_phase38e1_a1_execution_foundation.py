"""PHASE38E1: dormant A1 execution foundation.

Pins the evidence-graded capability model, Attempt identity binding,
positive-closure semantics, and the non-executing backend.  Every test is
provider-free and process-free: no OpenClaw, provider, container runtime,
subprocess, or network access occurs, and process/network/file primitives
are patched to fail if touched.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import os
import socket
import subprocess
from pathlib import Path

import pytest

from app.services.a1_execution import (
    REQUIRED_A1_CAPABILITIES,
    A1AttemptIdentity,
    A1Capability,
    AttemptIdentityMismatch,
    BackendCapabilityProfile,
    CapabilityDeclaration,
    CapabilityEvidence,
    ClosureDomain,
    ClosureEvidence,
    ClosureEvidenceDurability,
    ClosureObservation,
    ClosureSignal,
    ClosureStatus,
    DormantExecutionBackend,
    ExecutionStatus,
    PreparationStatus,
    StartAuthorization,
    StartRefused,
    TerminationRequestStatus,
    decide_start_authorization,
    evaluate_attempt_closure,
    evaluate_capability_eligibility,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "services" / "a1_execution"
APP_DIR = Path(__file__).resolve().parents[1]

ATTEMPT = A1AttemptIdentity("attempt-0000000000000001", "dispatch-00000000000001", 1)
OTHER_ATTEMPT = A1AttemptIdentity(
    "attempt-0000000000000002", "dispatch-00000000000001", 1
)
STALE_GENERATION = A1AttemptIdentity(
    "attempt-0000000000000001", "dispatch-00000000000001", 2
)

LOCAL_EMPTY = ClosureObservation(
    ATTEMPT,
    ClosureDomain.LOCAL_PROCESS,
    ClosureSignal.EMPTY_SCOPE_OBSERVED,
    "evidence-local-000001",
)
PROVIDER_DONE = ClosureObservation(
    ATTEMPT,
    ClosureDomain.PROVIDER_SESSION,
    ClosureSignal.AUTHORITATIVE_PROVIDER_COMPLETION,
    "evidence-provider-0001",
)
DURABLE = ClosureEvidence(
    ATTEMPT, ClosureEvidenceDurability.DURABLE_VERIFIED, "evidence-manifest-0001"
)


def _profile(**overrides: CapabilityDeclaration) -> BackendCapabilityProfile:
    declarations = {
        capability: CapabilityDeclaration(
            capability,
            CapabilityEvidence.VERIFIED,
            f"evidence-{capability.value[:20]}-01",
        )
        for capability in REQUIRED_A1_CAPABILITIES
    }
    for name, declaration in overrides.items():
        declarations[A1Capability[name]] = declaration
    return BackendCapabilityProfile("test-backend", declarations)


def _declare(name: str, evidence: CapabilityEvidence, ref: str | None = None):
    return CapabilityDeclaration(A1Capability[name], evidence, ref)


# T1 -------------------------------------------------------------------------


def test_capability_model_covers_required_set_and_dormant_declares_unsupported():
    assert {c.name for c in A1Capability} == {
        "ATTEMPT_IDENTITY_BINDING",
        "PROCESS_ISOLATION",
        "DESCENDANT_CONTAINMENT",
        "FILESYSTEM_ISOLATION",
        "NETWORK_ISOLATION",
        "POSITIVE_LOCAL_PROCESS_CLOSURE",
        "PROVIDER_REQUEST_IDENTITY",
        "POSITIVE_PROVIDER_CLOSURE",
        "DURABLE_CLOSURE_EVIDENCE",
        "RECOVERY_RECONCILIATION",
    }
    assert {e.name for e in CapabilityEvidence} == {
        "SUPPORTED_BY_DESIGN",
        "IMPLEMENTED",
        "VERIFIED",
        "UNSUPPORTED",
        "UNKNOWN",
    }
    profile = DormantExecutionBackend(ATTEMPT).capability_profile()
    assert all(
        profile.declaration_for(c).evidence is CapabilityEvidence.UNSUPPORTED
        for c in REQUIRED_A1_CAPABILITIES
    )
    with pytest.raises(TypeError):
        profile.declarations[A1Capability.PROCESS_ISOLATION] = None  # type: ignore[index]


# T2 / T3 / T4 ---------------------------------------------------------------


def test_missing_capability_is_unknown_and_blocks():
    profile = BackendCapabilityProfile.from_declarations(
        "partial",
        _declare(
            "PROCESS_ISOLATION", CapabilityEvidence.VERIFIED, "evidence-process-0001"
        ),
    )
    decision = evaluate_capability_eligibility(profile)
    assert not decision.capability_requirements_met
    assert (A1Capability.ATTEMPT_IDENTITY_BINDING, "unknown") in decision.blocking


def test_explicit_unknown_blocks():
    decision = evaluate_capability_eligibility(
        _profile(
            NETWORK_ISOLATION=_declare("NETWORK_ISOLATION", CapabilityEvidence.UNKNOWN)
        )
    )
    assert decision.blocking == ((A1Capability.NETWORK_ISOLATION, "unknown"),)


def test_unsupported_blocks():
    decision = evaluate_capability_eligibility(
        _profile(
            POSITIVE_PROVIDER_CLOSURE=_declare(
                "POSITIVE_PROVIDER_CLOSURE", CapabilityEvidence.UNSUPPORTED
            )
        )
    )
    assert decision.blocking == (
        (A1Capability.POSITIVE_PROVIDER_CLOSURE, "unsupported"),
    )


@pytest.mark.parametrize(
    "evidence", [CapabilityEvidence.SUPPORTED_BY_DESIGN, CapabilityEvidence.IMPLEMENTED]
)
def test_design_or_implemented_without_verification_blocks(evidence):
    decision = evaluate_capability_eligibility(
        _profile(
            DESCENDANT_CONTAINMENT=_declare(
                "DESCENDANT_CONTAINMENT", evidence, "evidence-design-000001"
            )
        )
    )
    assert decision.blocking == ((A1Capability.DESCENDANT_CONTAINMENT, evidence.value),)


def test_verified_without_evidence_reference_blocks():
    decision = evaluate_capability_eligibility(
        _profile(
            DURABLE_CLOSURE_EVIDENCE=_declare(
                "DURABLE_CLOSURE_EVIDENCE", CapabilityEvidence.VERIFIED
            )
        )
    )
    assert decision.blocking == (
        (A1Capability.DURABLE_CLOSURE_EVIDENCE, "verified_without_evidence_ref"),
    )


def test_all_verified_still_never_permits_runtime_admission():
    decision = evaluate_capability_eligibility(_profile())
    assert decision.capability_requirements_met
    assert decision.runtime_admission_permitted is False
    authorization = decide_start_authorization(ATTEMPT, _profile())
    assert authorization.permitted is False
    assert authorization.reasons == ("runtime_admission_disabled",)


# T5 -------------------------------------------------------------------------


@pytest.mark.parametrize("wrong", [OTHER_ATTEMPT, STALE_GENERATION])
def test_attempt_identity_mismatch_rejected_by_backend_and_evaluator(wrong):
    backend = DormantExecutionBackend(ATTEMPT)
    for call in (
        backend.prepare(wrong),
        backend.inspect(wrong),
        backend.request_termination(wrong),
        backend.verify_closure(wrong),
        backend.closure_evidence(wrong),
    ):
        with pytest.raises(AttemptIdentityMismatch):
            asyncio.run(call)
    foreign = ClosureObservation(
        wrong,
        ClosureDomain.LOCAL_PROCESS,
        ClosureSignal.EMPTY_SCOPE_OBSERVED,
        "evidence-foreign-0001",
    )
    with pytest.raises(AttemptIdentityMismatch):
        evaluate_attempt_closure(ATTEMPT, [foreign, PROVIDER_DONE], DURABLE)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "attempt_id": "../etc/passwd-000000",
            "dispatch_id": "dispatch-00000000000001",
            "claim_generation": 1,
        },
        {
            "attempt_id": "short",
            "dispatch_id": "dispatch-00000000000001",
            "claim_generation": 1,
        },
        {
            "attempt_id": "attempt-0000000000000001",
            "dispatch_id": "dispatch-00000000000001",
            "claim_generation": 0,
        },
        {
            "attempt_id": "attempt-0000000000000001",
            "dispatch_id": "dispatch-00000000000001",
            "claim_generation": True,
        },
    ],
)
def test_identity_rejects_non_opaque_or_invalid_values(kwargs):
    with pytest.raises(ValueError):
        A1AttemptIdentity(**kwargs)


# T6 / T7 / T8 ---------------------------------------------------------------


@pytest.fixture
def no_side_effects(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden side effect")

    for target, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (os, "fork"),
        (os, "system"),
        (os, "execv"),
        (os, "execvp"),
        (os, "posix_spawn"),
        (os, "kill"),
        (os, "killpg"),
        (os, "open"),
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (builtins, "open"),
    ):
        if hasattr(target, name):
            monkeypatch.setattr(target, name, _forbidden)


def test_dormant_backend_refuses_start_and_has_no_side_effects(no_side_effects):
    backend = DormantExecutionBackend(ATTEMPT)
    authorization = decide_start_authorization(ATTEMPT, backend.capability_profile())

    async def exercise():
        prepared = await backend.prepare(ATTEMPT)
        with pytest.raises(StartRefused) as refused:
            await backend.start(ATTEMPT, authorization)
        status = await backend.inspect(ATTEMPT)
        termination = await backend.request_termination(ATTEMPT)
        verdict = await backend.verify_closure(ATTEMPT)
        evidence = await backend.closure_evidence(ATTEMPT)
        return prepared, refused.value, status, termination, verdict, evidence

    prepared, refused, status, termination, verdict, evidence = asyncio.run(exercise())
    assert prepared.status is PreparationStatus.NOT_AUTHORIZED
    assert "dormant_backend_never_executes" in refused.reasons
    assert "runtime_admission_disabled" in refused.reasons
    assert status.status is ExecutionStatus.NOT_STARTED
    assert termination.status is TerminationRequestStatus.NOTHING_TO_TERMINATE
    assert verdict.overall is ClosureStatus.UNKNOWN
    assert evidence is None


def test_permitting_start_authorization_cannot_be_constructed():
    with pytest.raises(ValueError):
        StartAuthorization(ATTEMPT, "any-backend", permitted=True)


# T9 – T15 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,signal",
    [
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.PARENT_EXIT),
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.CLI_EXIT),
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.PROCESS_GROUP_KILLED),
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.CONTAINER_EXIT),
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.TIMEOUT),
        (ClosureDomain.LOCAL_PROCESS, ClosureSignal.HEARTBEAT_MISSING),
        (ClosureDomain.PROVIDER_SESSION, ClosureSignal.CANCELLATION_REQUESTED),
        (ClosureDomain.PROVIDER_SESSION, ClosureSignal.HTTP_DISCONNECT),
        (ClosureDomain.PROVIDER_SESSION, ClosureSignal.SESSION_NOT_FOUND),
        (ClosureDomain.PROVIDER_SESSION, ClosureSignal.BACKEND_DECLARATION),
    ],
)
def test_insufficient_signals_never_establish_closure(domain, signal):
    weak = ClosureObservation(ATTEMPT, domain, signal, "evidence-weak-0000001")
    others = [o for o in (LOCAL_EMPTY, PROVIDER_DONE) if o.domain is not domain]
    verdict = evaluate_attempt_closure(ATTEMPT, [weak, *others], DURABLE)
    assert verdict.status_for(domain) is ClosureStatus.UNKNOWN
    assert verdict.overall is ClosureStatus.UNKNOWN


def test_unknown_provider_status_blocks_overall_closure():
    verdict = evaluate_attempt_closure(ATTEMPT, [LOCAL_EMPTY], DURABLE)
    assert (
        verdict.status_for(ClosureDomain.LOCAL_PROCESS)
        is ClosureStatus.POSITIVELY_VERIFIED
    )
    assert verdict.status_for(ClosureDomain.PROVIDER_SESSION) is ClosureStatus.UNKNOWN
    assert verdict.overall is ClosureStatus.UNKNOWN


def test_provider_closure_alone_is_insufficient():
    verdict = evaluate_attempt_closure(ATTEMPT, [PROVIDER_DONE], DURABLE)
    assert verdict.overall is ClosureStatus.UNKNOWN


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        ClosureEvidence(
            ATTEMPT, ClosureEvidenceDurability.NOT_DURABLE, "evidence-manifest-0001"
        ),
        ClosureEvidence(
            ATTEMPT, ClosureEvidenceDurability.UNKNOWN, "evidence-manifest-0001"
        ),
        ClosureEvidence(ATTEMPT, ClosureEvidenceDurability.DURABLE_VERIFIED),
    ],
)
def test_missing_or_non_durable_evidence_blocks_closure(evidence):
    verdict = evaluate_attempt_closure(ATTEMPT, [LOCAL_EMPTY, PROVIDER_DONE], evidence)
    assert verdict.overall is ClosureStatus.UNKNOWN
    assert "closure_evidence_not_durable" in verdict.reasons


def test_positive_signal_without_evidence_reference_is_unknown():
    unreferenced = ClosureObservation(
        ATTEMPT, ClosureDomain.LOCAL_PROCESS, ClosureSignal.EMPTY_SCOPE_OBSERVED
    )
    verdict = evaluate_attempt_closure(ATTEMPT, [unreferenced, PROVIDER_DONE], DURABLE)
    assert verdict.status_for(ClosureDomain.LOCAL_PROCESS) is ClosureStatus.UNKNOWN


@pytest.mark.parametrize(
    "signal", [ClosureSignal.CLEANUP_FAILED, ClosureSignal.STILL_RUNNING]
)
def test_cleanup_failure_or_running_is_not_closed_even_with_positive_signals(signal):
    failure = ClosureObservation(
        ATTEMPT, ClosureDomain.LOCAL_PROCESS, signal, "evidence-failure-00001"
    )
    verdict = evaluate_attempt_closure(
        ATTEMPT, [LOCAL_EMPTY, failure, PROVIDER_DONE], DURABLE
    )
    assert verdict.status_for(ClosureDomain.LOCAL_PROCESS) is ClosureStatus.NOT_CLOSED
    assert verdict.overall is ClosureStatus.NOT_CLOSED


def test_all_predicates_with_durable_evidence_verify_closure():
    backend = DormantExecutionBackend(
        ATTEMPT,
        synthetic_observations=[LOCAL_EMPTY, PROVIDER_DONE],
        synthetic_evidence=DURABLE,
    )
    verdict = asyncio.run(backend.verify_closure(ATTEMPT))
    assert verdict.overall is ClosureStatus.POSITIVELY_VERIFIED
    assert verdict.reasons == ()


# T16 ------------------------------------------------------------------------


def test_eligibility_and_closure_are_deterministic_and_order_independent():
    mixed = _profile(
        NETWORK_ISOLATION=_declare("NETWORK_ISOLATION", CapabilityEvidence.UNKNOWN),
        PROCESS_ISOLATION=_declare("PROCESS_ISOLATION", CapabilityEvidence.UNSUPPORTED),
    )
    reordered = BackendCapabilityProfile(
        "test-backend", dict(reversed(list(mixed.declarations.items())))
    )
    first = evaluate_capability_eligibility(mixed)
    assert first == evaluate_capability_eligibility(mixed)
    assert first == evaluate_capability_eligibility(reordered)
    assert [c for c, _ in first.blocking] == [
        A1Capability.PROCESS_ISOLATION,
        A1Capability.NETWORK_ISOLATION,
    ]
    forward = evaluate_attempt_closure(ATTEMPT, [LOCAL_EMPTY, PROVIDER_DONE], DURABLE)
    backward = evaluate_attempt_closure(ATTEMPT, [PROVIDER_DONE, LOCAL_EMPTY], DURABLE)
    assert forward == backward


# T17 / T18 ------------------------------------------------------------------

ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "dataclasses",
    "enum",
    "re",
    "types",
    "typing",
    "app",
}
FORBIDDEN_TOKENS = (
    "aarch64",
    "arm64",
    "x86",
    "amd64",
    "nvidia",
    "cuda",
    "vllm",
    "llama",
    "ollama",
    "systemd",
    "cgroup",
    "docker",
    "gx10",
    "ai-gateway",
    "host.docker",
    "/proc",
    "/sys/",
    "openclaw",
    "subprocess",
)


def _package_sources() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8") for p in sorted(PACKAGE_DIR.glob("*.py"))
    }


def test_core_contract_has_no_platform_provider_or_process_dependencies():
    sources = _package_sources()
    assert set(sources) == {
        "__init__.py",
        "backend.py",
        "capabilities.py",
        "closure.py",
        "dormant.py",
        "identity.py",
    }
    for name, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
                if node.module and node.module.startswith("app."):
                    assert node.module.startswith("app.services.a1_execution"), name
            else:
                continue
            assert roots <= ALLOWED_IMPORT_ROOTS, (name, roots)
        lowered = source.lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in lowered, (name, token)


def test_no_production_module_references_the_a1_package():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR)
        if relative.parts[0] == "tests" or path.is_relative_to(PACKAGE_DIR):
            continue
        if "a1_execution" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(relative))
    assert offenders == []


def test_protocol_exposes_no_command_or_shell_surface():
    names = {name for name in dir(DormantExecutionBackend) if not name.startswith("_")}
    assert names == {
        "attempt",
        "backend_id",
        "capability_profile",
        "closure_evidence",
        "inspect",
        "prepare",
        "request_termination",
        "start",
        "verify_closure",
    }


def test_side_effect_guard_is_armed(no_side_effects):
    with pytest.raises(AssertionError):
        subprocess.run(["true"])
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
