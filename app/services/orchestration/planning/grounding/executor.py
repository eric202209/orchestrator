"""Bounded, deterministic, read-only execution of grounding actions."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from app.services.orchestration.planning.repository_orientation import (
    tracked_product_paths,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
)
from app.services.orchestration.validation import path_authority

from .contracts import (
    EnclosingSymbolLocator,
    GroundingActionKind,
    GroundingBudgetAccounting,
    GroundingBudgetDelta,
    GroundingBudgetLimits,
    GroundingBudgetSnapshot,
    GroundingExecutionError,
    GroundingObservation,
    GroundingOutcome,
    GroundingRequest,
    GroundingRequestRejection,
    GroundingSearchHit,
    InspectFileAction,
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    MAX_HIT_COUNT,
    MAX_OBSERVATION_BYTES,
    MAX_SNIPPET_CHARS,
    MountedRouteLocator,
    ResolveStructureAction,
    SearchTextAction,
    SourceDocument,
    StructuralRelation,
    SymbolDefinitionLocator,
    observation_from_request,
)
from .structure import (
    PythonStructureError,
    bounded_region,
    extract_structural_facts,
    resolve_enclosing_symbol,
    resolve_mounted_route,
    resolve_symbol_definition,
)


# A directory scope is expanded from the Git-tracked product paths, but is
# searched in fixed batches.  This bounds each subprocess argv/output without
# making the provider predict repository cardinality.
SEARCH_BATCH_FILE_COUNT = 64
MAX_SOURCE_PARSE_BYTES = 1024 * 1024
SEARCH_TIMEOUT_SECONDS = 30


class GroundingExecutor:
    """Execute one already-validated request against one project snapshot."""

    def __init__(
        self,
        project_dir: Path,
        *,
        search_timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
        snapshot_identity: str | None = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.search_timeout_seconds = search_timeout_seconds
        self.snapshot_identity = snapshot_identity
        self._tracked_paths: frozenset[str] | None = None

    def execute(
        self,
        request: GroundingRequest,
        *,
        budget: GroundingBudgetSnapshot | None = None,
        limits: GroundingBudgetLimits | None = None,
    ) -> GroundingObservation:
        """Execute without task text, provider access, shell, or mutation capability."""

        current_budget = budget or GroundingBudgetSnapshot()
        if limits is not None and limits.repository_actions is not None:
            if current_budget.repository_actions + 1 > limits.repository_actions:
                raise self._rejection(
                    request,
                    "budget_repository_actions_exceeded",
                    "grounding repository-action budget is exhausted",
                )
        try:
            if request.action_identity == GroundingActionKind.SEARCH_TEXT.value:
                return self._execute_search(request, current_budget, limits)
            if request.action_identity == GroundingActionKind.INSPECT_FILE.value:
                return self._execute_inspect_file(request, current_budget, limits)
            if request.action_identity == GroundingActionKind.RESOLVE_STRUCTURE.value:
                return self._execute_structure(request, current_budget, limits)
        except GroundingRequestRejection as exc:
            if exc.grounding_run_id is None:
                raise self._rejection(request, exc.code, str(exc)) from exc
            raise
        except GroundingExecutionError as exc:
            if exc.grounding_run_id is None:
                raise self._error(request, exc.code, str(exc)) from exc
            raise
        except OSError as exc:
            raise self._error(request, "filesystem_error", str(exc)) from exc
        raise self._rejection(
            request, "unsupported_action", "unsupported grounding action"
        )

    def _tracked(self) -> frozenset[str]:
        if self._tracked_paths is None:
            paths = tracked_product_paths(self.project_dir)
            if paths is None:
                raise GroundingExecutionError(
                    "tracked_paths_unavailable", "tracked paths unavailable"
                )
            self._tracked_paths = frozenset(paths)
        return self._tracked_paths

    def _rejection(
        self, request: GroundingRequest, code: str, message: str
    ) -> GroundingRequestRejection:
        return GroundingRequestRejection(
            code,
            message,
            grounding_run_id=request.grounding_run_id,
            request_id=request.request_id,
            action_kind=request.action_identity,
        )

    def _error(
        self, request: GroundingRequest, code: str, message: str
    ) -> GroundingExecutionError:
        return GroundingExecutionError(
            code,
            message,
            grounding_run_id=request.grounding_run_id,
            request_id=request.request_id,
        )

    def _declare_product(self, raw_path: str) -> path_authority.CanonicalPath:
        try:
            canonical = path_authority.declare(raw_path)
        except (path_authority.PathAuthorityError, TypeError, ValueError) as exc:
            raise GroundingRequestRejection("unsafe_path", str(exc)) from exc
        if (
            path_authority.classify_trust(canonical)
            is not path_authority.TrustClass.PRODUCT
        ):
            raise GroundingRequestRejection(
                "path_not_product_owned", "path is not product-owned"
            )
        return canonical

    def _observe_path(
        self, raw_path: str, *, include_content: bool = True
    ) -> path_authority.PathObservation:
        canonical = self._declare_product(raw_path)
        try:
            observation = path_authority.observe(
                self.project_dir, canonical, include_content=include_content
            )
        except path_authority.PathObservationError as exc:
            raise GroundingRequestRejection("unsafe_path", str(exc)) from exc
        if observation.symlink_segment:
            raise GroundingRequestRejection(
                "symlink_path", "symlink traversal is not permitted"
            )
        if observation.entry_type is path_authority.EntryType.SPECIAL:
            raise GroundingRequestRejection(
                "special_path", "special files are not readable"
            )
        return observation

    def _validate_file(
        self, raw_path: str
    ) -> tuple[path_authority.CanonicalPath, bool]:
        canonical = self._declare_product(raw_path)
        observation = self._observe_path(canonical.value)
        if not observation.exists:
            return canonical, False
        if observation.entry_type is not path_authority.EntryType.REGULAR_FILE:
            raise GroundingRequestRejection(
                "not_a_file", "requested path is not a regular file"
            )
        if canonical.value not in self._tracked():
            raise GroundingRequestRejection(
                "untracked_path", "untracked source reads are not allowed"
            )
        return canonical, True

    def _validate_scope(
        self, raw_scope: str, *, deadline: float | None = None
    ) -> tuple[str, ...]:
        canonical = self._declare_product(raw_scope)
        observation = self._observe_path(canonical.value)
        if not observation.exists:
            return ()
        if observation.entry_type is path_authority.EntryType.REGULAR_FILE:
            if canonical.value not in self._tracked():
                raise GroundingRequestRejection(
                    "untracked_path", "untracked source reads are not allowed"
                )
            return (canonical.value,)
        if observation.entry_type is not path_authority.EntryType.DIRECTORY:
            raise GroundingRequestRejection(
                "invalid_scope", "scope is not a file or directory"
            )
        prefix = f"{canonical.value}/"
        candidates = sorted(path for path in self._tracked() if path.startswith(prefix))
        validated: list[str] = []
        for candidate in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                raise GroundingExecutionError(
                    "search_timeout", "bounded search deadline exhausted"
                )
            candidate_observation = self._observe_path(candidate, include_content=False)
            if not candidate_observation.exists:
                continue
            if (
                candidate_observation.entry_type
                is not path_authority.EntryType.REGULAR_FILE
            ):
                raise GroundingRequestRejection(
                    "invalid_scope_file", "scope contains a non-file path"
                )
            validated.append(candidate)
        return tuple(validated)

    def _read_source(
        self,
        raw_path: str,
        *,
        max_bytes: int = MAX_SOURCE_PARSE_BYTES,
        allow_truncated: bool = False,
    ) -> SourceDocument:
        canonical, exists = self._validate_file(raw_path)
        if not exists:
            raise self._error_for_path(
                "source_missing", f"source file is missing: {canonical.value}"
            )
        source_path = self.project_dir / canonical.value
        try:
            descriptor = os.open(
                source_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise GroundingExecutionError("source_open_failed", str(exc)) from exc
        chunks: list[bytes] = []
        try:
            before = os.fstat(descriptor)
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes and not allow_truncated:
                    raise GroundingExecutionError(
                        "source_parse_limit", "source exceeds deterministic parse bound"
                    )
            after = os.fstat(descriptor)
        except OSError as exc:
            raise GroundingExecutionError("source_read_failed", str(exc)) from exc
        finally:
            os.close(descriptor)
        if self._stat_signature(before) != self._stat_signature(after):
            raise GroundingExecutionError(
                "source_changed_during_read",
                f"source changed during read: {canonical.value}",
            )
        version = current_source_version_identity(source_path)
        if version is None:
            raise GroundingExecutionError(
                "source_disappeared", f"source disappeared: {canonical.value}"
            )
        try:
            observation = self._observe_path(canonical.value)
        except GroundingRequestRejection as exc:
            raise GroundingExecutionError("source_stability_failed", str(exc)) from exc
        if (
            not observation.exists
            or observation.entry_type is not path_authority.EntryType.REGULAR_FILE
        ):
            raise GroundingExecutionError(
                "source_stability_failed", "source is no longer a regular file"
            )
        if version != current_source_version_identity(source_path):
            raise GroundingExecutionError(
                "source_changed_during_read",
                f"source changed after read: {canonical.value}",
            )
        return SourceDocument(
            path=canonical.value,
            raw=b"".join(chunks)[: max_bytes + 1],
            source_version=version,
            content_sha256=observation.content_sha256,
        )

    def _error_for_path(self, code: str, message: str) -> GroundingExecutionError:
        return GroundingExecutionError(code, message)

    @staticmethod
    def _stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )

    @staticmethod
    def _observation_id(request: GroundingRequest) -> str:
        identity = (
            f"{request.grounding_run_id}\0{request.request_id}\0{request.action_digest}"
        )
        return f"grounding-observation-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"

    def _finish_budget(
        self,
        request: GroundingRequest,
        current_budget: GroundingBudgetSnapshot,
        delta: GroundingBudgetDelta,
        limits: GroundingBudgetLimits | None,
    ) -> GroundingBudgetSnapshot:
        try:
            return (
                GroundingBudgetAccounting(current_budget).apply(delta, limits).snapshot
            )
        except GroundingRequestRejection as exc:
            raise self._rejection(request, exc.code, str(exc)) from exc

    def _make_observation(
        self,
        request: GroundingRequest,
        current_budget: GroundingBudgetSnapshot,
        limits: GroundingBudgetLimits | None,
        *,
        outcome: GroundingOutcome,
        delta: GroundingBudgetDelta,
        **kwargs: Any,
    ) -> GroundingObservation:
        cumulative = self._finish_budget(request, current_budget, delta, limits)
        return observation_from_request(
            request,
            observation_id=self._observation_id(request),
            outcome=outcome,
            budget_delta=delta,
            budget_cumulative=cumulative,
            workspace_identity=str(self.project_dir),
            snapshot_identity=self.snapshot_identity,
            **kwargs,
        )

    def _source_metadata(
        self, documents: Iterable[SourceDocument]
    ) -> tuple[dict[str, str], dict[str, str | None]]:
        versions: dict[str, str] = {}
        hashes: dict[str, str | None] = {}
        for document in documents:
            versions[document.path] = document.source_version
            hashes[document.path] = document.content_sha256
        return versions, hashes

    def _execute_search(
        self,
        request: GroundingRequest,
        current_budget: GroundingBudgetSnapshot,
        limits: GroundingBudgetLimits | None,
    ) -> GroundingObservation:
        action = request.action
        if not isinstance(action, SearchTextAction):
            raise self._rejection(
                request, "invalid_action_contract", "search action contract mismatch"
            )
        deadline = time.monotonic() + self.search_timeout_seconds
        scopes = tuple(self._declare_product(scope).value for scope in action.scopes)
        files: list[str] = []
        for scope in scopes:
            files.extend(self._validate_scope(scope, deadline=deadline))
        files = sorted(set(files))
        truncated = False
        hits: list[GroundingSearchHit] = []
        source_versions: dict[str, str] = {}
        source_hashes: dict[str, str | None] = {}
        if files:
            executable = shutil.which("rg")
            if executable is None:
                raise self._error(
                    request,
                    "search_unavailable",
                    "bounded search executable unavailable",
                )
            for start in range(0, len(files), SEARCH_BATCH_FILE_COUNT):
                batch = tuple(files[start : start + SEARCH_BATCH_FILE_COUNT])
                versions_before = {
                    path: current_source_version_identity(self.project_dir / path)
                    for path in batch
                }
                if any(version is None for version in versions_before.values()):
                    raise self._error(
                        request,
                        "source_stability_failed",
                        "a search source disappeared",
                    )
                command = [
                    executable,
                    "--no-heading",
                    "--with-filename",
                    "--line-number",
                    "--color",
                    "never",
                    "--sort",
                    "path",
                    "--max-count",
                    str(MAX_HIT_COUNT),
                    "--max-columns",
                    str(MAX_SNIPPET_CHARS),
                    "--",
                    action.query,
                    *batch,
                ]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._error(
                        request,
                        "search_timeout",
                        "bounded search deadline exhausted",
                    )
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=self.project_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                    )
                    stdout, _ = process.communicate(timeout=remaining)
                    raw_output = stdout or b""
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.communicate()
                    raise self._error(request, "search_timeout", str(exc)) from exc
                except OSError as exc:
                    raise self._error(
                        request, "search_execution_failed", str(exc)
                    ) from exc
                return_code = process.returncode
                if return_code not in {0, 1}:
                    raise self._error(
                        request, "search_execution_failed", "bounded search failed"
                    )

                batch_truncated = len(raw_output) > MAX_OBSERVATION_BYTES
                truncated = truncated or batch_truncated
                output_text = raw_output[:MAX_OBSERVATION_BYTES].decode(
                    "utf-8", errors="replace"
                )
                output_lines = output_text.splitlines()
                batch_hits: list[GroundingSearchHit] = []
                for line_index, line in enumerate(output_lines):
                    parts = line.split(":", 2)
                    if len(parts) != 3:
                        if batch_truncated and line_index == len(output_lines) - 1:
                            continue
                        raise self._error(
                            request,
                            "search_output_invalid",
                            "search output was malformed",
                        )
                    path, line_number, snippet = parts
                    try:
                        line_value = int(line_number)
                    except ValueError as exc:
                        raise self._error(
                            request,
                            "search_output_invalid",
                            "search line number was invalid",
                        ) from exc
                    if path not in batch or line_value <= 0:
                        raise self._error(
                            request,
                            "search_output_invalid",
                            "search returned an unvalidated path",
                        )
                    batch_hits.append(
                        GroundingSearchHit(
                            path, line_value, snippet[:MAX_SNIPPET_CHARS]
                        )
                    )
                    if len(hits) + len(batch_hits) >= MAX_HIT_COUNT:
                        truncated = True
                        break

                for path in dict.fromkeys(hit.path for hit in batch_hits):
                    version_after = current_source_version_identity(
                        self.project_dir / path
                    )
                    if version_after != versions_before[path] or version_after is None:
                        raise self._error(
                            request,
                            "source_changed_during_search",
                            f"source changed during search: {path}",
                        )
                    try:
                        observation = self._observe_path(path)
                    except GroundingRequestRejection as exc:
                        raise self._error(
                            request, "source_stability_failed", str(exc)
                        ) from exc
                    if (
                        not observation.exists
                        or observation.entry_type
                        is not path_authority.EntryType.REGULAR_FILE
                        or observation.content_sha256 is None
                    ):
                        raise self._error(
                            request,
                            "source_stability_failed",
                            f"search source is not stable: {path}",
                        )
                    stable_version = current_source_version_identity(
                        self.project_dir / path
                    )
                    if stable_version != version_after:
                        raise self._error(
                            request,
                            "source_changed_during_search",
                            f"source changed after search: {path}",
                        )
                    source_versions[path] = stable_version
                    source_hashes[path] = observation.content_sha256
                hits.extend(batch_hits)
                if len(hits) >= MAX_HIT_COUNT:
                    break
        hits.sort(key=lambda hit: (hit.path, hit.line_number, hit.snippet))
        content = "\n".join(
            f"{hit.path}:{hit.line_number}:{hit.snippet}" for hit in hits
        ).encode("utf-8")
        content = content[:MAX_OBSERVATION_BYTES]
        source_paths = tuple(dict.fromkeys(hit.path for hit in hits))
        delta = GroundingBudgetDelta(
            repository_actions=1,
            source_evidence_bytes=len(content) if hits else 0,
            # Search results are bounded candidate/navigation evidence.  The
            # coordinator keeps the paths available for refinement, but only
            # substantive inspect/structure observations consume file/region
            # evidence budgets.
            distinct_files=0,
            positive_regions=0,
        )
        return self._make_observation(
            request,
            current_budget,
            limits,
            outcome=GroundingOutcome.FOUND if hits else GroundingOutcome.NOT_FOUND,
            delta=delta,
            source_paths=source_paths,
            source_scopes=scopes,
            normalized_query=action.query,
            hits=tuple(hits),
            bounded_content=content if hits else b"",
            structural_facts={"result_order": "path_line"},
            source_versions=source_versions,
            source_hashes=source_hashes,
            truncated=truncated,
            result_count=len(hits),
            result_limit=MAX_HIT_COUNT,
        )

    def _execute_inspect_file(
        self,
        request: GroundingRequest,
        current_budget: GroundingBudgetSnapshot,
        limits: GroundingBudgetLimits | None,
    ) -> GroundingObservation:
        action = request.action
        if not isinstance(action, InspectFileAction):
            raise self._rejection(
                request, "invalid_action_contract", "inspect action contract mismatch"
            )
        canonical, exists = self._validate_file(action.path)
        if not exists:
            return self._make_observation(
                request,
                current_budget,
                limits,
                outcome=GroundingOutcome.NOT_FOUND,
                delta=GroundingBudgetDelta(repository_actions=1),
                source_paths=(canonical.value,),
                result_limit=1,
            )
        document = self._read_source(
            canonical.value, max_bytes=MAX_FILE_BYTES, allow_truncated=True
        )
        bounded = document.raw[:MAX_FILE_BYTES]
        truncated = len(document.raw) > MAX_FILE_BYTES
        if truncated and bounded and not bounded.endswith(b"\n"):
            newline = bounded.rfind(b"\n")
            bounded = bounded[: newline + 1] if newline >= 0 else b""
        lines = bounded.splitlines(keepends=True)
        if len(lines) > MAX_FILE_LINES:
            bounded = b"".join(lines[:MAX_FILE_LINES])
            truncated = True
        if truncated:
            facts = {"parse_status": "source_truncated"}
        else:
            try:
                facts = extract_structural_facts(document.path, document.raw)
            except PythonStructureError:
                facts = {"parse_status": "error"}
        versions, hashes = self._source_metadata((document,))
        delta = GroundingBudgetDelta(
            repository_actions=1,
            source_evidence_bytes=len(bounded),
            distinct_files=1,
            positive_regions=1,
        )
        return self._make_observation(
            request,
            current_budget,
            limits,
            outcome=GroundingOutcome.FOUND,
            delta=delta,
            source_paths=(document.path,),
            bounded_content=bounded,
            structural_facts=facts,
            source_versions=versions,
            source_hashes=hashes,
            truncated=truncated,
            result_count=1,
            result_limit=1,
        )

    def _execute_structure(
        self,
        request: GroundingRequest,
        current_budget: GroundingBudgetSnapshot,
        limits: GroundingBudgetLimits | None,
    ) -> GroundingObservation:
        action = request.action
        if not isinstance(action, ResolveStructureAction):
            raise self._rejection(
                request, "invalid_action_contract", "structure action contract mismatch"
            )
        locator = action.locator
        if not isinstance(
            locator,
            (SymbolDefinitionLocator, EnclosingSymbolLocator, MountedRouteLocator),
        ):
            raise self._rejection(
                request, "invalid_locator", "unsupported structure locator"
            )
        document_path = locator.path
        canonical, exists = self._validate_file(document_path)
        if not exists:
            return self._make_observation(
                request,
                current_budget,
                limits,
                outcome=GroundingOutcome.NOT_FOUND,
                delta=GroundingBudgetDelta(repository_actions=1),
                source_paths=(canonical.value,),
                result_limit=1,
            )
        document = self._read_source(canonical.value)
        try:
            if action.relation is StructuralRelation.SYMBOL_DEFINITION:
                if not isinstance(locator, SymbolDefinitionLocator):
                    raise self._rejection(
                        request, "invalid_locator", "symbol locator mismatch"
                    )
                resolution = resolve_symbol_definition(document, locator.name)
            elif action.relation is StructuralRelation.ENCLOSING_SYMBOL:
                if not isinstance(locator, EnclosingSymbolLocator):
                    raise self._rejection(
                        request, "invalid_locator", "enclosing locator mismatch"
                    )
                resolution = resolve_enclosing_symbol(document, locator.line)
            elif action.relation is StructuralRelation.MOUNTED_ROUTE:
                if not isinstance(locator, MountedRouteLocator):
                    raise self._rejection(
                        request, "invalid_locator", "route locator mismatch"
                    )
                resolution = resolve_mounted_route(
                    document,
                    locator,
                    self._tracked(),
                    self._read_source,
                )
            else:
                raise self._rejection(
                    request, "unsupported_relation", "unsupported structural relation"
                )
        except PythonStructureError as exc:
            raise self._error(request, "python_parse_failed", str(exc)) from exc

        versions, hashes = self._source_metadata(resolution.documents)
        bounded = b""
        truncated = False
        if resolution.identity is not None:
            bounded, truncated = bounded_region(document, resolution.identity)
        facts: dict[str, object] = {}
        if resolution.candidates:
            facts["candidates"] = resolution.candidates
        source_paths = tuple(dict.fromkeys(item.path for item in resolution.documents))
        delta = GroundingBudgetDelta(
            repository_actions=1,
            source_evidence_bytes=len(bounded),
            distinct_files=len(source_paths),
            positive_regions=1 if resolution.outcome is GroundingOutcome.FOUND else 0,
        )
        return self._make_observation(
            request,
            current_budget,
            limits,
            outcome=resolution.outcome,
            delta=delta,
            source_paths=source_paths,
            structural_identity=resolution.identity,
            bounded_content=bounded,
            structural_facts=facts,
            source_versions=versions,
            source_hashes=hashes,
            truncated=truncated,
            result_count=(
                1 if resolution.identity is not None else len(resolution.candidates)
            ),
            result_limit=max(1, len(resolution.candidates)),
        )
