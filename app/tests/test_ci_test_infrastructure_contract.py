from pathlib import Path

import pytest

from app.tests.conftest import PRIMARY_CATEGORY_MARKERS, primary_test_category


class _CollectedItem:
    def __init__(self, module_name: str, *markers: str):
        self.path = Path(module_name)
        self._markers = set(markers)

    def get_closest_marker(self, name: str):
        return name if name in self._markers else None


@pytest.mark.parametrize(
    ("module_name", "markers", "expected"),
    [
        ("test_recovery_lifecycle.py", (), "recovery"),
        ("test_auth.py", (), "security"),
        ("test_mobile_dashboard_endpoint.py", (), "api_contract"),
        ("test_start_script_scheduler_singleton.py", (), "deployment"),
        ("test_session_transition_policy.py", (), "product_contract"),
        ("test_service_integration.py", ("integration",), "integration_contract"),
        ("test_provider_certification.py", (), "evidence_historical"),
        ("test_legacy_compat.py", (), "compatibility"),
        ("test_anything.py", ("live",), "live_validation"),
        ("test_parser.py", (), "critical_regression"),
    ],
)
def test_primary_category_rules_are_exclusive(module_name, markers, expected):
    item = _CollectedItem(module_name, *markers)

    assert primary_test_category(item) == expected
    assert expected in PRIMARY_CATEGORY_MARKERS


def test_collected_backend_tests_have_one_primary_category(request):
    for item in request.session.items:
        categories = {
            marker.name
            for marker in item.iter_markers()
            if marker.name in PRIMARY_CATEGORY_MARKERS
        }
        assert len(categories) == 1, item.nodeid


def test_non_live_backend_tests_have_one_ci_partition(request):
    for item in request.session.items:
        markers = {marker.name for marker in item.iter_markers()}
        if "live" in markers:
            continue

        partitions = (
            "unit" in markers and "semantic" not in markers and "slow" not in markers,
            "semantic" in markers,
            "semantic" not in markers and ("unit" not in markers or "slow" in markers),
        )
        assert sum(partitions) == 1, item.nodeid
