from pathlib import Path


def test_dashboard_nav_points_to_dashboard_route():
    source = Path("frontend/src/layouts/AppShell.tsx").read_text(encoding="utf-8")

    assert "title: 'Dashboard'" in source
    assert "href: '/dashboard'" in source
