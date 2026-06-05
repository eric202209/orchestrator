"""Slug formatting helpers for the tiny slug fixture."""


def slugify(value: str) -> str:
    """Return a URL slug for user-provided text."""
    return str(value).replace(" ", "-")
