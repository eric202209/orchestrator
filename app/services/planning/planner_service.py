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


@dataclass
class ParsedPlannerTask:
    title: str
    description: str
    execution_profile: str = "full_lifecycle"
    priority: int = 0
    plan_position: Optional[int] = None
    estimated_effort: Optional[str] = None


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
        markdown = (markdown or "").strip()
        if not markdown:
            return []

        task_section = cls._extract_task_section(markdown)
        parsed_tasks: List[ParsedPlannerTask] = []
        seen_keys: set[str] = set()

        for extracted_index, (title, description) in enumerate(
            cls._extract_candidate_tasks(task_section), start=1
        ):
            normalized_title = re.sub(r"\s+", " ", title or "").strip(" :-")
            normalized_description = re.sub(r"\s+", " ", (description or "").strip())
            if not normalized_title:
                continue

            explicit_profile, normalized_title, normalized_description = (
                cls._extract_profile_metadata(normalized_title, normalized_description)
            )

            dedupe_key = normalized_title.lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            inferred_position = cls._infer_plan_position(
                normalized_title, normalized_description, extracted_index
            )
            inferred_priority = cls._infer_priority(
                normalized_title, normalized_description
            )
            parsed_tasks.append(
                ParsedPlannerTask(
                    title=normalized_title,
                    description=normalized_description
                    or f"Implement {normalized_title.lower()}",
                    execution_profile=explicit_profile
                    or cls._infer_execution_profile(
                        normalized_title, normalized_description
                    ),
                    priority=inferred_priority,
                    plan_position=inferred_position,
                    estimated_effort=cls._estimate_effort(
                        normalized_title, normalized_description
                    ),
                )
            )

        return parsed_tasks

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
    def _extract_profile_metadata(
        cls, title: str, description: str
    ) -> tuple[Optional[str], str, str]:
        combined = f"{title} {description}"
        match = re.search(
            r"\b(?:profile|mode)\s*=\s*(full_lifecycle|execute_only|test_only|debug_only|review_only)\b",
            combined,
            re.IGNORECASE,
        )
        if not match:
            return None, title, description

        profile = match.group(1).lower()
        if profile not in cls.EXECUTION_PROFILES:
            return None, title, description

        cleaned_title = re.sub(
            r"\b(?:profile|mode)\s*=\s*(full_lifecycle|execute_only|test_only|debug_only|review_only)\b",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip(" |-")
        cleaned_description = re.sub(
            r"\b(?:profile|mode)\s*=\s*(full_lifecycle|execute_only|test_only|debug_only|review_only)\b",
            "",
            description,
            flags=re.IGNORECASE,
        ).strip(" |-")
        return profile, cleaned_title or title, cleaned_description or description

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
