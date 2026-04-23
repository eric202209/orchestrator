"""Backend-specific prompt renderers."""

from __future__ import annotations

import json

from app.services.model_adaptation.schemas import PromptEnvelope


def render_openclaw_prompt(envelope: PromptEnvelope) -> str:
    """Render a neutral prompt envelope into the current OpenClaw text format."""

    sections = [
        f"Objective:\n{envelope.objective.strip()}",
        f"Execution Mode:\n{envelope.execution_mode.strip()}",
    ]
    if envelope.instructions:
        sections.append(
            "Instructions:\n"
            + "\n".join(f"- {instruction}" for instruction in envelope.instructions)
        )
    if envelope.context:
        context_lines = [
            f"- {key}: {value}"
            for key, value in envelope.context.items()
            if value is not None
        ]
        if context_lines:
            sections.append("Context:\n" + "\n".join(context_lines))
    if envelope.expected_output:
        sections.append(f"Expected Output:\n{envelope.expected_output.strip()}")
    if envelope.prompt_body:
        sections.append(f"Prompt Body:\n{envelope.prompt_body.strip()}")
    return "\n\n".join(sections)


def render_openai_responses_prompt(envelope: PromptEnvelope) -> str:
    """Render a neutral prompt envelope as a compact JSON-style input payload."""

    payload = {
        "objective": envelope.objective.strip(),
        "execution_mode": envelope.execution_mode.strip(),
        "instructions": [
            item.strip() for item in envelope.instructions if item.strip()
        ],
        "context": {
            key: value for key, value in envelope.context.items() if value is not None
        },
        "expected_output": (envelope.expected_output or "").strip(),
        "prompt_body": (envelope.prompt_body or "").strip(),
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
