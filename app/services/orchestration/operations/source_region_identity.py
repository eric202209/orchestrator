"""Strict, provider-free identity for one source region."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from app.services.orchestration.validation.path_authority import CanonicalPath

SOURCE_REGION_SCHEMA_VERSION = "source-region/1"
SOURCE_REGION_DERIVATION_KIND = "exact_region"
SOURCE_REGION_SELECTOR_KEYS = frozenset(
    {
        "schema_version",
        "canonical_path",
        "expected_source_version",
        "start_byte",
        "end_byte",
        "selected_region_sha256",
        "derivation_kind",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceRegionIdentityError(ValueError):
    """A malformed or unsupported source-region selector."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _canonical_selector_payload(
    *,
    schema_version: str,
    canonical_path: str,
    expected_source_version: str,
    start_byte: int,
    end_byte: int,
    selected_region_sha256: str,
    derivation_kind: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "canonical_path": canonical_path,
        "expected_source_version": expected_source_version,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "selected_region_sha256": selected_region_sha256,
        "derivation_kind": derivation_kind,
    }


@dataclass(frozen=True)
class SourceRegionIdentity:
    """Immutable identity of one exact UTF-8 byte region in one source file."""

    schema_version: str
    canonical_path: CanonicalPath
    expected_source_version: str
    start_byte: int
    end_byte: int
    selected_region_sha256: str
    derivation_kind: str

    def __post_init__(self) -> None:
        from app.services.orchestration.validation.path_authority import CanonicalPath

        if self.schema_version != SOURCE_REGION_SCHEMA_VERSION:
            raise SourceRegionIdentityError(
                "unsupported_schema_version",
                f"unsupported source-region schema: {self.schema_version!r}",
            )
        if not isinstance(self.canonical_path, CanonicalPath):
            raise SourceRegionIdentityError(
                "canonical_path_invalid", "canonical_path must be declared"
            )
        if (
            not isinstance(self.expected_source_version, str)
            or not self.expected_source_version
            or any(
                ord(char) < 32 or ord(char) == 127
                for char in self.expected_source_version
            )
        ):
            raise SourceRegionIdentityError(
                "expected_source_version_invalid",
                "expected_source_version must be a non-empty safe string",
            )
        if isinstance(self.start_byte, bool) or not isinstance(self.start_byte, int):
            raise SourceRegionIdentityError(
                "offset_invalid", "start_byte must be an integer"
            )
        if isinstance(self.end_byte, bool) or not isinstance(self.end_byte, int):
            raise SourceRegionIdentityError(
                "offset_invalid", "end_byte must be an integer"
            )
        if self.start_byte < 0 or self.end_byte < 0 or self.start_byte > self.end_byte:
            raise SourceRegionIdentityError(
                "offset_invalid",
                "region offsets must satisfy 0 <= start_byte <= end_byte",
            )
        if not isinstance(self.selected_region_sha256, str) or not _SHA256_RE.fullmatch(
            self.selected_region_sha256
        ):
            raise SourceRegionIdentityError(
                "region_hash_invalid", "selected_region_sha256 must be lowercase sha256"
            )
        if self.derivation_kind != SOURCE_REGION_DERIVATION_KIND:
            raise SourceRegionIdentityError(
                "unsupported_derivation_kind",
                f"unsupported derivation_kind: {self.derivation_kind!r}",
            )

    @classmethod
    def from_dict(cls, payload: Any) -> "SourceRegionIdentity":
        if not isinstance(payload, Mapping):
            raise SourceRegionIdentityError(
                "selector_invalid", "selector must be an object"
            )
        keys = set(payload.keys())
        missing = SOURCE_REGION_SELECTOR_KEYS - keys
        extra = keys - SOURCE_REGION_SELECTOR_KEYS
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing={sorted(missing)}")
            if extra:
                detail.append(f"unknown={sorted(extra)}")
            raise SourceRegionIdentityError(
                "selector_shape_invalid",
                "source-region selector keys invalid: " + ", ".join(detail),
            )
        try:
            from app.services.orchestration.validation.path_authority import (
                PathDeclarationError,
                declare,
            )

            canonical_path = declare(payload["canonical_path"])
        except PathDeclarationError as exc:
            raise SourceRegionIdentityError("canonical_path_invalid", str(exc)) from exc
        return cls(
            schema_version=payload["schema_version"],
            canonical_path=canonical_path,
            expected_source_version=payload["expected_source_version"],
            start_byte=payload["start_byte"],
            end_byte=payload["end_byte"],
            selected_region_sha256=payload["selected_region_sha256"],
            derivation_kind=payload["derivation_kind"],
        )

    def to_dict(self) -> dict[str, Any]:
        return _canonical_selector_payload(
            schema_version=self.schema_version,
            canonical_path=self.canonical_path.value,
            expected_source_version=self.expected_source_version,
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            selected_region_sha256=self.selected_region_sha256,
            derivation_kind=self.derivation_kind,
        )

    @property
    def selector_identity(self) -> str:
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def from_region(
        cls,
        *,
        canonical_path: str | CanonicalPath,
        expected_source_version: str,
        start_byte: int,
        end_byte: int,
        selected_region_sha256: str,
        derivation_kind: str = SOURCE_REGION_DERIVATION_KIND,
    ) -> "SourceRegionIdentity":
        from app.services.orchestration.validation.path_authority import CanonicalPath

        path = (
            canonical_path
            if isinstance(canonical_path, CanonicalPath)
            else _declare_path(canonical_path)
        )
        return cls(
            schema_version=SOURCE_REGION_SCHEMA_VERSION,
            canonical_path=path,
            expected_source_version=expected_source_version,
            start_byte=start_byte,
            end_byte=end_byte,
            selected_region_sha256=selected_region_sha256,
            derivation_kind=derivation_kind,
        )


def _declare_path(value: Any) -> "CanonicalPath":
    from app.services.orchestration.validation.path_authority import declare

    return declare(value)
