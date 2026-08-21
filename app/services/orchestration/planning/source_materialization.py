"""Planning source materialization helpers."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import tokenize
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping

from app.services.orchestration.planning.planner_contract_registry import (
    planner_contract_source_paths,
    planner_contract_test_paths,
)
from app.services.orchestration.planning.repair_faithfulness import (
    extract_required_file_paths,
)

# These bounds are the existing completion-repair source-reader contract. The
# reader itself is imported lazily below to avoid importing the phases package
# while the planning package is initializing.
MAX_RELEVANT_FILES = 25
MAX_SOURCE_CONTENT_PER_FILE_CHARS = 2000
MAX_SOURCE_CONTENT_TOTAL_CHARS = 5000
_SOURCE_TRUNCATED_MARKER = "... [truncated]"


def _read_source_text(
    path: Path, relative_path: str, cache: dict[str, str]
) -> str | None:
    """Read one already workspace-validated file with the reader's decoding rules."""

    cached = cache.get(relative_path)
    if cached is not None:
        return cached
    try:
        # Keep CRLF/LF bytes stable: semantic target-match offsets are byte
        # coordinates consumed later by the UTF-8 region resolver.
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            text = handle.read()
    except OSError:
        return None
    cache[relative_path] = text
    return text


SOURCE_MATERIALIZATION_EXTENSIONS = ".py .js .jsx .ts .tsx .css .html .md".split()
IMPLEMENTATION_SOURCE_EXTENSIONS = ".py .js .jsx .ts .tsx .css .html".split()
SOURCE_MATERIALIZATION_REPAIR_MARKERS = (
    "missing_source_materialization",
    "does not materialize any source changes",
    "no source materialization",
    "plan does not materialize source changes",
    "contextual python control-flow fragments",
    "unsafe_python_append",
    "framework_mismatch",
    "decorators whose root name is undefined",
    "undefined decorator root",
    "undefined_python_test_names",
    "obvious undefined names",
    "placeholder_only_implementation",
    "placeholder or stub implementations",
)

SOURCE_STATUS_EXISTING = "existing_file_with_materialized_source"
SOURCE_STATUS_NEW = "new_file_authorized_for_creation"
SOURCE_STATUS_MISSING = "missing_expected_file"
SOURCE_STATUS_UNREADABLE = "unreadable_or_binary_file"
SOURCE_STATUS_OMITTED = "source_omitted_by_explicit_bound"

SELECTION_FULL_FILE = "full_file"
SELECTION_TARGET_EXACT = "target_centered_exact_match"
SELECTION_TARGET_SYMBOL = "target_centered_symbol_match"
SELECTION_HEAD_FALLBACK = "head_fallback_no_target"
SELECTION_OMITTED_TOTAL_BUDGET = "omitted_total_budget"
SELECTION_NEW_FILE = "new_file_no_source"
SELECTION_TARGET_WITH_STRUCTURAL_HEAD = "target_centered_with_structural_head"

SPAN_PRIMARY_TARGET = "primary_target_region"
SPAN_STRUCTURAL_HEAD = "structural_head_region"
SPAN_TARGET_MATCH = "target_match_region"

# A second span never adds budget: it is carved out of the same per-file
# allocation, and only enough of it to carry a module head/import region.
_STRUCTURAL_HEAD_BUDGET_BYTES = 600
_STRUCTURAL_HEAD_BUDGET_SHARE = 3
_STRUCTURAL_EDIT_REQUIREMENT_RE = re.compile(
    r"\bimports?\b|\bimported\b|\bimporting\b|\bmodule[-\s]level\b"
    r"|\btop[-\s]level\s+(?:declaration|definition|import)\b",
    re.IGNORECASE,
)

TARGET_HINT_MATCHED = "target_hint_matched"
TARGET_HINT_NOT_FOUND = "target_hint_not_found"
TARGET_HINT_ABSENT = "no_target_hint"

HINT_TYPE_EXACT_CALL = "exact_call"
HINT_TYPE_QUOTED_SNIPPET = "quoted_snippet"
HINT_TYPE_SYMBOL = "symbol"

_EXACT_HINT_TYPES = (HINT_TYPE_EXACT_CALL, HINT_TYPE_QUOTED_SNIPPET)

# Deterministic hint-type ranking used when several hints match one file.
_HINT_TYPE_RANK = {
    HINT_TYPE_EXACT_CALL: 0,
    HINT_TYPE_QUOTED_SNIPPET: 1,
    HINT_TYPE_SYMBOL: 2,
}

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}

_TRUNCATED_PREFIX_MARKER = _SOURCE_TRUNCATED_MARKER + "\n"
_TRUNCATED_SUFFIX_MARKER = "\n" + _SOURCE_TRUNCATED_MARKER

_CREATION_WORD_RE = re.compile(
    r"\b(add|author|create|generate|introduce|new|scaffold|write)\b|\bif\s+needed\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MaterializedSourceSpan:
    """One bounded, prompt-visible byte range of a materialized file."""

    kind: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    included_source_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MaterializedSourceFile:
    """One bounded, provenance-bearing file fact supplied to planning.

    ``start_byte``/``end_byte``/``start_line``/``end_line`` always describe the
    primary target-centred span.  ``spans`` is the complete authority when a
    structural head span is also visible.
    """

    relative_path: str
    workspace_identity: str
    content: str | None
    content_hash: str | None
    version_identity: str | None
    status: str
    truncated: bool
    source_length: int | None
    source_length_chars: int | None
    included_prompt_length: int
    expected: bool = False
    creation_authorized: bool = False
    omission_reason: str | None = None
    priority: str = "P3"
    selection_strategy: str | None = None
    full_source_bytes: int | None = None
    included_source_bytes: int = 0
    start_byte: int | None = None
    end_byte: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    truncated_before: bool = False
    truncated_after: bool = False
    target_hint: str | None = None
    target_hint_type: str | None = None
    target_hint_authority: str | None = None
    target_hint_status: str = TARGET_HINT_ABSENT
    target_match_count: int = 0
    target_match_start: int | None = None
    target_match_end: int | None = None
    target_region_eligibility_reason: str | None = None
    target_included: bool = False
    spans: tuple[MaterializedSourceSpan, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlannerSourceMaterialization:
    """Bounded source facts shared by first-pass planning, repair, and validation."""

    workspace_identity: str
    files: tuple[MaterializedSourceFile, ...] = field(default_factory=tuple)
    maximum_files: int = MAX_RELEVANT_FILES
    maximum_bytes_per_file: int = MAX_SOURCE_CONTENT_PER_FILE_CHARS
    maximum_total_source_bytes: int = MAX_SOURCE_CONTENT_TOTAL_CHARS
    materialized_source_bytes: int = 0
    unavailable_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return not self.unavailable_reasons

    def file_map(self) -> dict[str, MaterializedSourceFile]:
        return {item.relative_path: item for item in self.files}

    def to_metadata(self) -> dict[str, Any]:
        return {
            "workspace_identity": self.workspace_identity,
            "maximum_files": self.maximum_files,
            "maximum_bytes_per_file": self.maximum_bytes_per_file,
            "maximum_total_source_bytes": self.maximum_total_source_bytes,
            "materialized_source_bytes": self.materialized_source_bytes,
            "file_count": len(self.files),
            "expected_file_count": sum(1 for item in self.files if item.expected),
            "materialized_file_count": sum(
                1 for item in self.files if item.status == SOURCE_STATUS_EXISTING
            ),
            "target_materialized_file_count": sum(
                1 for item in self.files if item.target_included
            ),
            "unavailable_reasons": list(self.unavailable_reasons),
            "files": [
                {
                    key: value
                    for key, value in item.to_dict().items()
                    if key != "content"
                }
                for item in self.files
            ],
        }

    def to_prompt_block(self, *, provider_safe: bool = False) -> str:
        return render_planner_source_materialization(self, provider_safe=provider_safe)


def _safe_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        return ""
    normalized = path.as_posix().lstrip("./")
    return normalized if normalized and normalized != "." else ""


def _ordered_unique_paths(values: Iterable[Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _safe_relative_path(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        paths.append(normalized)
    return paths


def _unsafe_requested_paths(values: Iterable[Any]) -> list[str]:
    """Retain invalid requested paths long enough to report fail-closed evidence."""
    unsafe: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = str(value or "").strip().replace("\\", "/")
        if raw and not _safe_relative_path(raw) and raw not in seen:
            seen.add(raw)
            unsafe.append(raw)
    return unsafe


def _workspace_identity_text(project_dir: Path, workspace_identity: Any = None) -> str:
    if workspace_identity is not None:
        physical_root = getattr(workspace_identity, "physical_runtime_root", None)
        if physical_root:
            return str(Path(physical_root).resolve())
        if isinstance(workspace_identity, str) and workspace_identity.strip():
            return str(Path(workspace_identity).resolve())
    return str(Path(project_dir).resolve())


def current_source_version_identity(path: Path) -> str | None:
    """Return the existing workspace version identity without caching content."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return ":".join(
        str(value)
        for value in (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    )


def _binary_or_unreadable(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
        if b"\x00" in sample:
            return "binary"
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "binary_or_non_text"
    except OSError:
        return "unreadable"
    return None


@dataclass(frozen=True)
class SourceTargetHint:
    """One bounded, authority-bearing target hint extracted from task input."""

    text: str
    hint_type: str
    authority: str
    target_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_HINT_PATH_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*/[a-zA-Z_][a-zA-Z0-9_./]*\.[a-zA-Z0-9_]+)\b"
)
_HINT_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_HINT_QUOTED_RE = re.compile(r"\"([^\"\n]{2,120})\"|'([^'\n]{2,120})'")
_HINT_CALL_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(\s*\))")
_HINT_DEFINITION_RE = re.compile(r"\b(?:def|class|function|method)\s+([A-Za-z_]\w*)")
_HINT_PATH_LIKE_RE = re.compile(r"[/\\]|\.(py|js|jsx|ts|tsx|css|html|md)$")
_CLAUSE_SEPARATORS = (",", ";", "\n")
_HINT_MINIMUM_LENGTH = 3
_HINT_PATH_ASSOCIATION_WINDOW = 200
_MAXIMUM_TARGET_HINTS = 12


def _hint_is_usable(text: str) -> bool:
    candidate = text.strip()
    if len(candidate) < _HINT_MINIMUM_LENGTH:
        return False
    if _HINT_PATH_LIKE_RE.search(candidate):
        return False
    # Reject prose: a usable hint must look like code.
    return bool(re.search(r"[(_.\[\]=]", candidate)) and bool(
        re.match(r"^[A-Za-z_]", candidate)
    )


def _clause_span(text: str, position: int) -> tuple[int, int]:
    """Return the clause boundaries containing ``position``.

    A path named in the same clause as a hint is the authoritative association;
    clause separators never appear inside a file path.
    """

    start = max(
        (text.rfind(separator, 0, position) + 1 for separator in _CLAUSE_SEPARATORS),
        default=0,
    )
    ends = [
        index
        for index in (
            text.find(separator, position) for separator in _CLAUSE_SEPARATORS
        )
        if index >= 0
    ]
    return max(start, 0), min(ends) if ends else len(text)


def _associated_hint_path(
    path_spans: list[tuple[int, int, str]], position: int, text: str
) -> str | None:
    clause_start, clause_end = _clause_span(text, position)
    in_clause = [
        (start, end, path)
        for start, end, path in path_spans
        if clause_start <= start and end <= clause_end
    ]
    best: tuple[int, str] | None = None
    for start, end, path in in_clause or path_spans:
        if start <= position < end:
            distance = 0
        elif position < start:
            distance = start - position
        else:
            distance = position - end
        if distance > _HINT_PATH_ASSOCIATION_WINDOW:
            continue
        if best is None or distance < best[0]:
            best = (distance, path)
    return best[1] if best else None


def extract_source_target_hints(
    task_description: str,
    *,
    planner_contract: Mapping[str, Any] | None = None,
) -> tuple[SourceTargetHint, ...]:
    """Extract bounded, high-confidence target hints from authoritative task input.

    Only code-shaped literals are retained: exact calls, quoted or backticked
    snippets, and explicitly declared definition names.  Ordinary prose words are
    never treated as search terms.
    """

    text = str(task_description or "")
    contract_text = ""
    if isinstance(planner_contract, Mapping):
        for key in ("task_description", "description", "summary", "objective"):
            value = planner_contract.get(key)
            if isinstance(value, str) and value.strip():
                contract_text = value
                break

    hints: list[SourceTargetHint] = []
    seen: set[tuple[str, str]] = set()

    for authority, body in (
        ("task_description", text),
        ("planner_contract", contract_text),
    ):
        if not body:
            continue
        path_spans = [
            (match.start(1), match.end(1), match.group(1))
            for match in _HINT_PATH_RE.finditer(body)
        ]
        candidates: list[tuple[int, str, str]] = []
        for match in _HINT_BACKTICK_RE.finditer(body):
            candidate = match.group(1).strip()
            hint_type = _literal_hint_type(candidate)
            candidates.append((match.start(1), candidate, hint_type))
        for match in _HINT_QUOTED_RE.finditer(body):
            candidate = (match.group(1) or match.group(2) or "").strip()
            hint_type = _literal_hint_type(candidate)
            candidates.append((match.start(), candidate, hint_type))
        for match in _HINT_CALL_RE.finditer(body):
            # Do not reinterpret ``def foo()`` as the call-shaped literal
            # ``foo()``. A definition remains a locator until a future
            # structural resolver can establish its body boundary.
            prefix = body[max(0, match.start(1) - 24) : match.start(1)]
            if re.search(r"\b(?:def|class|function|method)\s+$", prefix):
                continue
            candidates.append(
                (match.start(1), match.group(1).strip(), HINT_TYPE_EXACT_CALL)
            )
        for match in _HINT_DEFINITION_RE.finditer(body):
            candidates.append(
                (match.start(1), match.group(1).strip(), HINT_TYPE_SYMBOL)
            )

        for position, candidate, hint_type in sorted(candidates, key=lambda x: x[0]):
            if not _hint_is_usable(candidate):
                continue
            key = (candidate, hint_type)
            if key in seen:
                continue
            seen.add(key)
            hints.append(
                SourceTargetHint(
                    text=candidate,
                    hint_type=hint_type,
                    authority=authority,
                    target_path=_associated_hint_path(path_spans, position, body),
                )
            )
            if len(hints) >= _MAXIMUM_TARGET_HINTS:
                return tuple(hints)
    return tuple(hints)


def _literal_hint_type(candidate: str) -> str:
    """Keep a definition locator out of the exact replacement-span families."""

    if re.match(r"^(?:async\s+)?(?:def|class|function|method)\b", candidate):
        return HINT_TYPE_SYMBOL
    return HINT_TYPE_EXACT_CALL if "(" in candidate else HINT_TYPE_QUOTED_SNIPPET


def _line_start_bytes(text: str) -> tuple[int, ...]:
    starts: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line.encode("utf-8"))
    if not starts:
        starts.append(0)
    return tuple(starts)


def _text_position_byte_offset(
    text: str, starts: tuple[int, ...], position: tuple[int, int]
) -> int | None:
    line_number, column = position
    lines = text.splitlines(keepends=True)
    if (
        line_number < 1
        or line_number > len(lines)
        or column < 0
        or column > len(lines[line_number - 1])
    ):
        return None
    return starts[line_number - 1] + len(
        lines[line_number - 1][:column].encode("utf-8")
    )


def _python_docstring_spans(
    text: str, starts: tuple[int, ...]
) -> tuple[tuple[int, int, str], ...]:
    tree = ast.parse(text)
    owners: list[tuple[Any, str]] = [(tree, "python_module_docstring")]
    owners.extend(
        (
            node,
            (
                "python_class_docstring"
                if isinstance(node, ast.ClassDef)
                else "python_function_docstring"
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    spans: list[tuple[int, int, str]] = []
    for owner, reason in owners:
        body = getattr(owner, "body", ())
        if not body:
            continue
        statement = body[0]
        value = getattr(statement, "value", None)
        if (
            not isinstance(statement, ast.Expr)
            or not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
        ):
            continue
        start_line = getattr(statement, "lineno", None)
        start_column = getattr(statement, "col_offset", None)
        end_line = getattr(statement, "end_lineno", None)
        end_column = getattr(statement, "end_col_offset", None)
        if (
            not isinstance(start_line, int)
            or not isinstance(start_column, int)
            or not isinstance(end_line, int)
            or not isinstance(end_column, int)
            or start_line < 1
            or end_line < start_line
            or start_line > len(starts)
            or end_line > len(starts)
        ):
            continue
        start = starts[start_line - 1] + start_column
        end = starts[end_line - 1] + end_column
        if end > start:
            spans.append((start, end, reason))
    return tuple(spans)


def _python_target_region_eligibility_reason(
    text: str, start: int, end: int
) -> str | None:
    """Reject selected Python comments/docstrings without rejecting strings."""

    starts = _line_start_bytes(text)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        token_spans: list[tuple[int, int, int]] = []
        for token in tokens:
            if token.type not in {tokenize.COMMENT, tokenize.STRING}:
                continue
            token_start = _text_position_byte_offset(text, starts, token.start)
            token_end = _text_position_byte_offset(text, starts, token.end)
            if token_start is not None and token_end is not None:
                token_spans.append((token_start, token_end, token.type))
    except (tokenize.TokenError, ValueError):
        return "python_region_classification_unavailable"

    for token_start, token_end, token_type in token_spans:
        if token_start <= start and end <= token_end:
            if token_type == tokenize.COMMENT:
                return "python_comment"
    try:
        docstring_spans = _python_docstring_spans(text, starts)
    except (IndentationError, SyntaxError, ValueError):
        for token_start, token_end, token_type in token_spans:
            if (
                token_type == tokenize.STRING
                and token_start <= start
                and end <= token_end
            ):
                return "python_string_region_classification_unavailable"
        return None
    for doc_start, doc_end, reason in docstring_spans:
        if doc_start <= start and end <= doc_end:
            return reason
    return None


def _target_region_eligibility_reason(
    relative_path: str,
    text: str,
    start: int,
    end: int,
    hint_type: str | None,
) -> str | None:
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".py":
        return _python_target_region_eligibility_reason(text, start, end)
    if suffix in {".md", ".markdown"} and hint_type == HINT_TYPE_EXACT_CALL:
        return "documentation_prose_materialization"
    return None


def _line_spans(encoded: bytes) -> list[tuple[int, int]]:
    """Return (start, end) byte spans for each line, newline included."""

    spans: list[tuple[int, int]] = []
    start = 0
    for index, byte in enumerate(encoded):
        if byte == 0x0A:
            spans.append((start, index + 1))
            start = index + 1
    if start < len(encoded) or not spans:
        spans.append((start, len(encoded)))
    return spans


def _line_index_for_byte(spans: list[tuple[int, int]], position: int) -> int:
    for index, (start, end) in enumerate(spans):
        if start <= position < end:
            return index
    return len(spans) - 1


def _align_forward(encoded: bytes, position: int) -> int:
    """Move a byte offset forward to the next UTF-8 code-point boundary."""

    while 0 < position < len(encoded) and (encoded[position] & 0xC0) == 0x80:
        position += 1
    return position


def _align_backward(encoded: bytes, position: int) -> int:
    """Move a byte offset backward to a UTF-8 code-point boundary."""

    while 0 < position < len(encoded) and (encoded[position] & 0xC0) == 0x80:
        position -= 1
    return position


@dataclass(frozen=True)
class _SelectedRegion:
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    truncated_before: bool
    truncated_after: bool
    content: str


def _render_region(
    encoded: bytes,
    spans: list[tuple[int, int]],
    start_byte: int,
    end_byte: int,
) -> _SelectedRegion:
    truncated_before = start_byte > 0
    truncated_after = end_byte < len(encoded)
    body = encoded[start_byte:end_byte].decode("utf-8", errors="ignore")
    content = "".join(
        [
            _TRUNCATED_PREFIX_MARKER if truncated_before else "",
            body,
            _TRUNCATED_SUFFIX_MARKER if truncated_after else "",
        ]
    )
    return _SelectedRegion(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=_line_index_for_byte(spans, start_byte) + 1,
        end_line=_line_index_for_byte(spans, max(start_byte, end_byte - 1)) + 1,
        truncated_before=truncated_before,
        truncated_after=truncated_after,
        content=content,
    )


def _select_source_region(
    text: str,
    *,
    budget_bytes: int,
    match_span: tuple[int, int] | None,
) -> _SelectedRegion | None:
    """Select a bounded, line-aligned region of ``text`` within ``budget_bytes``.

    When ``match_span`` is supplied the region is centered on that occurrence and
    the occurrence is never cut; otherwise the bounded head of the file is used.
    """

    encoded = text.encode("utf-8")
    spans = _line_spans(encoded)
    if len(encoded) <= budget_bytes:
        return _render_region(encoded, spans, 0, len(encoded))

    marker_reserve = len(_TRUNCATED_PREFIX_MARKER.encode("utf-8")) + len(
        _TRUNCATED_SUFFIX_MARKER.encode("utf-8")
    )
    available = budget_bytes - marker_reserve
    if available <= 0:
        return None

    if match_span is None:
        first_line = last_line = 0
        match_start, match_end = 0, 0
    else:
        match_start, match_end = match_span
        first_line = _line_index_for_byte(spans, match_start)
        last_line = _line_index_for_byte(spans, max(match_start, match_end - 1))

    start_byte = spans[first_line][0]
    end_byte = spans[last_line][1]
    if end_byte - start_byte > available:
        # A single anchoring line exceeds the budget: slice on code-point
        # boundaries around the match itself without cutting it.
        if match_span is None:
            end_byte = _align_backward(encoded, start_byte + available)
            return _render_region(encoded, spans, start_byte, end_byte)
        if match_end - match_start > available:
            return None
        slack = available - (match_end - match_start)
        start_byte = _align_forward(encoded, max(0, match_start - slack // 2))
        end_byte = _align_backward(encoded, min(len(encoded), start_byte + available))
        if end_byte < match_end:
            end_byte = _align_forward(encoded, match_end)
            start_byte = _align_forward(encoded, max(0, end_byte - available))
        return _render_region(encoded, spans, start_byte, end_byte)

    before = first_line - 1
    after = last_line + 1
    while before >= 0 or after < len(spans):
        grew = False
        if after < len(spans):
            candidate = spans[after][1]
            if candidate - start_byte <= available:
                end_byte = candidate
                after += 1
                grew = True
        if before >= 0:
            candidate = spans[before][0]
            if end_byte - candidate <= available:
                start_byte = candidate
                before -= 1
                grew = True
        if not grew:
            break
    return _render_region(encoded, spans, start_byte, end_byte)


def _structural_head_span_requested(
    task_text: str, relative_path: str, *, is_expected: bool
) -> bool:
    """Return whether the task deterministically implies a file-head edit.

    Only an expected editable Python file qualifies, and only when the task
    itself states an import or top-level declaration requirement.  This is a
    task-shape test, never a search term: no prose word is extracted as a hint.
    """

    if not is_expected or not relative_path.endswith(".py"):
        return False
    return bool(_STRUCTURAL_EDIT_REQUIREMENT_RE.search(task_text or ""))


def _select_structural_head_regions(
    text: str,
    *,
    budget_bytes: int,
    primary: _SelectedRegion,
    match_span: tuple[int, int] | None,
) -> tuple[_SelectedRegion, _SelectedRegion] | None:
    """Return a (head, primary) span pair within the same ``budget_bytes``.

    The pair is returned only when both spans fit and stay disjoint; otherwise
    the caller deterministically keeps its single full-budget primary span.
    """

    if not primary.truncated_before:
        return None
    head_budget = min(
        _STRUCTURAL_HEAD_BUDGET_BYTES, budget_bytes // _STRUCTURAL_HEAD_BUDGET_SHARE
    )
    if head_budget <= 0:
        return None
    reduced_primary = _select_source_region(
        text, budget_bytes=budget_bytes - head_budget, match_span=match_span
    )
    head = _select_source_region(text, budget_bytes=head_budget, match_span=None)
    if reduced_primary is None or head is None:
        return None
    if head.end_byte >= reduced_primary.start_byte:
        # The regions would overlap or abut; one contiguous span is correct.
        return None
    return head, reduced_primary


def _compose_span_content(text: str, regions: tuple[_SelectedRegion, ...]) -> str:
    """Render ordered, disjoint regions as one bounded excerpt with elisions."""

    encoded = text.encode("utf-8")
    parts: list[str] = []
    if regions[0].start_byte > 0:
        parts.append(_TRUNCATED_PREFIX_MARKER)
    for index, region in enumerate(regions):
        body = encoded[region.start_byte : region.end_byte].decode(
            "utf-8", errors="ignore"
        )
        parts.append(body)
        if index + 1 < len(regions):
            if not body.endswith("\n"):
                parts.append("\n")
            parts.append(_TRUNCATED_PREFIX_MARKER)
    if regions[-1].end_byte < len(encoded):
        parts.append(_TRUNCATED_SUFFIX_MARKER)
    return "".join(parts)


def _source_spans(
    regions: tuple[_SelectedRegion, ...]
) -> tuple[MaterializedSourceSpan, ...]:
    kinds = (
        (SPAN_STRUCTURAL_HEAD, SPAN_PRIMARY_TARGET)
        if len(regions) > 1
        else (SPAN_PRIMARY_TARGET,)
    )
    return tuple(
        MaterializedSourceSpan(
            kind=kind,
            start_byte=region.start_byte,
            end_byte=region.end_byte,
            start_line=region.start_line,
            end_line=region.end_line,
            included_source_bytes=region.end_byte - region.start_byte,
        )
        for kind, region in zip(kinds, regions)
    )


def _select_hint_for_source(
    hints: tuple[SourceTargetHint, ...], relative_path: str, text: str
) -> tuple[SourceTargetHint, int, int, int] | None:
    """Return the best (hint, match_start, match_end, match_count) for a file."""

    encoded = text.encode("utf-8")
    ranked: list[tuple[tuple[int, int, int, int], SourceTargetHint, int, int]] = []
    for index, hint in enumerate(hints):
        needle = hint.text.encode("utf-8")
        if not needle:
            continue
        count = encoded.count(needle)
        if count == 0:
            continue
        if hint.target_path == relative_path:
            path_rank = 0
        elif not hint.target_path:
            path_rank = 1
        else:
            path_rank = 2
        ranked.append(
            (
                (
                    path_rank,
                    _HINT_TYPE_RANK.get(hint.hint_type, 3),
                    count,
                    index,
                ),
                hint,
                encoded.find(needle),
                count,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    _, hint, start, count = ranked[0]
    return hint, start, start + len(hint.text.encode("utf-8")), count


def _is_test_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.parts and path.parts[0] in {"test", "tests"}:
        return True
    if "tests" in path.parts or "test" in path.parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _prioritized_source_paths(
    root: Path,
    candidates: list[str],
    *,
    expected_set: set[str],
    supporting_set: set[str],
    target_hints: tuple[SourceTargetHint, ...],
    source_cache: dict[str, str],
    maximum_files: int,
) -> dict[str, str]:
    """Order candidate paths by deterministic source priority (P0 first).

    P0 expected editable files, P1 expected read-only/test files, P2 non-expected
    files containing a task target hint, P3 context-selected support files, and
    P4 anything else.  Ties keep the original candidate order.
    """

    ranked: list[tuple[tuple[int, int], str, str]] = []
    prescans = 0
    for index, relative_path in enumerate(candidates):
        if relative_path in expected_set:
            priority = "P1" if _is_test_path(relative_path) else "P0"
        else:
            priority = "P3" if relative_path in supporting_set else "P4"
            if target_hints and prescans < maximum_files:
                path = (root / relative_path).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    path = None
                if (
                    path is not None
                    and path.is_file()
                    and not _binary_or_unreadable(path)
                ):
                    prescans += 1
                    text = _read_source_text(path, relative_path, source_cache)
                    if text is not None and _select_hint_for_source(
                        target_hints, relative_path, text
                    ):
                        priority = "P2"
        ranked.append(((_PRIORITY_RANK[priority], index), relative_path, priority))
    ranked.sort(key=lambda item: item[0])
    return {relative_path: priority for _, relative_path, priority in ranked}


def _creation_authorized_for_path(task_description: str, relative_path: str) -> bool:
    text = str(task_description or "")
    lowered = text.lower()
    path_lower = relative_path.lower()
    start = 0
    while True:
        index = lowered.find(path_lower, start)
        if index < 0:
            return False
        window = text[max(0, index - 180) : index + len(relative_path) + 180]
        if _CREATION_WORD_RE.search(window):
            return True
        start = index + len(relative_path)


def planner_expected_source_paths(
    *,
    task_description: str,
    planner_contract: Mapping[str, Any] | None = None,
    additional_paths: Iterable[Any] = (),
) -> tuple[str, ...]:
    """Select explicit task/contract paths without walking the repository."""

    return tuple(
        _ordered_unique_paths(
            [
                *extract_required_file_paths(task_description),
                *planner_contract_source_paths(planner_contract),
                *planner_contract_test_paths(planner_contract),
                *additional_paths,
            ]
        )
    )


def plan_target_paths(plan: Any) -> tuple[str, ...]:
    """Extract only declared plan file targets for validation/repair fallback."""

    value = plan
    if isinstance(plan, str):
        try:
            value = json.loads(plan)
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, list):
        return ()
    paths: list[str] = []
    for step in value:
        if not isinstance(step, Mapping):
            continue
        paths.extend(step.get("expected_files") or [])
        for operation in step.get("ops") or []:
            if isinstance(operation, Mapping):
                paths.append(operation.get("path"))
    return tuple(_ordered_unique_paths(paths))


def materialize_planner_source_context(
    project_dir: Path,
    *,
    task_description: str = "",
    planner_contract: Mapping[str, Any] | None = None,
    expected_paths: Iterable[Any] = (),
    supporting_paths: Iterable[Any] | None = None,
    workspace_identity: Any = None,
    maximum_files: int = MAX_RELEVANT_FILES,
    maximum_bytes_per_file: int = MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    maximum_total_source_bytes: int = MAX_SOURCE_CONTENT_TOTAL_CHARS,
    creation_authorized_paths: Iterable[Any] | None = None,
    source_cache: dict[str, str] | None = None,
) -> PlannerSourceMaterialization:
    """Materialize only named paths through the existing bounded source reader."""

    root = Path(project_dir).resolve()
    identity = _workspace_identity_text(root, workspace_identity)
    requested_expected_paths = [
        *planner_expected_source_paths(
            task_description=task_description,
            planner_contract=planner_contract,
        ),
        *expected_paths,
    ]
    unsafe_expected = _unsafe_requested_paths(requested_expected_paths)
    expected = _ordered_unique_paths(requested_expected_paths)
    expected_set = set(expected)
    creation_authorized_set = set(
        _ordered_unique_paths(creation_authorized_paths or ())
    )
    selected_supporting = list(supporting_paths or ())
    if supporting_paths is None:
        try:
            from app.services.project.source_imports import (
                python_test_source_context_from_tests,
            )

            selected_supporting = extract_required_file_paths(
                python_test_source_context_from_tests(root)
            )
        except Exception:
            selected_supporting = []
    supporting_set = set(_ordered_unique_paths(selected_supporting))
    candidates = _ordered_unique_paths([*expected, *selected_supporting])
    task_text = str(task_description or "")
    target_hints = extract_source_target_hints(
        task_text, planner_contract=planner_contract
    )
    source_cache = source_cache if source_cache is not None else {}
    priorities = _prioritized_source_paths(
        root,
        candidates,
        expected_set=expected_set,
        supporting_set=supporting_set,
        target_hints=target_hints,
        source_cache=source_cache,
        maximum_files=maximum_files,
    )
    selected = list(priorities)
    records: list[MaterializedSourceFile] = [
        MaterializedSourceFile(
            relative_path=relative_path,
            workspace_identity=identity,
            content=None,
            content_hash=None,
            version_identity=None,
            status=SOURCE_STATUS_UNREADABLE,
            truncated=False,
            source_length=None,
            source_length_chars=None,
            included_prompt_length=0,
            expected=True,
            creation_authorized=False,
            omission_reason="unsafe_path",
            priority="P0",
        )
        for relative_path in unsafe_expected
    ]
    unavailable: list[str] = [
        f"{relative_path}:unsafe_path" for relative_path in unsafe_expected
    ]
    total_bytes = 0

    for index, relative_path in enumerate(selected):
        is_expected = relative_path in expected_set
        priority = priorities[relative_path]
        creation_authorized = is_expected and (
            relative_path in creation_authorized_set
            or _creation_authorized_for_path(task_text, relative_path)
        )
        if index >= maximum_files:
            status = SOURCE_STATUS_OMITTED
            reason = "maximum_files"
            records.append(
                MaterializedSourceFile(
                    relative_path=relative_path,
                    workspace_identity=identity,
                    content=None,
                    content_hash=None,
                    version_identity=None,
                    status=status,
                    truncated=False,
                    source_length=None,
                    source_length_chars=None,
                    included_prompt_length=0,
                    expected=is_expected,
                    creation_authorized=creation_authorized,
                    omission_reason=reason,
                    priority=priority,
                    selection_strategy=SELECTION_OMITTED_TOTAL_BUDGET,
                )
            )
            if is_expected:
                unavailable.append(f"{relative_path}:{reason}")
            continue

        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            status = SOURCE_STATUS_UNREADABLE
            reason = "unsafe_path"
            path = root / "__unsafe__"
        else:
            reason = None

        if reason == "unsafe_path":
            records.append(
                MaterializedSourceFile(
                    relative_path=relative_path,
                    workspace_identity=identity,
                    content=None,
                    content_hash=None,
                    version_identity=None,
                    status=SOURCE_STATUS_UNREADABLE,
                    truncated=False,
                    source_length=None,
                    source_length_chars=None,
                    included_prompt_length=0,
                    expected=is_expected,
                    creation_authorized=False,
                    omission_reason=reason,
                    priority=priority,
                )
            )
            if is_expected:
                unavailable.append(f"{relative_path}:{reason}")
            continue

        if not path.is_file():
            status = SOURCE_STATUS_NEW if creation_authorized else SOURCE_STATUS_MISSING
            reason = None if creation_authorized else "expected_path_missing"
            records.append(
                MaterializedSourceFile(
                    relative_path=relative_path,
                    workspace_identity=identity,
                    content=None,
                    content_hash=None,
                    version_identity=None,
                    status=status,
                    truncated=False,
                    source_length=None,
                    source_length_chars=None,
                    included_prompt_length=0,
                    expected=is_expected,
                    creation_authorized=creation_authorized,
                    omission_reason=reason,
                    priority=priority,
                    selection_strategy=(
                        SELECTION_NEW_FILE if creation_authorized else None
                    ),
                )
            )
            if is_expected and reason:
                unavailable.append(f"{relative_path}:{reason}")
            continue

        binary_reason = _binary_or_unreadable(path)
        if binary_reason:
            records.append(
                MaterializedSourceFile(
                    relative_path=relative_path,
                    workspace_identity=identity,
                    content=None,
                    content_hash=None,
                    version_identity=current_source_version_identity(path),
                    status=SOURCE_STATUS_UNREADABLE,
                    truncated=False,
                    source_length=path.stat().st_size,
                    source_length_chars=None,
                    included_prompt_length=0,
                    expected=is_expected,
                    creation_authorized=False,
                    omission_reason=binary_reason,
                    priority=priority,
                )
            )
            if is_expected:
                unavailable.append(f"{relative_path}:{binary_reason}")
            continue

        text = _read_source_text(path, relative_path, source_cache)
        full_bytes = len(text.encode("utf-8")) if text is not None else None
        region: _SelectedRegion | None = None
        regions: tuple[_SelectedRegion, ...] = ()
        selected_hint: SourceTargetHint | None = None
        match_span: tuple[int, int] | None = None
        match_count = 0
        target_region_eligibility_reason: str | None = None
        strategy: str | None = None
        hint_status = TARGET_HINT_ABSENT

        if total_bytes >= maximum_total_source_bytes:
            content = None
            status = SOURCE_STATUS_OMITTED
            omission_reason = "maximum_total_source_bytes"
            strategy = SELECTION_OMITTED_TOTAL_BUDGET
        elif text is None:
            content = None
            status = SOURCE_STATUS_OMITTED
            omission_reason = "source_reader_omitted"
        else:
            selection = _select_hint_for_source(target_hints, relative_path, text)
            if selection is not None:
                selected_hint, match_start, match_end, match_count = selection
                match_span = (match_start, match_end)
                hint_status = TARGET_HINT_MATCHED
                target_region_eligibility_reason = _target_region_eligibility_reason(
                    relative_path,
                    text,
                    match_start,
                    match_end,
                    selected_hint.hint_type,
                )
            elif target_hints:
                hint_status = TARGET_HINT_NOT_FOUND
            remaining = maximum_total_source_bytes - total_bytes
            cap = min(maximum_bytes_per_file, remaining)
            region = _select_source_region(
                text, budget_bytes=cap, match_span=match_span
            )
            if region is None and match_span is not None:
                # The target could not be fitted; fall back to the bounded head
                # without claiming target grounding.
                match_span = None
                selected_hint = None
                match_count = 0
                hint_status = TARGET_HINT_NOT_FOUND
                region = _select_source_region(text, budget_bytes=cap, match_span=None)
            if region is None:
                content = None
                status = SOURCE_STATUS_OMITTED
                omission_reason = "maximum_total_source_bytes"
                strategy = SELECTION_OMITTED_TOTAL_BUDGET
            else:
                head_pair = (
                    _select_structural_head_regions(
                        text,
                        budget_bytes=cap,
                        primary=region,
                        match_span=match_span,
                    )
                    if match_span is not None
                    and _structural_head_span_requested(
                        task_text, relative_path, is_expected=is_expected
                    )
                    else None
                )
                if head_pair is not None:
                    region = head_pair[1]
                    regions = head_pair
                    content = _compose_span_content(text, regions)
                else:
                    regions = (region,)
                    content = region.content
                status = SOURCE_STATUS_EXISTING
                omission_reason = None
                if head_pair is not None:
                    strategy = SELECTION_TARGET_WITH_STRUCTURAL_HEAD
                elif not region.truncated_before and not region.truncated_after:
                    strategy = SELECTION_FULL_FILE
                elif match_span is None:
                    strategy = SELECTION_HEAD_FALLBACK
                elif (
                    selected_hint is not None
                    and selected_hint.hint_type in _EXACT_HINT_TYPES
                ):
                    strategy = SELECTION_TARGET_EXACT
                else:
                    strategy = SELECTION_TARGET_SYMBOL

        target_included = bool(
            region is not None
            and match_span is not None
            and region.start_byte <= match_span[0]
            and match_span[1] <= region.end_byte
        )

        if content is None:
            included_length = 0
            content_hash = None
            truncated = False
        else:
            included_length = len(content)
            total_bytes += len(content.encode("utf-8"))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            truncated = bool(
                region is not None
                and (region.truncated_before or region.truncated_after)
            )

        if status == SOURCE_STATUS_OMITTED and is_expected:
            unavailable.append(f"{relative_path}:{omission_reason or 'source_omitted'}")
        records.append(
            MaterializedSourceFile(
                relative_path=relative_path,
                workspace_identity=identity,
                content=content,
                content_hash=content_hash,
                version_identity=current_source_version_identity(path),
                status=status,
                truncated=truncated,
                source_length=path.stat().st_size,
                source_length_chars=len(text) if text is not None else None,
                included_prompt_length=included_length,
                expected=is_expected,
                creation_authorized=False,
                omission_reason=omission_reason,
                priority=priority,
                selection_strategy=strategy,
                full_source_bytes=full_bytes,
                included_source_bytes=(
                    len(content.encode("utf-8")) if content is not None else 0
                ),
                start_byte=region.start_byte if region else None,
                end_byte=region.end_byte if region else None,
                start_line=region.start_line if region else None,
                end_line=region.end_line if region else None,
                truncated_before=bool(region and region.truncated_before),
                truncated_after=bool(region and region.truncated_after),
                target_hint=selected_hint.text if selected_hint else None,
                target_hint_type=selected_hint.hint_type if selected_hint else None,
                target_hint_authority=(
                    selected_hint.authority if selected_hint else None
                ),
                target_hint_status=hint_status,
                target_match_count=match_count,
                target_match_start=match_span[0] if match_span else None,
                target_match_end=match_span[1] if match_span else None,
                target_region_eligibility_reason=target_region_eligibility_reason,
                target_included=target_included,
                spans=_source_spans(regions) if regions else (),
            )
        )

    return PlannerSourceMaterialization(
        workspace_identity=identity,
        files=tuple(records),
        maximum_files=maximum_files,
        maximum_bytes_per_file=maximum_bytes_per_file,
        maximum_total_source_bytes=maximum_total_source_bytes,
        materialized_source_bytes=sum(
            len(item.content.encode("utf-8"))
            for item in records
            if item.content is not None
        ),
        unavailable_reasons=tuple(dict.fromkeys(unavailable)),
    )


def materialized_source_file(
    materialization: Any, relative_path: str
) -> MaterializedSourceFile | None:
    normalized = _safe_relative_path(relative_path)
    if not normalized:
        return None
    if isinstance(materialization, PlannerSourceMaterialization):
        return materialization.file_map().get(normalized)
    files = getattr(materialization, "files", None)
    if isinstance(files, Mapping):
        value = files.get(normalized)
        return value if isinstance(value, MaterializedSourceFile) else None
    return None


def materialized_source_content(
    materialization: Any, relative_path: str, project_dir: Path
) -> str | None:
    record = materialized_source_file(materialization, relative_path)
    if record is None or record.status != SOURCE_STATUS_EXISTING:
        return None
    root = Path(project_dir).resolve()
    if record.workspace_identity != str(root):
        return None
    path = (root / record.relative_path).resolve()
    if current_source_version_identity(path) != record.version_identity:
        return None
    return record.content


def _visible_span_lines(item: MaterializedSourceFile) -> list[str]:
    """Identify every visible range when more than one span is supplied."""

    if len(item.spans) < 2:
        return []
    return [
        "visible_spans: "
        + "; ".join(
            f"{span.kind} lines {span.start_line}-{span.end_line}"
            for span in item.spans
        ),
        "Each visible span is a separate exact region of the same file version;"
        " the elision markers between them are not source text.",
    ]


def render_planner_source_materialization(
    materialization: PlannerSourceMaterialization | None,
    *,
    provider_safe: bool = False,
) -> str:
    if provider_safe:
        return _render_provider_planner_source_materialization(materialization)
    if materialization is None or not materialization.files:
        return ""
    lines = [
        "## CURRENT SOURCE MATERIALIZATION",
        "The following current workspace source was read before planning and is authoritative evidence.",
        "Exact edits may rely only on the supplied current source and its provenance.",
        "A future read_file command is not planning-time evidence.",
        "replace_in_file.old_text must occur in the materialized version for the exact path and version.",
        "New files may use write_file only when their status is new_file_authorized_for_creation.",
        "Omitted or truncated source does not authorize fabricated exact replacement.",
        "Each visible region was deliberately selected around the task target; use the visible text.",
        "Never reconstruct a whole file from a partial excerpt.",
        (
            "Bounds: "
            f"maximum files={materialization.maximum_files}, "
            f"maximum bytes per file={materialization.maximum_bytes_per_file}, "
            f"maximum total source bytes={materialization.maximum_total_source_bytes}."
        ),
        "workspace_identity: current isolated task workspace",
    ]
    for item in materialization.files:
        lines.extend(
            [
                f"### {item.relative_path}",
                f"status: {item.status}",
                f"expected: {str(item.expected).lower()}",
                f"creation_authorized: {str(item.creation_authorized).lower()}",
                f"version_identity: {item.version_identity or '(none)'}",
                f"content_hash: {item.content_hash or '(none)'}",
                f"selection_strategy: {item.selection_strategy or '(none)'}",
                (
                    "visible_lines: "
                    + (
                        f"{item.start_line}-{item.end_line}"
                        if item.start_line is not None
                        else "(none)"
                    )
                ),
                f"target_hint: {item.target_hint or '(none)'}",
                f"target_included: {str(item.target_included).lower()}",
                f"truncated: {str(item.truncated).lower()}",
                f"omission_reason: {item.omission_reason or '(none)'}",
            ]
        )
        lines.extend(_visible_span_lines(item))
        if item.content is not None:
            lines.extend(["content:", item.content])
        else:
            lines.append("content: (not supplied)")
    if materialization.unavailable_reasons:
        lines.append(
            "planning_source_materialization_unavailable: "
            + ", ".join(materialization.unavailable_reasons)
        )
    return "\n".join(lines)


def provider_planning_contract_capabilities(
    materialization: PlannerSourceMaterialization | None,
) -> tuple[bool, bool]:
    """Return ``(semantic_available, grounded_legacy_available)``.

    Semantic availability is derived from the same filtered inventory that
    renders provider-visible target handles.  Legacy replacement is available
    only when an existing, non-truncated source body is actually materialized;
    new-file and omitted-source records therefore cannot invite fabricated
    ``old`` text.
    """

    if not isinstance(materialization, PlannerSourceMaterialization):
        return False, False
    from app.services.orchestration.planning.semantic_target_inventory import (
        build_semantic_target_inventory,
    )

    inventory = build_semantic_target_inventory(materialization)
    grounded_legacy = any(
        item.status == SOURCE_STATUS_EXISTING
        and item.expected
        and item.content is not None
        and not item.truncated
        and int(item.included_source_bytes or 0) > 0
        for item in materialization.files
    )
    return bool(inventory.handles), grounded_legacy


def _render_provider_planner_source_materialization(
    materialization: PlannerSourceMaterialization | None,
) -> str:
    """Render only provider-safe source facts and opaque target handles."""

    if materialization is None or not materialization.files:
        return ""
    from app.services.orchestration.planning.semantic_target_inventory import (
        build_semantic_target_inventory,
    )

    inventory = build_semantic_target_inventory(materialization)
    grounded_legacy = any(
        item.status == SOURCE_STATUS_EXISTING
        and item.expected
        and item.content is not None
        and not item.truncated
        and int(item.included_source_bytes or 0) > 0
        for item in materialization.files
    )
    handles = {handle.path: handle for handle in inventory.handles}
    lines = [
        "## CURRENT SOURCE MATERIALIZATION",
        "The following bounded current workspace source is planning evidence.",
        "Use only the supplied visible source and the Orchestrator-issued target handles.",
        "A future read_file command is not planning-time evidence.",
        "Omitted or truncated source does not authorize fabricated exact replacement.",
        "Never reconstruct a whole file from a partial excerpt.",
        (
            "Bounds: "
            f"maximum files={materialization.maximum_files}, "
            f"maximum bytes per file={materialization.maximum_bytes_per_file}, "
            f"maximum total source bytes={materialization.maximum_total_source_bytes}."
        ),
    ]
    if inventory.handles:
        lines[4:4] = [
            "When a target_id is listed for a path, it may be used for replace_in_file.",
            "Do not invent target IDs or emit selector internals, offsets, versions, or hashes.",
            "When no target_id is listed, legacy replace_in_file uses exact old/new source evidence.",
        ]
    elif grounded_legacy:
        lines[4:4] = [
            "Semantic target mode is unavailable for this task. Do not emit target_id.",
            "Legacy replace_in_file may use exact old/new from the supplied current source evidence.",
        ]
    else:
        lines[4:4] = [
            "Semantic target mode is unavailable for this task. Do not emit target_id.",
            "Legacy replace_in_file is unavailable because no exact current source evidence is supplied.",
            "Use only non-replace operations that do not require fabricated existing-file content.",
        ]
    for item in materialization.files:
        handle = handles.get(item.relative_path)
        record_lines = [
            f"### {item.relative_path}",
            f"status: {item.status}",
            f"expected: {str(item.expected).lower()}",
            f"creation_authorized: {str(item.creation_authorized).lower()}",
            f"truncated: {str(item.truncated).lower()}",
            f"omission_reason: {item.omission_reason or '(none)'}",
        ]
        if handle is not None:
            record_lines.extend(
                [
                    f"target_id: {handle.target_id}",
                    f"target_label: {handle.label}",
                    f"target_context: {handle.context}",
                ]
            )
        lines.extend(record_lines)
        if item.content is not None:
            lines.extend(["content:", item.content])
        else:
            lines.append("content: (not supplied)")
    if materialization.unavailable_reasons:
        lines.append(
            "planning_source_materialization_unavailable: "
            + ", ".join(materialization.unavailable_reasons)
        )
    return "\n".join(lines)


def render_repair_source_materialization(
    materialization: PlannerSourceMaterialization | None,
    *,
    rejected_paths: Collection[str] = (),
    compaction_level: int = 0,
    provider_safe: bool = False,
) -> str:
    """Render a repair-only bounded projection of existing source evidence.

    First-pass planning always uses ``render_planner_source_materialization``.
    Repair may shed lower-priority support only after its complete prompt has
    exceeded the fixed bound; internal materialization and provenance remain
    untouched.
    """

    if materialization is None or not materialization.files:
        return ""
    if provider_safe:
        return _render_provider_repair_source_materialization(
            materialization,
            rejected_paths=rejected_paths,
            compaction_level=compaction_level,
        )
    if compaction_level == 0:
        return render_planner_source_materialization(materialization)
    rejected = set(_ordered_unique_paths(rejected_paths))
    omitted = 0
    lines = [
        "## CURRENT SOURCE MATERIALIZATION",
        "Current workspace source below is authoritative evidence.",
        "Exact edits may rely only on supplied source for its exact path and version.",
        "New files may use write_file only when status is new_file_authorized_for_creation.",
    ]
    for item in materialization.files:
        priority = _repair_projection_priority(item, rejected)
        # Level 4 is the required-only fail-closed boundary.  R0/R1 retain
        # their complete evidence; every support class is omitted rather than
        # being misreported as part of the minimum required projection.
        if compaction_level >= 4 and priority not in {"R0", "R1"}:
            continue
        if compaction_level >= 3 and priority == "R5":
            omitted += 1
            continue
        # R2 test/read-only evidence is useful only when an already-recorded
        # structured target can anchor it.  A head excerpt is not grounding.
        metadata_only = compaction_level >= 1 and priority in {"R4", "R5"}
        if compaction_level >= 2 and priority == "R2" and not item.target_hint:
            metadata_only = True
        content = item.content
        reduced = False
        if compaction_level >= 2 and priority in {"R2", "R4"} and content:
            content = _repair_projection_excerpt(content, item.target_hint, 560)
            reduced = content != item.content
        lines.extend(
            [
                f"### {item.relative_path}",
                f"status: {item.status}",
                f"version_identity: {item.version_identity or '(none)'}",
                f"content_hash: {item.content_hash or '(none)'}",
            ]
        )
        if metadata_only:
            lines.extend(
                [
                    "repair_projection: metadata_only",
                    (
                        "omission_reason: metadata_only_no_repair_evidence"
                        if priority == "R2" and not item.target_hint
                        else f"omission_reason: lower_priority_support_{priority}"
                    ),
                ]
            )
            continue
        lines.extend(
            [
                "visible_lines: "
                + (
                    f"{item.start_line}-{item.end_line}"
                    if item.start_line is not None
                    else "(none)"
                ),
                *_visible_span_lines(item),
                f"target_hint: {item.target_hint or '(none)'}",
                f"target_included: {str(item.target_included).lower()}",
                f"selection_strategy: {item.selection_strategy or '(none)'}",
                f"truncated: {str(item.truncated).lower()}",
                (
                    "repair_projection: repair_evidence_centered"
                    if reduced and priority == "R2"
                    else (
                        "repair_projection: reduced_excerpt"
                        if reduced
                        else "repair_projection: full_excerpt"
                    )
                ),
            ]
        )
        lines.extend(["content:", content or "(not supplied)"])
    if omitted and compaction_level < 4:
        lines.append(
            f"{omitted} lower-priority supporting source records omitted to preserve bounded target evidence."
        )
    return "\n".join(lines)


def _render_provider_repair_source_materialization(
    materialization: PlannerSourceMaterialization,
    *,
    rejected_paths: Collection[str],
    compaction_level: int,
) -> str:
    """Compact repair projection with the same provider-safe field boundary."""

    from app.services.orchestration.planning.semantic_target_inventory import (
        build_semantic_target_inventory,
    )

    inventory = build_semantic_target_inventory(materialization)
    handles = {handle.path: handle for handle in inventory.handles}
    rejected = set(_ordered_unique_paths(rejected_paths))
    omitted = 0
    lines = [
        "## CURRENT SOURCE MATERIALIZATION",
        "Current bounded workspace source below is authoritative planning evidence.",
        "Use listed Orchestrator-issued target IDs only; selector internals are not provider data.",
        "Legacy old/new remains available when no target ID is listed.",
    ]
    for item in materialization.files:
        priority = _repair_projection_priority(item, rejected)
        if compaction_level >= 4 and priority not in {"R0", "R1"}:
            continue
        if compaction_level >= 3 and priority == "R5":
            omitted += 1
            continue
        metadata_only = compaction_level >= 1 and priority in {"R4", "R5"}
        if compaction_level >= 2 and priority == "R2" and not item.target_hint:
            metadata_only = True
        content = item.content
        reduced = False
        if compaction_level >= 2 and priority in {"R2", "R4"} and content:
            content = _repair_projection_excerpt(content, item.target_hint, 560)
            reduced = content != item.content
        handle = handles.get(item.relative_path)
        record_lines = [
            f"### {item.relative_path}",
            f"status: {item.status}",
            f"expected: {str(item.expected).lower()}",
            f"truncated: {str(item.truncated).lower()}",
        ]
        if handle is not None:
            record_lines.extend(
                [
                    f"target_id: {handle.target_id}",
                    f"target_label: {handle.label}",
                    f"target_context: {handle.context}",
                ]
            )
        lines.extend(record_lines)
        if metadata_only:
            lines.append("repair_projection: metadata_only")
            continue
        lines.extend(
            [
                (
                    "repair_projection: repair_evidence_centered"
                    if reduced and priority == "R2"
                    else (
                        "repair_projection: reduced_excerpt"
                        if reduced
                        else "repair_projection: full_excerpt"
                    )
                ),
                "content:",
                content or "(not supplied)",
            ]
        )
    if omitted and compaction_level < 4:
        lines.append(
            f"{omitted} lower-priority supporting source records omitted to preserve bounded target evidence."
        )
    return "\n".join(lines)


def _repair_projection_priority(
    item: MaterializedSourceFile, rejected_paths: set[str]
) -> str:
    # A rejected mutating operation is runtime authority.  It must outrank
    # task-derived hint materialization, including when no target was found.
    if item.relative_path in rejected_paths:
        return "R0"
    if item.expected and item.priority == "P0":
        return "R1"
    if item.expected and item.status != SOURCE_STATUS_NEW:
        return "R2"
    if item.status == SOURCE_STATUS_NEW and item.creation_authorized:
        return "R3"
    if not item.expected and item.target_included:
        return "R4"
    return "R5"


def repair_projection_required_records(
    materialization: PlannerSourceMaterialization | None,
    rejected_paths: Collection[str] = (),
) -> list[tuple[MaterializedSourceFile, str]]:
    """Return deterministic R0/R1 records required by a repair projection."""

    if materialization is None:
        return []
    rejected = set(_ordered_unique_paths(rejected_paths))
    return [
        (item, priority)
        for item in materialization.files
        if (priority := _repair_projection_priority(item, rejected)) in {"R0", "R1"}
    ]


def _repair_projection_excerpt(
    content: str, hint: str | None, maximum_bytes: int
) -> str:
    """Shorten support evidence on UTF-8 boundaries, centred on its target hint."""

    encoded = content.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return content
    hint_bytes = (hint or "").encode("utf-8")
    match = encoded.find(hint_bytes) if hint_bytes else -1
    if match < 0:
        # Callers must turn no-evidence R2 records into metadata-only.  Keep
        # this defensive branch honest for R4 callers too: no generic head
        # excerpt may be represented as repair evidence.
        return ""
    start = max(0, match - maximum_bytes // 2)
    end = min(len(encoded), start + maximum_bytes)
    start = _utf8_start(encoded, start)
    end = _utf8_end(encoded, end)
    body = encoded[start:end].decode("utf-8")
    return (
        ("... [truncated]\n" if start else "")
        + body
        + ("\n... [truncated]" if end < len(encoded) else "")
    )


def _truncate_utf8_bytes(encoded: bytes, maximum_bytes: int) -> str:
    end = _utf8_end(encoded, maximum_bytes)
    suffix = "\n... [truncated]" if end < len(encoded) else ""
    return encoded[:end].decode("utf-8") + suffix


def _utf8_start(encoded: bytes, position: int) -> int:
    while position < len(encoded) and position > 0 and encoded[position] & 0xC0 == 0x80:
        position += 1
    return position


def _utf8_end(encoded: bytes, position: int) -> int:
    position = min(position, len(encoded))
    while position > 0 and position < len(encoded) and encoded[position] & 0xC0 == 0x80:
        position -= 1
    return position


def plan_source_materialization_paths(plan: Any) -> set[str]:
    """Return concrete source-like file write targets from a plan."""

    if not isinstance(plan, list):
        return set()

    paths: set[str] = set()
    for step in plan:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                continue
            path_text = (
                str(operation.get("path") or "").strip().rstrip("/").lstrip("./")
            )
            if not path_text:
                continue
            path = Path(path_text)
            if path.suffix.lower() not in SOURCE_MATERIALIZATION_EXTENSIONS:
                continue
            paths.add(path.as_posix())
    return paths


def repair_removed_source_materialization(
    previous_plan: Any, repaired_plan: Any
) -> list[str]:
    previous_source_paths = plan_source_materialization_paths(previous_plan)
    if not previous_source_paths:
        return []
    repaired_source_paths = plan_source_materialization_paths(repaired_plan)
    if repaired_source_paths:
        return []
    return sorted(previous_source_paths)


def top_level_package_roots(project_dir: Path) -> set[str]:
    roots: set[str] = set()
    try:
        for child in project_dir.iterdir():
            if (
                child.is_dir()
                and child.name not in {"tests", "test", "__pycache__"}
                and (child / "__init__.py").exists()
            ):
                roots.add(child.name)
    except OSError:
        return roots
    return roots


def is_concrete_source_materialization_path(path_text: str, project_dir: Path) -> bool:
    normalized = str(path_text or "").strip().rstrip("/").lstrip("./")
    if not normalized:
        return False
    path = Path(normalized)
    parts = path.parts
    if not parts or parts[0] in {"tests", "test"}:
        return False
    if path.suffix.lower() not in IMPLEMENTATION_SOURCE_EXTENSIONS:
        return False
    if parts[0] == "src" and len(parts) > 1:
        return True
    return parts[0] in top_level_package_roots(project_dir)


def plan_has_concrete_source_materialization(
    plan: Any,
    project_dir: Path,
    *,
    authoritative_source_paths: Collection[str] | None = None,
) -> bool:
    """Return whether a plan writes a concrete implementation source file.

    A registered planner contract may name a source file that is intentionally
    absent from a fresh runtime workspace. Such a path is accepted only when a
    structured file operation targets that exact relative contract path; the
    ordinary project/package-root guard remains the default for legacy plans.
    """

    if not isinstance(plan, list):
        return False

    def safe_relative_path(path_text: Any) -> str:
        raw_path = str(path_text or "").strip().replace("\\", "/")
        parsed_path = Path(raw_path)
        if not raw_path or parsed_path.is_absolute() or ".." in parsed_path.parts:
            return ""
        return raw_path.rstrip("/").lstrip("./")

    contract_paths = {
        normalized
        for raw_path in (authoritative_source_paths or ())
        for normalized in [safe_relative_path(raw_path)]
        if normalized
        and Path(normalized).suffix.lower() in IMPLEMENTATION_SOURCE_EXTENSIONS
        and Path(normalized).parts[0] not in {"test", "tests"}
    }
    for step in plan:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                continue
            operation_path = str(operation.get("path") or "")
            normalized_operation_path = safe_relative_path(operation_path)
            if normalized_operation_path in contract_paths or (
                is_concrete_source_materialization_path(
                    operation_path,
                    project_dir,
                )
            ):
                return True
    return False


def repair_context_requires_source_materialization(
    *,
    execution_profile: str | None,
    reason: str = "",
    rejection_reasons: list[str] | None = None,
) -> bool:
    if str(execution_profile or "") not in {"implementation", "full_lifecycle"}:
        return False
    text = "\n".join(
        [str(reason or "")] + [str(item or "") for item in (rejection_reasons or [])]
    ).lower()
    return any(marker in text for marker in SOURCE_MATERIALIZATION_REPAIR_MARKERS)
