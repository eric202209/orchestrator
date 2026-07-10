from app.celery_app import celery_app


def test_periodic_maintenance_schedule_uses_registered_tasks():
    schedule = celery_app.conf.beat_schedule

    assert schedule["cleanup-old-logs"] == {
        "task": "app.tasks.maintenance.cleanup_old_logs",
        "schedule": schedule["cleanup-old-logs"]["schedule"],
        "kwargs": {"days": 30},
    }
    assert schedule["recover-orphaned-running-sessions"] == {
        "task": "app.tasks.maintenance.sweep_orphaned_running_sessions",
        "schedule": schedule["recover-orphaned-running-sessions"]["schedule"],
        "kwargs": {},
    }

    celery_app.loader.import_default_modules()
    assert "app.tasks.maintenance.cleanup_old_logs" in celery_app.tasks
    assert "app.tasks.maintenance.sweep_orphaned_running_sessions" in celery_app.tasks
