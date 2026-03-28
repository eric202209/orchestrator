"""
Regression tests for API format consistency
Prevents future API format inconsistencies by validating response structures
"""

import pytest
from fastapi.testclient import TestClient
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app

client = TestClient(app)


class TestAPIFormatConsistency:
    """
    Regression tests to ensure API responses maintain consistent format.
    These tests prevent bugs where API endpoints return unexpected structures.
    """

    def test_health_check_format(self):
        """
        Test that /health endpoint returns consistent JSON structure.

        Expected format:
        {
            "status": "healthy",
            "version": "1.0.0",
            "timestamp": "ISO-8601"
        }
        """
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()

        # Validate required fields exist
        assert "status" in data, "Missing 'status' field"
        assert data["status"] == "healthy", "Status should be 'healthy'"

        assert "version" in data, "Missing 'version' field"
        assert isinstance(data["version"], str), "Version should be string"

        assert "timestamp" in data, "Missing 'timestamp' field"
        assert isinstance(data["timestamp"], str), "Timestamp should be ISO-8601 string"

    def test_project_list_format(self):
        """
        Test that /api/v1/projects endpoint returns consistent array structure.

        Expected format:
        {
            "projects": [...],
            "total": int,
            "skip": int,
            "limit": int
        }
        """
        response = client.get("/api/v1/projects?skip=0&limit=10")

        assert response.status_code == 200
        data = response.json()

        # Validate structure
        assert "projects" in data, "Missing 'projects' array"
        assert isinstance(data["projects"], list), "Projects should be array"

        assert "total" in data, "Missing 'total' count"
        assert isinstance(data["total"], int), "Total should be integer"

        assert "skip" in data, "Missing 'skip' offset"
        assert "limit" in data, "Missing 'limit' value"

    def test_project_detail_format(self):
        """
        Test that GET /api/v1/projects/{id} returns consistent structure.

        Expected fields: id, title, description, status, created_at, updated_at
        """
        # First, create a test project
        test_project = {
            "title": "Regression Test Project",
            "description": "Test project for API format validation",
            "status": "active",
        }

        response = client.post("/api/v1/projects", json=test_project)
        assert response.status_code in [200, 201], "Should be able to create project"

        project_id = response.json().get("id")
        assert project_id is not None, "Should receive project ID"

        # Now test retrieval
        response = client.get(f"/api/v1/projects/{project_id}")
        assert response.status_code == 200

        data = response.json()

        # Validate required fields
        required_fields = [
            "id",
            "title",
            "description",
            "status",
            "created_at",
            "updated_at",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"

        # Cleanup
        client.delete(f"/api/v1/projects/{project_id}")

    def test_task_list_format(self):
        """
        Test that /api/v1/tasks endpoint returns consistent structure.
        """
        response = client.get("/api/v1/tasks?skip=0&limit=10")

        assert response.status_code == 200
        data = response.json()

        # Validate structure
        assert "tasks" in data, "Missing 'tasks' array"
        assert isinstance(data["tasks"], list), "Tasks should be array"

        assert "total" in data, "Missing 'total' count"
        assert "skip" in data, "Missing 'skip' offset"
        assert "limit" in data, "Missing 'limit' value"

    def test_error_response_format(self):
        """
        Test that error responses follow consistent format.

        Expected format:
        {
            "detail": "Error message"
        }
        """
        # Test 404 error
        response = client.get("/api/v1/projects/nonexistent-id")

        assert response.status_code == 404
        data = response.json()

        assert "detail" in data, "Error responses should have 'detail' field"
        assert isinstance(data["detail"], str), "Detail should be string message"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
