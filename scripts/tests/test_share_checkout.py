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


def _second_group() -> int | None:
    """Return a group the test user is in besides its primary one, else None."""
    others = sorted(set(os.getgroups()) - {os.getgid()})
    return others[0] if others else None


def _write_console_key(*, checkout: Path) -> Path:
    """Write certs/console.key the way scripts/proxy_tls.py leaves it: 0600 in a 0700 directory."""
    key = checkout / "certs" / "console.key"
    key.parent.mkdir()
    key.parent.chmod(0o700)
    key.write_text("key\n", encoding="utf-8")
    key.chmod(0o600)
    return key


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
    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    for directory in (checkout, checkout / "scripts" / "tests", checkout / "env"):
        assert _mode(directory) & 0o2070 == 0o2070


def test_files_gain_group_write_and_keep_world_read(checkout: Path) -> None:
    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    assert _mode(checkout / "Makefile") == 0o664
    assert _mode(checkout / ".env.example") == 0o664
    assert _mode(checkout / "scripts" / "run.sh") == 0o775


@pytest.mark.parametrize(
    "name", [".env", ".env.bak-1", "access-summary.md", "env/engine.env"]
)
def test_secrets_are_group_read_write_and_closed_to_others(
    checkout: Path, name: str
) -> None:
    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    assert _mode(checkout / name) == 0o660


def test_a_second_run_changes_nothing(checkout: Path) -> None:
    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    again = share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    assert again == 0


def test_dry_run_reports_and_touches_nothing(checkout: Path) -> None:
    before = _mode(checkout / ".env")

    changed = share_checkout.share_tree(
        dry_run=True, gid=os.getgid(), repo_dir=checkout
    )

    assert changed > 0
    assert _mode(checkout / ".env") == before


def test_a_symlink_is_not_followed(checkout: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("x", encoding="utf-8")
    outside.chmod(0o600)
    (checkout / "link").symlink_to(outside)

    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    assert _mode(outside) == 0o600


def test_the_cert_directory_keeps_its_modes(checkout: Path) -> None:
    key = _write_console_key(checkout=checkout)

    share_checkout.share_tree(dry_run=False, gid=os.getgid(), repo_dir=checkout)

    assert _mode(key) == 0o600
    assert _mode(key.parent) == 0o700


def test_the_cert_directory_keeps_its_group(checkout: Path) -> None:
    share_gid = _second_group()
    if share_gid is None:
        pytest.skip("the test user is in no second group to share with")
    key = _write_console_key(checkout=checkout)
    before = key.stat().st_gid

    share_checkout.share_tree(dry_run=False, gid=share_gid, repo_dir=checkout)

    assert (checkout / "Makefile").stat().st_gid == share_gid
    assert key.stat().st_gid == before
    assert _mode(key) == 0o600


def test_git_is_told_the_repository_is_group_shared(checkout: Path) -> None:
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    share_checkout.configure_git(dry_run=False, repo_dir=checkout)

    shared = subprocess.run(
        ["git", "-C", str(checkout), "config", "core.sharedRepository"],
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    assert shared.stdout.strip() == "group"
