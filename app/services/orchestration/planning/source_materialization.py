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


def _read_bounded_source_contents(
    project_dir: Path, rel_paths: list[str]
) -> dict[str, str]:
    from app.services.orchestration.phases.completion_repair_capsule import (
        _read_bounded_source_contents as read_bounded_source_contents,
    )

    return read_bounded_source_contents(project_dir, rel_paths)


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


def _bounded_utf8_content(content: str, maximum_bytes: int) -> tuple[str | None, bool]:
    """Fit reader output to a UTF-8 byte bound without splitting a code point."""

    encoded = content.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return content, False
    marker_bytes = _SOURCE_TRUNCATED_MARKER.encode("utf-8")
    if maximum_bytes <= len(marker_bytes):
        return None, False
    prefix = encoded[: maximum_bytes - len(marker_bytes)].decode(
        "utf-8", errors="ignore"
    )
    return prefix + _SOURCE_TRUNCATED_MARKER, True


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
    selected = _ordered_unique_paths([*expected, *selected_supporting])
    task_text = str(task_description or "")
    records: list[MaterializedSourceFile] = []
    unavailable: list[str] = []
    total_bytes = 0

    for index, relative_path in enumerate(selected):
        is_expected = relative_path in expected_set
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
                )
            )
            if is_expected:
                unavailable.append(f"{relative_path}:{binary_reason}")
            continue

        if total_bytes >= maximum_total_source_bytes:
            content = None
            status = SOURCE_STATUS_OMITTED
            omission_reason = "maximum_total_source_bytes"
        else:
            bounded = _read_bounded_source_contents(root, [relative_path])
            content = bounded.get(relative_path)
            if content is None:
                status = SOURCE_STATUS_OMITTED
                omission_reason = "source_reader_omitted"
            else:
                remaining = maximum_total_source_bytes - total_bytes
                cap = min(maximum_bytes_per_file, remaining)
                content, _ = _bounded_utf8_content(content, cap)
                if content is None:
                    status = SOURCE_STATUS_OMITTED
                    omission_reason = "maximum_total_source_bytes"
                else:
                    status = SOURCE_STATUS_EXISTING
                    omission_reason = None

        if content is None:
            included_length = 0
            content_hash = None
            truncated = False
            source_chars = None
        else:
            included_length = len(content)
            total_bytes += len(content.encode("utf-8"))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            truncated = content.endswith(_SOURCE_TRUNCATED_MARKER)
            source_chars = None if truncated else len(content)

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
                source_length_chars=source_chars,
                included_prompt_length=included_length,
                expected=is_expected,
                creation_authorized=False,
                omission_reason=omission_reason,
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
                f"source_length: {item.source_length if item.source_length is not None else '(none)'}",
                f"included_prompt_length: {item.included_prompt_length}",
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
