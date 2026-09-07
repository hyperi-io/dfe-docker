#  Project:      dfe-docker
#  File:         tests/test_creds.py
#  Purpose:      Assert the admin password prints to a terminal and nowhere else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The access summary is the one place the minted admin password is printed.

`make up` and `make dev` end with it, so its stdout is a build log as often as it
is a terminal. These pin the branch: the value on a TTY, the .env key that holds
it otherwise, and DFE_CREDS_SHOW=0 taking the second branch on a TTY too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import creds

_MINTED = "s3cr3t-minted-value"
_ENV = f"""\
DFE_ENV=dev
DFE_AUTH_LOCAL_ADMIN_NAME=admin
DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}
DFE_AUTH_BREAKGLASS_PASSWORD=another-minted-value
"""


def test_tty_prints_the_password(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert _MINTED in capsys.readouterr().out


def test_non_tty_prints_the_fetch_hint_instead(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    out = capsys.readouterr().out
    assert _MINTED not in out
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in out


def test_show_zero_hides_it_on_a_tty(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("DFE_CREDS_SHOW", "0")

    assert creds.main() == 0
    out = capsys.readouterr().out
    assert _MINTED not in out
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in out


def test_the_break_glass_password_is_never_printed(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert "another-minted-value" not in capsys.readouterr().out


def test_a_missing_dotenv_reports_rather_than_prints(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert creds.main() == 1
    assert "make init" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("is_tty", "setting", "expected"),
    [
        (True, "", True),
        (True, "0", False),
        (True, "false", False),
        (True, "1", True),
        (False, "", False),
        (False, "1", False),
    ],
)
def test_show_password_rule(is_tty: bool, setting: str, expected: bool) -> None:
    assert creds.show_password(is_tty=is_tty, setting=setting) is expected
