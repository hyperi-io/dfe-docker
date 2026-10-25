#  Project:      dfe-docker
#  File:         tests/test_share_checkout.py
#  Purpose:      Assert a shared checkout is group-writable and its secrets stay closed to others
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The one-time conversion that lets an operator group work in the daemon's checkout.

A test cannot change a file's group to one the test user is not in, so these run
with the user's own group and assert the modes, which is the part that varies.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "ops" / "daemon-update"
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

import share_checkout  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout shaped like one the daemon created: owner-only files and dirs."""
    root = tmp_path / "dfe-docker"
    (root / "scripts" / "tests").mkdir(parents=True)
    (root / "env").mkdir()
    (root / "Makefile").write_text("", encoding="utf-8")
    (root / "scripts" / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (root / "scripts" / "run.sh").write_text("", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / ".env.example").write_text("", encoding="utf-8")
    (root / ".env.bak-1").write_text("SECRET=1\n", encoding="utf-8")
    (root / "env" / "engine.env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "access-summary.md").write_text("pw\n", encoding="utf-8")
    for directory in (root, root / "scripts", root / "scripts" / "tests", root / "env"):
        directory.chmod(0o755)
    for secret in (
        root / ".env",
        root / ".env.bak-1",
        root / "access-summary.md",
        root / "env" / "engine.env",
    ):
        secret.chmod(0o644)
    (root / "Makefile").chmod(0o644)
    (root / "scripts" / "tests" / "test_x.py").chmod(0o644)
    (root / "scripts" / "run.sh").chmod(0o755)
    (root / ".env.example").chmod(0o644)
    return root


def test_directories_become_group_writable_and_setgid(checkout: Path) -> None:
    share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    for directory in (checkout, checkout / "scripts" / "tests", checkout / "env"):
        assert _mode(directory) & 0o2070 == 0o2070


def test_files_gain_group_write_and_keep_world_read(checkout: Path) -> None:
    share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    assert _mode(checkout / "Makefile") == 0o664
    assert _mode(checkout / ".env.example") == 0o664
    assert _mode(checkout / "scripts" / "run.sh") == 0o775


@pytest.mark.parametrize(
    "name", [".env", ".env.bak-1", "access-summary.md", "env/engine.env"]
)
def test_secrets_are_group_read_write_and_closed_to_others(
    checkout: Path, name: str
) -> None:
    share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    assert _mode(checkout / name) == 0o660


def test_a_second_run_changes_nothing(checkout: Path) -> None:
    share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    again = share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    assert again == 0


def test_dry_run_reports_and_touches_nothing(checkout: Path) -> None:
    before = _mode(checkout / ".env")

    changed = share_checkout.share_tree(
        repo_dir=checkout, gid=os.getgid(), dry_run=True
    )

    assert changed > 0
    assert _mode(checkout / ".env") == before


def test_a_symlink_is_not_followed(checkout: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("x", encoding="utf-8")
    outside.chmod(0o600)
    (checkout / "link").symlink_to(outside)

    share_checkout.share_tree(repo_dir=checkout, gid=os.getgid(), dry_run=False)

    assert _mode(outside) == 0o600


def test_git_is_told_the_repository_is_group_shared(checkout: Path) -> None:
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    share_checkout.configure_git(repo_dir=checkout, dry_run=False)

    shared = subprocess.run(
        ["git", "-C", str(checkout), "config", "core.sharedRepository"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shared.stdout.strip() == "group"
