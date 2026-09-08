"""Provider-free PHASE35-GSC1 search-scope contract tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.planning.grounding import (
    GroundingBudgetLimits,
    GroundingExecutor,
    GroundingOutcome,
    GroundingRequestRejection,
    GroundingRequest,
    parse_grounding_request,
)


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


def _large_app_repo(root: Path, *, matching: bool = True) -> Path:
    files = {
        f"app/modules/module_{index:04d}.py": (
            "needle = True\n" if matching and index % 2 == 0 else "value = True\n"
        )
        for index in range(520)
    }
    return _git_repo(root, files)


def _request(
    payload: dict[str, object], *, request_id: str = "gsc1-request"
) -> GroundingRequest:
    return parse_grounding_request(
        payload,
        grounding_run_id="gsc1-run",
        request_id=request_id,
    )


def test_directory_scope_over_512_tracked_files_executes(tmp_path):
    root = _large_app_repo(tmp_path)

    observation = GroundingExecutor(root, snapshot_identity="gsc1-snapshot").execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
    )

    assert observation.outcome is GroundingOutcome.FOUND
    assert observation.result_count <= 20


def test_large_scope_matching_term_is_found_and_negative_is_truthful(tmp_path):
    root = _large_app_repo(tmp_path)
    executor = GroundingExecutor(root)

    found = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            request_id="found",
        )
    )
    negative = executor.execute(
        _request(
            {
                "action": "search_text",
                "query": "does_not_exist",
                "scopes": ["app"],
            },
            request_id="negative",
        )
    )

    assert found.outcome is GroundingOutcome.FOUND
    assert negative.outcome is GroundingOutcome.NOT_FOUND
    assert negative.hits == ()
    assert negative.bounded_content == b""


def test_large_scope_hit_and_observation_bounds_are_global(tmp_path):
    root = _large_app_repo(tmp_path)

    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
    )

    assert len(observation.hits) <= 20
    assert observation.result_count <= 20
    assert len(observation.bounded_content) <= 8192
    assert all(len(hit.snippet) <= 240 for hit in observation.hits)


def test_large_scope_order_is_deterministic_and_source_identity_is_retained(tmp_path):
    root = _large_app_repo(tmp_path)
    executor = GroundingExecutor(root, snapshot_identity="gsc1-snapshot")

    first = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            request_id="first",
        )
    )
    second = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            request_id="second",
        )
    )

    first_hits = [(hit.path, hit.line_number, hit.snippet) for hit in first.hits]
    second_hits = [(hit.path, hit.line_number, hit.snippet) for hit in second.hits]
    assert first_hits == second_hits
    assert first_hits == sorted(
        first_hits, key=lambda item: (item[0], item[1], item[2])
    )
    assert first.source_versions
    assert set(first.source_versions) == set(first.source_hashes)
    assert all(first.source_versions[path] for path in first.source_paths)


def test_broad_scope_ignores_untracked_file_but_explicit_untracked_is_rejected(
    tmp_path,
):
    root = _large_app_repo(tmp_path)
    untracked = root / "app/untracked.py"
    untracked.write_text("needle = 'secret'\n", encoding="utf-8")
    executor = GroundingExecutor(root)

    broad = executor.execute(
        _request({"action": "search_text", "query": "secret", "scopes": ["app"]})
    )
    assert broad.outcome is GroundingOutcome.NOT_FOUND
    assert "app/untracked.py" not in broad.source_paths

    with pytest.raises(GroundingRequestRejection) as exc_info:
        executor.execute(
            _request(
                {
                    "action": "search_text",
                    "query": "secret",
                    "scopes": ["app/untracked.py"],
                }
            )
        )
    assert exc_info.value.code == "untracked_path"


@pytest.mark.parametrize(
    "path", ["../outside.py", "/tmp/outside.py", "app/../outside.py"]
)
def test_search_scope_traversal_and_outside_paths_are_rejected(tmp_path, path):
    root = _git_repo(tmp_path, {"app/main.py": "needle = True\n"})

    with pytest.raises(GroundingRequestRejection):
        GroundingExecutor(root).execute(
            _request({"action": "search_text", "query": "needle", "scopes": [path]})
        )


def test_symlink_escape_and_special_scope_are_rejected(tmp_path):
    root = _git_repo(tmp_path, {"app/main.py": "needle = True\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("needle = 'outside'\n", encoding="utf-8")
    symlink = root / "app/link.py"
    symlink.symlink_to(outside)
    fifo = root / "app/pipe"
    os.mkfifo(fifo)

    for path in ("app/link.py", "app/pipe"):
        with pytest.raises(GroundingRequestRejection):
            GroundingExecutor(root).execute(
                _request(
                    {"action": "search_text", "query": "needle", "scopes": [path]},
                    request_id=path,
                )
            )


def test_root_scope_is_explicitly_not_supported_and_named_scope_works(tmp_path):
    root = _git_repo(tmp_path, {"app/main.py": "needle = True\n"})

    with pytest.raises(GroundingRequestRejection) as exc_info:
        _request({"action": "search_text", "query": "needle", "scopes": ["."]})
    assert exc_info.value.code == "invalid_request"
    assert "path_traversal_segment" in str(exc_info.value)

    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
    )
    assert observation.outcome is GroundingOutcome.FOUND


def test_narrow_file_and_multiple_legal_scopes_remain_supported(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "app/one.py": "needle = 1\n",
            "app/two.py": "needle = 2\n",
            "tests/test_two.py": "needle = 3\n",
        },
    )
    executor = GroundingExecutor(root)

    narrow = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app/one.py"]},
            request_id="narrow",
        )
    )
    multiple = executor.execute(
        _request(
            {
                "action": "search_text",
                "query": "needle",
                "scopes": ["app", "tests"],
            },
            request_id="multiple",
        )
    )

    assert narrow.source_paths == ("app/one.py",)
    assert multiple.source_paths == ("app/one.py", "app/two.py", "tests/test_two.py")


def test_broad_search_preserves_budgets_and_does_not_mutate_repository(tmp_path):
    root = _large_app_repo(tmp_path)
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]}),
        limits=GroundingBudgetLimits(
            repository_actions=1,
            source_evidence_bytes=8192,
            distinct_files=20,
            positive_regions=20,
        ),
    )

    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert observation.budget_delta.repository_actions == 1
    assert observation.budget_delta.source_evidence_bytes <= 8192
    assert observation.budget_delta.distinct_files <= 20
    assert observation.budget_delta.positive_regions <= 20
    assert before == after


@pytest.mark.parametrize("query", ["project", "sign-in", "sign_in"])
def test_frozen_pgv3_search_candidates_reach_observation_on_current_repository(query):
    root = Path(__file__).parents[1]
    request = _request(
        {"action": "search_text", "query": query, "scopes": ["app"]},
        request_id=f"pgv3-{query}",
    )

    observation = GroundingExecutor(root).execute(request)

    assert observation.outcome in {GroundingOutcome.FOUND, GroundingOutcome.NOT_FOUND}
    assert observation.outcome is not None
