#  Project:      dfe-docker
#  File:         tests/test_render_dial.py
#  Purpose:      Assert `make dial` writes each dial key into the file that holds it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where `make dial` writes.

The deployment keys go to .env and the engine's own retention to env/engine.env,
the file compose hands the engine.
"""

import re
from pathlib import Path

import pytest

import render_dial

_DIAL = """\
platform: docker-vm
profile: slim
retention:
  default_ttl_days: 30
"""
_TTL_KEY = "DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS"


@pytest.fixture
def dial(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point render_dial at a throwaway dial and .env and return the dial."""
    path = dotenv.parent / "deployment.yaml"
    path.write_text(_DIAL, encoding="utf-8", newline="\n")
    monkeypatch.setattr(render_dial, "DEPLOYMENT_DIAL", path)
    monkeypatch.setattr(render_dial, "DOTENV_FILE", dotenv)
    return path


def test_the_retention_lands_in_the_engine_env_and_the_rest_in_dotenv(
    dial: Path, dotenv: Path, engine_env: Path
) -> None:
    dotenv.write_text("# DFE_PROFILE=\n", encoding="utf-8", newline="\n")
    engine_env.write_text(f"# {_TTL_KEY}=90\n", encoding="utf-8", newline="\n")

    assert render_dial.main() == 0
    assert engine_env.read_text(encoding="utf-8") == f"{_TTL_KEY}=30\n"
    assert dotenv.read_text(encoding="utf-8") == "DFE_PROFILE=slim\n"


def test_a_retention_with_no_engine_env_refuses_before_writing_anything(
    capsys: pytest.CaptureFixture[str], dial: Path, dotenv: Path
) -> None:
    dotenv.write_text("# DFE_PROFILE=\n", encoding="utf-8", newline="\n")

    assert render_dial.main() == 1
    assert "env/engine.env" in capsys.readouterr().err
    assert dotenv.read_text(encoding="utf-8") == "# DFE_PROFILE=\n"


@pytest.mark.parametrize(
    ("first_line", "message"),
    [
        ("substrate: docker-vm", "the dial key `substrate` was renamed to `platform`"),
        ("platform: k8s", "handles platform 'docker-vm', the dial says 'k8s'"),
        ("", "has no `platform:` key - set `platform: docker-vm`"),
    ],
    ids=["retired key", "other platform", "no platform"],
)
def test_a_dial_without_platform_docker_vm_is_refused(
    dial: Path, dotenv: Path, first_line: str, message: str
) -> None:
    dotenv.write_text("# DFE_PROFILE=\n", encoding="utf-8", newline="\n")
    dial.write_text(f"{first_line}\nprofile: slim\n", encoding="utf-8", newline="\n")

    with pytest.raises(SystemExit, match=re.escape(message)):
        render_dial.main()
    assert dotenv.read_text(encoding="utf-8") == "# DFE_PROFILE=\n"
