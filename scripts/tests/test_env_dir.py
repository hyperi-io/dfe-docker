#  Project:      dfe-docker
#  File:         tests/test_env_dir.py
#  Purpose:      Assert env/ is left group-writable and setgid for the engine's custom env files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The mode `make init` and every start target leave on env/.

The engine writes env/<app>.custom.env through the directory's group and Compose
reads it back as the operator, so env/ needs group write and the setgid bit.
"""

import stat
from pathlib import Path

import pytest

import env_files
import init


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def env_dir(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point init and env_files at a throwaway tree with one template; return its env/."""
    (dotenv.parent / ".env.example").write_text(
        "# CLICKHOUSE_SECURE=false\n", encoding="utf-8"
    )
    templates = dotenv.parent / "env.example"
    templates.mkdir()
    (templates / "engine.env").write_text("# DFE_API_PORT=8003\n", encoding="utf-8")
    target = dotenv.parent / "env"
    monkeypatch.setattr(init, "DOTENV_TEMPLATE", dotenv.parent / ".env.example")
    for module in (init, env_files):
        monkeypatch.setattr(module, "ENV_TEMPLATE_DIR", templates)
        monkeypatch.setattr(module, "ENV_DIR", target)
    monkeypatch.setattr(env_files, "DOTENV_FILE", dotenv)
    monkeypatch.setattr(env_files, "DOTENV_TEMPLATE", dotenv.parent / ".env.example")
    monkeypatch.delenv(init.RETENTION_KEY, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return target


def test_init_leaves_env_group_writable_and_setgid(env_dir: Path) -> None:
    assert init.main() == 0

    assert _mode(env_dir) & init.ENV_DIR_GROUP_BITS == init.ENV_DIR_GROUP_BITS


def test_a_start_target_heals_an_env_dir_an_older_init_made(env_dir: Path) -> None:
    env_dir.mkdir()
    (env_dir / "engine.env").write_text("", encoding="utf-8")
    env_dir.chmod(0o700)

    assert env_files.main() == 0

    assert _mode(env_dir) == 0o2770


def test_only_group_bits_are_added_and_nothing_is_taken_away(dotenv: Path) -> None:
    target = dotenv.parent / "env"
    target.mkdir()
    target.chmod(0o755)

    init.share_env_dir(env_dir=target)

    assert _mode(target) == 0o2775


def test_an_already_shared_dir_is_left_alone(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = dotenv.parent / "env"
    target.mkdir()
    target.chmod(0o2770)

    init.share_env_dir(env_dir=target)

    assert _mode(target) == 0o2770
    assert capsys.readouterr().err == ""
