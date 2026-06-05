"""HITL sentinel contract: render and parse the agent-emitted intervention sentinel.

Format: <<<HITL_REQUEST:{...}>>>
Both the agent prompt (render) and the output parser (parse) import from here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

_PREFIX = "<<<HITL_REQUEST:"
_SUFFIX = ">>>"
_RE = re.compile(r"<<<HITL_REQUEST:(\{.*?\})>>>", re.DOTALL)


def render(payload: Dict[str, Any]) -> str:
    """Wrap *payload* in HITL sentinel delimiters."""
    return f"{_PREFIX}{json.dumps(payload, separators=(',', ':'))}{_SUFFIX}"


def _coerce_output_text(output: Any) -> str:
    """Extract text-like content before applying the HITL sentinel regex."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(text for item in output if (text := _coerce_output_text(item)))
    if isinstance(output, dict):
        for key in ("text", "output_text", "output", "content", "message"):
            text = _coerce_output_text(output.get(key))
            if text:
                return text
        return ""
    return ""


def parse(output: Any) -> Optional[Dict[str, Any]]:
    """Return parsed payload dict if *output* contains HITL sentinel, else None."""
    m = _RE.search(_coerce_output_text(output))
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None
