#  Project:      dfe-docker
#  File:         tests/test_make_apply.py
#  Purpose:      Assert what the start and apply goals run, read off a dry run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The command lines `make apply` and the start goals print, in a copy of the Makefile.

These goals include .profile.mk, which scripts/resolve_profile.py writes from the
real profiles and the engine's instance index. A stand-in resolver writes a fixed
one instead, so what is asserted is the Makefile's own wiring: apply removes the
instance containers the fragment no longer declares before it recreates anything,
against the same compose project, and a start goal on a checkout with no .env has
it minted before its first compose call.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Written only when it differs, as the real resolver does: make re-reads a
# remade include, so rewriting it every run would never settle.
_RESOLVER = '''\
from pathlib import Path

target = Path(".profile.mk")
content = """\\
export PROFILE_FLAGS := --profile clickhouse
export DFE_SERVICES := dfe-loader dfe-transform-vrl-cisco-ios
export DFE_INSTANCES_RESOLVED := true
export DFE_AUTH_RESOLVED := false
export DFE_OTEL_RESOLVED := false
"""
if not target.is_file() or target.read_text(encoding="utf-8") != content:
    target.write_text(content, encoding="utf-8")
'''

_PRUNE = "scripts/instances.py --prune"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """The Makefile, a stand-in resolver, and no .env."""
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "resolve_profile.py").write_text(
        _RESOLVER, encoding="utf-8", newline="\n"
    )
    return tmp_path


def _dry_run(*, cwd: Path, args: list[str]) -> list[list[str]]:
    """The command lines one goal prints under -n, each split as the shell would."""
    dropped = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DFE_") and key not in dropped
    }
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", *args],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]


def _index(lines: list[list[str]], *, starts: list[str], has: str = "") -> int:
    """Where the first line starting with ``starts`` (and carrying ``has``) is."""
    for number, tokens in enumerate(lines):
        if tokens[: len(starts)] == starts and (not has or has in tokens):
            return number
    raise AssertionError(f"no {' '.join(starts)} ... {has} line in {lines}")


@pytest.mark.parametrize("dev", ["", "1"])
def test_apply_prunes_the_same_project_before_it_recreates(
    checkout: Path, dev: str
) -> None:
    lines = _dry_run(cwd=checkout, args=["apply", "SERVICES=dfe-loader", f"DEV={dev}"])

    prune = _index(lines, starts=["python3", *_PRUNE.split()])
    up = _index(lines, starts=["docker", "compose"], has="up")
    assert prune < up
    compose_args = lines[up][2 : lines[up].index("up")]
    assert lines[prune][lines[prune].index("--") + 1 :] == compose_args


@pytest.mark.parametrize("dev", ["", "1"])
def test_a_bare_apply_prunes_and_recreates_nothing(checkout: Path, dev: str) -> None:
    # The engine names no command after a delete, and an up with no service
    # names recreates the whole stack.
    lines = _dry_run(cwd=checkout, args=["apply", f"DEV={dev}"])

    _index(lines, starts=["python3", *_PRUNE.split()])
    assert [tokens for tokens in lines if tokens[:2] == ["docker", "compose"]] == []


@pytest.mark.parametrize("dev", ["", "1"])
def test_a_bare_apply_prunes_the_project_a_named_one_does(
    checkout: Path, dev: str
) -> None:
    def prune_args(lines: list[list[str]]) -> list[str]:
        prune = lines[_index(lines, starts=["python3", *_PRUNE.split()])]
        return prune[prune.index("--") + 1 :]

    bare = _dry_run(cwd=checkout, args=["apply", f"DEV={dev}"])
    named = _dry_run(cwd=checkout, args=["apply", "SERVICES=dfe-loader", f"DEV={dev}"])

    assert prune_args(bare) == prune_args(named)


def test_a_bare_apply_prunes_against_the_profile_it_re_resolves(
    checkout: Path,
) -> None:
    # Written before the engine declared any instance, so it chains no fragment.
    (checkout / ".profile.mk").write_text(
        "export PROFILE_FLAGS := --profile clickhouse\n"
        "export DFE_SERVICES := dfe-loader\n"
        "export DFE_INSTANCES_RESOLVED := false\n",
        encoding="utf-8",
        newline="\n",
    )

    lines = _dry_run(cwd=checkout, args=["apply"])

    prune = lines[_index(lines, starts=["python3", *_PRUNE.split()])]
    assert "docker-compose.instances.yml" in prune


@pytest.mark.parametrize("goal", ["ci", "dev", "infra"])
def test_a_start_goal_mints_env_before_its_first_compose_call(
    checkout: Path, goal: str
) -> None:
    lines = _dry_run(cwd=checkout, args=[goal])

    init = _index(lines, starts=["python3", "scripts/init.py"])
    assert init < _index(lines, starts=["docker", "compose"])
