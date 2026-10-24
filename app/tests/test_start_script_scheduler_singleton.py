from pathlib import Path


START_SCRIPT = Path(__file__).parents[2] / "start.sh"


def test_start_script_refuses_to_preserve_an_existing_celery_beat():
    source = START_SCRIPT.read_text()
    guard_offset = source.index("require_no_owned_service beat")
    launch_offset = source.index('setsid nohup "${VENV_DIR}/bin/celery"', guard_offset)

    assert "Refusing to preserve a potentially mixed-version scheduler" in source
    assert 'check_process "celery -A app.celery_app beat"' not in source
    assert launch_offset > guard_offset
