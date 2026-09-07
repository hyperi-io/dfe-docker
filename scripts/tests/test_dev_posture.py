#  Project:      dfe-docker
#  File:         tests/test_dev_posture.py
#  Purpose:      Assert `make dev` refuses a deployment and backs up what it rewrites
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make dev` overwrites a minted admin password with the shipped default.

That is right on a dev box and destructive anywhere else, so the two properties
worth pinning are the refusal on a non-dev DFE_ENV and the backup taken before
the rewrite -- a minted password is unrecoverable once overwritten.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import dev_posture

_MINTED = "s3cr3t-minted-value"
_DEPLOYMENT = f"""\
## A deployment's .env.
DFE_ENV=production
DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}
DFE_UI_PORT=3000
"""
_DEV_BOX = _DEPLOYMENT.replace("production", "dev")
_NO_POSTURE = f"""\
DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}
DFE_UI_PORT=3000
"""


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob(".env.bak-*"))


def test_a_non_dev_posture_is_refused(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_DEPLOYMENT, encoding="utf-8", newline="\n")

    assert dev_posture.main() == 2
    assert dotenv.read_text(encoding="utf-8") == _DEPLOYMENT
    assert _backups(dotenv.parent) == []
    assert "not a dev posture" in capsys.readouterr().err


def test_a_missing_dotenv_is_a_different_exit_code(dotenv: Path) -> None:
    assert dev_posture.main() == 1


def test_the_rewrite_sets_both_keys_and_leaves_the_rest(dotenv: Path) -> None:
    dotenv.write_text(_DEV_BOX, encoding="utf-8", newline="\n")

    assert dev_posture.main() == 0
    written = dotenv.read_text(encoding="utf-8")
    assert "DFE_ENV=dev" in written
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD=changeme" in written
    assert _MINTED not in written
    assert "DFE_UI_PORT=3000" in written


def test_an_absent_posture_is_written_rather_than_refused(dotenv: Path) -> None:
    dotenv.write_text(_NO_POSTURE, encoding="utf-8", newline="\n")

    assert dev_posture.main() == 0
    assert "DFE_ENV=dev" in dotenv.read_text(encoding="utf-8")


def test_the_replaced_file_is_backed_up_0600_and_named(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_DEV_BOX, encoding="utf-8", newline="\n")

    assert dev_posture.main() == 0
    backups = _backups(dotenv.parent)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == _DEV_BOX
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert backups[0].name in capsys.readouterr().err


def test_a_second_run_changes_nothing_and_takes_no_backup(dotenv: Path) -> None:
    dotenv.write_text(_DEV_BOX, encoding="utf-8", newline="\n")
    assert dev_posture.main() == 0
    settled = dotenv.read_text(encoding="utf-8")

    assert dev_posture.main() == 0
    assert dotenv.read_text(encoding="utf-8") == settled
    assert len(_backups(dotenv.parent)) == 1
