"""Planner generation and parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

TASK_LINE_RE = re.compile(
    r"^\s*(?:[-*]|\d+\.)\s*(?:\[(?: |x)?\]\s*)?(?:TASK_START:\s*)?(?P<title>[^|\n:]+?)(?:\s*(?:\||:)\s*(?P<description>.+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
TASK_HEADING_RE = re.compile(
    r"^\s*#{3,6}\s*(?:TASK_START:\s*)?(?P<title>[^\n:]+?)(?:\s*:\s*(?P<description>.+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
TASK_START_ROW_RE = re.compile(
    r"^\s*(?:(?:[-*]|\d+\.)\s*(?:\[(?: |x)?\]\s*)?|#{3,6}\s*)TASK_START:",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<run>`+|~+)[ \t]*$")
# A section heading whose title is exactly "Acceptance criteria"/"criterion"
# (optionally emphasised, optionally followed by ":"), not a Task title that
# merely starts with those words.
ACCEPTANCE_HEADING_TEXT_RE = re.compile(
    r"^[*_\s]*acceptance\s+criteri(?:a|on)[*_\s]*(?::.*)?$", re.IGNORECASE
)
TOP_LEVEL_LIST_ROW_RE = re.compile(r"^(?:[-*]|\d+\.)(?=\s|\[)")
CHECKBOX_ROW_RE = re.compile(r"^(?:[-*]|\d+\.)\s*\[(?: |x)?\]", re.IGNORECASE)
# Same grammar as candidate_checks._ACCEPTANCE_HEADING, the only heading that
# grants operator acceptance-command authority (kept equal by a test).
OPERATOR_ACCEPTANCE_HEADING_RE = re.compile(
    r"^\s*acceptance criteria:(.*)$", re.IGNORECASE
)
PLANNER_ACCEPTANCE_MARKER = "Planner "


@dataclass
class ParsedPlannerTask:
    title: str
    description: str
    execution_profile: str = "full_lifecycle"
    workflow_stage: Optional[str] = None
    priority: int = 0
    plan_position: Optional[int] = None
    estimated_effort: Optional[str] = None


@dataclass(frozen=True)
class PlannerMarkdownParseResult:
    tasks: List[ParsedPlannerTask]
    mode: str
    rejection_reason: Optional[str] = None


class PlannerService:
    """Utilities for turning requirements into reviewable task plans."""

    DEFAULT_OBJECTIVES = [
        "Clarify the implementation scope and constraints",
        "Break the work into atomic, reviewable engineering tasks",
        "Prepare the project for safe execution in Orchestrator",
    ]
    EXECUTION_PROFILES = {
        "full_lifecycle",
        "execute_only",
        "test_only",
        "debug_only",
        "review_only",
    }
    EXECUTION_PROFILE_ALIASES = {
        "plan_only": "review_only",
        "validate_only": "test_only",
        "validation_only": "test_only",
    }
    WORKFLOW_STAGES = {
        "diagnose",
        "plan",
        "execute",
        "debug",
        "validate",
        "complete",
        "review",
    }
    SCOPE_STAGE_ALIASES = {
        "plan_only": "plan",
        "validate_only": "validate",
        "validation_only": "validate",
        "test_only": "validate",
        "review_only": "review",
        "debug_only": "debug",
        "execute_only": "execute",
        "full_lifecycle": None,
    }

    @classmethod
    def generate_markdown(
        cls,
        requirement: str,
        project_name: str,
        source_brain: str = "local",
        project_description: Optional[str] = None,
    ) -> str:
        requirement = (requirement or "").strip()
        project_name = (project_name or "Untitled Project").strip()
        objective_lines = cls._build_objectives(requirement, project_description)
        task_lines = cls._build_tasks(requirement)
        overview = requirement
        if project_description:
            overview = (
                f"{requirement}\n\nExisting context: {project_description.strip()}"
            )

        markdown_lines = [
            f"# Project: {project_name}",
            "",
            "## Overview",
            overview,
            "",
            "## Objectives",
        ]
        markdown_lines.extend([f"- {item}" for item in objective_lines])
        markdown_lines.extend(
            [
                "",
                "## Task List",
                *task_lines,
                "",
                f"## Planner Notes",
                f"- Brain: {source_brain}",
                "- Review, edit, and commit the tasks before execution.",
            ]
        )
        return "\n".join(markdown_lines).strip()

    @classmethod
    def parse_markdown(cls, markdown: str) -> List[ParsedPlannerTask]:
        return cls.parse_markdown_result(markdown).tasks

    @classmethod
    def parse_markdown_result(cls, markdown: str) -> PlannerMarkdownParseResult:
        """Parse planner markdown and report the parser mode used.

        ``task_start``: only ``TASK_START:`` rows are tasks.  ``heading``: the
        shallowest ``###``-``######`` headings are tasks and their multi-line
        bodies are kept verbatim.  ``legacy``: the historical parser.  Input
        whose task boundaries are ambiguous is ``rejected`` with no tasks.
        """

        markdown = (markdown or "").strip()
        if not markdown:
            return PlannerMarkdownParseResult([], "empty")

        task_section = cls._extract_task_section(markdown)
        lines = task_section.splitlines()
        fenced, unclosed_from = cls._fence_mask(lines)
        start_rows = [
            index
            for index, line in enumerate(lines)
            if not fenced[index] and TASK_START_ROW_RE.match(line)
        ]
        heading_levels = cls._heading_levels(lines, fenced)
        task_headings = [
            (index, level)
            for index, level in heading_levels
            if not cls._is_acceptance_heading(lines[index])
        ]
        acceptance_headings = [
            index
            for index, line in enumerate(lines)
            if not fenced[index]
            and re.match(r"^\s*#{3,6}\s+", line)
            and cls._is_acceptance_heading(line)
        ]
        if unclosed_from is not None and any(
            TASK_START_ROW_RE.match(line) or TASK_HEADING_RE.match(line)
            for line in lines[unclosed_from:]
        ):
            return PlannerMarkdownParseResult([], "rejected", "unclosed_code_fence")

        start_row_set = set(start_rows)
        independent_task_headings = [
            index
            for index, level in task_headings
            if level == 3 and index not in start_row_set
        ]
        if start_rows and independent_task_headings:
            return PlannerMarkdownParseResult([], "rejected", "mixed_task_syntax")

        task_start_forms = {
            cls._task_start_row_form(lines[index]) for index in start_rows
        }
        task_start_forms.discard(None)
        if len(task_start_forms) > 1:
            return PlannerMarkdownParseResult([], "rejected", "mixed_task_syntax")

        heading_metadata_sources: Optional[List[tuple[str, str]]] = None
        if start_rows:
            mode = "task_start"
            if cls._has_top_level_pipe_row(
                lines, fenced, skip=set(start_rows)
            ) or cls._has_unprefixed_checkbox_sibling(lines, fenced, set(start_rows)):
                return PlannerMarkdownParseResult([], "rejected", "mixed_task_syntax")
            candidates = cls._task_start_candidates(lines, start_rows)
        elif task_headings:
            mode = "heading"
            level = min(item[1] for item in task_headings)
            heads = [
                index for index, item_level in task_headings if item_level == level
            ]
            if cls._has_top_level_pipe_row(lines[: heads[0]], fenced[: heads[0]]):
                return PlannerMarkdownParseResult(
                    [], "rejected", "task_rows_before_first_heading"
                )
            candidates = cls._heading_candidates(lines, heads)
            # Explicit scope metadata is read from the title, a structured
            # pipe field on the inline heading description, or an exact
            # metadata-only first paragraph. Natural-language body text is
            # not metadata authority.
            heading_metadata_sources = []
            for index in heads:
                match = TASK_HEADING_RE.match(lines[index])
                inline = (match.group("description") or "").strip()
                if inline:
                    heading_metadata_sources.append(("inline", inline))
                else:
                    heading_metadata_sources.append(
                        (
                            "paragraph",
                            cls._consume_following_description(
                                lines[: cls._next_fenced_index(fenced, index)], index
                            ),
                        )
                    )
        else:
            if acceptance_headings:
                # A canonical acceptance section owns its following criteria.
                # Legacy rows before it remain valid Tasks; criteria rows do
                # not become Tasks through the fallback parser.
                first_acceptance_heading = min(acceptance_headings)
                legacy_prefix = "\n".join(lines[:first_acceptance_heading]).strip()
                candidates = cls._extract_candidate_tasks(legacy_prefix)
                if not candidates:
                    return PlannerMarkdownParseResult(
                        [], "rejected", "acceptance_only_no_task"
                    )
                mode = "legacy"
            else:
                mode = "legacy"
                candidates = cls._extract_candidate_tasks(task_section)

        parsed_tasks: List[ParsedPlannerTask] = []
        seen_keys: set[str] = set()
        scope_rejection_reason: Optional[str] = None

        for extracted_index, (title, description) in enumerate(candidates, start=1):
            normalized_title = re.sub(r"\s+", " ", title or "").strip(" :-")
            if not normalized_title:
                continue

            if heading_metadata_sources is not None:
                source_kind, metadata_source = heading_metadata_sources[
                    extracted_index - 1
                ]
                allow_pipe_suffix = source_kind == "inline"
                normalized_description = description or ""
                (
                    explicit_profile,
                    explicit_stage,
                    normalized_title,
                    cleaned_metadata_source,
                    explicit_stage_supplied,
                    metadata_conflict,
                    invalid_scope_metadata,
                ) = cls._extract_scope_metadata(
                    normalized_title,
                    metadata_source,
                    allow_pipe_suffix=allow_pipe_suffix,
                    allow_metadata_only=True,
                )
                if invalid_scope_metadata:
                    scope_rejection_reason = "invalid_scope_metadata"
                elif (
                    metadata_conflict
                    and scope_rejection_reason != "invalid_scope_metadata"
                ):
                    scope_rejection_reason = "conflicting_scope_metadata"
                if cleaned_metadata_source != metadata_source:
                    if source_kind == "inline" and normalized_description.startswith(
                        metadata_source
                    ):
                        normalized_description = (
                            cleaned_metadata_source
                            + normalized_description[len(metadata_source) :]
                        )
                    elif source_kind == "paragraph":
                        normalized_description = cls._remove_heading_metadata_line(
                            normalized_description,
                            metadata_source,
                            cleaned_metadata_source,
                        )
                inference_source = cleaned_metadata_source
            else:
                normalized_description = re.sub(
                    r"\s+", " ", (description or "").strip()
                )
                (
                    explicit_profile,
                    explicit_stage,
                    normalized_title,
                    normalized_description,
                    explicit_stage_supplied,
                    metadata_conflict,
                    invalid_scope_metadata,
                ) = cls._extract_scope_metadata(
                    normalized_title,
                    normalized_description,
                    allow_pipe_suffix=True,
                    # Exact metadata-only paragraphs are a heading-local
                    # compatibility form. In legacy rows, a colon-delimited
                    # description containing only a scope token is still
                    # description text; structured metadata belongs in the
                    # explicit trailing pipe fields.
                    allow_metadata_only=mode == "task_start",
                )
                if invalid_scope_metadata:
                    scope_rejection_reason = "invalid_scope_metadata"
                elif (
                    metadata_conflict
                    and scope_rejection_reason != "invalid_scope_metadata"
                ):
                    scope_rejection_reason = "conflicting_scope_metadata"
                inference_source = normalized_description

            dedupe_key = normalized_title.lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            inference_text = cls._scope_inference_text(inference_source)
            inferred_position = cls._infer_plan_position(
                normalized_title, inference_text, extracted_index
            )
            inferred_priority = cls._infer_priority(normalized_title, inference_text)
            parsed_tasks.append(
                ParsedPlannerTask(
                    title=normalized_title,
                    description=cls._neutralize_acceptance_headings(
                        normalized_description
                        or f"Implement {normalized_title.lower()}"
                    ),
                    execution_profile=explicit_profile
                    or cls._infer_execution_profile(normalized_title, inference_text),
                    workflow_stage=(
                        explicit_stage
                        if explicit_stage_supplied or explicit_stage is not None
                        else cls._infer_workflow_stage(
                            normalized_title, inference_text, explicit_profile
                        )
                    ),
                    priority=inferred_priority,
                    plan_position=inferred_position,
                    estimated_effort=cls._estimate_effort(
                        normalized_title, inference_text
                    ),
                )
            )

        if scope_rejection_reason:
            return PlannerMarkdownParseResult([], "rejected", scope_rejection_reason)

        # PlanCommitService persists ``item.plan_position or index``. Validate
        # that effective persisted value across the complete parser result so
        # explicit and fallback positions cannot collide.
        effective_positions: set[int] = set()
        for index, task in enumerate(parsed_tasks, start=1):
            effective_position = task.plan_position or index
            if effective_position in effective_positions:
                return PlannerMarkdownParseResult([], "rejected", "invalid_task_order")
            effective_positions.add(effective_position)

        return PlannerMarkdownParseResult(parsed_tasks, mode)

    @staticmethod
    def _neutralize_acceptance_headings(text: str) -> str:
        """Keep planner text from opening an operator acceptance section.

        Every line the Validator's extractor would read as an
        ``Acceptance criteria:`` heading gets ``Planner `` inserted after its
        leading whitespace; nothing else changes.  Deterministic, idempotent
        and reversible.  Fenced lines are included because the extractor does
        not skip fences.
        """

        out: List[str] = []
        for line in text.splitlines(keepends=True):
            content = (line.splitlines() or [""])[0]
            if OPERATOR_ACCEPTANCE_HEADING_RE.match(content):
                indent = re.match(r"\s*", content).end()
                line = line[:indent] + PLANNER_ACCEPTANCE_MARKER + line[indent:]
            out.append(line)
        return "".join(out)

    @staticmethod
    def _fence_mask(lines: List[str]) -> tuple[List[bool], Optional[int]]:
        """Mark CommonMark-style backtick/tilde fence lines and contents.

        Returns the mask and the index of a fence left open at the end.
        """

        fenced: List[bool] = []
        marker: Optional[str] = None
        marker_length = 0
        opened_at: Optional[int] = None
        for index, line in enumerate(lines):
            opening = FENCE_RE.match(line)
            if marker is None and opening:
                run = opening.group("run")
                info = opening.group("info")
                if run[0] == "`" and "`" in info:
                    fenced.append(False)
                    continue
                marker, marker_length, opened_at = run[0], len(run), index
                fenced.append(True)
                continue

            if marker is not None:
                fenced.append(True)
                closing = FENCE_CLOSE_RE.match(line)
                if (
                    closing
                    and closing.group("run")[0] == marker
                    and len(closing.group("run")) >= marker_length
                ):
                    marker, opened_at = None, None
                    marker_length = 0
            else:
                fenced.append(False)
        return fenced, opened_at

    @staticmethod
    def _heading_levels(lines: List[str], fenced: List[bool]) -> List[tuple[int, int]]:
        headings: List[tuple[int, int]] = []
        for index, line in enumerate(lines):
            if fenced[index] or not TASK_HEADING_RE.match(line):
                continue
            hashes = re.match(r"\s*(#+)", line).group(1)
            if len(hashes) <= 6:
                headings.append((index, len(hashes)))
        return headings

    @staticmethod
    def _is_acceptance_heading(line: str) -> bool:
        return bool(ACCEPTANCE_HEADING_TEXT_RE.match(line.lstrip(" \t#")))

    @staticmethod
    def _has_top_level_pipe_row(
        lines: List[str], fenced: List[bool], skip: Optional[set[int]] = None
    ) -> bool:
        """True when an unindented list row would have been a pipe-style task."""

        for index, line in enumerate(lines):
            if fenced[index] or (skip and index in skip) or line[:1].isspace():
                continue
            if "|" in line and TASK_LINE_RE.match(line):
                return True
        return False

    @classmethod
    def _task_start_candidates(
        cls, lines: List[str], start_rows: List[int]
    ) -> List[tuple[str, str]]:
        rows = set(start_rows)
        # Same regex pass as the legacy parser, restricted to TASK_START rows:
        # list rows first, then heading rows, preserving extraction order.
        row_text = "\n".join(
            line if index in rows else "" for index, line in enumerate(lines)
        )
        candidates = [
            (match.group("title") or "", (match.group("description") or "").strip())
            for match in TASK_LINE_RE.finditer(row_text)
        ]
        for index in start_rows:
            match = TASK_HEADING_RE.match(lines[index])
            if not match:
                continue
            title = (match.group("title") or "").strip()
            description = (match.group("description") or "").strip()
            if not description:
                description = cls._consume_following_description(lines, index)
            candidates.append((title, description))
        return candidates

    @staticmethod
    def _task_start_row_form(line: str) -> Optional[str]:
        """Return the independently recognizable top-level TASK_START row form."""

        if re.match(r"^(?:[-*]|\d+\.)\s*(?:\[(?: |x)?\]\s*)?TASK_START:", line, re.I):
            return "list"
        if re.match(r"^ {0,3}#{3,6}\s*TASK_START:", line, re.I):
            return "heading"
        return None

    @staticmethod
    def _has_unprefixed_checkbox_sibling(
        lines: List[str], fenced: List[bool], start_rows: set[int]
    ) -> bool:
        """True when a top-level checkbox row lacks ``TASK_START:`` but sits in
        the same top-level list as a ``TASK_START:`` row.

        A list ends at an unindented line that is not a list row (prose, a
        label such as ``Acceptance criteria:``, a heading or a fence), so
        checklists that follow such a line are left alone.
        """

        has_start = has_unprefixed = False
        for index, line in enumerate(lines):
            if not line.strip() or line[:1].isspace():
                continue
            if fenced[index] or not TOP_LEVEL_LIST_ROW_RE.match(line):
                if has_start and has_unprefixed:
                    return True
                has_start = has_unprefixed = False
                continue
            if index in start_rows:
                has_start = True
            elif CHECKBOX_ROW_RE.match(line):
                has_unprefixed = True
        return has_start and has_unprefixed

    @staticmethod
    def _next_fenced_index(fenced: List[bool], index: int) -> int:
        return next(
            (i for i in range(index + 1, len(fenced)) if fenced[i]), len(fenced)
        )

    @classmethod
    def _heading_candidates(
        cls, lines: List[str], heads: List[int]
    ) -> List[tuple[str, str]]:
        candidates: List[tuple[str, str]] = []
        for position, index in enumerate(heads):
            end = heads[position + 1] if position + 1 < len(heads) else len(lines)
            match = TASK_HEADING_RE.match(lines[index])
            title = (match.group("title") or "").strip()
            inline = (match.group("description") or "").strip()
            body_lines = lines[index + 1 : end]
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()
            body = "\n".join(body_lines).rstrip()
            description = "\n".join(part for part in (inline, body) if part)
            candidates.append((title, description))
        return candidates

    @classmethod
    def _extract_task_section(cls, markdown: str) -> str:
        match = re.search(
            r"^##\s+Task List\s*$([\s\S]*?)(?=^##\s+|\Z)",
            markdown,
            re.MULTILINE,
        )
        return match.group(1).strip() if match else markdown

    @classmethod
    def _extract_candidate_tasks(cls, task_section: str) -> List[tuple[str, str]]:
        candidates: List[tuple[str, str]] = []
        section_lines = task_section.splitlines()

        for match in TASK_LINE_RE.finditer(task_section):
            candidates.append(
                (
                    match.group("title") or "",
                    (match.group("description") or "").strip(),
                )
            )

        for match in TASK_HEADING_RE.finditer(task_section):
            title = (match.group("title") or "").strip()
            description = (match.group("description") or "").strip()
            heading_line_index = cls._find_line_index(section_lines, match.group(0))
            if heading_line_index is not None and not description:
                description = cls._consume_following_description(
                    section_lines, heading_line_index
                )
            candidates.append((title, description))

        if candidates:
            return candidates

        fallback_items: List[tuple[str, str]] = []
        for line in section_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.lower().startswith(("overview", "objective", "notes")):
                continue

            title, description = cls._split_fallback_line(stripped)
            if title:
                fallback_items.append((title, description))

        return fallback_items

    @classmethod
    def _find_line_index(cls, lines: List[str], target_line: str) -> Optional[int]:
        normalized_target = target_line.strip()
        for index, line in enumerate(lines):
            if line.strip() == normalized_target:
                return index
        return None

    @classmethod
    def _consume_following_description(
        cls, lines: List[str], heading_index: int
    ) -> str:
        description_parts: List[str] = []
        for line in lines[heading_index + 1 :]:
            stripped = line.strip()
            if not stripped:
                if description_parts:
                    break
                continue
            if stripped.startswith(("#", "- ", "* ")) or re.match(
                r"^\d+\.\s", stripped
            ):
                break
            description_parts.append(stripped)
        return " ".join(description_parts).strip()

    @classmethod
    def _split_fallback_line(cls, line: str) -> tuple[str, str]:
        cleaned = re.sub(r"^(?:[-*]|\d+\.)\s*", "", line).strip()
        if not cleaned:
            return "", ""

        if "|" in cleaned:
            title, description = cleaned.split("|", 1)
            return title.strip(), description.strip()

        if ": " in cleaned:
            title, description = cleaned.split(": ", 1)
            return title.strip(), description.strip()

        return cleaned, ""

    @classmethod
    def _infer_plan_position(
        cls, title: str, description: str, fallback_index: int
    ) -> int:
        combined = f"{title} {description}"

        explicit_patterns = [
            r"\b(?:step|task|item|order)\s*=\s*(\d{1,3})\b",
            r"\b(?:step|task|item|order)\s*(\d{1,3})\b",
            r"^\s*(\d{1,3})[.)-]?\s+",
        ]
        for pattern in explicit_patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    pass

        phase_match = re.search(r"\bphase\s*(\d{1,3})\b", combined, re.IGNORECASE)
        if phase_match:
            try:
                phase_number = int(phase_match.group(1))
                return phase_number * 100 + fallback_index
            except ValueError:
                pass

        return fallback_index

    @classmethod
    def _infer_priority(cls, title: str, description: str) -> int:
        combined = f"{title} {description}".lower()

        if re.search(r"\bp0\b", combined):
            return 100
        if re.search(r"\bp1\b", combined):
            return 90
        if re.search(r"\bp2\b", combined):
            return 70
        if re.search(r"\bp3\b", combined):
            return 50

        if any(phrase in combined for phrase in ["critical", "urgent", "blocker"]):
            return 100
        if any(phrase in combined for phrase in ["high priority", "highest priority"]):
            return 90
        if any(phrase in combined for phrase in ["medium priority", "normal priority"]):
            return 60
        if any(phrase in combined for phrase in ["low priority", "nice to have"]):
            return 30

        if any(
            word in combined for word in ["first", "foundation", "setup", "bootstrap"]
        ):
            return 80
        if any(
            word in combined for word in ["verify", "polish", "cleanup", "document"]
        ):
            return 40

        return 60

    @classmethod
    def _infer_execution_profile(cls, title: str, description: str) -> str:
        combined = f"{title} {description}".lower()

        if any(
            word in combined
            for word in [
                "review",
                "audit",
                "inspect",
                "analysis",
                "analyze",
                "architecture",
                "inventory",
                "inspect changes",
                "code review",
            ]
        ):
            return "review_only"
        if any(
            word in combined
            for word in ["test", "testing", "verify", "verification", "qa"]
        ):
            return "test_only"
        if any(
            word in combined for word in ["debug", "fix", "investigate", "root cause"]
        ):
            return "debug_only"
        if any(
            word in combined
            for word in ["implement", "build", "create", "add", "execute"]
        ):
            return "execute_only"
        return "full_lifecycle"

    @classmethod
    def _infer_workflow_stage(
        cls, title: str, description: str, execution_profile: Optional[str] = None
    ) -> Optional[str]:
        if execution_profile:
            stage = cls.SCOPE_STAGE_ALIASES.get(execution_profile)
            if stage:
                return stage

        combined = f"{title} {description}".lower()
        if any(word in combined for word in ["diagnose", "inspect", "investigate"]):
            return "diagnose"
        if any(word in combined for word in ["plan", "design", "approach"]):
            return "plan"
        if any(word in combined for word in ["debug", "fix", "repair", "root cause"]):
            return "debug"
        if any(
            word in combined for word in ["validate", "verify", "test", "testing", "qa"]
        ):
            return "validate"
        if any(word in combined for word in ["complete", "final", "outcome"]):
            return "complete"
        if any(word in combined for word in ["review", "audit"]):
            return "review"
        if any(
            word in combined for word in ["execute", "implement", "build", "create"]
        ):
            return "execute"
        return None

    @classmethod
    def _extract_scope_metadata(
        cls,
        title: str,
        description: str,
        *,
        allow_pipe_suffix: bool,
        allow_metadata_only: bool,
    ) -> tuple[Optional[str], Optional[str], str, str, bool, bool, bool]:
        """Extract only metadata in declared structural fields.

        Scope-looking text in prose is data. Supported authority is a
        trailing token in a Task heading title, a trailing pipe-delimited
        field in a Task row/inline heading description, or a heading's exact
        metadata-only first paragraph. The output description is changed only
        when one of those structural fields is recognized.
        """

        cleaned_title = title
        assignments = []
        invalid_scope_metadata = False
        title_field_re = re.compile(
            r"(?:\s*\|\s*|\s+)"
            r"(?P<field>(?:profile|mode|stage|workflow_stage|scope)"
            r"(?:\s*=\s*[^\s]*)?)\s*$",
            re.IGNORECASE,
        )
        while True:
            match = title_field_re.search(cleaned_title)
            if not match:
                break
            assignment = cls._parse_scope_field(match.group("field"))
            if assignment is None:
                invalid_scope_metadata = True
            else:
                assignments.append(assignment)
            cleaned_title = cleaned_title[: match.start()].rstrip(" |")

        cleaned_description = description
        if description:
            fields = description.split("|")
            parsed_fields = [cls._parse_scope_field(field) for field in fields]
            if allow_metadata_only and all(
                cls._is_exact_scope_field(field) for field in fields
            ):
                for field, assignment in zip(fields, parsed_fields):
                    if assignment is None:
                        invalid_scope_metadata = True
                    else:
                        assignments.append(assignment)
                cleaned_description = ""
            elif allow_pipe_suffix:
                suffix_start = len(fields)
                while suffix_start > 1:
                    field = fields[suffix_start - 1]
                    if cls._scope_field_key(field) is None:
                        break
                    assignment = cls._parse_scope_field(field)
                    if assignment is None:
                        invalid_scope_metadata = True
                    else:
                        assignments.append(assignment)
                    suffix_start -= 1
                if suffix_start < len(fields):
                    cleaned_description = "|".join(fields[:suffix_start]).rstrip()

        (
            profile,
            stage,
            explicit_stage_supplied,
            metadata_conflict,
        ) = cls._resolve_scope_metadata(assignments)
        return (
            profile,
            stage,
            cleaned_title or title,
            cleaned_description,
            explicit_stage_supplied,
            metadata_conflict,
            invalid_scope_metadata,
        )

    @staticmethod
    def _scope_field_key(field: str) -> Optional[str]:
        """Return a recognized key only when a structural field starts with it."""

        stripped = field.lstrip()
        if not stripped or stripped[0] in "`\"'>" or "`" in field:
            return None
        match = re.match(
            r"(profile|mode|stage|workflow_stage|scope)(?=\s*(?:=|$))",
            stripped,
            re.IGNORECASE,
        )
        return match.group(1).lower() if match else None

    @classmethod
    def _is_exact_scope_field(cls, field: str) -> bool:
        """Whether a first-paragraph pipe field is metadata-only syntax."""

        if cls._scope_field_key(field) is None:
            return False
        return bool(
            re.fullmatch(
                r"\s*(?:profile|mode|stage|workflow_stage|scope)"
                r"(?:\s*=\s*[^\s|]*)?\s*",
                field,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _parse_scope_field(
        cls, field: str
    ) -> Optional[tuple[str, Optional[str], Optional[str]]]:
        if "`" in field:
            return None
        match = re.fullmatch(
            r"\s*(profile|mode|stage|workflow_stage|scope)\s*=\s*"
            r"([A-Za-z_]+)\.?\s*",
            field,
            re.IGNORECASE,
        )
        if not match:
            return None

        key, raw_value = match.group(1).lower(), match.group(2).lower()
        profile_values = cls.EXECUTION_PROFILES | set(cls.EXECUTION_PROFILE_ALIASES)
        if key in {"profile", "mode"}:
            if raw_value not in profile_values:
                return None
            return (
                "profile",
                cls.EXECUTION_PROFILE_ALIASES.get(raw_value, raw_value),
                cls.SCOPE_STAGE_ALIASES.get(raw_value),
            )

        stage_values = cls.WORKFLOW_STAGES | profile_values
        if raw_value not in stage_values:
            return None
        if raw_value in cls.WORKFLOW_STAGES:
            return "stage", raw_value, None
        return "stage", cls.SCOPE_STAGE_ALIASES.get(raw_value), None

    @classmethod
    def _resolve_scope_metadata(
        cls, assignments: List[tuple[str, Optional[str], Optional[str]]]
    ) -> tuple[Optional[str], Optional[str], bool, bool]:
        profiles = {value for kind, value, _stage in assignments if kind == "profile"}
        profile_stages = [
            stage
            for kind, _value, stage in assignments
            if kind == "profile" and stage is not None
        ]
        stage_values = [value for kind, value, _stage in assignments if kind == "stage"]
        if (
            len(profiles) > 1
            or len(set(profile_stages)) > 1
            or len(set(stage_values)) > 1
        ):
            return None, None, bool(stage_values), True

        profile = next(iter(profiles), None)
        profile_stage = profile_stages[0] if profile_stages else None
        explicit_stage_supplied = bool(stage_values)
        explicit_stage = stage_values[0] if stage_values else None
        stage = explicit_stage if explicit_stage_supplied else profile_stage
        return profile, stage, explicit_stage_supplied, False

    @staticmethod
    def _remove_heading_metadata_line(
        description: str, metadata_source: str, cleaned_source: str
    ) -> str:
        if not metadata_source or cleaned_source:
            return description
        lines = description.splitlines()
        if not lines or lines[0].strip() != metadata_source.strip():
            return description
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines)

    @classmethod
    def _scope_inference_text(cls, text: str) -> str:
        """Keep examples, fences, acceptance criteria, and scope tokens out of inference."""

        lines = text.splitlines()
        fenced, _unclosed_from = cls._fence_mask(lines)
        visible_lines: List[str] = []
        in_acceptance_section = False
        for index, line in enumerate(lines):
            if fenced[index]:
                continue
            if cls._is_acceptance_content_heading(line):
                in_acceptance_section = True
            if in_acceptance_section:
                continue
            visible_lines.append(cls._strip_inline_code(line))

        visible_text = "\n".join(visible_lines)
        return re.sub(
            r"\b(?:profile|mode|stage|workflow_stage|scope)\s*=\s*" r"[A-Za-z_]+\.?",
            " ",
            visible_text,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _is_acceptance_content_heading(line: str) -> bool:
        candidate = line.strip()
        candidate = re.sub(r"^#{1,6}\s+", "", candidate)
        candidate = re.sub(r"^[-*+]\s*", "", candidate)
        candidate = candidate.strip("*_ ")
        return bool(ACCEPTANCE_HEADING_TEXT_RE.fullmatch(candidate))

    @staticmethod
    def _strip_inline_code(text: str) -> str:
        pattern = re.compile(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")

        def blank_code_span(match: re.Match) -> str:
            return re.sub(r"[^\n]", " ", match.group(0))

        return pattern.sub(blank_code_span, text)

    @classmethod
    def _build_objectives(
        cls, requirement: str, project_description: Optional[str]
    ) -> List[str]:
        objectives = list(cls.DEFAULT_OBJECTIVES)
        requirement_lower = requirement.lower()
        if "api" in requirement_lower:
            objectives.insert(1, "Define backend endpoints and payload contracts")
        if "ui" in requirement_lower or "frontend" in requirement_lower:
            objectives.insert(1, "Design the user workflow and interaction states")
        if "database" in requirement_lower or "schema" in requirement_lower:
            objectives.insert(
                1, "Identify required persistence and relationship changes"
            )
        if project_description:
            objectives.append(
                "Preserve the project's existing architecture and conventions"
            )
        return objectives[:5]

    @classmethod
    def _build_tasks(cls, requirement: str) -> List[str]:
        requirement_lower = requirement.lower()
        tasks = [
            (
                "Inspect current project architecture",
                "Enumerate the real workspace files first, then inspect the existing code paths, tests, fixtures, configs, data flow, and extension points relevant to the requirement. Treat the current project files as the source of truth and capture any gaps or bugs that must be fixed before implementation.",
            ),
            (
                "Implement the core changes",
                f"Build the main functionality needed to satisfy: {requirement.strip()}",
            ),
        ]

        if any(word in requirement_lower for word in ["ui", "frontend", "page", "tab"]):
            tasks.insert(
                1,
                (
                    "Create or update the user interface",
                    "Add the frontend interactions, states, and validation needed for the new workflow.",
                ),
            )
        if any(word in requirement_lower for word in ["api", "backend", "endpoint"]):
            tasks.insert(
                1,
                (
                    "Extend backend APIs",
                    "Add or update the server endpoints and request/response validation for the feature.",
                ),
            )
        if any(word in requirement_lower for word in ["database", "model", "schema"]):
            tasks.insert(
                1,
                (
                    "Update persistence models",
                    "Adjust the database schema and related models needed to store the new feature state.",
                ),
            )
        tasks.extend(
            [
                (
                    "Integrate the workflow with project execution",
                    "Connect the new behavior to existing project/session/task flows so it is ready for use.",
                ),
                (
                    "Verify and refine",
                    "Run targeted checks, confirm the UX, and address any integration issues.",
                ),
            ]
        )

        deduped: List[tuple[str, str]] = []
        seen_titles = set()
        for title, description in tasks:
            if title in seen_titles:
                continue
            seen_titles.add(title)
            deduped.append((title, description))

        return [
            f"- [ ] TASK_START: {title} | {description}"
            for title, description in deduped
        ]

    @classmethod
    def _estimate_effort(cls, title: str, description: str) -> str:
        combined = f"{title} {description}".lower()
        if any(
            word in combined
            for word in ["refactor", "architecture", "workflow", "integrate"]
        ):
            return "medium"
        if any(word in combined for word in ["verify", "test", "review", "inspect"]):
            return "small"
        return "medium"
