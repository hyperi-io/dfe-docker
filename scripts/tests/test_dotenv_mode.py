#  Project:      dfe-docker
#  File:         tests/test_dotenv_mode.py
#  Purpose:      Assert .env, which carries every generated secret, is never readable by other users
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The .env file mode.

A new .env is created owner and group only whatever the umask; an existing one
left open to other users is closed on the next `make init` without touching its
owner or group bits; every rewrite reasserts the mode.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

import _common
import init

_SETTLED = "\n".join(f"{key}=already-minted-value" for key in init.GENERATED_SECRETS)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def open_umask() -> Iterator[None]:
    """Run with a umask that would leave a plain write readable by everyone."""
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def layout(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point init.main at a throwaway tree; return the template."""
    template = dotenv.parent / ".env.example"
    template.write_text("# CLICKHOUSE_SECURE=false\n", encoding="utf-8", newline="\n")
    env_templates = dotenv.parent / "env.example"
    env_templates.mkdir()
    (env_templates / "engine.env").write_text("# DFE_API_PORT=8003\n", encoding="utf-8")
    monkeypatch.setattr(init, "DOTENV_TEMPLATE", template)
    monkeypatch.setattr(init, "ENV_TEMPLATE_DIR", env_templates)
    monkeypatch.setattr(init, "ENV_DIR", dotenv.parent / "env")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.delenv(init.RETENTION_KEY, raising=False)
    return template


def test_a_new_dotenv_is_owner_and_group_only(
    dotenv: Path, layout: Path, open_umask: None
) -> None:
    assert init.main() == 0
    assert _mode(dotenv) == _common.DOTENV_MODE


def test_an_existing_dotenv_open_to_others_is_closed_and_keeps_its_group_bits(
    dotenv: Path, layout: Path, open_umask: None
) -> None:
    dotenv.write_text(_SETTLED + "\n", encoding="utf-8", newline="\n")
    dotenv.chmod(0o664)

    assert init.main() == 0
    assert _mode(dotenv) == 0o660
    assert dotenv.read_text(encoding="utf-8") == _SETTLED + "\n"


def test_an_existing_private_dotenv_is_left_alone(
    dotenv: Path, layout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_SETTLED + "\n", encoding="utf-8", newline="\n")
    dotenv.chmod(0o600)

    assert init.main() == 0
    assert _mode(dotenv) == 0o600
    assert "Closed to other users" not in capsys.readouterr().out


def test_a_secret_top_up_reasserts_the_mode(
    dotenv: Path, layout: Path, open_umask: None
) -> None:
    dotenv.write_text("DFE_ENV=dev\n", encoding="utf-8", newline="\n")
    dotenv.chmod(0o644)

    assert init.main() == 0
    assert _mode(dotenv) == _common.DOTENV_MODE


def test_write_private_narrows_an_existing_file(
    tmp_path: Path, open_umask: None
) -> None:
    path = tmp_path / "secret"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o666)

    _common.write_private(path=path, text="new\n")

    assert _mode(path) == 0o600
    assert path.read_text(encoding="utf-8") == "new\n"
