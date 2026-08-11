"""Phase 33C-2 canonical path-authority primitives.

This module implements the value types and pure/security primitives designed in
Phase 33B (`docs/roadmap/done/phase33/phase33b-single-path-authority-contract-design-20260810.md`).
Plan acceptance constructs the immutable value and Phase 33C-4 consumes it at
Execution's mutation and observed-scope gates. Candidate Validation, Candidate
Repair, the Change Set, and Publication remain outside this authority boundary.

The module keeps three concerns permanently separate, because collapsing them is
the Attempt-16 category error (``observed path`` silently became ``authorized
path``):

``DECLARATION``
    :func:`declare` — lexical, deterministic, **no filesystem access at all**.
    Answers "is this string a syntactically admissible relative product path?".

``OBSERVATION``
    :func:`observe` — filesystem evidence about an *already declared* canonical
    path.  Never resolves, never follows a symlink, hashes within a bound.
    Answers "what is actually on disk here?".

``AUTHORIZATION``
    :class:`PathGrant` / :class:`AcceptedPathAuthority` — immutable grants
    constructed by a deterministic owner *outside* this module.  Answers "who
    authorized this path, for what operation class?".  Authorization is never
    inferred from observation: no observation field is named ``authorized``,
    ``grant``, ``allowed``, or ``permission``.

Phase 29 harvest (concepts only — this module imports nothing from
``app/services/execution/``):

* the lexical path contract and its "never accesses the filesystem" guarantee,
  from ``execution/changeset.py::validate_changeset_path``;
* the :class:`PathObservation` shape, from
  ``execution/workspace_authority.py::PathObservation``;
* the per-segment ``lstat`` / ``O_NOFOLLOW`` / re-``fstat`` / bounded-hash
  observation discipline, from ``_inspect_operation_path``;
* the nesting path-conflict rule, from ``_paths_conflict``.

Deliberate divergences from Phase 29 are documented at each site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
import errno
import hashlib
import json
import os
import re
import stat
import unicodedata

from app.services.workspace.workspace_paths import is_hydration_excluded_path

# --- Lexical bounds (harvested from Phase 29 `changeset.py`) -----------------

MAX_PATH_LENGTH = 1024
MAX_PATH_SEGMENTS = 64
MAX_SEGMENT_LENGTH = 255

# Phase 29 used `[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]`, which lets TAB, LF and CR
# through.  A declaration containing a raw newline is never legitimate and makes
# log/report lines forgeable, so the whole C0 range plus DEL is rejected here.
# This is a deliberate strengthening of the harvested rule.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Phase 33C-1 narrowed an overly broad `^[A-Za-z]:` rule, which rejected the
# legal POSIX filename `a:b/file.txt`.  A drive letter is only a drive letter
# when the colon is followed by a separator or ends the string.
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:([\\/]|$)")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Roots a *candidate declaration* may never name, at any case.  These are
# orchestration/VCS control surfaces where the declaration itself is the hazard:
# writing into `.git` corrupts the repository, and writing into `.agent` forges
# orchestration metadata that publication and change-set capture trust.
#
# This is deliberately NOT the whole `HYDRATION_EXCLUDED_NAMES` set.  Toolchain
# roots such as `venv/` and `node_modules/` are an *ownership classification*
# concern (see `TrustClass` below), not a declaration-safety concern; rejecting
# them lexically would re-mix the two semantics that all of Phase 33 exists to
# keep apart.  A declaration of `venv/x` is syntactically well-formed and is
# simply classified as non-product content by observation.
PROTECTED_ROOT_SEGMENTS = frozenset(
    {
        ".git",
        ".agent",
        ".openclaw",
        ".orchestrator",
        ".claude",
    }
)

# --- Observation bounds (harvested from Phase 29 `workspace_authority.py`) ---

MAX_OBSERVED_HASH_BYTES = 8 * 1024 * 1024
_OBSERVE_CHUNK_BYTES = 64 * 1024

# Hydration-excluded roots that are trusted *toolchain* content rather than
# orchestration-owned content.  Derived as a subset of the single existing
# ownership authority so no second, divergent table is created: everything else
# in `HYDRATION_EXCLUDED_NAMES` is orchestration-internal.
_TOOLCHAIN_EXCLUDED_NAMES = frozenset(
    {
        "venv",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "site-packages",
    }
)

SCHEMA_VERSION = "accepted-path-authority/1"


class PathAuthorityError(RuntimeError):
    """Bounded path-authority failure carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PathDeclarationError(PathAuthorityError):
    """A path string is not an admissible canonical relative declaration."""


class PathObservationError(PathAuthorityError):
    """Filesystem observation could not produce stable evidence."""


class PathGrantError(PathAuthorityError):
    """A grant or authority record is internally inconsistent."""


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclarationContext:
    """Caller-supplied lexical context for :func:`declare`.

    ``task_execution_dir_name`` is the basename of the bound TaskExecution
    runtime directory.  A product declaration repeating it as its first segment
    is rejected, mirroring the live executor rule
    (``ExecutorService.resolve_workspace_product_path``'s
    ``duplicated_task_execution_segment``) — but lexically, without the
    ``.resolve()`` that rule performs.
    """

    task_execution_dir_name: str | None = None


@dataclass(frozen=True)
class CanonicalPath:
    """An accepted canonical relative product path declaration.

    Canonical form: relative, POSIX separators, Unicode NFC, **case preserved**.
    No filesystem-dependent transformation is applied and no traversal is ever
    collapsed — ``app/../real.py`` is rejected by :func:`declare`, never
    rewritten to ``real.py``.
    """

    value: str
    segments: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def fold_key(self) -> str:
        """Case-folded identity used for alias and conflict detection only.

        The stored path is never folded.  Folding exists so that
        ``App/Real.py`` and ``app/real.py`` cannot coexist as independent
        grants, regardless of whether the host filesystem happens to be
        case-sensitive.
        """

        return "/".join(segment.casefold() for segment in self.segments)

    @property
    def parent_directories(self) -> tuple[str, ...]:
        """Ancestor directory paths, outermost first, excluding the path itself."""

        return tuple(
            "/".join(self.segments[: index + 1])
            for index in range(len(self.segments) - 1)
        )


def declare(text: Any, *, context: DeclarationContext | None = None) -> CanonicalPath:
    """Validate and canonicalize one relative product path declaration.

    Pure and lexical.  This function performs **zero filesystem access**: no
    ``exists``, ``stat``, ``lstat``, ``resolve``, ``open``, or traversal.  It
    therefore cannot and does not detect symlinks — that is :func:`observe`'s
    job, and the two must never substitute for each other.
    """

    if not isinstance(text, str):
        raise PathDeclarationError("path_not_string", "path must be a string")
    if text == "" or text.strip() == "":
        raise PathDeclarationError("path_empty", "path must be a non-empty string")
    if len(text) > MAX_PATH_LENGTH:
        raise PathDeclarationError("path_too_long", "path exceeds the length bound")

    value = unicodedata.normalize("NFC", text)

    if len(value) > MAX_PATH_LENGTH:
        raise PathDeclarationError("path_too_long", "path exceeds the length bound")
    if _CONTROL_RE.search(value):
        raise PathDeclarationError(
            "path_control_character", "path contains a control character"
        )
    if value != value.strip():
        raise PathDeclarationError(
            "path_untrimmed", "path has leading or trailing whitespace"
        )
    if "\\" in value:
        raise PathDeclarationError(
            "path_backslash_separator", "path must use '/' separators"
        )
    if "://" in value:
        raise PathDeclarationError("path_uri_like", "path must not be a URL/URI")
    if value.startswith("/"):
        raise PathDeclarationError("path_absolute", "path must be relative")
    if value.startswith("~"):
        raise PathDeclarationError(
            "path_home_expansion", "home expansion is not allowed"
        )
    if _DRIVE_LETTER_RE.match(value):
        raise PathDeclarationError(
            "path_drive_letter", "drive-letter paths are not allowed"
        )
    if value.endswith("/"):
        raise PathDeclarationError(
            "path_trailing_separator", "path must not end with a separator"
        )

    raw_segments = value.split("/")
    if len(raw_segments) > MAX_PATH_SEGMENTS:
        raise PathDeclarationError(
            "path_too_many_segments", "path has too many segments"
        )

    segments: list[str] = []
    for segment in raw_segments:
        if segment == "":
            raise PathDeclarationError(
                "path_empty_segment", "path has an empty segment"
            )
        if segment in (".", ".."):
            raise PathDeclarationError(
                "path_traversal_segment", "traversal segments are not allowed"
            )
        if segment != segment.strip():
            raise PathDeclarationError(
                "path_segment_untrimmed",
                "path segment has leading or trailing whitespace",
            )
        if len(segment) > MAX_SEGMENT_LENGTH:
            raise PathDeclarationError(
                "path_segment_too_long", "path segment exceeds the length bound"
            )
        segments.append(segment)

    if segments[0].casefold() in PROTECTED_ROOT_SEGMENTS:
        raise PathDeclarationError("path_protected_root", "path root is protected")

    bound_task_dir = (context.task_execution_dir_name if context else None) or ""
    if bound_task_dir and segments[0] == bound_task_dir:
        raise PathDeclarationError(
            "path_task_execution_root",
            "path repeats the bound TaskExecution directory segment",
        )

    return CanonicalPath("/".join(segments), tuple(segments))


def paths_conflict(left: CanonicalPath, right: CanonicalPath) -> bool:
    """True when two grants cannot coexist in one authority.

    Harvested from Phase 29 ``_paths_conflict`` (identity or nesting), adapted in
    two ways:

    * it compares **case-folded** identities, so ``App`` and ``app/real.py``
      conflict on every host filesystem;
    * it is expressed over grants, which are file-granular.  A grant for ``a``
      and a grant for ``a/b.py`` are contradictory because ``a`` cannot be both
      a granted file and a directory containing another granted file.
    """

    first = left.fold_key
    second = right.fold_key
    return (
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


class EntryType(str, Enum):
    MISSING = "missing"
    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    SPECIAL = "special"


class TrustClass(str, Enum):
    PRODUCT = "product"
    ORCHESTRATION_INTERNAL = "orchestration_internal"
    TRUSTED_TOOLCHAIN = "trusted_toolchain"


@dataclass(frozen=True)
class PathObservation:
    """Immutable filesystem evidence about one declared canonical path.

    This record describes *what is there*.  It never describes what is allowed:
    there is deliberately no ``authorized``/``grant``/``allowed``/``permission``
    field, and no consumer may derive one from these values.

    ``exists`` and ``entry_type`` are only meaningful when ``symlink_segment`` is
    ``False``.  When a symlink is found in any segment, observation stops rather
    than following it, so it reports what it can prove and nothing more.
    """

    path: CanonicalPath
    exists: bool
    entry_type: EntryType
    symlink_segment: bool
    content_sha256: str | None
    byte_length: int | None
    trust_class: TrustClass

    def payload(self) -> dict[str, Any]:
        return {
            "path": self.path.value,
            "exists": self.exists,
            "entry_type": self.entry_type.value,
            "symlink_segment": self.symlink_segment,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "trust_class": self.trust_class.value,
        }


def classify_trust(path: CanonicalPath) -> TrustClass:
    """Classify ownership of a declared path using the existing authority.

    Ownership is decided by ``is_hydration_excluded_path`` /
    ``HYDRATION_EXCLUDED_NAMES`` — the same predicate change-set capture,
    canonical baseline counting, and publication preflight already use.  This
    function is a typed *adapter* over that single source of truth, splitting
    the excluded set into trusted toolchain content and orchestration-owned
    content; it introduces no second table of excluded names.

    Classification is purely lexical over the declared path and never inspects
    or follows a symlink target.
    """

    if not is_hydration_excluded_path(Path(path.value)):
        return TrustClass.PRODUCT
    if any(segment in _TOOLCHAIN_EXCLUDED_NAMES for segment in path.segments):
        return TrustClass.TRUSTED_TOOLCHAIN
    return TrustClass.ORCHESTRATION_INTERNAL


def _missing_observation(
    path: CanonicalPath, *, symlink_segment: bool
) -> PathObservation:
    return PathObservation(
        path=path,
        exists=False,
        entry_type=EntryType.MISSING,
        symlink_segment=symlink_segment,
        content_sha256=None,
        byte_length=None,
        trust_class=classify_trust(path),
    )


def observe(root: Path, path: CanonicalPath) -> PathObservation:
    """Observe one already-declared canonical path beneath ``root``.

    The declaration step is not performed here: the signature requires a
    :class:`CanonicalPath`, so a raw string cannot be quietly declared inside
    observation.

    Filesystem discipline, harvested from Phase 29 ``_inspect_operation_path``:

    * every segment is ``lstat``-ed in turn;
    * ``Path.resolve()`` is never called;
    * a symlink segment is recorded and **never traversed**;
    * regular files are opened ``O_RDONLY | O_NOFOLLOW | O_CLOEXEC``,
      ``fstat``-ed before the read and re-``fstat``-ed after it, so a file
      replaced mid-observation is detected rather than silently hashed.

    Two deliberate divergences from Phase 29, which raised in both cases:

    * a symlink segment is *evidence*, not an error — it is reported as
      ``symlink_segment=True`` so the caller's authorization gate can fail
      closed with the reason in hand;
    * a regular file larger than :data:`MAX_OBSERVED_HASH_BYTES` is reported as
      ``entry_type=regular_file`` with ``byte_length`` set and
      ``content_sha256=None``.  Observation never reads an unbounded file and
      never invents a partial digest that would look like a full SHA-256
      identity.  That value combination is the deterministic, unambiguous
      encoding of "present, measured, deliberately not hashed".
    """

    current = root
    last_index = len(path.segments) - 1
    for index, segment in enumerate(path.segments):
        current = current / segment
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return _missing_observation(path, symlink_segment=False)
        except OSError as exc:
            raise PathObservationError(
                "path_metadata_unreadable", "path metadata is unreadable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            # Never traverse, never resolve, never read the target.  A final
            # symlink is an entry that exists; an intermediate one leaves the
            # declared path unobservable, and observation refuses to claim
            # existence it cannot prove without following.
            if index == last_index:
                return PathObservation(
                    path=path,
                    exists=True,
                    entry_type=EntryType.SPECIAL,
                    symlink_segment=True,
                    content_sha256=None,
                    byte_length=None,
                    trust_class=classify_trust(path),
                )
            return _missing_observation(path, symlink_segment=True)

    if stat.S_ISDIR(metadata.st_mode):
        return PathObservation(
            path=path,
            exists=True,
            entry_type=EntryType.DIRECTORY,
            symlink_segment=False,
            content_sha256=None,
            byte_length=None,
            trust_class=classify_trust(path),
        )
    if not stat.S_ISREG(metadata.st_mode):
        return PathObservation(
            path=path,
            exists=True,
            entry_type=EntryType.SPECIAL,
            symlink_segment=False,
            content_sha256=None,
            byte_length=None,
            trust_class=classify_trust(path),
        )

    return _observe_regular_file(current, path)


def _observe_regular_file(full: Path, path: CanonicalPath) -> PathObservation:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(full, flags)
    except FileNotFoundError:
        return _missing_observation(path, symlink_segment=False)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return _missing_observation(path, symlink_segment=True)
        raise PathObservationError(
            "path_unreadable", "path cannot be opened read-only"
        ) from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PathObservationError(
                "path_changed_during_observation",
                "path changed to a non-regular entry during observation",
            )
        if before.st_size > MAX_OBSERVED_HASH_BYTES:
            return PathObservation(
                path=path,
                exists=True,
                entry_type=EntryType.REGULAR_FILE,
                symlink_segment=False,
                content_sha256=None,
                byte_length=int(before.st_size),
                trust_class=classify_trust(path),
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _OBSERVE_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_OBSERVED_HASH_BYTES:
                raise PathObservationError(
                    "path_changed_during_observation",
                    "path grew past the hashing bound during observation",
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            total,
        ):
            raise PathObservationError(
                "path_changed_during_observation",
                "path changed during observation",
            )
    finally:
        os.close(descriptor)

    return PathObservation(
        path=path,
        exists=True,
        entry_type=EntryType.REGULAR_FILE,
        symlink_segment=False,
        content_sha256=digest.hexdigest(),
        byte_length=total,
        trust_class=classify_trust(path),
    )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class GrantClass(str, Enum):
    EXISTING_MUTABLE = "existing_mutable"
    EXISTING_READONLY = "existing_readonly"
    CREATION_AUTHORIZED = "creation_authorized"
    DELETION_AUTHORIZED = "deletion_authorized"


class GrantProvenance(str, Enum):
    """Who authorized a path.

    Phase 33B §5.5 deliberately rejects ``execution_observed`` and
    ``change_set_observed``: they name observations, not authorizations, and
    admitting them would let the system answer "who authorized this?" with "the
    thing that did it" — the Attempt-16 defect.  Observations are carried by
    :class:`PathObservation` and the Change Set, which have no provenance field.

    The existing-versus-creation distinction is carried by :class:`GrantClass`
    and is deliberately not duplicated here.
    """

    TASK_EXPLICIT_SCOPE = "task_explicit_scope"
    SOURCE_GROUNDING = "source_grounding"
    ACCEPTED_PLAN = "accepted_plan"
    OPERATOR_AUTHORIZED = "operator_authorized"
    SYSTEM_INTERNAL = "system_internal"


_HASH_REQUIRED_CLASSES = frozenset(
    {
        GrantClass.EXISTING_MUTABLE,
        GrantClass.EXISTING_READONLY,
        GrantClass.DELETION_AUTHORIZED,
    }
)


@dataclass(frozen=True)
class PathGrant:
    """One immutable, file-granular authorization.

    There are no directory grants, no glob grants, and no prefix grants.  A
    ``creation_authorized`` grant implies deterministic materialization of its
    own missing parent directories and nothing else — see
    :meth:`AcceptedPathAuthority.creation_parent_directories`.
    """

    path: CanonicalPath
    grant_class: GrantClass
    provenance: GrantProvenance
    baseline_content_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, CanonicalPath):
            raise PathGrantError(
                "grant_path_not_declared", "grant path must be a declared CanonicalPath"
            )
        if not isinstance(self.grant_class, GrantClass):
            raise PathGrantError("grant_class_invalid", "unknown grant class")
        if not isinstance(self.provenance, GrantProvenance):
            raise PathGrantError("grant_provenance_invalid", "unknown grant provenance")
        if self.grant_class in _HASH_REQUIRED_CLASSES:
            if not isinstance(self.baseline_content_hash, str) or not _SHA256_RE.match(
                self.baseline_content_hash
            ):
                raise PathGrantError(
                    "grant_baseline_hash_required",
                    f"{self.grant_class.value} requires a sha256 baseline_content_hash",
                )
        elif self.baseline_content_hash is not None:
            raise PathGrantError(
                "grant_baseline_hash_forbidden",
                f"{self.grant_class.value} must not carry a baseline_content_hash",
            )

    def payload(self) -> dict[str, Any]:
        return {
            "path": self.path.value,
            "grant_class": self.grant_class.value,
            "provenance": self.provenance.value,
            "baseline_content_hash": self.baseline_content_hash,
        }


_GRANT_KEYS = frozenset({"path", "grant_class", "provenance", "baseline_content_hash"})
_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "authority_identity",
        "accepted_plan_identity",
        "workspace_identity",
        "maximum_scope_digest",
        "grants",
    }
)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _require_identity_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise PathGrantError(f"{field}_invalid", f"{field} is required")
    if len(value) > MAX_PATH_LENGTH or _CONTROL_RE.search(value):
        raise PathGrantError(f"{field}_invalid", f"{field} is malformed")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise PathGrantError(f"{field}_invalid", f"{field} must be a sha256 hex digest")
    return value


@dataclass(frozen=True)
class AcceptedPathAuthority:
    """The immutable, frozen grant set an accepted plan authorizes.

    Contains no runtime-mutated state, no filesystem handles, no ORM models, and
    no provider or model identity.  ``grants`` is held in a canonical order so
    that identity is independent of construction order.
    """

    authority_identity: str
    accepted_plan_identity: str
    workspace_identity: str
    maximum_scope_digest: str
    grants: tuple[PathGrant, ...]

    @staticmethod
    def _canonical_grants(grants: Iterable[PathGrant]) -> tuple[PathGrant, ...]:
        ordered = tuple(grants)
        for grant in ordered:
            if not isinstance(grant, PathGrant):
                raise PathGrantError(
                    "grant_invalid", "grants must be PathGrant instances"
                )
        # Grant counts are bounded by the plan's requested scope (a few dozen),
        # so the pairwise conflict scan is deliberately simple and exhaustive
        # rather than relying on sort adjacency, which does not detect all
        # nesting pairs.
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                if left.path.value == right.path.value:
                    raise PathGrantError(
                        "duplicate_grant_path",
                        f"duplicate grant for path: {left.path.value}",
                    )
                if left.path.fold_key == right.path.fold_key:
                    raise PathGrantError(
                        "path_alias_conflict",
                        "case-aliased grants: "
                        f"{left.path.value} and {right.path.value}",
                    )
                if paths_conflict(left.path, right.path):
                    raise PathGrantError(
                        "path_conflict",
                        f"nested grants conflict: {left.path.value} "
                        f"and {right.path.value}",
                    )
        return tuple(sorted(ordered, key=lambda item: (item.path.fold_key,)))

    @staticmethod
    def compute_identity(
        *,
        grants: tuple[PathGrant, ...],
        accepted_plan_identity: str,
        workspace_identity: str,
    ) -> str:
        """Deterministic authority identity.

        Binds exactly the grants, the accepted plan, and the workspace.  It
        contains no timestamp, no object repr, no dictionary iteration order,
        and no filesystem metadata, so it is stable across process restarts.
        ``maximum_scope_digest`` is deliberately excluded: it is separately
        auditable evidence about the bound, not part of what was granted.
        """

        return hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "grants": [grant.payload() for grant in grants],
                    "accepted_plan_identity": accepted_plan_identity,
                    "workspace_identity": workspace_identity,
                }
            )
        ).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        accepted_plan_identity: str,
        workspace_identity: str,
        maximum_scope_digest: str,
        grants: Iterable[PathGrant] = (),
    ) -> "AcceptedPathAuthority":
        plan_identity = _require_identity_text(
            accepted_plan_identity, "accepted_plan_identity"
        )
        workspace = _require_identity_text(workspace_identity, "workspace_identity")
        digest = _require_sha256(maximum_scope_digest, "maximum_scope_digest")
        canonical_grants = cls._canonical_grants(grants)
        return cls(
            authority_identity=cls.compute_identity(
                grants=canonical_grants,
                accepted_plan_identity=plan_identity,
                workspace_identity=workspace,
            ),
            accepted_plan_identity=plan_identity,
            workspace_identity=workspace,
            maximum_scope_digest=digest,
            grants=canonical_grants,
        )

    # -- lookup -------------------------------------------------------------

    def grant_for(self, path: CanonicalPath) -> PathGrant | None:
        """Return the exact grant for ``path``, or ``None``.

        Lookup is exact on the canonical path.  There is no prefix, glob, or
        parent-directory fallback: absence of a grant is the default.
        """

        for grant in self.grants:
            if grant.path.value == path.value:
                return grant
        return None

    def authorizes(self, path: CanonicalPath, grant_class: GrantClass) -> bool:
        grant = self.grant_for(path)
        return grant is not None and grant.grant_class is grant_class

    def creation_parent_directories(self) -> tuple[str, ...]:
        """Parent directories implied by ``creation_authorized`` grants.

        Derived on demand, never stored as grants, and never writable authority:
        a creation grant for ``app/new/module.py`` implies materializing
        ``app`` and ``app/new`` and authorizes no sibling or child file.
        :meth:`authorizes` on ``app/other.py`` or ``app/new/other.py`` stays
        ``False``.
        """

        parents: set[str] = set()
        for grant in self.grants:
            if grant.grant_class is GrantClass.CREATION_AUTHORIZED:
                parents.update(grant.path.parent_directories)
        return tuple(sorted(parents))

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe value representation.

        Phase 33C-3 persists this inside the existing
        ``TaskCheckpoint.state_snapshot`` ``details["accepted_path_authority"]``
        key.  This module deliberately defines no database model, no migration,
        and no checkpoint access.
        """

        return {
            "schema_version": SCHEMA_VERSION,
            "authority_identity": self.authority_identity,
            "accepted_plan_identity": self.accepted_plan_identity,
            "workspace_identity": self.workspace_identity,
            "maximum_scope_digest": self.maximum_scope_digest,
            "grants": [grant.payload() for grant in self.grants],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "AcceptedPathAuthority":
        """Strictly parse a persisted authority record.

        Rejects unknown or missing keys, unknown enum values, malformed hashes,
        alias conflicts, and nested-path conflicts, and recomputes
        ``authority_identity`` — a tampered or drifted record never loads as
        valid authority.
        """

        if not isinstance(payload, Mapping):
            raise PathGrantError("authority_payload_invalid", "payload must be a map")
        keys = set(payload.keys())
        missing = _AUTHORITY_KEYS - keys
        if missing:
            raise PathGrantError(
                "authority_payload_invalid",
                f"missing required keys: {sorted(missing)}",
            )
        extra = keys - _AUTHORITY_KEYS
        if extra:
            raise PathGrantError(
                "authority_payload_invalid", f"unknown keys: {sorted(extra)}"
            )
        if payload["schema_version"] != SCHEMA_VERSION:
            raise PathGrantError(
                "authority_schema_unsupported",
                f"unsupported schema_version: {payload['schema_version']!r}",
            )
        raw_grants = payload["grants"]
        if not isinstance(raw_grants, list):
            raise PathGrantError("authority_payload_invalid", "grants must be a list")

        grants = [_grant_from_payload(item) for item in raw_grants]
        authority = cls.create(
            accepted_plan_identity=payload["accepted_plan_identity"],
            workspace_identity=payload["workspace_identity"],
            maximum_scope_digest=payload["maximum_scope_digest"],
            grants=grants,
        )
        stored_identity = _require_sha256(
            payload["authority_identity"], "authority_identity"
        )
        if stored_identity != authority.authority_identity:
            raise PathGrantError(
                "authority_identity_mismatch",
                "stored authority_identity does not match its contents",
            )
        return authority


def _grant_from_payload(payload: Any) -> PathGrant:
    if not isinstance(payload, Mapping):
        raise PathGrantError("grant_payload_invalid", "grant must be a map")
    keys = set(payload.keys())
    missing = _GRANT_KEYS - keys
    if missing:
        raise PathGrantError(
            "grant_payload_invalid", f"missing grant keys: {sorted(missing)}"
        )
    extra = keys - _GRANT_KEYS
    if extra:
        raise PathGrantError(
            "grant_payload_invalid", f"unknown grant keys: {sorted(extra)}"
        )
    try:
        grant_class = GrantClass(payload["grant_class"])
    except ValueError as exc:
        raise PathGrantError(
            "grant_class_invalid", f"unknown grant class: {payload['grant_class']!r}"
        ) from exc
    try:
        provenance = GrantProvenance(payload["provenance"])
    except ValueError as exc:
        raise PathGrantError(
            "grant_provenance_invalid",
            f"unknown grant provenance: {payload['provenance']!r}",
        ) from exc
    baseline_hash = payload["baseline_content_hash"]
    if baseline_hash is not None and not isinstance(baseline_hash, str):
        raise PathGrantError(
            "grant_baseline_hash_invalid", "baseline_content_hash must be a string"
        )
    return PathGrant(
        path=declare(payload["path"]),
        grant_class=grant_class,
        provenance=provenance,
        baseline_content_hash=baseline_hash,
    )
