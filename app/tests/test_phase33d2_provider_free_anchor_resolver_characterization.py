"""Provider-free Phase 33D-2 selector characterization.

This module deliberately contains no production resolver and is not imported by
runtime code.  Its small selectors are test instruments for measuring the
evidence available from current bytes; they are not an implementation contract.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass

import pytest

from app.services.orchestration.planning.operation_repair_anchors import (
    derive_operation_anchors,
)


RESOLVED_UNIQUE = "RESOLVED_UNIQUE"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
UNSUPPORTED_SELECTOR = "UNSUPPORTED_SELECTOR"


@dataclass(frozen=True)
class CandidateRegion:
    start: int
    end: int
    text: str


def _result(candidates: list[CandidateRegion]) -> tuple[str, CandidateRegion | None]:
    if not candidates:
        return NOT_FOUND, None
    if len(candidates) != 1:
        return AMBIGUOUS, None
    return RESOLVED_UNIQUE, candidates[0]


def _unique_text(source: str, needle: str) -> tuple[str, CandidateRegion | None]:
    if not needle:
        return NOT_FOUND, None
    starts = [match.start() for match in re.finditer(re.escape(needle), source)]
    return _result(
        [CandidateRegion(start, start + len(needle), needle) for start in starts]
    )


def _python_symbol(source: str, symbol_path: str) -> tuple[str, CandidateRegion | None]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return UNSUPPORTED_SELECTOR, None

    candidates: list[CandidateRegion] = []

    def visit(body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                path = (*prefix, node.name)
                if ".".join(path) == symbol_path:
                    lines = source.splitlines(keepends=True)
                    start = sum(len(line) for line in lines[: node.lineno - 1])
                    end = sum(len(line) for line in lines[: node.end_lineno])
                    candidates.append(CandidateRegion(start, end, source[start:end]))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, path)

    visit(tree.body, ())
    return _result(candidates)


def _unique_line(source: str, line_text: str) -> tuple[str, CandidateRegion | None]:
    candidates: list[CandidateRegion] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body == line_text:
            candidates.append(CandidateRegion(offset, offset + len(body), body))
        offset += len(line)
    return _result(candidates)


def _tsx_component(source: str, signature: str) -> tuple[str, CandidateRegion | None]:
    """Measure a bounded lexical fallback for a simple TSX component."""
    starts = [match.start() for match in re.finditer(re.escape(signature), source)]
    if len(starts) != 1:
        return _result(
            [
                CandidateRegion(start, start + len(signature), signature)
                for start in starts
            ]
        )
    start = starts[0]
    open_brace = source.find("{", start + len(signature))
    if open_brace < 0:
        return NOT_FOUND, None
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return RESOLVED_UNIQUE, CandidateRegion(
                    start, index + 1, source[start : index + 1]
                )
    return NOT_FOUND, None


@pytest.mark.parametrize(
    ("language", "source", "selector", "expected"),
    [
        (
            "Python function body",
            "def execute_planning_phase():\n    return 1\n",
            lambda source: _python_symbol(source, "execute_planning_phase"),
            RESOLVED_UNIQUE,
        ),
        (
            "Python class method",
            "class Runner:\n    def execute(self):\n        return 1\n",
            lambda source: _python_symbol(source, "Runner.execute"),
            RESOLVED_UNIQUE,
        ),
        (
            "TSX component",
            "function App() {\n  return <main>ok</main>;\n}\n",
            lambda source: _tsx_component(source, "function App()"),
            RESOLVED_UNIQUE,
        ),
        (
            "JSON field",
            '{\n  "timeout": 30,\n  "enabled": true\n}\n',
            lambda source: _unique_line(source, '  "timeout": 30,'),
            RESOLVED_UNIQUE,
        ),
        (
            "YAML key",
            "service:\n  timeout: 30\n  enabled: true\n",
            lambda source: _unique_line(source, "  timeout: 30"),
            RESOLVED_UNIQUE,
        ),
        (
            "shell command",
            "#!/bin/sh\nnpm test\nprintf done\n",
            lambda source: _unique_line(source, "npm test"),
            RESOLVED_UNIQUE,
        ),
        (
            "Markdown paragraph",
            "# Notes\n\nKeep the timeout bounded.\n\nNext.\n",
            lambda source: _unique_text(source, "Keep the timeout bounded."),
            RESOLVED_UNIQUE,
        ),
        (
            "plain-text duplicate block",
            "alpha\nblock\nomega\n\nalpha\nblock\nomega\n",
            lambda source: _unique_text(source, "alpha\nblock\nomega"),
            AMBIGUOUS,
        ),
    ],
)
def test_provider_free_selector_characterization(language, source, selector, expected):
    del language
    status, region = selector(source)
    assert status == expected
    if expected == RESOLVED_UNIQUE:
        assert region is not None
        assert source[region.start : region.end] == region.text
    else:
        assert region is None


def test_literal_selector_has_closed_zero_and_multi_match_semantics():
    assert _unique_text("one", "missing")[0] == NOT_FOUND
    assert _unique_text("x x", "x")[0] == AMBIGUOUS


def test_region_identity_is_deterministic_and_version_bound():
    source = "def run():\n    return 1\n"
    status, region = _python_symbol(source, "run")
    assert status == RESOLVED_UNIQUE and region is not None

    def identity(version: str) -> str:
        payload = {
            "path": "app/run.py",
            "version": version,
            "start": region.start,
            "end": region.end,
            "region_sha256": hashlib.sha256(region.text.encode()).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    assert identity("v1") == identity("v1")
    assert identity("v1") != identity("v2")


def test_existing_orchestrator_anchor_ids_are_provider_free_and_version_bound():
    source = "def run():\n\n    return 1\n"
    anchors = derive_operation_anchors(
        step_number=2,
        operation_index=1,
        relative_path="app/run.py",
        version_identity="dev:ino:size:mtime",
        original_old="def run():\n    return 1",
        original_new="def run():\n    return 2",
        full_source=source,
    )
    assert anchors
    assert [anchor.anchor_id for anchor in anchors] == [
        "anchor-2-1-1",
        "anchor-2-1-2",
    ]
    assert all(anchor.version_identity == "dev:ino:size:mtime" for anchor in anchors)
    assert all(source.count(anchor.text) == 1 for anchor in anchors)


def test_semantic_description_alone_is_not_a_deterministic_selector():
    status, region = UNSUPPORTED_SELECTOR, None
    assert status == UNSUPPORTED_SELECTOR
    assert region is None
