from pathlib import Path
import subprocess

from app.config import BASE_DIR, Settings
from scripts import dev_runtime


def test_repository_defaults_keep_database_and_runtime_inside_checkout() -> None:
    settings = Settings(_env_file=None)

    database_path = Path(settings.DATABASE_URL.removeprefix("sqlite:///"))

    assert database_path.resolve() == (BASE_DIR / "orchestrator.db").resolve()
    assert (
        Path(settings.RUNTIME_ROOT).expanduser().resolve()
        == (BASE_DIR / "runtime").resolve()
    )


def test_development_runtime_root_stays_inside_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()

    config = dev_runtime.RuntimeConfig.for_repo(repo)

    assert config.root == (repo / ".dev-runtime" / "phase40b-r1").resolve()
    assert repo.resolve() in config.root.parents
    assert config.root.resolve() not in repo.resolve().parents
    dev_runtime._require_runtime_root(config)


def test_startup_validates_the_canonical_deployment_contract() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source ./start.sh; load_env; validate_canonical_deployment_config",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    rejected = subprocess.run(
        [
            "bash",
            "-c",
            "source ./start.sh; load_env; RUNTIME_ROOT=/tmp/unintended-runtime; validate_canonical_deployment_config",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rejected.returncode == 78
    assert "RUNTIME_ROOT must resolve" in rejected.stderr
