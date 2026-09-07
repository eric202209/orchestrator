"""Provider-neutral PGI1 contracts for bounded repository grounding.

This module contains the closed request/observation contracts and validation;
the coordinator lifecycle is defined in the adjacent coordinator modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, TypeAlias

from app.services.orchestration.validation.path_authority import (
    PathAuthorityError,
    declare,
)


MAX_QUERY_CHARS = 256
MAX_SCOPE_COUNT = 4
MAX_PATH_CHARS = 256
MAX_HIT_COUNT = 20
MAX_SNIPPET_CHARS = 240
MAX_OBSERVATION_BYTES = 8192
MAX_FILE_BYTES = 4096
MAX_FILE_LINES = 200
MAX_STRUCTURAL_REGION_BYTES = 8192
MAX_LINE_NUMBER = 1_000_000

SCHEMA_VERSION = "grounding-observation/1"
PROVENANCE_MODEL_REQUEST = "model_request"
PROVENANCE_DETERMINISTIC_EXECUTOR = "deterministic_executor"

_COMMAND_SYNTAX_RE = re.compile(r"&&|\|\||;|\$\(|`|[<>]")
_PYTHON_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class GroundingActionKind(str, Enum):
    """Closed set of repository questions available to the future coordinator."""

    SEARCH_TEXT = "search_text"
    INSPECT_FILE = "inspect_file"
    RESOLVE_STRUCTURE = "resolve_structure"


class StructuralRelation(str, Enum):
    SYMBOL_DEFINITION = "symbol_definition"
    ENCLOSING_SYMBOL = "enclosing_symbol"
    MOUNTED_ROUTE = "mounted_route"


class GroundingOutcome(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


class ObservationProvenance(str, Enum):
    DETERMINISTIC_EXECUTOR = PROVENANCE_DETERMINISTIC_EXECUTOR


class RequestProvenance(str, Enum):
    MODEL_REQUEST = PROVENANCE_MODEL_REQUEST


@dataclass(frozen=True, slots=True)
class SymbolDefinitionLocator:
    path: str
    name: str


@dataclass(frozen=True, slots=True)
class EnclosingSymbolLocator:
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class MountedRouteLocator:
    path: str
    method: str
    decorator_path: str


StructureLocator: TypeAlias = (
    SymbolDefinitionLocator | EnclosingSymbolLocator | MountedRouteLocator
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze(value)


def _validate_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise ValueError(f"{label} must be a non-empty control-free string")


def _normalize_query(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    if not value or len(value) > MAX_QUERY_CHARS:
        raise ValueError("query is empty or exceeds the bounded query length")
    if _CONTROL_RE.search(value) or _COMMAND_SYNTAX_RE.search(value):
        raise ValueError("query contains unsupported control or command syntax")
    return value


def _normalize_path(value: Any, label: str = "path") -> str:
    if not isinstance(value, str) or len(value) > MAX_PATH_CHARS:
        raise ValueError(f"{label} is not a bounded path string")
    return declare(value).value


def _normalize_route_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise ValueError(f"{label} is not a bounded route path")
    if _CONTROL_RE.search(value) or not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute local route path")
    if "//" in value or "?" in value or "#" in value:
        raise ValueError(f"{label} contains unsupported route syntax")
    if value != "/" and value.endswith("/"):
        return value.rstrip("/") + "/"
    return value.rstrip("/") or "/"


def _normalize_method(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("method must be a string")
    method = value.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError("unsupported HTTP method")
    return method


@dataclass(frozen=True, slots=True)
class SearchTextAction:
    query: str
    scopes: tuple[str, ...]
    provenance: RequestProvenance = RequestProvenance.MODEL_REQUEST

    @property
    def action(self) -> str:
        return GroundingActionKind.SEARCH_TEXT.value

    @property
    def normalized_payload(self) -> Mapping[str, Any]:
        return _mapping(
            {"action": self.action, "query": self.query, "scopes": self.scopes}
        )


@dataclass(frozen=True, slots=True)
class InspectFileAction:
    path: str
    provenance: RequestProvenance = RequestProvenance.MODEL_REQUEST

    @property
    def action(self) -> str:
        return GroundingActionKind.INSPECT_FILE.value

    @property
    def normalized_payload(self) -> Mapping[str, Any]:
        return _mapping({"action": self.action, "path": self.path})


@dataclass(frozen=True, slots=True)
class ResolveStructureAction:
    relation: StructuralRelation
    locator: StructureLocator
    provenance: RequestProvenance = RequestProvenance.MODEL_REQUEST

    @property
    def action(self) -> str:
        return GroundingActionKind.RESOLVE_STRUCTURE.value

    @property
    def normalized_payload(self) -> Mapping[str, Any]:
        if isinstance(self.locator, SymbolDefinitionLocator):
            locator = {"path": self.locator.path, "name": self.locator.name}
        elif isinstance(self.locator, EnclosingSymbolLocator):
            locator = {"path": self.locator.path, "line": self.locator.line}
        else:
            locator = {
                "path": self.locator.path,
                "method": self.locator.method,
                "decorator_path": self.locator.decorator_path,
            }
        return _mapping(
            {"action": self.action, "relation": self.relation.value, "locator": locator}
        )


GroundingAction: TypeAlias = (
    SearchTextAction | InspectFileAction | ResolveStructureAction
)


@dataclass(frozen=True, slots=True)
class GroundingRequest:
    grounding_run_id: str
    request_id: str
    action: GroundingAction
    provenance: RequestProvenance = RequestProvenance.MODEL_REQUEST
    normalized_payload: Mapping[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        _validate_identity(self.grounding_run_id, "grounding_run_id")
        _validate_identity(self.request_id, "request_id")
        if self.provenance != PROVENANCE_MODEL_REQUEST:
            raise ValueError("grounding requests must carry model_request provenance")
        if self.action.provenance != PROVENANCE_MODEL_REQUEST:
            raise ValueError("actions must carry model_request provenance")
        object.__setattr__(self, "normalized_payload", self.action.normalized_payload)

    @property
    def action_identity(self) -> str:
        return self.action.action

    @property
    def action_digest(self) -> str:
        payload = json.dumps(
            _thaw(self.normalized_payload), sort_keys=True, separators=(",", ":")
        )
        return sha256(payload.encode("utf-8")).hexdigest()


class GroundingRequestRejection(ValueError):
    """A request that was rejected before it could become repository evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        grounding_run_id: str | None = None,
        request_id: str | None = None,
        action_kind: str | None = None,
        field: str | None = None,
    ) -> None:
        self.code = code
        self.grounding_run_id = grounding_run_id
        self.request_id = request_id
        self.action_kind = action_kind
        self.field = field
        super().__init__(message)


class GroundingExecutionError(RuntimeError):
    """A read-only executor failure that is neither a repository negative nor rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        grounding_run_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.grounding_run_id = grounding_run_id
        self.request_id = request_id
        super().__init__(message)


def _reject(
    code: str,
    message: str,
    *,
    run_id: str | None,
    request_id: str | None,
    action_kind: str | None = None,
    field: str | None = None,
) -> GroundingRequestRejection:
    return GroundingRequestRejection(
        code,
        message,
        grounding_run_id=run_id,
        request_id=request_id,
        action_kind=action_kind,
        field=field,
    )


def parse_grounding_request(
    payload: Mapping[str, Any], *, grounding_run_id: str, request_id: str
) -> GroundingRequest:
    """Parse the closed action union and reject unknown fields fail-closed."""

    try:
        if not isinstance(payload, Mapping):
            raise ValueError("request must be an object")
        action = payload.get("action")
        if action == GroundingActionKind.SEARCH_TEXT.value:
            if set(payload) != {"action", "query", "scopes"}:
                raise ValueError("search_text fields are exactly action, query, scopes")
            raw_scopes = payload["scopes"]
            if not isinstance(raw_scopes, (list, tuple)) or not raw_scopes:
                raise ValueError("scopes must be a non-empty list")
            if len(raw_scopes) > MAX_SCOPE_COUNT:
                raise ValueError("too many scopes")
            scopes = tuple(_normalize_path(scope, "scope") for scope in raw_scopes)
            if len(set(scopes)) != len(scopes):
                raise ValueError("duplicate scopes are not permitted")
            parsed_action: GroundingAction = SearchTextAction(
                query=_normalize_query(payload["query"]), scopes=scopes
            )
        elif action == GroundingActionKind.INSPECT_FILE.value:
            if set(payload) != {"action", "path"}:
                raise ValueError("inspect_file fields are exactly action and path")
            parsed_action = InspectFileAction(path=_normalize_path(payload["path"]))
        elif action == GroundingActionKind.RESOLVE_STRUCTURE.value:
            if set(payload) != {"action", "relation", "locator"}:
                raise ValueError(
                    "resolve_structure fields are exactly action, relation, locator"
                )
            relation = StructuralRelation(payload["relation"])
            locator = payload["locator"]
            if not isinstance(locator, Mapping):
                raise ValueError("locator must be an object")
            if relation is StructuralRelation.SYMBOL_DEFINITION:
                if set(locator) != {"path", "name"}:
                    raise ValueError(
                        "symbol_definition locator fields are exactly path and name"
                    )
                name = locator["name"]
                if not isinstance(name, str) or not _PYTHON_NAME_RE.fullmatch(name):
                    raise ValueError("symbol name must be an exact Python identifier")
                parsed_locator: StructureLocator = SymbolDefinitionLocator(
                    path=_normalize_path(locator["path"]), name=name
                )
            elif relation is StructuralRelation.ENCLOSING_SYMBOL:
                if set(locator) != {"path", "line"}:
                    raise ValueError(
                        "enclosing_symbol locator fields are exactly path and line"
                    )
                line = locator["line"]
                if (
                    isinstance(line, bool)
                    or not isinstance(line, int)
                    or line <= 0
                    or line > MAX_LINE_NUMBER
                ):
                    raise ValueError("line must be a bounded positive integer")
                parsed_locator = EnclosingSymbolLocator(
                    path=_normalize_path(locator["path"]), line=line
                )
            else:
                if set(locator) != {"path", "method", "decorator_path"}:
                    raise ValueError(
                        "mounted_route locator fields are exactly path, method, decorator_path"
                    )
                parsed_locator = MountedRouteLocator(
                    path=_normalize_path(locator["path"]),
                    method=_normalize_method(locator["method"]),
                    decorator_path=_normalize_route_path(
                        locator["decorator_path"], "decorator_path"
                    ),
                )
            parsed_action = ResolveStructureAction(
                relation=relation, locator=parsed_locator
            )
        else:
            raise ValueError("unsupported grounding action")
    except (KeyError, PathAuthorityError, TypeError, ValueError) as exc:
        raise _reject(
            "invalid_request",
            str(exc),
            run_id=grounding_run_id,
            request_id=request_id,
            action_kind=str(action) if action is not None else None,
        ) from exc

    try:
        return GroundingRequest(
            grounding_run_id=grounding_run_id,
            request_id=request_id,
            action=parsed_action,
        )
    except ValueError as exc:
        raise _reject(
            "invalid_request_identity",
            str(exc),
            run_id=grounding_run_id,
            request_id=request_id,
            action_kind=parsed_action.action,
        ) from exc


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Stable bytes handed from the executor to deterministic structure code."""

    path: str
    raw: bytes
    source_version: str
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class GroundingSearchHit:
    path: str
    line_number: int
    snippet: str


@dataclass(frozen=True, slots=True)
class StructuralIdentity:
    relation: StructuralRelation
    source_path: str
    symbol_name: str | None = None
    handler_name: str | None = None
    http_method: str | None = None
    decorator_path: str | None = None
    local_router_prefix: str | None = None
    effective_route_path: str | None = None
    mount_chain: tuple[str, ...] = ()
    start_line: int = 0
    end_line: int = 0
    start_byte: int = 0
    end_byte: int = 0

    @property
    def handler_identity(self) -> str | None:
        if self.handler_name is None:
            return None
        return f"{self.source_path}:{self.handler_name}"


@dataclass(frozen=True, slots=True)
class GroundingBudgetDelta:
    provider_requests: int = 0
    repository_actions: int = 0
    source_evidence_bytes: int = 0
    distinct_files: int = 0
    positive_regions: int = 0

    def __post_init__(self) -> None:
        if any(
            not isinstance(getattr(self, name), int)
            or isinstance(getattr(self, name), bool)
            or getattr(self, name) < 0
            for name in (
                "provider_requests",
                "repository_actions",
                "source_evidence_bytes",
                "distinct_files",
                "positive_regions",
            )
        ):
            raise ValueError("budget deltas cannot be negative")


@dataclass(frozen=True, slots=True)
class GroundingBudgetSnapshot:
    provider_requests: int = 0
    repository_actions: int = 0
    source_evidence_bytes: int = 0
    distinct_files: int = 0
    positive_regions: int = 0

    def __post_init__(self) -> None:
        if any(
            not isinstance(getattr(self, name), int)
            or isinstance(getattr(self, name), bool)
            or getattr(self, name) < 0
            for name in (
                "provider_requests",
                "repository_actions",
                "source_evidence_bytes",
                "distinct_files",
                "positive_regions",
            )
        ):
            raise ValueError("budget counters cannot be negative")


@dataclass(frozen=True, slots=True)
class GroundingBudgetLimits:
    provider_requests: int | None = None
    repository_actions: int | None = None
    source_evidence_bytes: int | None = None
    distinct_files: int | None = None
    positive_regions: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "provider_requests",
            "repository_actions",
            "source_evidence_bytes",
            "distinct_files",
            "positive_regions",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"budget limit {name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class GroundingBudgetAccounting:
    snapshot: GroundingBudgetSnapshot = GroundingBudgetSnapshot()

    def apply(
        self,
        delta: GroundingBudgetDelta,
        limits: GroundingBudgetLimits | None = None,
    ) -> "GroundingBudgetAccounting":
        values = {
            name: getattr(self.snapshot, name) + getattr(delta, name)
            for name in (
                "provider_requests",
                "repository_actions",
                "source_evidence_bytes",
                "distinct_files",
                "positive_regions",
            )
        }
        if limits is not None:
            for name, limit in (
                ("provider_requests", limits.provider_requests),
                ("repository_actions", limits.repository_actions),
                ("source_evidence_bytes", limits.source_evidence_bytes),
                ("distinct_files", limits.distinct_files),
                ("positive_regions", limits.positive_regions),
            ):
                if limit is not None and values[name] > limit:
                    raise GroundingRequestRejection(
                        f"budget_{name}_exceeded",
                        f"grounding budget exceeded for {name}",
                    )
        return GroundingBudgetAccounting(GroundingBudgetSnapshot(**values))


@dataclass(frozen=True, slots=True)
class GroundingObservation:
    schema_version: str
    observation_id: str
    grounding_run_id: str
    request_id: str
    action_identity: str
    normalized_action: Mapping[str, Any]
    outcome: GroundingOutcome
    source_paths: tuple[str, ...] = ()
    source_scopes: tuple[str, ...] = ()
    normalized_query: str | None = None
    structural_identity: StructuralIdentity | None = None
    hits: tuple[GroundingSearchHit, ...] = ()
    bounded_content: bytes = b""
    structural_facts: Mapping[str, Any] = field(default_factory=dict)
    source_versions: Mapping[str, str] = field(default_factory=dict)
    source_hashes: Mapping[str, str | None] = field(default_factory=dict)
    workspace_identity: str | None = None
    snapshot_identity: str | None = None
    provenance: ObservationProvenance = ObservationProvenance.DETERMINISTIC_EXECUTOR
    truncated: bool = False
    result_count: int = 0
    result_limit: int = 0
    budget_delta: GroundingBudgetDelta = GroundingBudgetDelta()
    budget_cumulative: GroundingBudgetSnapshot = GroundingBudgetSnapshot()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported grounding observation schema")
        _validate_identity(self.observation_id, "observation_id")
        _validate_identity(self.grounding_run_id, "grounding_run_id")
        _validate_identity(self.request_id, "request_id")
        if self.action_identity not in {kind.value for kind in GroundingActionKind}:
            raise ValueError("unknown action identity")
        if not isinstance(self.outcome, GroundingOutcome):
            raise ValueError("invalid grounding outcome")
        if self.result_count < 0 or self.result_limit < 0:
            raise ValueError("result counts cannot be negative")
        if not isinstance(self.bounded_content, bytes):
            raise ValueError("bounded_content must be immutable bytes")
        if len(self.bounded_content) > MAX_OBSERVATION_BYTES:
            raise ValueError("bounded_content exceeds the observation byte bound")
        if self.result_count > self.result_limit:
            raise ValueError("result_count exceeds result_limit")
        if self.normalized_action.get("action") != self.action_identity:
            raise ValueError("normalized action identity does not match observation")
        for path in (*self.source_paths, *self.source_scopes):
            try:
                if declare(path).value != path:
                    raise ValueError("observation paths must be canonical")
            except PathAuthorityError as exc:
                raise ValueError("observation paths must be canonical") from exc
        if self.outcome is GroundingOutcome.NOT_FOUND:
            if self.hits or self.structural_identity or self.bounded_content:
                raise ValueError("NOT_FOUND cannot carry fabricated positive evidence")
        object.__setattr__(self, "normalized_action", _mapping(self.normalized_action))
        object.__setattr__(self, "structural_facts", _mapping(self.structural_facts))
        object.__setattr__(self, "source_versions", _mapping(self.source_versions))
        object.__setattr__(self, "source_hashes", _mapping(self.source_hashes))


def observation_from_request(
    request: GroundingRequest,
    *,
    observation_id: str,
    outcome: GroundingOutcome,
    **kwargs: Any,
) -> GroundingObservation:
    """Small constructor helper used by deterministic executors."""

    return GroundingObservation(
        schema_version=SCHEMA_VERSION,
        observation_id=observation_id,
        grounding_run_id=request.grounding_run_id,
        request_id=request.request_id,
        action_identity=request.action_identity,
        normalized_action=request.normalized_payload,
        outcome=outcome,
        **kwargs,
    )
