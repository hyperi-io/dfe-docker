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
     `make init`, `make stack VERSION=<new>` and `make ci`, and record the
     version on success.

A target that ranks BELOW the applied version is refused at step 4 and logged. The
newest PUBLISHED manifest can be older than what a VM runs -- a box installed from
a cut branch is exactly that -- and applying it would roll the deployment
backwards.

A stack version is images PLUS the compose that runs them, so step 4 refreshes
the checkout first. Pinning new images against an old docker-compose.yml is a
half-update that reports success: the 2.2.0-rc.2 engine, for one, needs a
DFE_ENV declaration and a healthcheck path that older compose files do not have,
so the schema authority restart-loops while the timer records a clean run. Set
DFE_UPDATE_SKIP_GIT_PULL=1 on a box whose checkout is managed some other way.

The same goes for .env. A version can add a generated secret, and the power-on
self test fails while one is still at its built-in default, so `make init` tops
up the existing .env before the bring-up. It never overwrites a value.

DFE_UPDATE_WIPE_STATE=1 inserts `make clean` between the pin and the bring-up, so
a DISPOSABLE box re-initialises from empty volumes on every new version and the
first-start paths get exercised for real. It deletes ClickHouse, Kafka, the DLQ
spool and the engine's config volume -- sources, orgs and local accounts included,
none of which the image can reseed. Leave it unset anywhere the data matters.

The pull replaces this script mid-run; Python has already loaded it, so the
current tick finishes on the old code and the next uses the new.

Idempotent: when the VM is already on the newest version it is a no-op. Fails
loudly (non-zero) so systemd marks the unit failed and the next timer tick retries.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# The tag discovery is shared with `make stack VERSION=latest`, so it lives in
# the checkout's scripts/ and is imported from there. `_sort_key` comes with it so
# the floor below ranks versions the same way the discovery does.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _registry import (  # noqa: E402
    RegistryError,
    _sort_key,
    latest_tag,
    manifest_repo,
)

# Records the version this VM last successfully applied. Gitignored; lives beside
# the checkout so it survives across timer runs.
STATE_FILENAME = ".dfe-stack-applied"


class UpdateError(Exception):
    """Raised when discovery or the update run fails."""


def _log(message: str) -> None:
    print(f"[dfe-docker-update] {message}", flush=True)


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


def _applied_version(state_path: Path) -> str | None:
    if not state_path.is_file():
        return None
    value = state_path.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _is_downgrade(*, applied: str, target: str) -> bool:
    """Whether the published target ranks BELOW the version this VM already runs.

    A pair that cannot be ranked -- a hand-written state file, a tag that is not
    semver -- is not a downgrade: there is no ordering to refuse on.
    """
    try:
        return _sort_key(version=target) < _sort_key(version=applied)
    except RegistryError:
        return False


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


def _wipe_state_enabled() -> bool:
    """Whether this box destroys its data on every version change.

    OFF by default: `make clean` deletes ClickHouse, Kafka, the DLQ spool and the
    engine's config volume, and none of that is recoverable. Only a box whose data
    is expendable should set it.
    """
    return os.environ.get("DFE_UPDATE_WIPE_STATE", "").strip() in {"1", "true", "yes"}


def _apply(repo_dir: Path, version: str) -> None:
    """Run OUR updater: refresh the checkout, top up .env, pin the version, then pull + restart.

    With DFE_UPDATE_WIPE_STATE set, `make clean` runs between the pin and the
    bring-up so the new version initialises from nothing -- the disposable-VM
    workflow, where the point is to exercise the first-start paths rather than to
    keep data. It sits inside the version-change branch its caller already guards,
    so a wipe costs one new stack version, not one timer tick.

    The slot is not arbitrary. It follows `make stack` because docker-compose.yml
    declares its image pins `:?`-required and compose resolves those before it runs
    anything, so an unpinned tree aborts `down -v` exactly as it aborts `up`. And it
    follows the checkout refresh, so the compose file naming the volume set is the
    one shipping with the version being installed.
    """
    _refresh_checkout(repo_dir)
    commands = [["make", "init"], ["make", "stack", f"VERSION={version}"]]
    if _wipe_state_enabled():
        _log("DFE_UPDATE_WIPE_STATE is set -- deleting every volume before bring-up")
        commands.append(["make", "clean"])
    commands.append(["make", "ci"])
    for cmd in commands:
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
        repo = manifest_repo()
        latest = latest_tag(
            prereleases="include" if allow_prerelease else "exclude", repo=repo
        )
        applied = _applied_version(state_path)
        _log(f"repo={repo} latest={latest} applied={applied or '(none)'}")

        if applied == latest:
            _log("already current - nothing to do")
            return 0
        if applied and _is_downgrade(applied=applied, target=latest):
            _log(
                f"refusing {applied} -> {latest}: the newest published manifest is "
                "older than the version this VM has applied, so the update would "
                "roll it backwards"
            )
            return 0
        if args.dry_run:
            _log(f"dry-run: would update {applied or '(none)'} -> {latest}")
            _log(f"dry-run: checkout refresh would {_refresh_plan(repo_dir)}")
            if _wipe_state_enabled():
                _log(
                    "dry-run: DFE_UPDATE_WIPE_STATE is set -- EVERY volume would be "
                    "deleted (ClickHouse, Kafka, DLQ spool, engine config)"
                )
            return 0

        _log(f"updating {applied or '(none)'} -> {latest}")
        _apply(repo_dir, latest)
        _record_applied(state_path, latest)
        _log(f"updated to {latest}")
    except (RegistryError, UpdateError) as error:
        _log(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
