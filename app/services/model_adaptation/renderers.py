"""Backend-specific prompt renderers."""

from __future__ import annotations

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
    return "\n\n".join(sections)
