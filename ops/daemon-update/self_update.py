#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         ops/daemon-update/self_update.py
#  Purpose:      Move a dfe-docker VM to the newest published stack when one ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Move a dfe-docker VM to the newest published stack when one ships."""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The tag discovery and the stack lock live in the checkout's scripts/
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
# Bytecode goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(_REPO_ROOT / ".tmp" / "pycache")

import _stack_lock  # noqa: E402
import stack  # noqa: E402
from _registry import (  # noqa: E402
    DISCOVERY_WORDS,
    RegistryError,
    _sort_key,
    images_version,
    latest_tag,
    manifest_repo,
)

# Gitignored, so it survives a fast-forward of the checkout
STATE_FILENAME = ".dfe-stack-applied"
_STATUS_ARGS = ("status", "--porcelain", "--untracked-files=no")
_TRUE_VALUES = {"1", "true", "yes"}


class UpdateError(Exception):
    """Raised when discovery or the update run fails."""


def _applied_version(*, state_path: Path) -> str | None:
    """Return the version this VM last applied, None when it has applied nothing."""
    if not (state_path.is_file()):
        return None
    return state_path.read_text(encoding="utf-8", errors="replace").strip() or None


def _apply(*, repo_dir: Path, version: str) -> None:
    """Refresh the checkout, then init, pin and start the version.

    `make clean` follows the pin because compose cannot run `down -v` on an unpinned tree, and follows the refresh so the compose file naming the volumes is the new version's own.
    """
    _refresh_checkout(repo_dir=repo_dir)
    targets = [["init"], ["stack", f"VERSION={version}"]]
    if _flag_set(name="DFE_UPDATE_WIPE_STATE"):
        _log(message="DFE_UPDATE_WIPE_STATE is set, deleting every volume first")
        targets.append(["clean"])
    targets.append(["ci"])
    for target in targets:
        _make(repo_dir=repo_dir, target=target)


def _flag_set(*, name: str) -> bool:
    """Return whether an environment flag is switched on."""
    return os.environ.get(name, "").strip() in _TRUE_VALUES


def _git(*, args: Sequence[str], repo_dir: Path) -> subprocess.CompletedProcess:
    """Run git in the checkout and capture what it prints."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


def _is_downgrade(*, applied: str, target: str) -> bool:
    """Return whether the target ranks below the applied version.

    A pair that cannot be ranked, such as a hand-written state file, is not a downgrade because there is no order to refuse on.
    """
    try:
        return _sort_key(version=target) < _sort_key(version=applied)
    except RegistryError:
        return False


def _log(*, message: str) -> None:
    """Write one line to the journal."""
    print(f"[dfe-docker-update] {message}", flush=True)


def _make(*, repo_dir: Path, target: Sequence[str]) -> None:
    """Run one make target with its output going straight to the journal."""
    command = " ".join(("make", *target))
    _log(message=f"running: {command}")
    result = subprocess.run(["make", *target], check=False, cwd=repo_dir)
    if result.returncode != 0:
        raise UpdateError(f"{command!r} failed (exit {result.returncode})")


def _refresh_checkout(*, repo_dir: Path) -> None:
    """Fast-forward the checkout, refusing uncommitted changes to tracked files."""
    if not ((repo_dir / ".git").exists()):
        _log(message="no .git, so the checkout stays put and compose may lag the pins")
        return
    dirty = _git(args=_STATUS_ARGS, repo_dir=repo_dir)
    if dirty.returncode != 0:
        raise UpdateError(f"git status failed: {dirty.stderr.strip()!r}")
    if dirty.stdout.strip():
        raise UpdateError(
            f"the checkout has uncommitted changes to tracked files: {dirty.stdout.strip()!r}"
        )
    _log(message="running: git pull --ff-only")
    pull = _git(args=("pull", "--ff-only"), repo_dir=repo_dir)
    if pull.returncode != 0:
        detail = pull.stderr.strip() or pull.stdout.strip()
        raise UpdateError(
            f"git pull --ff-only failed, the checkout needs a credential for the remote or has diverged from it: {detail!r}"
        )
    _log(message=pull.stdout.strip())


def _refresh_plan(*, repo_dir: Path) -> str:
    """Return what a refresh would do, for --dry-run."""
    if not ((repo_dir / ".git").exists()):
        return "be skipped, there is no .git, so compose may lag the pins"
    dirty = _git(args=_STATUS_ARGS, repo_dir=repo_dir)
    if dirty.returncode != 0:
        return f"fail, git status errored: {dirty.stderr.strip()!r}"
    if dirty.stdout.strip():
        return "refuse, the checkout has uncommitted changes to tracked files"
    return "run git pull --ff-only"


def _target(*, allow_prerelease: bool, repo: str) -> tuple[str, str]:
    """Return the version this box should be on and what `make stack` pins to reach it.

    A box following image releases has no stack tag to name that version, so it names the newest stack plus a digest of the image pins laid over it.
    """
    if not (stack._repin_requested()):
        latest = latest_tag(
            prereleases="include" if allow_prerelease else "exclude", repo=repo
        )
        return latest, latest
    word = "rc" if allow_prerelease else "latest"
    prereleases = DISCOVERY_WORDS[word]
    pins = stack._latest_image_pins(prereleases=prereleases)
    version = latest_tag(prereleases=prereleases, repo=repo)
    return images_version(pins=pins.values(), version=version), word


def _tick(*, dry_run: bool, repo_dir: Path) -> None:
    """Discover the newest version and apply it when it differs."""
    allow_prerelease = _flag_set(name="DFE_UPDATE_ALLOW_PRERELEASE")
    repo = manifest_repo()
    latest, pin = _target(allow_prerelease=allow_prerelease, repo=repo)
    state_path = repo_dir / STATE_FILENAME
    applied = _applied_version(state_path=state_path)
    _log(message=f"repo={repo!r} latest={latest!r} applied={applied!r}")

    if applied == latest:
        if dry_run:
            plan = _refresh_plan(repo_dir=repo_dir)
            _log(message=f"dry-run: already current, checkout refresh would {plan}")
            return
        _refresh_checkout(repo_dir=repo_dir)
        _log(message="already current, checkout refreshed")
        return
    if applied and _is_downgrade(applied=applied, target=latest):
        _log(
            message=f"refusing {applied!r} -> {latest!r}: the newest published stack is older than the one this VM applied"
        )
        return
    if dry_run:
        _log(message=f"dry-run: would update {applied!r} -> {latest!r}")
        _log(
            message=f"dry-run: checkout refresh would {_refresh_plan(repo_dir=repo_dir)}"
        )
        if _flag_set(name="DFE_UPDATE_WIPE_STATE"):
            _log(
                message="dry-run: DFE_UPDATE_WIPE_STATE is set, every volume would be deleted"
            )
        return

    _log(message=f"updating {applied!r} -> {latest!r}")
    _apply(repo_dir=repo_dir, version=pin)
    state_path.write_text(f"{latest}\n", encoding="utf-8", newline="\n")
    _log(message=f"updated to {latest!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the latest and applied versions and exit without updating",
    )
    args = parser.parse_args()

    try:
        # A dry run changes nothing, so it takes no lock
        if args.dry_run:
            _tick(dry_run=True, repo_dir=_REPO_ROOT)
            return 0
        with _stack_lock.stack_lock(directory=_REPO_ROOT) as held:
            if not (held):
                _log(
                    message=f"skipping this tick: a proof or another update holds {_stack_lock.LOCK_FILENAME!r}, the next tick retries"
                )
                return 0
            _tick(dry_run=False, repo_dir=_REPO_ROOT)
    except (RegistryError, UpdateError) as error:
        _log(message=f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
