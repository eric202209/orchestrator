from pathlib import Path


ROOT = Path(__file__).parents[2]
DEPLOY = (ROOT / "scripts/maintenance/dogfood_deploy.sh").read_text()
START = (ROOT / "start.sh").read_text()


def test_deploy_requires_clean_revision_before_shutdown():
    clean_gate = DEPLOY.index("git status --porcelain --untracked-files=all")
    shutdown = DEPLOY.index("./stop_all.sh")

    assert clean_gate < shutdown
    assert "dogfood deployment requires a clean committed worktree" in DEPLOY


def test_deploy_locks_shared_identity_and_requires_admission():
    for variable in (
        "ORCHESTRATOR_GIT_SHA",
        "ORCHESTRATOR_REPO_GIT_SHA",
        "ORCHESTRATOR_BUILD_TIME",
        "ORCHESTRATOR_CONFIG_SHA256",
        "ORCHESTRATOR_CONFIG_SOURCE",
    ):
        assert f"export {variable}=" in DEPLOY
        assert variable in START

    assert "dogfood_admission.py" in DEPLOY
    assert "--skip-smoke" not in DEPLOY


def test_start_fails_closed_for_noninteractive_mixed_version_topology():
    assert "Existing processes detected in non-interactive mode." in START
    assert "refusing a mixed-version start" in START
    assert "Refusing to preserve a potentially mixed-version scheduler" in START


def test_start_uses_bounded_expected_node_celery_control_readiness():
    assert "wait_for_celery_worker.py" in START
    assert "--expected-node" in START
    assert "CELERY_CONTROL_READINESS_TIMEOUT_SECONDS:-30" in START
    assert "CELERY_CONTROL_READINESS_INTERVAL_SECONDS:-1" in START
    assert 'local expected_worker_node="celery@$(hostname)"' in START


def test_execution_disabled_normal_start_skips_execution_services_and_database_init():
    main = START[
        START.index("# Start all services in order") : START.index("# Check health")
    ]
    assert "if execution_enabled; then" in main
    gated_services = main[main.index("if execution_enabled; then") :]
    for call in ("ensure_database", "start_redis", "start_qdrant", "start_workers"):
        assert call in gated_services
    assert gated_services.index("start_backend") > gated_services.index("fi")
    assert "execution_enabled()" in START


def test_execution_disabled_health_check_does_not_require_redis_worker_beat_or_qdrant():
    health = START[START.index("check_health()") : START.index("# Main execution")]
    assert "if execution_enabled; then" in health
    assert "redis-cli ping" in health
    assert "wait_for_celery_worker.py" in health
    assert "http://localhost:6333/healthz" in health


def test_execution_disabled_start_does_not_initialize_checkout_database():
    database = START[
        START.index("ensure_database()") : START.index("# Function to stop existing")
    ]
    assert "if ! execution_enabled; then" in database
    assert "return 0" in database
