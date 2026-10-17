"""Attempt-bound identity for A1 execution backends.

The identity is a value object, not an ownership record: the future SQLite
Attempt ledger remains the sole authority for issuance, claim, and capacity
(contract §§7–9).  Identifiers are server-generated opaque references; they
must never carry paths or content-derived values (contract §12.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Opaque, server-generated reference grammar (contract §12.2 rule 3): no path
# separators, whitespace, or free-form text.
OPAQUE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")


class AttemptIdentityMismatch(ValueError):
    """An operation or observation targeted a different Attempt identity."""


def validate_opaque_ref(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not OPAQUE_REF_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be an opaque server-generated reference")
    return value


@dataclass(frozen=True)
class A1AttemptIdentity:
    """Identity of one issued A1 Attempt as seen by an execution backend.

    ``claim_generation`` fences every backend operation to the claim that
    owns the Attempt; a stale generation is a different identity.
    """

    attempt_id: str
    dispatch_id: str
    claim_generation: int

    def __post_init__(self) -> None:
        validate_opaque_ref(self.attempt_id, "attempt_id")
        validate_opaque_ref(self.dispatch_id, "dispatch_id")
        if type(self.claim_generation) is not int or self.claim_generation < 1:
            raise ValueError("claim_generation must be a positive integer")


def require_same_attempt(
    expected: A1AttemptIdentity, observed: A1AttemptIdentity
) -> None:
    """Raise unless ``observed`` is exactly the ``expected`` Attempt identity."""

    if expected != observed:
        raise AttemptIdentityMismatch(
            "observation does not belong to the bound Attempt identity"
        )
