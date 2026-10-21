"""Offline, test-only durable claims for Worker release v2 approvals.

This module is deliberately not wired into a runtime or promotion executor.
Its filesystem store only accepts an existing, private directory beneath the
system temporary directory, so it cannot serve as the future production
authority store.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from . import release_entrypoint

UTC = dt.timezone.utc
CLAIM_SCHEMA_VERSION = 1
CLAIMED = "CLAIMED"
OPERATION_STARTED = "OPERATION_STARTED"
OPERATION_SUCCEEDED = "OPERATION_SUCCEEDED"
OPERATION_FAILED = "OPERATION_FAILED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

NO_CLAIM = "NO_CLAIM"
CLAIMED_NOT_STARTED = "CLAIMED_NOT_STARTED"
CLAIM_INVALID = "CLAIM_INVALID"

_LEGAL_TRANSITIONS = {
    CLAIMED: frozenset({OPERATION_STARTED}),
    OPERATION_STARTED: frozenset(
        {OPERATION_SUCCEEDED, OPERATION_FAILED, OUTCOME_UNKNOWN}
    ),
    OPERATION_SUCCEEDED: frozenset(),
    OPERATION_FAILED: frozenset(),
    OUTCOME_UNKNOWN: frozenset(),
}
_APPROVAL_ID_RE = re.compile(r"P39-P0B-WR2-[A-Z0-9][A-Z0-9-]{4,62}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_RELEASE_ID_RE = re.compile(r"wr2-[0-9a-f]{64}\Z")
_OPERATION_ID_PREFIX = "sha256:"
_IDENTITY_FIELDS = (
    "claim_schema_version",
    "approval_id",
    "approval_schema",
    "approval_key_id",
    "signed_operator_identity",
    "approval_purpose",
    "promotion_action",
    "execution_artifact_digest",
    "release_id",
    "canonical_manifest_sha256",
    "approved_configuration_profile",
    "approval_valid_from",
    "approval_valid_until",
)
_RECORD_FIELDS = frozenset(
    {
        *_IDENTITY_FIELDS,
        "claim_id",
        "claim_created_at",
        "claim_operation_identity",
        "claim_state",
        "state_history",
    }
)
_APPROVAL_SCHEMA_FIELDS = frozenset(
    {
        "envelope_kind",
        "envelope_schema_version",
        "subject_kind",
        "subject_schema_version",
    }
)
_PROFILE_FIELDS = frozenset({"policy_id", "profile_reference"})
_PROFILE_REFERENCE_FIELDS = frozenset({"path", "sha256"})
_HISTORY_EVENT_FIELDS = frozenset({"state", "at"})
_MAX_RECORD_BYTES = 64 * 1024


class PromotionClaimError(RuntimeError):
    """Base error for offline promotion claim contract failures."""


class ClaimValidationError(PromotionClaimError):
    """The requested approval identity or claim ID is malformed."""


class ClaimStoreConfigurationError(PromotionClaimError):
    """The reference store was not configured with a safe temporary root."""


class ClaimCollisionError(PromotionClaimError):
    """An approval ID already has a claim or colliding record."""


class ClaimRecordInvalid(PromotionClaimError):
    """A persisted claim is invalid, inconsistent, or unsafe to use."""


class ClaimTransitionError(PromotionClaimError):
    """A requested state transition is illegal or does not match the record."""


class ClaimPersistenceUncertain(PromotionClaimError):
    """A filesystem operation failed after persistence may have begun."""


class _DuplicateJsonField(ValueError):
    pass


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ClaimValidationError("claim canonical serialization failed") from exc


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonField("duplicate JSON field")
        value[key] = item
    return value


def _parse_canonical_record(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_RECORD_BYTES:
        raise ClaimRecordInvalid("claim_record_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(
                ValueError("floats are not permitted")
            ),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("nonstandard numbers are not permitted")
            ),
        )
    except (_DuplicateJsonField, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ClaimRecordInvalid("claim_record_json_invalid") from exc
    if not isinstance(value, dict):
        raise ClaimRecordInvalid("claim_record_root_invalid")
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ClaimRecordInvalid("claim_record_value_invalid") from exc
    if canonical != raw:
        raise ClaimRecordInvalid("claim_record_noncanonical")
    return value


def _format_utc(value: dt.datetime) -> str:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ClaimValidationError("claim_timestamp_must_be_timezone_aware")
    if value.utcoffset() is None:
        raise ClaimValidationError("claim_timestamp_must_be_timezone_aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: Any, *, record: bool) -> dt.datetime:
    error = ClaimRecordInvalid if record else ClaimValidationError
    if not isinstance(value, str) or not value.endswith("Z"):
        raise error("claim_timestamp_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise error("claim_timestamp_invalid") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise error("claim_timestamp_not_utc")
    expected = parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    if expected != value:
        raise error("claim_timestamp_noncanonical")
    return parsed.astimezone(UTC)


def _normalize_verified_utc(value: Any) -> str:
    if not isinstance(value, str):
        raise ClaimValidationError("claim_approval_timestamp_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClaimValidationError("claim_approval_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ClaimValidationError("claim_approval_timestamp_not_utc")
    return _format_utc(parsed)


def _identity_string(value: Any, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ClaimValidationError(f"claim_{field}_invalid")
    return value


def _claim_id_for_approval(approval_id: Any) -> str:
    if not isinstance(approval_id, str) or not _APPROVAL_ID_RE.fullmatch(approval_id):
        raise ClaimValidationError("claim_approval_id_invalid")
    # The storage key is approval-ID scoped across operation and schema changes.
    # The full operation identity is separately hashed into the record.
    material = b"phase39-p0b-approval-id/v1\0" + approval_id.encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _operation_identity(record: Mapping[str, Any]) -> str:
    identity = {field: record[field] for field in _IDENTITY_FIELDS}
    digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    return _OPERATION_ID_PREFIX + digest


def _validate_schema_identity(value: Any, *, record: bool) -> None:
    error = ClaimRecordInvalid if record else ClaimValidationError
    if not isinstance(value, dict) or set(value) != _APPROVAL_SCHEMA_FIELDS:
        raise error("claim_approval_schema_invalid")
    if (
        value["envelope_kind"] != release_entrypoint.WORKER_APPROVAL_ENVELOPE_KIND
        or type(value["envelope_schema_version"]) is not int
        or value["envelope_schema_version"]
        != release_entrypoint.WORKER_APPROVAL_SCHEMA_VERSION
        or value["subject_kind"] != release_entrypoint.WORKER_APPROVAL_SUBJECT_KIND
        or type(value["subject_schema_version"]) is not int
        or value["subject_schema_version"]
        != release_entrypoint.WORKER_APPROVAL_SCHEMA_VERSION
    ):
        raise error("claim_approval_schema_unsupported")


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ClaimRecordInvalid("claim_record_fields_invalid")
    if (
        type(record["claim_schema_version"]) is not int
        or record["claim_schema_version"] != CLAIM_SCHEMA_VERSION
    ):
        raise ClaimRecordInvalid("claim_schema_version_unsupported")
    if not isinstance(record["approval_id"], str) or not _APPROVAL_ID_RE.fullmatch(
        record["approval_id"]
    ):
        raise ClaimRecordInvalid("claim_approval_id_invalid")
    _claim_id = _claim_id_for_approval(record["approval_id"])
    if record["claim_id"] != _claim_id:
        raise ClaimRecordInvalid("claim_id_binding_invalid")
    _validate_schema_identity(record["approval_schema"], record=True)
    if (
        not isinstance(record["approval_key_id"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["approval_key_id"])
    ):
        raise ClaimRecordInvalid("claim_approval_key_id_invalid")

    for field, maximum in (
        ("signed_operator_identity", 256),
        ("approval_purpose", 128),
        ("promotion_action", 128),
    ):
        try:
            _identity_string(record[field], field=field, maximum=maximum)
        except ClaimValidationError as exc:
            raise ClaimRecordInvalid(str(exc)) from exc
    if record["approval_purpose"] != release_entrypoint.WORKER_APPROVAL_PURPOSE:
        raise ClaimRecordInvalid("claim_approval_purpose_invalid")
    if record["promotion_action"] != release_entrypoint.WORKER_APPROVAL_ACTION:
        raise ClaimRecordInvalid("claim_promotion_action_invalid")

    if (
        not isinstance(record["execution_artifact_digest"], str)
        or not _DIGEST_RE.fullmatch(record["execution_artifact_digest"])
        or not isinstance(record["canonical_manifest_sha256"], str)
        or not _DIGEST_RE.fullmatch(record["canonical_manifest_sha256"])
        or not isinstance(record["release_id"], str)
        or not _RELEASE_ID_RE.fullmatch(record["release_id"])
    ):
        raise ClaimRecordInvalid("claim_release_identity_invalid")
    profile = record["approved_configuration_profile"]
    if not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS:
        raise ClaimRecordInvalid("claim_configuration_profile_invalid")
    try:
        _identity_string(profile["policy_id"], field="policy_id", maximum=256)
    except ClaimValidationError as exc:
        raise ClaimRecordInvalid(str(exc)) from exc
    profile_reference = profile["profile_reference"]
    if (
        not isinstance(profile_reference, dict)
        or set(profile_reference) != _PROFILE_REFERENCE_FIELDS
    ):
        raise ClaimRecordInvalid("claim_configuration_profile_reference_invalid")
    try:
        profile_path = _identity_string(
            profile_reference["path"], field="profile_path", maximum=1024
        )
    except ClaimValidationError as exc:
        raise ClaimRecordInvalid(str(exc)) from exc
    if (
        not profile_path.startswith("/opt/orchestrator/worker-config/")
        or ".." in PurePosixPath(profile_path).parts
        or PurePosixPath(profile_path).as_posix() != profile_path
        or not profile_path.endswith("/profile.json")
        or not isinstance(profile_reference["sha256"], str)
        or not _DIGEST_RE.fullmatch(profile_reference["sha256"])
    ):
        raise ClaimRecordInvalid("claim_configuration_profile_reference_invalid")

    approval_start = _parse_utc(record["approval_valid_from"], record=True)
    approval_end = _parse_utc(record["approval_valid_until"], record=True)
    created_at = _parse_utc(record["claim_created_at"], record=True)
    if approval_end <= approval_start or not approval_start <= created_at < approval_end:
        raise ClaimRecordInvalid("claim_approval_window_invalid")

    expected_operation_id = _operation_identity(record)
    if record["claim_operation_identity"] != expected_operation_id:
        raise ClaimRecordInvalid("claim_operation_identity_invalid")
    if not isinstance(record["claim_state"], str) or record["claim_state"] not in _LEGAL_TRANSITIONS:
        raise ClaimRecordInvalid("claim_state_invalid")
    history = record["state_history"]
    if not isinstance(history, list) or not 1 <= len(history) <= 3:
        raise ClaimRecordInvalid("claim_state_history_invalid")
    prior_state: str | None = None
    prior_time: dt.datetime | None = None
    for index, event in enumerate(history):
        if not isinstance(event, dict) or set(event) != _HISTORY_EVENT_FIELDS:
            raise ClaimRecordInvalid("claim_state_history_event_invalid")
        state = event["state"]
        if not isinstance(state, str) or state not in _LEGAL_TRANSITIONS:
            raise ClaimRecordInvalid("claim_state_history_state_invalid")
        at = _parse_utc(event["at"], record=True)
        if index == 0:
            if state != CLAIMED or at != created_at:
                raise ClaimRecordInvalid("claim_state_history_origin_invalid")
        elif prior_state is None or state not in _LEGAL_TRANSITIONS[prior_state]:
            raise ClaimRecordInvalid("claim_state_history_transition_invalid")
        if prior_time is not None and at < prior_time:
            raise ClaimRecordInvalid("claim_state_history_time_invalid")
        prior_state, prior_time = state, at
    if prior_state != record["claim_state"]:
        raise ClaimRecordInvalid("claim_state_history_mismatch")
    return record


def _record_from_verification(
    verification: Mapping[str, Any], *, now: dt.datetime | None = None
) -> dict[str, Any]:
    if (
        verification.get("status") != "INTEGRITY_AND_PROMOTION_APPROVAL_VERIFIED"
        or verification.get("integrity_verified") is not True
        or verification.get("signature_verified") is not True
        or verification.get("promotion_approval_verified") is not True
        or verification.get("activation_eligible") is not False
        or verification.get("durable_one_time_claim_consumed") is not False
    ):
        raise ClaimValidationError("worker_release_verification_not_claimable")

    approval_id = verification.get("approval_id")
    claim_id = _claim_id_for_approval(approval_id)
    _validate_schema_identity(
        {
            "envelope_kind": verification.get("approval_envelope_kind"),
            "envelope_schema_version": verification.get(
                "approval_envelope_schema_version"
            ),
            "subject_kind": verification.get("approval_subject_kind"),
            "subject_schema_version": verification.get(
                "approval_subject_schema_version"
            ),
        },
        record=False,
    )
    identity = {
        "claim_schema_version": CLAIM_SCHEMA_VERSION,
        "approval_id": approval_id,
        "approval_schema": {
            "envelope_kind": verification["approval_envelope_kind"],
            "envelope_schema_version": verification[
                "approval_envelope_schema_version"
            ],
            "subject_kind": verification["approval_subject_kind"],
            "subject_schema_version": verification["approval_subject_schema_version"],
        },
        "approval_key_id": verification.get("key_id"),
        "signed_operator_identity": verification.get("operator_identity"),
        "approval_purpose": verification.get("approval_purpose"),
        "promotion_action": verification.get("approval_action"),
        "execution_artifact_digest": verification.get(
            "execution_artifact_digest"
        ),
        "release_id": verification.get("release_id"),
        "canonical_manifest_sha256": verification.get(
            "canonical_manifest_sha256"
        ),
        "approved_configuration_profile": {
            "policy_id": verification.get("configuration_policy_id"),
            "profile_reference": verification.get(
                "configuration_profile_reference"
            ),
        },
        "approval_valid_from": _normalize_verified_utc(
            verification.get("approval_valid_from")
        ),
        "approval_valid_until": _normalize_verified_utc(
            verification.get("approval_valid_until")
        ),
    }
    created_at = _format_utc(now or dt.datetime.now(UTC))
    record = {
        **identity,
        "claim_id": claim_id,
        "claim_created_at": created_at,
        "claim_operation_identity": "",
        "claim_state": CLAIMED,
        "state_history": [{"state": CLAIMED, "at": created_at}],
    }
    record["claim_operation_identity"] = _operation_identity(record)
    return _validate_record(record)


def claim_worker_release(
    release_path: str | Path,
    store: "OfflinePromotionClaimStore",
) -> "ClaimReceipt":
    """Verify and durably claim a v2 approval without promoting the release."""

    if not isinstance(store, OfflinePromotionClaimStore):
        raise ClaimStoreConfigurationError("offline_claim_store_required")
    from . import release_packaging

    verification = release_packaging.verify_worker_release(release_path)
    record = _record_from_verification(verification)
    store.claim(record)
    return ClaimReceipt(
        approval_id=record["approval_id"],
        claim_id=record["claim_id"],
        claim_operation_identity=record["claim_operation_identity"],
        claim_state=record["claim_state"],
        durable_claim_created=True,
        activation_eligible=False,
        promotion_performed=False,
    )


@dataclass(frozen=True)
class ClaimReceipt:
    """Evidence that the offline store recorded a claim, never readiness."""

    approval_id: str
    claim_id: str
    claim_operation_identity: str
    claim_state: str
    durable_claim_created: bool
    activation_eligible: bool
    promotion_performed: bool


@dataclass(frozen=True)
class ReconciliationResult:
    """Read-only status for one approval claim key."""

    status: str
    claim_id: str
    claim_state: str | None = None
    reason: str | None = None


class OfflinePromotionClaimStore:
    """POSIX filesystem reference store for temporary offline tests only.

    The caller must create the private temporary root first.  Acquisition is
    exclusive by approval ID (``O_CREAT|O_EXCL``); state changes use a stable
    per-claim ``flock`` and fsynced same-directory replacement.  This class
    is not a production host store and never chooses or creates a root.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        temporary_root = Path(os.path.abspath(tempfile.gettempdir()))
        workspace_root = Path(__file__).absolute().parents[3]
        if (
            self.root == temporary_root
            or temporary_root not in self.root.parents
            or self.root == workspace_root
            or workspace_root in self.root.parents
        ):
            raise ClaimStoreConfigurationError("offline_claim_root_must_be_temporary")
        self._check_root_path()

    @staticmethod
    def _check_directory_stat(info: os.stat_result) -> None:
        uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != uid
        ):
            raise ClaimStoreConfigurationError("offline_claim_root_permissions_invalid")

    def _check_root_path(self) -> os.stat_result:
        cursor = Path(self.root.anchor)
        try:
            for part in self.root.parts[1:]:
                cursor /= part
                info = os.lstat(cursor)
                if stat.S_ISLNK(info.st_mode):
                    raise ClaimStoreConfigurationError(
                        "offline_claim_root_symlink_ancestor"
                    )
        except OSError as exc:
            raise ClaimStoreConfigurationError(
                "offline_claim_root_unavailable"
            ) from exc
        info = os.lstat(self.root)
        self._check_directory_stat(info)
        return info

    @contextmanager
    def _root_fd(self) -> Iterator[int]:
        expected = self._check_root_path()
        try:
            fd = os.open(
                self.root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise ClaimPersistenceUncertain("claim_root_open_failed") from exc
        try:
            actual = os.fstat(fd)
            self._check_directory_stat(actual)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ClaimPersistenceUncertain("claim_root_changed_during_open")
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _check_regular_file(info: os.stat_result, *, label: str) -> None:
        uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != uid
        ):
            raise ClaimRecordInvalid(f"{label}_type_owner_mode_or_link_invalid")

    @staticmethod
    def _stat_name(dir_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ClaimRecordInvalid("claim_path_stat_failed") from exc

    @classmethod
    def _read_record_at(cls, dir_fd: int, claim_id: str) -> dict[str, Any]:
        name = f"{claim_id}.json"
        path_info = cls._stat_name(dir_fd, name)
        if path_info is None:
            raise FileNotFoundError(name)
        cls._check_regular_file(path_info, label="claim_record")
        try:
            fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=dir_fd,
            )
        except OSError as exc:
            raise ClaimRecordInvalid("claim_record_open_failed") from exc
        try:
            info = os.fstat(fd)
            cls._check_regular_file(info, label="claim_record")
            if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
                raise ClaimRecordInvalid("claim_record_changed_during_open")
            chunks: list[bytes] = []
            size = 0
            while True:
                block = os.read(fd, min(8192, _MAX_RECORD_BYTES + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > _MAX_RECORD_BYTES:
                    raise ClaimRecordInvalid("claim_record_size_invalid")
            record = _parse_canonical_record(b"".join(chunks))
            try:
                _validate_record(record)
            except (ClaimRecordInvalid, ClaimValidationError) as exc:
                raise ClaimRecordInvalid(str(exc)) from exc
            if record["claim_id"] != claim_id:
                raise ClaimRecordInvalid("claim_record_id_mismatch")
            return record
        finally:
            os.close(fd)

    @staticmethod
    def _side_artifacts(dir_fd: int, claim_id: str) -> list[str]:
        try:
            names = os.listdir(dir_fd)
        except OSError as exc:
            raise ClaimRecordInvalid("claim_directory_list_failed") from exc
        lock_name = f".{claim_id}.lock"
        temporary_prefix = f".{claim_id}.tmp."
        return [
            name
            for name in names
            if name.startswith(temporary_prefix)
            or (name.startswith(lock_name) and name != lock_name)
        ]

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short claim write")
            view = view[written:]

    def claim(self, record: Mapping[str, Any]) -> None:
        """Create the initial claim exactly once; never replaces any path."""

        candidate = dict(record)
        _validate_record(candidate)
        if (
            candidate["claim_state"] != CLAIMED
            or len(candidate["state_history"]) != 1
        ):
            raise ClaimValidationError("new_claim_must_start_claimed")
        raw = _canonical_json_bytes(candidate)
        claim_id = candidate["claim_id"]
        name = f"{claim_id}.json"
        with self._root_fd() as root_fd:
            if self._stat_name(root_fd, name) is not None:
                raise ClaimCollisionError(
                    "approval_id_already_claimed_or_claim_identity_collision"
                )
            artifacts = self._side_artifacts(root_fd, claim_id)
            if artifacts or self._stat_name(root_fd, f".{claim_id}.lock"):
                raise ClaimRecordInvalid("claim_orphan_side_artifact")
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError as exc:
                raise ClaimCollisionError(
                    "approval_id_already_claimed_or_claim_identity_collision"
                ) from exc
            except OSError as exc:
                raise ClaimPersistenceUncertain("claim_create_uncertain") from exc
            try:
                os.fchmod(fd, 0o600)
                self._check_regular_file(os.fstat(fd), label="claim_record")
                self._write_all(fd, raw)
                os.fsync(fd)
                os.fsync(root_fd)
            except OSError as exc:
                # A partial or ambiguously durable record is deliberately kept.
                raise ClaimPersistenceUncertain("claim_create_uncertain") from exc
            finally:
                os.close(fd)

    @contextmanager
    def _exclusive_claim_lock(self, root_fd: int, claim_id: str) -> Iterator[None]:
        name = f".{claim_id}.lock"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=root_fd,
            )
            created = True
        except FileExistsError:
            try:
                fd = os.open(name, flags, dir_fd=root_fd)
            except OSError as exc:
                raise ClaimPersistenceUncertain("claim_lock_open_failed") from exc
        except OSError as exc:
            raise ClaimPersistenceUncertain("claim_lock_create_uncertain") from exc

        try:
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            self._check_regular_file(info, label="claim_lock")
            named = self._stat_name(root_fd, name)
            if named is None or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise ClaimRecordInvalid("claim_lock_changed_during_open")
            if created:
                os.fsync(fd)
                os.fsync(root_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise ClaimPersistenceUncertain("claim_lock_failed") from exc
            named_after_lock = self._stat_name(root_fd, name)
            if named_after_lock is None or (
                named_after_lock.st_dev,
                named_after_lock.st_ino,
            ) != (info.st_dev, info.st_ino):
                raise ClaimRecordInvalid("claim_lock_changed_while_waiting")
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _replace_record(
        self, root_fd: int, claim_id: str, record: Mapping[str, Any]
    ) -> None:
        name = f"{claim_id}.json"
        raw = _canonical_json_bytes(dict(record))
        temporary_name = f".{claim_id}.tmp.{secrets.token_hex(12)}"
        try:
            fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise ClaimPersistenceUncertain("claim_transition_temp_create_failed") from exc
        replaced = False
        try:
            os.fchmod(fd, 0o600)
            self._check_regular_file(os.fstat(fd), label="claim_transition_temp")
            self._write_all(fd, raw)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(
                temporary_name,
                name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            replaced = True
            os.fsync(root_fd)
        except OSError as exc:
            raise ClaimPersistenceUncertain("claim_transition_persistence_uncertain") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if not replaced:
                # Keep an orphan if cleanup itself is uncertain. Reconciliation
                # detects it and reports an invalid/inconsistent claim.
                try:
                    os.unlink(temporary_name, dir_fd=root_fd)
                    os.fsync(root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def transition(
        self,
        claim_id: str,
        *,
        expected: str,
        state: str,
    ) -> None:
        if not isinstance(claim_id, str) or not _DIGEST_RE.fullmatch(claim_id):
            raise ClaimValidationError("claim_id_invalid")
        if expected not in _LEGAL_TRANSITIONS or state not in _LEGAL_TRANSITIONS:
            raise ClaimTransitionError("claim_state_invalid")
        if state not in _LEGAL_TRANSITIONS[expected]:
            raise ClaimTransitionError("claim_state_transition_illegal")
        timestamp = _format_utc(dt.datetime.now(UTC))
        with self._root_fd() as root_fd:
            with self._exclusive_claim_lock(root_fd, claim_id):
                try:
                    current = self._read_record_at(root_fd, claim_id)
                except FileNotFoundError as exc:
                    raise ClaimRecordInvalid("claim_record_missing") from exc
                if current["claim_state"] != expected:
                    raise ClaimTransitionError("claim_state_expected_mismatch")
                new_record = dict(current)
                new_record["claim_state"] = state
                new_record["state_history"] = [
                    *current["state_history"],
                    {"state": state, "at": timestamp},
                ]
                _validate_record(new_record)
                self._replace_record(root_fd, claim_id, new_record)

    def reconcile(
        self,
        approval_id: str,
        *,
        expected_operation_identity: str | None = None,
    ) -> ReconciliationResult:
        """Read a claim without creating locks, changing files, or inferring success."""

        claim_id = _claim_id_for_approval(approval_id)
        if expected_operation_identity is not None and (
            not isinstance(expected_operation_identity, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_operation_identity)
        ):
            raise ClaimValidationError("claim_operation_identity_invalid")
        try:
            with self._root_fd() as root_fd:
                try:
                    record = self._read_record_at(root_fd, claim_id)
                except FileNotFoundError:
                    artifacts = self._side_artifacts(root_fd, claim_id)
                    if artifacts or self._stat_name(root_fd, f".{claim_id}.lock"):
                        return ReconciliationResult(
                            status=CLAIM_INVALID,
                            claim_id=claim_id,
                            reason="claim_orphan_side_artifact",
                        )
                    return ReconciliationResult(status=NO_CLAIM, claim_id=claim_id)
                artifacts = self._side_artifacts(root_fd, claim_id)
                if artifacts:
                    return ReconciliationResult(
                        status=CLAIM_INVALID,
                        claim_id=claim_id,
                        claim_state=record["claim_state"],
                        reason="claim_transition_artifact_present",
                    )
                lock_info = self._stat_name(root_fd, f".{claim_id}.lock")
                if lock_info is not None:
                    self._check_regular_file(lock_info, label="claim_lock")
        except (ClaimRecordInvalid, ClaimPersistenceUncertain, ClaimStoreConfigurationError) as exc:
            return ReconciliationResult(
                status=CLAIM_INVALID,
                claim_id=claim_id,
                reason=str(exc),
            )

        if record["approval_id"] != approval_id:
            return ReconciliationResult(
                status=CLAIM_INVALID,
                claim_id=claim_id,
                reason="claim_approval_id_mismatch",
            )
        if (
            expected_operation_identity is not None
            and record["claim_operation_identity"] != expected_operation_identity
        ):
            return ReconciliationResult(
                status=CLAIM_INVALID,
                claim_id=claim_id,
                claim_state=record["claim_state"],
                reason="claim_identity_mismatch",
            )
        state = record["claim_state"]
        status = CLAIMED_NOT_STARTED if state == CLAIMED else state
        return ReconciliationResult(
            status=status,
            claim_id=claim_id,
            claim_state=state,
        )
