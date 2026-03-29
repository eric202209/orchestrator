"""Enhanced Error Handling for Orchestrator

Provides intelligent error recovery, retry logic, and JSON parsing improvements.
"""

import json
import logging
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

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
            return True, json.loads(text), ""
        except json.JSONDecodeError as e:
            logger.debug(f"[JSON-PARSE] Strategy 1 failed: {e}")

        # Strategy 2: Clean markdown code fences
        cleaned = self._clean_markdown_fences(text)
        if cleaned != text:
            try:
                return True, json.loads(cleaned), "Cleaned markdown fences"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 2 failed: {e}")

        # Strategy 3: Extract JSON from mixed content
        extracted = self._extract_json_from_text(text)
        if extracted:
            try:
                return True, json.loads(extracted), "Extracted from mixed content"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 3 failed: {e}")

        # Strategy 4: Fix common JSON errors
        fixed = self._fix_common_json_errors(text)
        if fixed != text:
            try:
                return True, json.loads(fixed), "Fixed common errors"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 4 failed: {e}")

        # Strategy 5: Try to find JSON array/object in text
        found = self._find_json_in_text(text)
        if found:
            # Try direct parsing first
            try:
                return True, json.loads(found), "Found JSON in text"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 5 direct failed: {e}")

            # If failed, try fixing errors in found JSON
            fixed = self._fix_common_json_errors(found)
            if fixed != found:
                try:
                    return True, json.loads(fixed), "Found and fixed JSON in text"
                except json.JSONDecodeError as e:
                    logger.debug(f"[JSON-PARSE] Strategy 5 fixed failed: {e}")

        # All strategies failed
        error_msg = f"Failed to parse {context} after {self.max_retries} attempts"
        logger.error(f"[JSON-PARSE] All strategies failed. Last attempt: {text[:200]}")
        return False, None, error_msg

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

        # Fix missing commas between array/object elements
        fixed = re.sub(r"\}\s*\{", "},{", fixed)
        fixed = re.sub(r"\}\s*,?\s*\[", "},[", fixed)
        fixed = re.sub(r"\]\s*,?\s*\{", "},{", fixed)

        # Fix trailing commas (remove them)
        fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)

        # Fix single quotes to double quotes (carefully)
        fixed = re.sub(r"'([^']*)'", r'"\1"', fixed)

        if fixed != text:
            logger.debug(f"[JSON-FIX] Applied {len(text) - len(fixed)} fixes")

        return fixed

    def _find_json_in_text(self, text: str) -> Optional[str]:
        """Find complete JSON array or object in text."""
        if not text:
            return None

        # Try to find start of JSON
        json_start = text.find("{")
        if json_start == -1:
            json_start = text.find("[")

        if json_start == -1:
            return None

        # Try to find matching end
        brace_count = 0
        bracket_count = 0
        in_string = False
        escape_next = False

        for i, char in enumerate(text[json_start:], json_start):
            if escape_next:
                escape_next = False
                continue

            if char == "\\" and in_string:
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
            elif char == "[":
                bracket_count += 1
            elif char == "]":
                bracket_count -= 1

            # Check if we've found a complete JSON structure
            if brace_count == 0 and bracket_count == 0 and i > json_start:
                return text[json_start : i + 1]

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
