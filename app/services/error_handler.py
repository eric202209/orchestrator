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
            try:
                return True, json.loads(found), "Found JSON in text"
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON-PARSE] Strategy 5 failed: {e}")

        # All strategies failed
        error_msg = f"Failed to parse {context} after {self.max_retries} attempts"
        logger.error(f"[JSON-PARSE] All strategies failed. Last attempt: {text[:200]}")
        return False, None, error_msg

    def _clean_markdown_fences(self, text: str) -> str:
        """Remove Markdown code fences (```json, ```)"""
        pattern = r"^\s*```(?:json)?\s*|\s*```$"
        return re.sub(pattern, "", text.strip())

    def _extract_json_from_text(self, text: str) -> Optional[str]:
        """Extract JSON from mixed content (markdown, explanations, etc.)"""
        # Try to find JSON array
        array_match = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
        if array_match:
            return array_match.group(0)

        # Try to find JSON object
        obj_match = re.search(r'\{\s*"[^"]+"\s*:', text, re.DOTALL)
        if obj_match:
            # Find matching closing brace
            brace_count = 0
            start = obj_match.start()
            for i, char in enumerate(text[start:], start):
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        return text[start : i + 1]
            return None
        return None

    def _fix_common_json_errors(self, text: str) -> str:
        """Fix common JSON formatting errors"""
        fixed = text

        # Fix unescaped quotes in strings
        # Pattern: "key": "value with "unescaped" quotes"
        fixed = re.sub(
            r'":\s*"([^"]*(?:"[^"]*)*)"',
            lambda m: self._fix_unescaped_quotes(m.group(0)),
            fixed,
        )

        # Fix trailing commas
        fixed = re.sub(r",\s*([\]}])", r"\1", fixed)

        # Fix single quotes to double quotes (for keys)
        fixed = re.sub(r"'([^']*)':", r'"\1":', fixed)

        # Fix unquoted keys
        fixed = re.sub(r"(\w+):", r'"\1":', fixed)

        # Remove text before JSON starts
        if "{" in fixed:
            fixed = fixed[fixed.index("{") :]
        if "[" in fixed:
            fixed = fixed[fixed.index("[") :]

        return fixed

    def _fix_unescaped_quotes(self, match: str) -> str:
        """Fix unescaped quotes within JSON string values"""
        # This is a simplified fix - in production, you'd want more robust handling
        return match.replace('""', '\\"')

    def _find_json_in_text(self, text: str) -> Optional[str]:
        """Find and return JSON object/array from text"""
        # Look for balanced braces
        for i, char in enumerate(text):
            if char in "{[":
                # Try to parse from this position
                for j in range(len(text), i, -1):
                    candidate = text[i:j]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        continue
        return None

    def should_retry(self, error: Exception, step_name: str = "step") -> bool:
        """
        Determine if an error should be retried.

        Returns:
            True if retry is recommended, False otherwise
        """
        error_msg = str(error).lower()

        # Never retry these errors
        no_retry_errors = [
            "json decode error",
            "key error",
            "attribute error",
            "type error",
            "value error",
            "permission denied",
            "not found",
            "invalid",
        ]

        for pattern in no_retry_errors:
            if pattern in error_msg:
                logger.info(
                    f"[RETRY] Skipping retry for {pattern} error in {step_name}"
                )
                return False

        # Retry network/transient errors
        retry_errors = [
            "timeout",
            "connection",
            "network",
            "temporarily",
            "rate limit",
            "busy",
        ]

        for pattern in retry_errors:
            if pattern in error_msg:
                if self.retry_count < self.max_retries:
                    logger.info(
                        f"[RETRY] Will retry {step_name} (attempt {self.retry_count + 1}/{self.max_retries})"
                    )
                    return True
                else:
                    logger.warning(f"[RETRY] Max retries exceeded for {step_name}")
                    return False

        # Default: retry on first 2 attempts, then give up
        if self.retry_count < 2:
            return True

        logger.warning(
            f"[RETRY] Unrecognized error, giving up after {self.retry_count} attempts"
        )
        return False

    def create_error_recovery_plan(
        self, error: Exception, context: str
    ) -> Dict[str, Any]:
        """
        Create a recovery plan based on the error type.

        Returns:
            Dictionary with recovery strategy and steps
        """
        error_msg = str(error)
        error_type = type(error).__name__

        recovery_plan = {
            "error_type": error_type,
            "error_message": error_msg,
            "context": context,
            "recommended_action": "manual_intervention",
            "steps": [],
        }

        if "JSON" in error_type or "json" in error_msg.lower():
            recovery_plan["recommended_action"] = "retry_with_parsing"
            recovery_plan["steps"] = [
                "Attempt to clean and parse JSON",
                "Use alternative parsing strategies",
                "Extract JSON from mixed content if needed",
                "Log raw output for debugging",
            ]

        elif "timeout" in error_msg.lower():
            recovery_plan["recommended_action"] = "increase_timeout"
            recovery_plan["steps"] = [
                "Increase timeout limit",
                "Check if task is still running",
                "Consider breaking task into smaller steps",
            ]

        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            recovery_plan["recommended_action"] = "retry_with_delay"
            recovery_plan["steps"] = [
                "Wait for network to stabilize",
                "Retry with exponential backoff",
                "Check network connectivity",
            ]

        elif "permission" in error_msg.lower() or "access" in error_msg.lower():
            recovery_plan["recommended_action"] = "permission_check"
            recovery_plan["steps"] = [
                "Verify file permissions",
                "Check workspace access",
                "Ensure proper authentication",
            ]

        return recovery_plan
