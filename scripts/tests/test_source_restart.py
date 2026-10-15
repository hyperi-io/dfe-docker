#  Project:      dfe-docker
#  File:         tests/test_source_restart.py
#  Purpose:      Prove the restart prefix the source runner is handed, and the
#                prune after it, run the make goals they name, against a copy
#                of the Makefile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The make goals scripts/test_source.py runs around dfe-infra's source runner.

The runner splits the restart prefix, appends the compose service an engine
hint names, and runs the result. Those hints name ``make apply
SERVICES=<service>``, the one command that creates a per-source container the
engine has only just declared, so the prefix is run here exactly as the runner
runs it, with make dry-running against a copy of the Makefile and a stand-in
resolver that has declared one.

The runner deletes the sources it created, and a delete names no command, so
the wrapper runs a bare ``make apply`` after it, which removes their containers.
"""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_source

REPO_ROOT = Path(__file__).resolve().parents[2]

# A per-source transform named the way dfe-engine names one, declared by the
# stand-in resolver below and by no committed service.
INSTANCE = "dfe-transform-vrl-fb018cfd8b"

_RESOLVER = f'''\
from pathlib import Path

target = Path(".tmp/profile.mk")
content = """\\
export PROFILE_FLAGS := --profile clickhouse --profile dfe
export DFE_SERVICES := dfe-loader dfe-receiver {INSTANCE}
export DFE_INSTANCES_RESOLVED := true
export DFE_AUTH_RESOLVED := false
export DFE_OTEL_RESOLVED := false
"""
if not target.is_file() or target.read_text(encoding="utf-8") != content:
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_text(content, encoding="utf-8")
'''

# What a developer's shell may carry that would change what make prints.
_DROPPED = ("DEV", "LIVE", "LIVE_UI", "LOCAL", "MAKELEVEL", "MFLAGS", "SERVICES")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """The Makefile, the stand-in resolver and the network precheck, in a directory of their own."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "Makefile", root / "Makefile")
    (root / "scripts" / "resolve_profile.py").write_text(
        _RESOLVER, encoding="utf-8", newline="\n"
    )
    shutil.copy(
        REPO_ROOT / "scripts" / "proxy_tls.py", root / "scripts" / "proxy_tls.py"
    )
    return root


def _dry_run_environment() -> dict[str, str]:
    """This shell's environment less what changes make's output, with every make dry-running.

    MAKEFLAGS=n dry-runs every make started under it, the sub-make included.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DFE_") and key not in _DROPPED
    }
    environment["MAKEFLAGS"] = "n"
    return environment


def _as_the_runner(prefix: str, service: str, *, cwd: Path) -> list[list[str]]:
    """Run the prefix as dfe-infra's runner does, and return the lines make printed."""
    environment = _dry_run_environment()
    result = subprocess.run(
        [*shlex.split(prefix), service],
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


def _compose_up(lines: list[list[str]]) -> list[list[str]]:
    return [
        tokens
        for tokens in lines
        if tokens[:2] == ["docker", "compose"] and "up" in tokens
    ]


@pytest.mark.parametrize("service", [INSTANCE, "dfe-receiver"])
def test_the_restart_prefix_recreates_the_hinted_service_through_make_apply(
    checkout: Path, tmp_path: Path, service: str
) -> None:
    # The runner inherits whatever directory test_source.py was started from.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    lines = _as_the_runner(
        test_source.restart_exec(project_dir=checkout), service, cwd=elsewhere
    )

    ups = _compose_up(lines)
    assert len(ups) == 1, lines
    up = ups[0]
    assert up[-1] == service
    # A recreate is what takes a changed env file; a restart keeps the old one.
    assert "--force-recreate" in up
    # The fragment declaring the per-source services, which only make chains.
    assert ".tmp/docker-compose.instances.yml" in up


_PRUNE = ["python3", "scripts/instances.py", "--prune"]
_WALKED = "source-walk-returned"


def _runner(status: int) -> list[str]:
    """A stand-in source walk that announces it ran and exits with ``status``."""
    return [sys.executable, "-c", f"print({_WALKED!r}); raise SystemExit({status})"]


@pytest.mark.parametrize("status", [0, 3], ids=["walk-passed", "walk-failed"])
def test_a_bare_apply_prunes_after_the_walk_whatever_it_returned(
    checkout: Path, capfd: pytest.CaptureFixture[str], status: int
) -> None:
    returned = test_source.walk(
        _runner(status), _dry_run_environment(), project_dir=checkout
    )

    printed = [
        shlex.split(line)
        for line in capfd.readouterr().out.splitlines()
        if line.strip()
    ]
    assert returned == status
    walked = printed.index([_WALKED])
    pruned = next(
        n for n, tokens in enumerate(printed) if tokens[: len(_PRUNE)] == _PRUNE
    )
    assert walked < pruned
    # An up naming no service would recreate the whole stack.
    assert [tokens for tokens in printed if tokens[:2] == ["docker", "compose"]] == []


@pytest.mark.parametrize(
    ("status", "expected"), [(0, 2), (3, 3)], ids=["walk-passed", "walk-failed"]
)
def test_a_prune_that_fails_fails_a_walk_that_passed(
    tmp_path: Path, status: int, expected: int
) -> None:
    # No Makefile here, so make exits 2.
    returned = test_source.walk(
        _runner(status), _dry_run_environment(), project_dir=tmp_path
    )

    assert returned == expected
