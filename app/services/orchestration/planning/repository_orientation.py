"""Provider-free Git-tracked orientation for one bounded discovery action.

Orientation is a *factual advisory* surface: it enumerates Git-tracked,
product-owned relative paths whose path text contains exact task literals, so
the single existing bounded discovery action can choose its scopes and query
with real repository vocabulary visible instead of guessing it.

It is deliberately not a search subsystem.  There is no ranking, no synonym
generation, no embedding, no index, and nothing is persisted: the enumeration
is derived from the Git index for the current request and discarded with it.
Visibility here grants no authority — an oriented path is never ``expected``,
never ``creation_authorized``, and never enters an APA.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ORIENTATION_BYTE_BUDGET = 3072
ORIENTATION_PATH_LIMIT = 40
ORIENTATION_MIN_LITERAL_CHARS = 4
ORIENTATION_MAX_LITERALS = 24
ORIENTATION_GIT_TIMEOUT_SECONDS = 15

ORIENTATION_SCOPE_TRACKED = "git-tracked"
ORIENTATION_SCOPE_UNAVAILABLE = "unavailable"

ORIENTATION_UNAVAILABLE_NOT_GIT = "project_is_not_a_git_work_tree"
ORIENTATION_UNAVAILABLE_NO_CANDIDATES = "no_tracked_path_matched_a_task_literal"

_GIT_SYMLINK_MODE = "120000"
_GIT_GITLINK_MODE = "160000"

_LITERAL_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Ordinary English prose words only.  This list must never acquire repository,
# task, or domain vocabulary: the rule has to stay task-agnostic.
_ORIENTATION_STOPWORDS = frozenset(
    """
    about above after again against also although always among another any anything
    because been before behind being below beside between both cannot could does
    doing done down during each either else enough even ever every everything from
    further have having here herself himself into itself just keep keeps kept like
    made make makes many might more most much must myself neither never next nothing
    once only onto other others ought over rather same shall should since
    some something such than that their them themselves then there these they thing
    things this those though through thus toward under until upon very were what
    when where whether which while whom whose will with within without would your
    yours yourself
    """.split()
)


@dataclass(frozen=True)
class RepositoryOrientation:
    """One bounded, request-local, advisory tracked-path candidate surface."""

    available: bool
    scope: str
    entries_shown: int
    entries_total: int
    truncated: bool
    bytes_used: int
    byte_budget: int
    paths: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def as_details(self) -> dict[str, object]:
        return {
            "orientation_available": self.available,
            "orientation_scope": self.scope,
            "orientation_entries_shown": self.entries_shown,
            "orientation_entries_total": self.entries_total,
            "orientation_truncated": self.truncated,
            "orientation_bytes_used": self.bytes_used,
            "orientation_byte_budget": self.byte_budget,
            "orientation_unavailable_reason": self.unavailable_reason,
        }

    def as_provider_advisory(self) -> dict[str, object]:
        """Return ``as_details`` plus the already-bounded candidate paths.

        ``as_details`` is the event-detail projection and deliberately carries
        counters only.  A provider that must choose one bounded query or one
        inspection needs the path names those counters describe, exactly as the
        older discovery seam already renders them.  The path list is the same
        tuple the derivation already bounded by ``ORIENTATION_PATH_LIMIT`` and
        ``ORIENTATION_BYTE_BUDGET``; nothing here re-derives or re-bounds it,
        and visibility still grants no authority.
        """

        return {**self.as_details(), "paths": list(self.paths)}


def _unavailable(reason: str) -> RepositoryOrientation:
    return RepositoryOrientation(
        available=False,
        scope=ORIENTATION_SCOPE_UNAVAILABLE,
        entries_shown=0,
        entries_total=0,
        truncated=False,
        bytes_used=0,
        byte_budget=ORIENTATION_BYTE_BUDGET,
        unavailable_reason=reason,
    )


def orientation_task_literals(task_description: str) -> tuple[str, ...]:
    """Split task text into deterministic exact literals, in first-seen order.

    Mechanical normalization only: lowercasing and punctuation splitting, which
    is what turns ``success-rate`` into ``success`` / ``rate`` and
    ``failure-only`` into ``failure``.  No stemming, no synonyms, no ranking.
    """

    literals: list[str] = []
    for token in _LITERAL_SPLIT_RE.split(str(task_description or "").lower()):
        if len(token) < ORIENTATION_MIN_LITERAL_CHARS or token.isdigit():
            continue
        if token in _ORIENTATION_STOPWORDS or token in literals:
            continue
        literals.append(token)
        if len(literals) >= ORIENTATION_MAX_LITERALS:
            break
    return tuple(literals)


def tracked_product_paths(project_dir: Path) -> tuple[str, ...] | None:
    """Enumerate Git-tracked, product-owned relative paths, or ``None``.

    ``None`` means the project is not a readable Git work tree.  There is no
    filesystem-crawler fallback by design.  Symlink and gitlink index entries
    are dropped from the index metadata itself, so no path outside existing
    ownership policy can become a candidate.
    """

    # Imported lazily: `app.services.orchestration.validation` is part of an
    # existing import cycle with planning, exactly as read_only_discovery does.
    import app.services.orchestration.validation.path_authority as path_authority

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-s", "-z"],
            cwd=str(project_dir),
            capture_output=True,
            shell=False,
            timeout=ORIENTATION_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    paths: list[str] = []
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            continue
        mode = metadata.split(b" ", 1)[0].decode("ascii", errors="replace")
        if mode in (_GIT_SYMLINK_MODE, _GIT_GITLINK_MODE):
            continue
        try:
            canonical = path_authority.declare(raw_path.decode("utf-8"))
        except (path_authority.PathAuthorityError, TypeError, ValueError):
            continue
        except UnicodeDecodeError:
            continue
        trust = path_authority.classify_trust(canonical)
        if trust is not path_authority.TrustClass.PRODUCT:
            continue
        paths.append(canonical.value)
    return tuple(sorted(dict.fromkeys(paths)))


def derive_repository_orientation(
    project_dir: Path,
    task_description: str,
    *,
    explicit_paths: Iterable[str] = (),
) -> RepositoryOrientation:
    """Derive the advisory candidate surface for the current request only."""

    tracked = tracked_product_paths(Path(project_dir))
    if tracked is None:
        return _unavailable(ORIENTATION_UNAVAILABLE_NOT_GIT)
    tracked_set = set(tracked)
    lowered = [(value, value.lower()) for value in tracked]

    candidates: list[str] = []
    seen: set[str] = set()

    def _append(value: str) -> None:
        if value not in seen:
            seen.add(value)
            candidates.append(value)

    # Paths the task or contract already names legitimately stay first; their
    # existing authority is unchanged and orientation never downgrades it.
    for value in explicit_paths:
        if isinstance(value, str) and value in tracked_set:
            _append(value)

    groups: list[tuple[int, str, list[str]]] = []
    for literal in orientation_task_literals(task_description):
        matches = [value for value, low in lowered if literal in low]
        if not matches:
            continue
        groups.append((len(matches), literal, matches))
    # Most selective literal first; ties broken lexically. This is a bounded
    # deterministic ordering, not a relevance ranking.
    groups.sort(key=lambda group: (group[0], group[1]))
    for _, _, matches in groups:
        for value in matches:
            _append(value)

    if not candidates:
        return _unavailable(ORIENTATION_UNAVAILABLE_NO_CANDIDATES)

    shown: list[str] = []
    bytes_used = 0
    for value in candidates:
        cost = len(f"- {value}\n".encode("utf-8"))
        if len(shown) >= ORIENTATION_PATH_LIMIT:
            break
        if bytes_used + cost > ORIENTATION_BYTE_BUDGET:
            break
        shown.append(value)
        bytes_used += cost

    return RepositoryOrientation(
        available=bool(shown),
        scope=ORIENTATION_SCOPE_TRACKED,
        entries_shown=len(shown),
        entries_total=len(candidates),
        truncated=len(shown) < len(candidates),
        bytes_used=bytes_used,
        byte_budget=ORIENTATION_BYTE_BUDGET,
        paths=tuple(shown),
        unavailable_reason=None if shown else ORIENTATION_UNAVAILABLE_NO_CANDIDATES,
    )


def render_repository_orientation(orientation: RepositoryOrientation | None) -> str:
    """Render the advisory block, or an empty string when unavailable."""

    if orientation is None or not orientation.available or not orientation.paths:
        return ""
    lines = [
        "REPOSITORY ORIENTATION (FACTS ONLY)",
        f"scope={orientation.scope}",
        f"entries_shown={orientation.entries_shown}",
        f"entries_total={orientation.entries_total}",
        f"truncated={'true' if orientation.truncated else 'false'}",
        f"bytes_used={orientation.bytes_used}",
        f"byte_budget={orientation.byte_budget}",
        "",
        *(f"- {value}" for value in orientation.paths),
        "",
        "These are factual Git-tracked paths only. They are advisory candidates, "
        "not an expected file list: a listed path is not authorized for "
        "creation or mutation, and an omitted path does not mean it is absent "
        "from the repository. Use these names to choose one bounded query and "
        "at most the allowed number of scopes instead of guessing repository "
        "vocabulary; search_text/read_file/stop behavior is unchanged.",
        "END REPOSITORY ORIENTATION",
    ]
    return "\n".join(lines)
