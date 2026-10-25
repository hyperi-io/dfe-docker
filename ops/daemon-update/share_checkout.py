#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         ops/daemon-update/share_checkout.py
#  Purpose:      Make a dfe-docker checkout writable by its service user AND an operator group
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Share a dfe-docker checkout between the update daemon's user and an operator group.

The daemon runs as a service user (dfe) and creates files in the checkout. An
operator who logs in as someone else then cannot run `git pull`, `make stack` or
`make post` there without `sudo -u dfe`: directories lack group write, `.env` is
0640 and git's object directories belong to the service user alone.

This puts the checkout in the shape both sides can work in:

- every directory is group-writable and setgid, so a new file takes the group;
- every file is group-writable;
- the files that hold secrets (`.env`, `.env.*`, `access-summary.md`, everything in
  `env/`) are group read/write and closed to others, never wider;
- git is told the repository is group-shared (`core.sharedRepository group`), and
  the checkout is a `safe.directory`, so git accepts an owner who is not the caller;
- each `--operator` user joins the group.

Run it once as root, from an existing install. It is idempotent: a second run
changes nothing. `--dry-run` lists what a run would change and touches nothing.

    sudo python3 ops/daemon-update/share_checkout.py --repo-dir /opt/dfe-docker \
        --operator ubuntu
"""

import argparse
import grp
import os
import pwd
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

GROUP_DIR_BITS = stat.S_IRWXG | stat.S_ISGID
GROUP_FILE_BITS = stat.S_IRGRP | stat.S_IWGRP
OTHERS_BITS = stat.S_IRWXO


class ShareError(Exception):
    """Raised when the checkout cannot be shared as asked."""


def _log(message: str) -> None:
    print(f"[share-checkout] {message}", flush=True)


def is_secret(*, relative: Path) -> bool:
    """Whether a path inside the checkout holds secrets and must stay closed to others.

    Args:
        relative: The path relative to the checkout root.

    Returns:
        True for `.env`, `.env.*` other than the committed template, the access
        summary, and anything under `env/`.
    """
    name = relative.name
    if relative.parts[0] == "env":
        return True
    if len(relative.parts) > 1:
        return False
    return (
        name == "access-summary.md"
        or name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
    )


def wanted_mode(*, mode: int, is_dir: bool, secret: bool) -> int:
    """Return the mode a path should have for group sharing.

    Args:
        mode: The path's permission bits now (including setgid).
        is_dir: Whether the path is a directory.
        secret: Whether the path holds secrets.

    Returns:
        The mode with group read/write added, group execute where the owner has it,
        setgid on a directory, and every bit for others removed on a secret file.
    """
    if is_dir:
        return mode | GROUP_DIR_BITS
    wanted = mode | GROUP_FILE_BITS
    if mode & stat.S_IXUSR:
        wanted |= stat.S_IXGRP
    if secret:
        wanted &= ~OTHERS_BITS
    return wanted


def _entries(*, repo_dir: Path) -> Iterator[Path]:
    """Yield the checkout root and every path below it, never following a symlink."""
    yield repo_dir
    for root, dirs, files in os.walk(repo_dir, followlinks=False):
        for name in (*dirs, *files):
            yield Path(root) / name


def share_tree(*, repo_dir: Path, gid: int, dry_run: bool) -> int:
    """Give the checkout's tree the group and modes sharing needs.

    Args:
        repo_dir: The dfe-docker checkout.
        gid: The group that shares it.
        dry_run: List the changes without making them.

    Returns:
        The number of paths changed (or that would be).
    """
    changed = 0
    for path in _entries(repo_dir=repo_dir):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        relative = path.relative_to(repo_dir) if path != repo_dir else Path(".")
        is_dir = stat.S_ISDIR(info.st_mode)
        current = stat.S_IMODE(info.st_mode)
        wanted = wanted_mode(
            mode=current,
            is_dir=is_dir,
            secret=relative != Path(".")
            and not is_dir
            and is_secret(relative=relative),
        )
        if info.st_gid == gid and current == wanted:
            continue
        changed += 1
        _log(
            f"{'would set' if dry_run else 'set'} {relative} "
            f"group {gid} mode {current:04o} -> {wanted:04o}"
        )
        if dry_run:
            continue
        if info.st_gid != gid:
            os.chown(path, -1, gid, follow_symlinks=False)
        if current != wanted:
            path.chmod(wanted)
    return changed


def _git(*args: str, as_uid: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        user=as_uid,
    )


def configure_git(*, repo_dir: Path, dry_run: bool) -> None:
    """Tell git the repository is group-shared, and trust it for every user.

    `core.sharedRepository group` makes git create group-writable objects, which is
    what lets a second user `git pull` into a repository the first one created.
    The `.git/config` edit runs as that file's owner, so the file keeps its owner.
    The system-level `safe.directory` entry is what stops git refusing a repository
    owned by someone other than the caller, and needs root.

    Args:
        repo_dir: The dfe-docker checkout.
        dry_run: Report without changing.
    """
    config = repo_dir / ".git" / "config"
    if not config.is_file():
        _log("not a git checkout -- git left alone")
        return
    owner = config.stat().st_uid if os.geteuid() == 0 else None
    shared = _git("config", "--file", str(config), "core.sharedRepository")
    if shared.stdout.strip() != "group":
        _log(f"{'would set' if dry_run else 'set'} core.sharedRepository group")
        if not dry_run:
            out = _git(
                *("config", "--file", str(config)),
                *("core.sharedRepository", "group"),
                as_uid=owner,
            )
            if out.returncode != 0:
                raise ShareError(f"git config failed: {out.stderr.strip()}")

    if os.geteuid() != 0:
        _log("not root -- safe.directory left alone (system git config needs root)")
        return
    trusted = _git("config", "--system", "--get-all", "safe.directory")
    if str(repo_dir) in trusted.stdout.split():
        return
    _log(f"{'would add' if dry_run else 'add'} system safe.directory {repo_dir}")
    if not dry_run:
        out = _git("config", "--system", "--add", "safe.directory", str(repo_dir))
        if out.returncode != 0:
            raise ShareError(f"git config --system failed: {out.stderr.strip()}")


def add_operator(*, user: str, group: str, dry_run: bool) -> None:
    """Add an operator to the sharing group, unless it is already a member.

    Args:
        user: The operator's login name.
        group: The sharing group.
        dry_run: Report without changing.

    Raises:
        ShareError: If the user does not exist or `usermod` fails.
    """
    try:
        account = pwd.getpwnam(user)
    except KeyError as error:
        raise ShareError(f"no such user: {user}") from error
    entry = grp.getgrnam(group)
    if account.pw_gid == entry.gr_gid or user in entry.gr_mem:
        return
    _log(f"{'would add' if dry_run else 'add'} {user} to group {group}")
    if dry_run:
        return
    out = subprocess.run(
        ["usermod", "-aG", group, user],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise ShareError(f"usermod failed: {out.stderr.strip()}")
    _log(f"{user} must log in again for the new group to apply")


def main() -> int:
    """Parse the command line and share the checkout."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0].strip())
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.environ.get("DFE_DOCKER_DIR", ".")).expanduser(),
        help="the dfe-docker checkout (default: $DFE_DOCKER_DIR or cwd)",
    )
    parser.add_argument(
        "--group",
        help="the sharing group (default: the checkout directory's own group)",
    )
    parser.add_argument(
        "--operator",
        action="append",
        default=[],
        metavar="USER",
        help="a login to add to the group; repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    args = parser.parse_args()

    repo_dir = args.repo_dir.resolve()
    try:
        if not (repo_dir / "Makefile").is_file():
            raise ShareError(f"{repo_dir} is not a dfe-docker checkout (no Makefile)")
        gid = grp.getgrnam(args.group).gr_gid if args.group else repo_dir.stat().st_gid
        group = grp.getgrgid(gid).gr_name
        _log(f"sharing {repo_dir} with group {group}")
        changed = share_tree(repo_dir=repo_dir, gid=gid, dry_run=args.dry_run)
        configure_git(repo_dir=repo_dir, dry_run=args.dry_run)
        for user in args.operator:
            add_operator(user=user, group=group, dry_run=args.dry_run)
    except (ShareError, KeyError, PermissionError) as error:
        _log(f"error: {error}")
        return 1
    _log(f"{changed} path(s) {'would change' if args.dry_run else 'changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
