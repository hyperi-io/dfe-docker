#  Project:      dfe-docker
#  File:         tests/test_e2e_posture.py
#  Purpose:      Assert `make e2e-posture` switches on, restores, and checks the Playwright posture
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The dfe-ui Playwright suite needs DFE_ENV=test and the engine's e2e routes on.

`on` writes both and keeps the .env it replaced, `off` puts that copy back, and
`check` is what the suite runs first so it refuses in one line instead of failing its
specs. A .env that names a deployment is refused, because `on` writes the shipped admin
password over the real one.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import e2e_posture

_MINTED = "s3cr3t-minted-value"


def _env(posture: str, extra: str = "") -> str:
    return f"DFE_ENV={posture}\nDFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}\nDFE_UI_PORT=3000\n{extra}"


def _saved(directory: Path) -> Path:
    return directory / ".env.pre-e2e"


@pytest.mark.parametrize("posture", ["dev", "test", ""])
def test_on_writes_the_posture_and_keeps_the_rest(dotenv: Path, posture: str) -> None:
    dotenv.write_text(_env(posture), encoding="utf-8", newline="\n")

    assert e2e_posture.main(argv=["on"]) == 0

    written = dotenv.read_text(encoding="utf-8")
    assert "DFE_ENV=test" in written
    assert "DFE_E2E_SERVER=true" in written
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD=changeme" in written
    assert "DFE_UI_PORT=3000" in written


def test_a_deployment_is_refused_and_left_alone(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_env("production"), encoding="utf-8", newline="\n")

    assert e2e_posture.main(argv=["on"]) == 2

    assert dotenv.read_text(encoding="utf-8") == _env("production")
    assert not _saved(dotenv.parent).exists()
    assert "not a dev posture" in capsys.readouterr().err


def test_on_keeps_the_original_0600_and_a_second_on_does_not_replace_it(
    dotenv: Path,
) -> None:
    dotenv.write_text(_env("dev"), encoding="utf-8", newline="\n")

    assert e2e_posture.main(argv=["on"]) == 0
    assert e2e_posture.main(argv=["on"]) == 0

    saved = _saved(dotenv.parent)
    assert saved.read_text(encoding="utf-8") == _env("dev")
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_off_restores_the_env_from_before_and_removes_the_copy(dotenv: Path) -> None:
    dotenv.write_text(_env("dev"), encoding="utf-8", newline="\n")
    e2e_posture.main(argv=["on"])

    assert e2e_posture.main(argv=["off"]) == 0

    assert dotenv.read_text(encoding="utf-8") == _env("dev")
    assert not _saved(dotenv.parent).exists()


def test_off_with_nothing_saved_changes_nothing(dotenv: Path) -> None:
    dotenv.write_text(_env("dev"), encoding="utf-8", newline="\n")

    assert e2e_posture.main(argv=["off"]) == 0
    assert dotenv.read_text(encoding="utf-8") == _env("dev")


@pytest.mark.parametrize(
    ("text", "want", "named"),
    [
        pytest.param(
            _env("dev"), 1, ["DFE_ENV=test", "DFE_E2E_SERVER=true"], id="plain_dev"
        ),
        pytest.param(
            _env("test"), 1, ["DFE_E2E_SERVER=true"], id="test_without_the_server"
        ),
        pytest.param(
            _env("dev", "DFE_E2E_SERVER=true\n"),
            1,
            ["DFE_ENV=test"],
            id="server_without_test",
        ),
        pytest.param(_env("test", "DFE_E2E_SERVER=TRUE\n"), 0, [], id="both_any_case"),
    ],
)
def test_check_names_what_is_missing(
    dotenv: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    want: int,
    named: list[str],
) -> None:
    dotenv.write_text(text, encoding="utf-8", newline="\n")

    assert e2e_posture.main(argv=["check"]) == want

    err = capsys.readouterr().err
    assert all(item in err for item in named)


def test_check_passes_after_on(dotenv: Path) -> None:
    dotenv.write_text(_env("dev"), encoding="utf-8", newline="\n")
    e2e_posture.main(argv=["on"])

    assert e2e_posture.main(argv=["check"]) == 0


def test_a_missing_dotenv_is_exit_one(dotenv: Path) -> None:
    assert e2e_posture.main(argv=["on"]) == 1
