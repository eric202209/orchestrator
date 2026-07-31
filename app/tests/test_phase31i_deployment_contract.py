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
