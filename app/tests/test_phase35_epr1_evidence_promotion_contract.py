"""PHASE35-EPR1 — evidence promotion contract proofs.

Provider-free proof that ``search_text`` is candidate/navigation evidence and
that only substantive ``inspect_file`` / positive ``resolve_structure``
evidence may satisfy SUFFICIENT and reach Planning.

These tests pin the two defects PHASE35-EPC1 reproduced:

D1  a cited search observation projected its rendered multi-file hit block once
    per cited path, so five paths produced five records of the same block and
    the materialization arrived over the total source-byte bound holding no
    file's real content;

D2  because ``inspect_file`` carries no structural span, the handoff's
    span-preference de-duplication never let an inspection replace an earlier
    search record for the same path, so navigation text displaced real source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingHandoffError,
    GroundingLifecycleState,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    build_grounding_planning_context,
)
from app.services.orchestration.planning.grounding.contracts import (
    GroundingBudgetLimits,
    is_substantive_observation,
    substantive_evidence_paths,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    MAX_SOURCE_CONTENT_TOTAL_CHARS,
)


HELPER_SOURCE = "def helper():\n" "    # needle lives here\n" "    return 'needle'\n"


def _repo(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


def _wide_repo(root: Path, count: int = 5) -> Path:
    """Five files that all match one query: the ODF1 D1 fan-out shape."""

    return _repo(root, {f"app/mod{index}.py": HELPER_SOURCE for index in range(count)})


@dataclass
class _Provider:
    responses: list
    calls: int = 0
    contexts: list = field(default_factory=list)

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


def _run(root: Path, responses, *, max_exploration: int = 3):
    provider = _Provider(list(responses))
    config = GroundingRunConfig(
        grounding_run_id="epr1-run",
        task_reference=GroundingTaskReference(task_id="epr1-task"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="epr1-snapshot",
        max_steps=12,
        max_exploration_provider_requests=max_exploration,
        operator_task="Find the implementation without changing the repository.",
        budget_limits=GroundingBudgetLimits(
            repository_actions=3,
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="epr1-snapshot"),
        provider=provider,
        config=config,
    ).run()
    return result, provider


SEARCH = {"action": "search_text", "query": "needle", "scopes": ["app"]}
INSPECT = {"action": "inspect_file", "path": "app/mod0.py"}
RESOLVE = {
    "action": "resolve_structure",
    "relation": "symbol_definition",
    "locator": {"path": "app/mod0.py", "name": "helper"},
}


def _need(action):
    return lambda _context: {
        "decision": "NEED_MORE_EVIDENCE",
        "next_action": action,
        "rationale": "One bounded refinement is required.",
    }


def _sufficient_last(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited bounded observation grounds the operator task.",
    }


def _sufficient_all(context):
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [
            item.observation_id
            for item in context.state.observation_history
            if item.outcome.value == "FOUND"
        ],
        "rationale": "The cited bounded observations ground the operator task.",
    }


# T1 -------------------------------------------------------------------------
def test_t1_search_only_sufficient_is_rejected(tmp_path):
    root = _wide_repo(tmp_path)
    result, provider = _run(root, [SEARCH, _sufficient_last])

    assert provider.calls == 2
    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    # The observation itself stays FOUND and recorded; only the promotion of it
    # to terminal sufficiency is refused.
    assert result.observations[0].outcome.value == "FOUND"
    assert len(result.observations[0].source_paths) == 5


def test_t1b_rejection_uses_the_semantic_sufficiency_code(tmp_path):
    root = _wide_repo(tmp_path)
    events: list[dict] = []
    provider = _Provider([SEARCH, _sufficient_last])
    config = GroundingRunConfig(
        grounding_run_id="epr1-code-run",
        task_reference=GroundingTaskReference(task_id="epr1-task"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="epr1-snapshot",
        max_steps=12,
        max_exploration_provider_requests=3,
        operator_task="Find the implementation.",
    )
    GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="epr1-snapshot"),
        provider=provider,
        config=config,
        event_sink=lambda event_type, details: events.append(dict(details))
        or {"event_id": "e"},
    ).run()

    matching = [
        item
        for item in events
        if item.get("failure_classification") == "insufficient_substantive_evidence"
    ]
    assert matching, "the semantic sufficiency rejection was not emitted"
    # It is a semantic contract rejection, not a provider wire failure.
    assert {item["failure_layer"] for item in matching} == {
        "L7_SEMANTIC_PROTOCOL_STATE"
    }
    assert {item["parser_rejection_code"] for item in matching} == {
        "insufficient_substantive_evidence"
    }
    assert all(item["parser_success"] is True for item in matching)


# T2 / T3 --------------------------------------------------------------------
def test_t2_search_then_inspect_sufficient_is_accepted(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_last])

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    context = build_grounding_planning_context(result, project_dir=root)
    assert [item.relative_path for item in context.source_materialization.files] == [
        "app/mod0.py"
    ]


def test_t3_search_plus_inspect_both_cited_materializes_inspect_content(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    search, inspection = result.observations
    assert search.action_identity == "search_text"
    assert len(search.source_paths) == 5
    assert len(result.cited_observation_ids) == 2

    context = build_grounding_planning_context(result, project_dir=root)

    files = context.source_materialization.files
    assert [item.relative_path for item in files] == ["app/mod0.py"]
    record = files[0]
    assert record.content == HELPER_SOURCE
    # The displaced-content defect: no foreign path may appear in the record.
    for index in range(1, 5):
        assert f"app/mod{index}.py" not in (record.content or "")
    assert all(
        item.observation_id == inspection.observation_id
        for item in context.cited_source_evidence
    )


# T4 -------------------------------------------------------------------------
def test_t4_search_plus_structure_both_cited_materializes_structure_region(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(RESOLVE), _sufficient_all])

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    context = build_grounding_planning_context(result, project_dir=root)

    files = context.source_materialization.files
    assert [item.relative_path for item in files] == ["app/mod0.py"]
    record = files[0]
    assert record.selection_strategy == "grounding_structural_region"
    assert record.start_line and record.end_line
    assert "def helper" in (record.content or "")
    for index in range(1, 5):
        assert f"app/mod{index}.py" not in (record.content or "")
    evidence = context.cited_source_evidence
    assert [item.source_path for item in evidence] == ["app/mod0.py"]
    assert evidence[0].structural_identity is not None


# T5 / T10 / T16 -------------------------------------------------------------
def test_t5_multi_path_search_never_duplicates_source_materialization(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    context = build_grounding_planning_context(result, project_dir=root)

    materialization = context.source_materialization
    assert len(materialization.files) == 1
    assert materialization.materialized_source_bytes == len(
        HELPER_SOURCE.encode("utf-8")
    )
    # Exactly one record per path, and no path repeated.
    paths = [item.relative_path for item in materialization.files]
    assert len(paths) == len(set(paths))


def test_t10_total_source_byte_bound_is_unchanged(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    context = build_grounding_planning_context(result, project_dir=root)

    assert MAX_SOURCE_CONTENT_TOTAL_CHARS == 5000
    materialization = context.source_materialization
    assert materialization.maximum_total_source_bytes == 5000
    assert materialization.materialized_source_bytes <= 5000


# T6 / T7 --------------------------------------------------------------------
def test_t6_search_only_path_is_absent_from_the_semantic_target_inventory(tmp_path):
    """A search-only path cannot reach the inventory, because it never
    materializes and the inventory reads nothing else."""

    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    context = build_grounding_planning_context(result, project_dir=root)
    materialization = context.source_materialization

    materialized = {item.relative_path for item in materialization.files}
    for index in range(1, 5):
        assert f"app/mod{index}.py" not in materialized

    # Explicit scope is the widest consideration the inventory offers, and it
    # still cannot reach a path that was never materialized.
    inventory = build_semantic_target_inventory(
        materialization,
        additional_candidate_paths=[f"app/mod{index}.py" for index in range(5)],
    )
    paths = {handle.path for handle in inventory.handles}
    for index in range(1, 5):
        assert f"app/mod{index}.py" not in paths
        assert f"app/mod{index}.py" not in inventory.eligible_existing_mutable_paths


def test_t7_substantive_path_is_the_only_inventory_candidate(tmp_path):
    """Materialization is necessary, not sufficient, for a target handle.

    Grounding records carry no target hint, so they mint no handles on their
    own.  What EPR1 guarantees is the necessary direction: only the
    substantively inspected path is present at all, so only it could ever be
    considered downstream.
    """

    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    context = build_grounding_planning_context(result, project_dir=root)
    materialization = context.source_materialization

    assert [item.relative_path for item in materialization.files] == ["app/mod0.py"]
    record = materialization.files[0]
    assert record.target_hint is None
    assert record.target_match_count == 0
    assert record.target_included is False
    # Hint-free grounding records produce no handles, for any path.
    inventory = build_semantic_target_inventory(
        materialization, additional_candidate_paths=["app/mod0.py"]
    )
    assert inventory.handles == ()


# T8 -------------------------------------------------------------------------
def test_t8_plv1_search_then_inspect_then_sufficient_still_passes(tmp_path):
    """The exact PLV1 shape that succeeded live: search, inspect, SUFFICIENT."""

    root = _repo(tmp_path, {"app/sample.py": HELPER_SOURCE})
    result, provider = _run(
        root,
        [
            SEARCH,
            _need({"action": "inspect_file", "path": "app/sample.py"}),
            _sufficient_last,
        ],
    )

    assert provider.calls == 3
    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    context = build_grounding_planning_context(result, project_dir=root)
    assert [item.relative_path for item in context.source_materialization.files] == [
        "app/sample.py"
    ]
    assert context.source_materialization.files[0].content == HELPER_SOURCE


# T9 -------------------------------------------------------------------------
def test_t9_sear1_search_accounting_is_unchanged(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])

    search, inspection = result.observations
    assert search.budget_delta.repository_actions == 1
    assert search.budget_delta.distinct_files == 0
    assert search.budget_delta.positive_regions == 0
    assert search.budget_delta.source_evidence_bytes == len(search.bounded_content)
    assert search.budget_delta.source_evidence_bytes > 0
    # Substantive accounting keeps charging files and regions.
    assert inspection.budget_delta.distinct_files == 1
    assert inspection.budget_delta.positive_regions == 1


# T11 ------------------------------------------------------------------------
def test_t11_stale_substantive_citation_still_fails_closed(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT

    (root / "app/mod0.py").write_text("changed = True\n", encoding="utf-8")

    with pytest.raises(GroundingHandoffError) as excinfo:
        build_grounding_planning_context(result, project_dir=root)
    assert excinfo.value.code == "stale_citation"


# T12 ------------------------------------------------------------------------
def test_t12_not_found_inspect_cannot_satisfy_the_substantive_gate(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(
        root,
        [
            SEARCH,
            _need({"action": "inspect_file", "path": "app/absent.py"}),
            _sufficient_all,
        ],
    )

    missing = result.observations[1]
    assert missing.action_identity == "inspect_file"
    assert missing.outcome.value == "NOT_FOUND"
    assert not is_substantive_observation(missing)
    # A NOT_FOUND inspection is not substantive, so the citation set is
    # search-only in substance and the run fails closed.
    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT


def test_t12b_not_found_search_is_never_substantive(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(
        root,
        [
            {"action": "search_text", "query": "absent_token", "scopes": ["app"]},
            _need(INSPECT),
            _sufficient_all,
        ],
    )
    empty_search = result.observations[0]
    assert empty_search.outcome.value == "NOT_FOUND"
    assert not is_substantive_observation(empty_search)
    assert substantive_evidence_paths(empty_search) == ()


# T13 ------------------------------------------------------------------------
def test_t13_terminal_assessment_search_only_sufficient_is_rejected(tmp_path):
    """The last turn of the run is bound by the same contract."""

    root = _wide_repo(tmp_path)
    result, provider = _run(
        root,
        [
            SEARCH,
            _need({"action": "search_text", "query": "helper", "scopes": ["app"]}),
            _sufficient_all,
        ],
        max_exploration=2,
    )

    assert provider.contexts[-1].turn_mode.name == "TERMINAL_ASSESSMENT"
    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST


# T14 ------------------------------------------------------------------------
def test_t14_insufficient_remains_legal(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(
        root,
        [SEARCH, lambda _c: {"decision": "INSUFFICIENT", "reason": "no candidate"}],
    )

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INSUFFICIENT_GROUNDING


# T15 ------------------------------------------------------------------------
def test_t15_repair_performs_no_repository_mutation(tmp_path):
    root = _wide_repo(tmp_path)
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    build_grounding_planning_context(result, project_dir=root)
    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert before == after


# T16 ------------------------------------------------------------------------
def test_t16_search_only_citation_cannot_widen_accepted_source_scope(tmp_path):
    """A path seen only by search may not reach Planning source scope.

    The citation set is forged directly so the handoff fence is proven on its
    own, independently of the coordinator gate in front of it.
    """

    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(INSPECT), _sufficient_all])
    search = result.observations[0]

    forged = replace(
        result,
        cited_observation_ids=(search.observation_id,),
        cited_source_paths=search.source_paths,
    )

    with pytest.raises(GroundingHandoffError) as excinfo:
        build_grounding_planning_context(forged, project_dir=root)
    assert excinfo.value.code == "insufficient_substantive_evidence"


# Predicate unit proofs ------------------------------------------------------
def test_predicate_classifies_the_three_actions(tmp_path):
    root = _wide_repo(tmp_path)
    result, _provider = _run(root, [SEARCH, _need(RESOLVE), _sufficient_all])
    search, structure = result.observations

    assert not is_substantive_observation(search)
    assert substantive_evidence_paths(search) == ()
    assert is_substantive_observation(structure)
    # A structural observation owns content for exactly the identified file,
    # never for every document its resolution happened to fence.
    assert substantive_evidence_paths(structure) == (
        structure.structural_identity.source_path,
    )
