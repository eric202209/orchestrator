from pathlib import Path


START_SCRIPT = Path(__file__).parents[2] / "start.sh"


def test_start_script_refuses_to_preserve_an_existing_celery_beat():
    source = START_SCRIPT.read_text()
    guard = 'if check_process "celery -A app.celery_app beat"; then'
    guard_offset = source.index(guard)
    launch_offset = source.index('setsid nohup "${VENV_DIR}/bin/celery"', guard_offset)

    assert source.index("return 1", guard_offset) < launch_offset
