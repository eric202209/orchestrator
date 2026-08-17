"""Tests for session lookup helpers."""

from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException

from app.services.session.session_lookup import get_session_or_404


class MockSessionModel:
    """Mock Session model for testing."""

    def __init__(self, session_id, deleted_at=None):
        self.id = session_id
        self.deleted_at = deleted_at


def test_get_session_or_404_found():
    """Test that get_session_or_404 returns a session when it exists."""
    mock_db = MagicMock()
    mock_session = MockSessionModel(session_id=1)
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_session
    mock_db.query.return_value = mock_query

    result = get_session_or_404(mock_db, 1)
    assert result.id == 1
    assert result.deleted_at is None


def test_get_session_or_404_not_found():
    """Test that get_session_or_404 raises 404 when session does not exist."""
    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None
    mock_db.query.return_value = mock_query

    with pytest.raises(HTTPException) as exc_info:
        get_session_or_404(mock_db, 999)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"


def test_get_session_or_404_excludes_deleted_by_default():
    """Test that deleted sessions are excluded by default."""
    mock_db = MagicMock()
    mock_session = MockSessionModel(session_id=1, deleted_at="2023-01-01")
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None
    mock_db.query.return_value = mock_query

    with pytest.raises(HTTPException) as exc_info:
        get_session_or_404(mock_db, 1)
    assert exc_info.value.status_code == 404


def test_get_session_or_404_includes_deleted_when_requested():
    """Test that deleted sessions are included when include_deleted=True."""
    mock_db = MagicMock()
    mock_session = MockSessionModel(session_id=1, deleted_at="2023-01-01")
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_session
    mock_db.query.return_value = mock_query

    result = get_session_or_404(mock_db, 1, include_deleted=True)
    assert result.id == 1
    assert result.deleted_at == "2023-01-01"
