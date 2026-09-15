#  Project:      dfe-docker
#  File:         tests/test_self_update.py
#  Purpose:      Assert the VM updater refuses a target older than the applied version
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The floor under the daemon updater.

The newest PUBLISHED manifest is not always the newest version a VM runs: a box
installed from a cut branch sits ahead of the registry, and without a floor the
next timer tick would install the older manifest over it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "ops" / "daemon-update"
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

import self_update  # noqa: E402


@pytest.mark.parametrize(
    ("applied", "target", "downgrade"),
    [
        ("2.2.0-rc.13", "2.2.0-rc.12", True),
        ("2.2.0-rc.13", "2.2.0-rc.14", False),
        ("2.2.0-rc.13", "2.2.0-rc.13", False),
        ("2.2.0", "2.2.0-rc.14", True),
        ("2.2.0-rc.14", "2.2.0", False),
        ("2.3.0", "2.2.9", True),
        # A hand-written state file cannot be ranked, so there is nothing to refuse.
        ("hand-edited", "2.2.0-rc.12", False),
        ("2.2.0-rc.13", "not-a-version", False),
    ],
)
def test_the_floor_ranks_the_target_against_the_applied_version(
    applied: str, target: str, downgrade: bool
) -> None:
    assert self_update._is_downgrade(applied=applied, target=target) is downgrade


def _checkout(*, applied: str, tmp_path: Path) -> Path:
    (tmp_path / "Makefile").write_text("", encoding="utf-8")
    (tmp_path / self_update.STATE_FILENAME).write_text(applied + "\n", encoding="utf-8")
    return tmp_path


def _discovery(*, monkeypatch: pytest.MonkeyPatch, latest: str) -> list[tuple]:
    applied: list[tuple] = []
    monkeypatch.setattr(self_update, "manifest_repo", lambda: "ghcr.io/test/manifest")
    monkeypatch.setattr(self_update, "latest_tag", lambda **kwargs: latest)
    monkeypatch.setattr(
        self_update, "_apply", lambda *args: applied.append(args) or None
    )
    return applied


def test_a_manifest_older_than_the_applied_version_is_refused(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    applied = _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.12")
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 0
    assert applied == []
    assert (repo / self_update.STATE_FILENAME).read_text().strip() == "2.2.0-rc.13"
    assert "refusing 2.2.0-rc.13 -> 2.2.0-rc.12" in capsys.readouterr().out


def test_a_newer_manifest_is_still_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    applied = _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.14")
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 0
    assert applied == [(repo, "2.2.0-rc.14")]
    assert (repo / self_update.STATE_FILENAME).read_text().strip() == "2.2.0-rc.14"
