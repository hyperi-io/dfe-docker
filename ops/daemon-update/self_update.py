#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         ops/daemon-update/self_update.py
#  Purpose:      Keep a dfe-docker VM current using OUR updater (make stack + ci)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Daemon-driven self-update for a dfe-docker single-VM deployment.

The stack's own updater (`make stack VERSION=X.Y.Z` -> pin from the signed OCI
stack-manifest -> `make ci` -> pull + up -d) is EXPLICIT-VERSION by design: it
hard-fails rather than ever pull `latest`. So a "stay current" daemon cannot just
call it - it must first DISCOVER the newest certified stack version, compare it
to what this VM last applied, and only run the updater when there is something
new. That is this script; a systemd timer runs it on a schedule.

Flow:
  1. List tags on the OCI stack-manifest repo (`oras repo tags`).
  2. Pick the highest STABLE semver (pre-releases skipped unless
     DFE_UPDATE_ALLOW_PRERELEASE=1).
  3. Compare to the last-applied version recorded in the state file.
  4. If newer (or nothing applied yet): fast-forward the checkout, then
     `make stack VERSION=<new>` then `make ci`, and record the version on success.

A stack version is images PLUS the compose that runs them, so step 4 refreshes
the checkout first. Pinning new images against an old docker-compose.yml is a
half-update that reports success: the 2.2.0-rc.2 engine, for one, needs a
DFE_ENV declaration and a healthcheck path that older compose files do not have,
so the schema authority restart-loops while the timer records a clean run. Set
DFE_UPDATE_SKIP_GIT_PULL=1 on a box whose checkout is managed some other way.

The pull replaces this script mid-run; Python has already loaded it, so the
current tick finishes on the old code and the next uses the new.

Idempotent: when the VM is already on the newest version it is a no-op. Fails
loudly (non-zero) so systemd marks the unit failed and the next timer tick retries.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# The signed stack-manifest artifact repo (mirrors scripts/stack.py's default).
DEFAULT_MANIFEST_REPO = "ghcr.io/hyperi-io/dfe-stack-manifest"

# X.Y.Z with an optional -rc.N / -beta.N style pre-release suffix.
_SEMVER = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)

# Records the version this VM last successfully applied. Gitignored; lives beside
# the checkout so it survives across timer runs.
STATE_FILENAME = ".dfe-stack-applied"


class UpdateError(Exception):
    """Raised when discovery or the update run fails."""


def _log(message: str) -> None:
    print(f"[dfe-docker-update] {message}", flush=True)


def _repo() -> str:
    return (
        os.environ.get("DFE_STACK_MANIFEST_REPO", "").strip() or DEFAULT_MANIFEST_REPO
    )


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _list_tags(repo: str) -> list[str]:
    """Return the tags on the manifest repo via `oras repo tags`."""
    import shutil

    if shutil.which("oras") is None:
        raise UpdateError("`oras` is not installed - cannot list stack-manifest tags")
    out = _run(["oras", "repo", "tags", repo])
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise UpdateError(f"`oras repo tags {repo}` failed: {detail}")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _pre_key(pre: str) -> tuple:
    """Pre-release precedence: all-digit identifiers compare numerically and rank below alphanumeric ones."""
    return tuple(
        (0, int(ident), "") if ident.isdigit() else (1, 0, ident)
        for ident in pre.split(".")
    )


def _sort_key(version: str) -> tuple:
    """Semver sort key. A release ranks ABOVE its own pre-releases."""
    m = _SEMVER.match(version)
    if m is None:
        raise UpdateError(f"not a semver tag: {version!r}")
    core = (int(m["major"]), int(m["minor"]), int(m["patch"]))
    # No pre-release sorts higher than any pre-release of the same core.
    return (*core, 1) if m["pre"] is None else (*core, 0, _pre_key(pre=m["pre"]))


def _is_stable(version: str) -> bool:
    m = _SEMVER.match(version)
    return m is not None and m["pre"] is None


def _latest_version(repo: str, allow_prerelease: bool) -> str:
    tags = [t for t in _list_tags(repo) if _SEMVER.match(t)]
    if not allow_prerelease:
        tags = [t for t in tags if _is_stable(t)]
    if not tags:
        kind = "semver" if allow_prerelease else "stable semver"
        raise UpdateError(f"no {kind} tags found on {repo}")
    return max(tags, key=_sort_key)


def _applied_version(state_path: Path) -> str | None:
    if not state_path.is_file():
        return None
    value = state_path.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _record_applied(state_path: Path, version: str) -> None:
    state_path.write_text(version + "\n", encoding="utf-8", newline="\n")


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo_dir), *args])


def _checkout_status(repo_dir: Path) -> str | None:
    """Return why the checkout cannot be fast-forwarded, or None when it can."""
    if not (repo_dir / ".git").exists():
        return "not a git checkout"
    if os.environ.get("DFE_UPDATE_SKIP_GIT_PULL", "").strip() in {"1", "true", "yes"}:
        return "DFE_UPDATE_SKIP_GIT_PULL is set"
    return None


def _refresh_checkout(repo_dir: Path) -> None:
    """Fast-forward the checkout so the compose file matches the pins about to land.

    Refuses on a dirty tree rather than discarding an operator's edit. A deployed
    box should be clean: .env, env/, deployment.yaml and the applied-state file
    are all gitignored.
    """
    skip = _checkout_status(repo_dir)
    if skip is not None:
        _log(f"checkout refresh skipped ({skip}) -- compose may lag the pins")
        return

    dirty = _git(repo_dir, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        raise UpdateError(f"`git status` failed: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise UpdateError(
            "the checkout has uncommitted changes to tracked files -- refusing to "
            f"pull over them:\n{dirty.stdout.strip()}"
        )

    _log("running: git pull --ff-only")
    out = _git(repo_dir, "pull", "--ff-only")
    sys.stdout.write(out.stdout)
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise UpdateError(
            f"`git pull --ff-only` failed: {detail} -- the checkout needs a "
            "credential for the remote, or has diverged from it"
        )


def _refresh_plan(repo_dir: Path) -> str:
    """What `_refresh_checkout` would do, for --dry-run. Touches nothing."""
    skip = _checkout_status(repo_dir)
    if skip is not None:
        return f"be SKIPPED ({skip}) -- compose may lag the pins"
    dirty = _git(repo_dir, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        return f"FAIL -- `git status` errored: {dirty.stderr.strip()}"
    if dirty.stdout.strip():
        return "REFUSE -- the checkout has uncommitted changes to tracked files"
    return "run `git pull --ff-only`"


def _apply(repo_dir: Path, version: str) -> None:
    """Run OUR updater: refresh the checkout, pin the version, then pull + restart."""
    _refresh_checkout(repo_dir)
    for cmd in (["make", "stack", f"VERSION={version}"], ["make", "ci"]):
        _log(f"running: {' '.join(cmd)}")
        out = _run(cmd, cwd=repo_dir)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr)
        if out.returncode != 0:
            raise UpdateError(f"`{' '.join(cmd)}` failed (exit {out.returncode})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0].strip())
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.environ.get("DFE_DOCKER_DIR", ".")).expanduser(),
        help="the dfe-docker checkout to operate on (default: $DFE_DOCKER_DIR or cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the latest vs applied version and exit without updating",
    )
    args = parser.parse_args()

    allow_prerelease = os.environ.get("DFE_UPDATE_ALLOW_PRERELEASE", "").strip() in {
        "1",
        "true",
        "yes",
    }
    repo_dir = args.repo_dir.resolve()
    state_path = repo_dir / STATE_FILENAME

    try:
        if not (repo_dir / "Makefile").is_file():
            raise UpdateError(f"{repo_dir} is not a dfe-docker checkout (no Makefile)")
        repo = _repo()
        latest = _latest_version(repo, allow_prerelease)
        applied = _applied_version(state_path)
        _log(f"repo={repo} latest={latest} applied={applied or '(none)'}")

        if applied == latest:
            _log("already current - nothing to do")
            return 0
        if args.dry_run:
            _log(f"dry-run: would update {applied or '(none)'} -> {latest}")
            _log(f"dry-run: checkout refresh would {_refresh_plan(repo_dir)}")
            return 0

        _log(f"updating {applied or '(none)'} -> {latest}")
        _apply(repo_dir, latest)
        _record_applied(state_path, latest)
        _log(f"updated to {latest}")
    except UpdateError as error:
        _log(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
