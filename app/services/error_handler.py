"""Enhanced Error Handling for Orchestrator

Provides intelligent error recovery, retry logic, and JSON parsing improvements.
"""

import json
import logging
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

from app.services.orchestration.validation.parsing import (
    _find_json_substring,
    _strip_markdown_fences,
    _should_skip_nested_non_plan_candidate,
)

logger = logging.getLogger(__name__)


class EnhancedErrorHandler:
    """Intelligent error handling and recovery for task execution."""

    def __init__(self, max_retries: int = 3, retry_delay: int = 60):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.retry_count = 0

    def attempt_json_parsing(
        self, text: str, context: str = "JSON"
    ) -> Tuple[bool, Any, str]:
        """
        Attempt to parse JSON with multiple recovery strategies.

        Returns:
            Tuple of (success, parsed_data, error_message)
        """
        if not text or not text.strip():
            return False, None, "Empty or whitespace-only input"

        # Strategy 1: Direct JSON parsing
        try:
            parsed = json.loads(text)
            if not self._should_skip_non_plan_candidate_for_planning(
                context, text, text, parsed
            ):
                return True, parsed, ""
        except json.JSONDecodeError as e:
            logger.debug(f"[JSON-PARSE] Strategy 1 failed: {e}")

        # Strategy 2: Clean markdown code fences
        cleaned = self._clean_markdown_fences(text)
        if cleaned != text:
            try:
                parsed_cleaned = json.loads(cleaned)
                if not self._should_skip_non_plan_candidate_for_planning(
                    context, text, cleaned, parsed_cleaned
                ):
                    return True, parsed_cleaned, "Cleaned markdown fences"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 2 failed: {e}")
            fixed_cleaned = self._fix_common_json_errors(cleaned)
            if fixed_cleaned != cleaned:
                try:
                    parsed_fixed_cleaned = json.loads(fixed_cleaned)
                    if self._should_skip_non_plan_candidate_for_planning(
                        context, text, fixed_cleaned, parsed_fixed_cleaned
                    ):
                        raise json.JSONDecodeError(
                            "non-plan JSON candidate", fixed_cleaned, 0
                        )
                    return (
                        True,
                        parsed_fixed_cleaned,
                        "Cleaned markdown fences and fixed common errors",
                    )
                except json.JSONDecodeError as e:
                    logger.debug(f"[JSON-PARSE] Strategy 2 fixed failed: {e}")

        # Strategy 3: Extract JSON from mixed content
        extracted = self._extract_json_from_text(text)
        if extracted:
            try:
                parsed_extracted = json.loads(extracted)
                if not _should_skip_nested_non_plan_candidate(
                    text, extracted, parsed_extracted
                ) and not self._should_skip_non_plan_candidate_for_planning(
                    context, text, extracted, parsed_extracted
                ):
                    return True, parsed_extracted, "Extracted from mixed content"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 3 failed: {e}")

        # Strategy 4: Fix common JSON errors
        fixed = self._fix_common_json_errors(text)
        if fixed != text:
            try:
                parsed_fixed = json.loads(fixed)
                if not self._should_skip_non_plan_candidate_for_planning(
                    context, text, fixed, parsed_fixed
                ):
                    return True, parsed_fixed, "Fixed common errors"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 4 failed: {e}")

        # Strategy 5: Try to find JSON array/object in text
        found = self._find_json_in_text(text)
        if found:
            # Try direct parsing first
            try:
                parsed_found = json.loads(found)
                if not self._should_skip_non_plan_candidate_for_planning(
                    context, text, found, parsed_found
                ):
                    return True, parsed_found, "Found JSON in text"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 5 direct failed: {e}")

            # If failed, try fixing errors in found JSON
            fixed = self._fix_common_json_errors(found)
            if fixed != found:
                try:
                    parsed_fixed_found = json.loads(fixed)
                    if not self._should_skip_non_plan_candidate_for_planning(
                        context, text, fixed, parsed_fixed_found
                    ):
                        return (
                            True,
                            parsed_fixed_found,
                            "Found and fixed JSON in text",
                        )
                except json.JSONDecodeError as e:
                    logger.debug(f"[JSON-PARSE] Strategy 5 fixed failed: {e}")

        # Strategy 6: Extract double-encoded or field-wrapped JSON payloads
        embedded = self._extract_embedded_json_payload(text)
        if embedded:
            try:
                parsed_embedded = json.loads(embedded)
                if not self._should_skip_non_plan_candidate_for_planning(
                    context, text, embedded, parsed_embedded
                ):
                    return (
                        True,
                        parsed_embedded,
                        "Extracted embedded JSON payload",
                    )
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 6 direct failed: {e}")

            fixed = self._fix_common_json_errors(embedded)
            if fixed != embedded:
                try:
                    parsed_fixed_embedded = json.loads(fixed)
                    if self._should_skip_non_plan_candidate_for_planning(
                        context, text, fixed, parsed_fixed_embedded
                    ):
                        raise json.JSONDecodeError("non-plan JSON candidate", fixed, 0)
                    return (
                        True,
                        parsed_fixed_embedded,
                        "Extracted and fixed embedded JSON payload",
                    )
                except json.JSONDecodeError as e:
                    logger.debug(f"[JSON-PARSE] Strategy 6 fixed failed: {e}")

        # All strategies failed
        error_msg = f"Failed to parse {context} after {self.retry_count + 1} attempts"
        logger.error(f"[JSON-PARSE] All strategies failed. Last attempt: {text[:200]}")
        return False, None, error_msg

    def _should_skip_non_plan_candidate_for_planning(
        self, context: str, source_text: str, candidate_text: str, parsed: Any
    ) -> bool:
        if context != "planning":
            return False

        source = _strip_markdown_fences(str(source_text or "")).lstrip()
        candidate = _strip_markdown_fences(str(candidate_text or "")).lstrip()
        source_looks_like_plan = source.startswith("[") or bool(
            re.search(r'"(?:step_number|commands|description)"\s*:', source[:3000])
        )
        if not source_looks_like_plan:
            return False

        if source == candidate:
            return False

        return not self._parsed_value_has_plan_shape(parsed)

    @staticmethod
    def _parsed_value_has_plan_shape(parsed: Any) -> bool:
        step_keys = {"step_number", "commands", "description"}
        if isinstance(parsed, dict):
            return bool(step_keys.intersection(parsed.keys()))
        if isinstance(parsed, list) and parsed:
            return isinstance(parsed[0], dict) and bool(
                step_keys.intersection(parsed[0].keys())
            )
        return False

    def _clean_markdown_fences(self, text: str) -> str:
        """Remove markdown code fences and extract JSON."""
        if not text:
            return text

        # Remove ```json or ``` wrappers
        pattern = r"^\s*```(?:json)?\s*|\s*```$"
        cleaned = re.sub(pattern, "", text.strip())
        return cleaned

    def _extract_json_from_text(self, text: str) -> Optional[str]:
        """Extract JSON from mixed content using regex."""
        if not text:
            return None

        # Try to find JSON array or object
        json_patterns = [
            r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}",  # Match nested objects
            r"\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\]",  # Match nested arrays
        ]

        for pattern in json_patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            if matches:
                # Return the longest match (most likely to be complete)
                return max(matches, key=len)

        return None

    def _fix_common_json_errors(self, text: str) -> str:
        """Fix common JSON formatting errors."""
        if not text:
            return text

        fixed = text

        fixed = self._normalize_markdown_links(fixed)
        fixed = self._repair_plan_like_json_strings(fixed)

        # Fix missing commas between array/object elements
        fixed = re.sub(r"\}\s*\{", "},{", fixed)
        fixed = re.sub(r"\}\s*,?\s*\[", "},[", fixed)
        fixed = re.sub(r"\]\s*,?\s*\{", "},{", fixed)

        # Fix trailing commas (remove them)
        fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)

        if fixed != text:
            logger.debug(f"[JSON-FIX] Applied {len(text) - len(fixed)} fixes")

        return fixed

    def _normalize_markdown_links(self, text: str) -> str:
        """Collapse markdown/autolink localhost artifacts back to plain URLs."""
        if not text:
            return text

        fixed = text
        fixed = re.sub(
            r"\[\]\((https?://[^)\s]+)\)<[^>]+>",
            r"\1",
            fixed,
        )
        fixed = re.sub(
            r"\[([^\]]*)\]\((https?://[^)\s]+)\)",
            r"\2",
            fixed,
        )
        fixed = re.sub(r"<(https?://[^>\s]+)>", r"\1", fixed)
        return fixed

    def _repair_plan_like_json_strings(self, text: str) -> str:
        """Repair common quote corruption in plan JSON string fields."""
        if not text:
            return text

        fixed = text

        def escape_inner_quotes(value: str) -> str:
            return re.sub(r'(?<!\\)"', r'\\"', value)

        def repair_string_field(match: re.Match[str]) -> str:
            prefix, value, suffix = match.groups()
            return prefix + escape_inner_quotes(value) + suffix

        for key in ("description", "verification", "rollback"):
            pattern = (
                rf'("{key}"\s*:\s*")'
                r"(.*?)"
                r'("(?=\s*,\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:|\s*}))'
            )
            fixed = re.sub(pattern, repair_string_field, fixed, flags=re.DOTALL)

        commands_pattern = (
            r'("commands"\s*:\s*\[)(.*?)(\](?=\s*,\s*"verification"\s*:))'
        )

        def repair_commands_block(match: re.Match[str]) -> str:
            prefix, body, suffix = match.groups()
            chars = list(body)
            result: list[str] = []
            in_string = False
            escape_next = False
            i = 0
            length = len(chars)

            while i < length:
                char = chars[i]
                if not in_string:
                    result.append(char)
                    if char == '"':
                        in_string = True
                    i += 1
                    continue

                if escape_next:
                    result.append(char)
                    escape_next = False
                    i += 1
                    continue

                if char == "\\":
                    result.append(char)
                    escape_next = True
                    i += 1
                    continue

                if char == '"':
                    j = i + 1
                    while j < length and chars[j].isspace():
                        j += 1
                    if j >= length or chars[j] in ",]":
                        result.append(char)
                        in_string = False
                    else:
                        result.append('\\"')
                    i += 1
                    continue

                result.append(char)
                i += 1

            return prefix + "".join(result) + suffix

        fixed = re.sub(commands_pattern, repair_commands_block, fixed, flags=re.DOTALL)
        fixed = self._repair_ops_content_strings(fixed)
        return fixed

    def _repair_ops_content_strings(self, text: str) -> str:
        """Escape unescaped quotes inside ops[].content string values."""

        key_pattern = '"content"'
        result: list[str] = []
        cursor = 0

        while True:
            key_index = text.find(key_pattern, cursor)
            if key_index < 0:
                result.append(text[cursor:])
                break

            colon_index = text.find(":", key_index + len(key_pattern))
            if colon_index < 0:
                result.append(text[cursor:])
                break

            value_start = colon_index + 1
            while value_start < len(text) and text[value_start].isspace():
                value_start += 1

            if value_start >= len(text) or text[value_start] != '"':
                result.append(text[cursor:value_start])
                cursor = value_start
                continue

            closing_quote = self._find_plan_string_closing_quote(text, value_start)
            if closing_quote is None:
                result.append(text[cursor:])
                break

            raw_value = text[value_start + 1 : closing_quote]
            result.append(text[cursor : value_start + 1])
            result.append(re.sub(r'(?<!\\)"', r'\\"', raw_value))
            result.append('"')
            cursor = closing_quote + 1

        return "".join(result)

    @staticmethod
    def _find_plan_string_closing_quote(text: str, value_start: int) -> Optional[int]:
        escape_next = False
        idx = value_start + 1
        while idx < len(text):
            char = text[idx]
            if escape_next:
                escape_next = False
                idx += 1
                continue
            if char == "\\":
                escape_next = True
                idx += 1
                continue
            if char != '"':
                idx += 1
                continue

            next_nonspace = idx + 1
            while next_nonspace < len(text) and text[next_nonspace].isspace():
                next_nonspace += 1
            if next_nonspace >= len(text):
                return idx

            if text[next_nonspace] == "}":
                after_object = next_nonspace + 1
                while after_object < len(text) and text[after_object].isspace():
                    after_object += 1
                if after_object >= len(text) or text[after_object] in ",]":
                    return idx

            idx += 1

        return None

    def _find_json_in_text(self, text: str) -> Optional[str]:
        """Find complete JSON array or object in text."""
        if not text:
            return None

        return _find_json_substring(text)

    def _extract_embedded_json_payload(self, text: str) -> Optional[str]:
        """Recover JSON that was embedded as an escaped string inside another payload."""
        if not text:
            return None

        field_patterns = [
            r'"finalAssistantVisibleText"\s*:\s*"((?:\\.|[^"])*)"',
            r'"text"\s*:\s*"((?:\\.|[^"])*)"',
            r'"output_text"\s*:\s*"((?:\\.|[^"])*)"',
        ]
        for pattern in field_patterns:
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            extracted = match.group(1)
            try:
                decoded = json.loads(f'"{extracted}"')
            except json.JSONDecodeError:
                continue
            cleaned = self._clean_markdown_fences(decoded)
            found = self._find_json_in_text(cleaned)
            if found:
                return found
            stripped = cleaned.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                return stripped

        stripped = text.strip()
        if (
            len(stripped) >= 2
            and stripped[0] == stripped[-1] == '"'
            and any(token in stripped for token in ('\\"', "\\n", "\\t"))
        ):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            cleaned = self._clean_markdown_fences(decoded)
            found = self._find_json_in_text(cleaned)
            if found:
                return found
            stripped_cleaned = cleaned.strip()
            if stripped_cleaned.startswith("{") or stripped_cleaned.startswith("["):
                return stripped_cleaned

        return None

    def should_retry(self, error: Exception, step_name: str = "step") -> bool:
        """Determine if an error should be retried."""
        error_str = str(error).lower()

        # Don't retry certain errors
        no_retry_errors = [
            "timeout",
            "permission denied",
            "not found",
            "invalid json",
            "empty response",
            "connection refused",
        ]

        for pattern in no_retry_errors:
            if pattern in error_str:
                logger.warning(f"[RETRY] Skipping retry for: {pattern}")
                return False

        # Retry transient errors
        if self.retry_count < self.max_retries:
            logger.info(
                f"[RETRY] Attempt {self.retry_count + 1}/{self.max_retries} for {step_name}"
            )
            return True

        logger.warning(
            f"[RETRY] Max retries ({self.max_retries}) exceeded for {step_name}"
        )
        return False

    def create_retry_error(
        self, original_error: Exception, step_name: str = "step"
    ) -> Exception:
        """Create a retry error with context."""
        self.retry_count += 1
        return RuntimeError(
            f"{step_name} failed: {str(original_error)}. "
            f"Retry {self.retry_count}/{self.max_retries} in {self.retry_delay}s"
        )


# Singleton instance for reuse
error_handler = EnhancedErrorHandler()
