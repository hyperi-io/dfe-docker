#  Project:      dfe-docker
#  File:         tests/test_self_update.py
#  Purpose:      Assert the VM updater's version floor and update command order
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

import subprocess
import sys
from pathlib import Path

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "ops" / "daemon-update"
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

import _stack_lock  # noqa: E402
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


def test_a_current_version_still_fast_forwards_the_checkout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _checkout(applied="2.2.1-rc.6", tmp_path=tmp_path)
    applied = _discovery(monkeypatch=monkeypatch, latest="2.2.1-rc.6")
    refreshed: list[Path] = []
    monkeypatch.setattr(self_update, "_refresh_checkout", refreshed.append)
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 0
    assert refreshed == [repo]
    assert applied == []
    assert "checkout refreshed" in capsys.readouterr().out


def test_a_current_version_with_a_refused_refresh_fails_the_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _checkout(applied="2.2.1-rc.6", tmp_path=tmp_path)
    _discovery(monkeypatch=monkeypatch, latest="2.2.1-rc.6")

    def refuse(repo_dir: Path) -> None:
        raise self_update.UpdateError("the checkout has uncommitted changes")

    monkeypatch.setattr(self_update, "_refresh_checkout", refuse)
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 1


def test_a_current_version_dry_run_touches_nothing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _checkout(applied="2.2.1-rc.6", tmp_path=tmp_path)
    _discovery(monkeypatch=monkeypatch, latest="2.2.1-rc.6")
    refreshed: list[Path] = []
    monkeypatch.setattr(self_update, "_refresh_checkout", refreshed.append)
    monkeypatch.setattr(
        self_update, "_refresh_plan", lambda repo_dir: "run `git pull --ff-only`"
    )
    monkeypatch.setattr(
        sys, "argv", ["self_update.py", "--repo-dir", str(repo), "--dry-run"]
    )

    assert self_update.main() == 0
    assert refreshed == []
    assert (
        "dry-run: already current; checkout refresh would run"
        in capsys.readouterr().out
    )


def test_a_newer_manifest_is_still_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    applied = _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.14")
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 0
    assert applied == [(repo, "2.2.0-rc.14")]
    assert (repo / self_update.STATE_FILENAME).read_text().strip() == "2.2.0-rc.14"


@pytest.mark.parametrize(
    ("wipe", "expected"),
    [
        ("", ["init", "stack VERSION=2.2.0", "ci"]),
        ("1", ["init", "stack VERSION=2.2.0", "clean", "ci"]),
    ],
)
def test_an_update_tops_up_env_before_the_bring_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wipe: str, expected: list[str]
) -> None:
    """A version that adds a generated secret fails the self test on an old .env."""
    ran: list[str] = []
    monkeypatch.setenv("DFE_UPDATE_WIPE_STATE", wipe)
    monkeypatch.setattr(self_update, "_refresh_checkout", lambda repo_dir: None)

    def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        ran.append(" ".join(cmd[1:]))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(self_update, "_run", _run)
    self_update._apply(tmp_path, "2.2.0")
    assert ran == expected


def test_a_tick_skips_while_a_proof_holds_the_stack_lock(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    applied = _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.14")
    refreshed: list[Path] = []
    monkeypatch.setattr(self_update, "_refresh_checkout", refreshed.append)
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    with _stack_lock.stack_lock(directory=repo) as proof_holds_it:
        assert proof_holds_it
        assert self_update.main() == 0

    assert applied == []
    assert refreshed == []
    assert (repo / self_update.STATE_FILENAME).read_text().strip() == "2.2.0-rc.13"
    assert "skipping this tick" in capsys.readouterr().out


def test_the_stack_lock_is_held_while_an_update_applies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    free_during_apply: list[bool] = []

    def probing_apply(repo_dir: Path, version: str) -> None:
        with _stack_lock.stack_lock(directory=repo_dir) as other:
            free_during_apply.append(other)

    _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.14")
    monkeypatch.setattr(self_update, "_apply", probing_apply)
    monkeypatch.setattr(sys, "argv", ["self_update.py", "--repo-dir", str(repo)])

    assert self_update.main() == 0
    assert free_during_apply == [False]
    with _stack_lock.stack_lock(directory=repo) as after:
        assert after


def test_a_dry_run_ignores_the_stack_lock(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
    _discovery(monkeypatch=monkeypatch, latest="2.2.0-rc.14")
    monkeypatch.setattr(self_update, "_refresh_plan", lambda repo_dir: "pull")
    monkeypatch.setattr(
        sys, "argv", ["self_update.py", "--repo-dir", str(repo), "--dry-run"]
    )
    with _stack_lock.stack_lock(directory=repo):
        assert self_update.main() == 0
    assert "dry-run: would update" in capsys.readouterr().out
