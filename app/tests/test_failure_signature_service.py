"""Tests for FailureSignatureService — normalisation and hashing."""

from __future__ import annotations

from app.services.knowledge.failure_signature_service import extract


def test_file_paths_stripped_from_normalized_message():
    exc = FileNotFoundError(
        "No such file or directory: /home/user/project/src/module.py"
    )
    sig1 = extract(exc, phase="execution", tool_name=None, retry_count=1)

    exc2 = FileNotFoundError("No such file or directory: /tmp/other/path/module.py")
    sig2 = extract(exc2, phase="execution", tool_name=None, retry_count=1)

    assert "/home" not in sig1.normalized_message
    assert "/tmp" not in sig2.normalized_message
    assert sig1.normalized_message == sig2.normalized_message


def test_uuid_stripped_from_normalized_message():
    exc = RuntimeError(
        "Task 3f7b4c2e-1a5d-4e8b-9f2a-0c6d8e3b1f7a failed: connection refused"
    )
    sig = extract(exc, phase="failure", tool_name=None, retry_count=1)
    assert "3f7b4c2e" not in sig.normalized_message
    assert "connection refused" in sig.normalized_message


def test_same_signature_hash_for_different_retry_counts():
    exc = ConnectionError("db connection refused")
    sig1 = extract(exc, phase="execution", tool_name=None, retry_count=1)
    sig2 = extract(exc, phase="execution", tool_name=None, retry_count=2)

    assert sig1.signature_hash() == sig2.signature_hash()


def test_retry_count_field_present_on_signature():
    exc = ValueError("invalid input")
    sig = extract(exc, phase="planning", tool_name="bash", retry_count=3)
    assert sig.retry_count == 3
