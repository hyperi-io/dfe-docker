#  Project:      dfe-docker
#  File:         tests/test_e2e_posture.py
#  Purpose:      Assert `make e2e-posture` switches on, restores and checks the Playwright posture
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The dfe-ui Playwright suite's posture: DFE_ENV=test in .env, the e2e routes and shipped admin password in env/engine.env."""

import stat
from pathlib import Path

import pytest

import e2e_posture

_MINTED = "s3cr3t-minted-value"


def _dotenv_text(*, extra: str = "", posture: str) -> str:
    """A .env in the given posture, with one key the switch must leave alone."""
    return f"DFE_ENV={posture}\nDFE_UI_PORT=3000\n{extra}"


def _engine_text(*, extra: str = "") -> str:
    """An env/engine.env holding a minted admin password."""
    return f"DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}\n{extra}"


def _saved(*, path: Path) -> Path:
    """The copy `on` keeps of one file."""
    return path.with_name(f"{path.name}.pre-e2e")


def _write(
    *, dotenv: Path, dotenv_text: str, engine_env: Path, engine_text: str
) -> None:
    """Write both env files."""
    dotenv.write_text(data=dotenv_text, encoding="utf-8", newline="\n")
    engine_env.write_text(data=engine_text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize("posture", ["dev", "test", ""])
def test_on_writes_the_posture_and_keeps_the_rest(
    dotenv: Path, engine_env: Path, posture: str
) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture=posture),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )

    assert e2e_posture.main(argv=["on"]) == 0

    dotenv_written = dotenv.read_text(encoding="utf-8")
    engine_written = engine_env.read_text(encoding="utf-8")
    assert "DFE_ENV=test" in dotenv_written
    assert "DFE_UI_PORT=3000" in dotenv_written
    assert "DFE_E2E_SERVER" not in dotenv_written
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD=changeme" in engine_written
    assert "DFE_E2E_SERVER=true" in engine_written


def test_a_deployment_is_refused_and_left_alone(
    capsys: pytest.CaptureFixture[str], dotenv: Path, engine_env: Path
) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture="production"),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )

    assert e2e_posture.main(argv=["on"]) == 2

    assert dotenv.read_text(encoding="utf-8") == _dotenv_text(posture="production")
    assert engine_env.read_text(encoding="utf-8") == _engine_text()
    assert not (_saved(path=dotenv).exists())
    assert not (_saved(path=engine_env).exists())
    assert "not a dev posture" in capsys.readouterr().err


def test_on_keeps_each_original_0600_and_a_second_on_does_not_replace_it(
    dotenv: Path, engine_env: Path
) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture="dev"),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )

    assert e2e_posture.main(argv=["on"]) == 0
    assert e2e_posture.main(argv=["on"]) == 0

    for path, original in (
        (dotenv, _dotenv_text(posture="dev")),
        (engine_env, _engine_text()),
    ):
        saved = _saved(path=path)
        assert saved.read_text(encoding="utf-8") == original
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_off_restores_both_files_and_removes_the_copies(
    dotenv: Path, engine_env: Path
) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture="dev"),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )
    e2e_posture.main(argv=["on"])

    assert e2e_posture.main(argv=["off"]) == 0

    assert dotenv.read_text(encoding="utf-8") == _dotenv_text(posture="dev")
    assert engine_env.read_text(encoding="utf-8") == _engine_text()
    assert not (_saved(path=dotenv).exists())
    assert not (_saved(path=engine_env).exists())


def test_off_with_nothing_saved_changes_nothing(dotenv: Path, engine_env: Path) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture="dev"),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )

    assert e2e_posture.main(argv=["off"]) == 0

    assert dotenv.read_text(encoding="utf-8") == _dotenv_text(posture="dev")
    assert engine_env.read_text(encoding="utf-8") == _engine_text()


@pytest.mark.parametrize(
    ("posture", "dotenv_extra", "engine_extra", "want", "named"),
    [
        pytest.param(
            "dev", "", "", 1, ["DFE_ENV=test", "DFE_E2E_SERVER=true"], id="plain_dev"
        ),
        pytest.param(
            "test", "", "", 1, ["DFE_E2E_SERVER=true"], id="test_without_the_server"
        ),
        pytest.param(
            "dev",
            "",
            "DFE_E2E_SERVER=true\n",
            1,
            ["DFE_ENV=test"],
            id="server_without_test",
        ),
        pytest.param(
            "test",
            "DFE_E2E_SERVER=true\n",
            "",
            1,
            ["DFE_E2E_SERVER=true"],
            id="server_in_dotenv_is_not_read",
        ),
        pytest.param("test", "", "DFE_E2E_SERVER=TRUE\n", 0, [], id="both_any_case"),
    ],
)
def test_check_names_what_is_missing(
    capsys: pytest.CaptureFixture[str],
    dotenv: Path,
    engine_env: Path,
    posture: str,
    dotenv_extra: str,
    engine_extra: str,
    want: int,
    named: list[str],
) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(extra=dotenv_extra, posture=posture),
        engine_env=engine_env,
        engine_text=_engine_text(extra=engine_extra),
    )

    assert e2e_posture.main(argv=["check"]) == want

    err = capsys.readouterr().err
    assert all(item in err for item in named)


def test_check_passes_after_on(dotenv: Path, engine_env: Path) -> None:
    _write(
        dotenv=dotenv,
        dotenv_text=_dotenv_text(posture="dev"),
        engine_env=engine_env,
        engine_text=_engine_text(),
    )
    e2e_posture.main(argv=["on"])

    assert e2e_posture.main(argv=["check"]) == 0


@pytest.mark.parametrize("present", ["dotenv", "engine_env", "neither"])
def test_a_missing_env_file_is_exit_one(
    dotenv: Path, engine_env: Path, present: str
) -> None:
    if present == "dotenv":
        dotenv.write_text(data=_dotenv_text(posture="dev"), encoding="utf-8")
    if present == "engine_env":
        engine_env.write_text(data=_engine_text(), encoding="utf-8")

    assert e2e_posture.main(argv=["on"]) == 1
