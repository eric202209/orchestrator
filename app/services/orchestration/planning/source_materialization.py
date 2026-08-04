"""Planning source materialization helpers."""

from __future__ import annotations

import hashlib
import json
import re
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
        text = path.read_text(encoding="utf-8", errors="replace")
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
    r"\b(add|author|create|generate|introduce|new|scaffold|write)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MaterializedSourceFile:
    """One bounded, provenance-bearing file fact supplied to planning."""

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
    target_included: bool = False

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

    def to_prompt_metadata(self) -> dict[str, Any]:
        """Return provenance metadata safe for model-visible prompt envelopes."""

        metadata = self.to_metadata()
        display_identity = "current isolated task workspace"
        metadata["workspace_identity"] = display_identity
        metadata["files"] = [
            {
                **item,
                "workspace_identity": display_identity,
            }
            for item in metadata["files"]
        ]
        return metadata

    def to_prompt_block(self) -> str:
        return render_planner_source_materialization(self)


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
            hint_type = (
                HINT_TYPE_EXACT_CALL if "(" in candidate else HINT_TYPE_QUOTED_SNIPPET
            )
            candidates.append((match.start(1), candidate, hint_type))
        for match in _HINT_QUOTED_RE.finditer(body):
            candidate = (match.group(1) or match.group(2) or "").strip()
            hint_type = (
                HINT_TYPE_EXACT_CALL if "(" in candidate else HINT_TYPE_QUOTED_SNIPPET
            )
            candidates.append((match.start(), candidate, hint_type))
        for match in _HINT_CALL_RE.finditer(body):
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
) -> PlannerSourceMaterialization:
    """Materialize only named paths through the existing bounded source reader."""

    root = Path(project_dir).resolve()
    identity = _workspace_identity_text(root, workspace_identity)
    expected = _ordered_unique_paths(
        [
            *planner_expected_source_paths(
                task_description=task_description,
                planner_contract=planner_contract,
                additional_paths=expected_paths,
            )
        ]
    )
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
    source_cache: dict[str, str] = {}
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
    records: list[MaterializedSourceFile] = []
    unavailable: list[str] = []
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
        selected_hint: SourceTargetHint | None = None
        match_span: tuple[int, int] | None = None
        match_count = 0
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
                content = region.content
                status = SOURCE_STATUS_EXISTING
                omission_reason = None
                if not region.truncated_before and not region.truncated_after:
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
                target_included=target_included,
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


def render_planner_source_materialization(
    materialization: PlannerSourceMaterialization | None,
) -> str:
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
