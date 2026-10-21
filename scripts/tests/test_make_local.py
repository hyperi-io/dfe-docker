#  Project:      dfe-docker
#  File:         scripts/tests/test_make_local.py
#  Purpose:      Assert which names `LOCAL=` accepts and what it hands the resolver, read off a dry run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What `make dev LOCAL=...` accepts and builds, in a copy of the Makefile.

A per-source app is never in DFE_SERVICES, only its instances are, so LOCAL also accepts the apps the profile runs per source. The resolver is handed LOCAL, because only it can put a local image on an instance.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = ["python3", "scripts/build_dev_images.py"]
_RESOLVER_ARGS = Path(".tmp") / "resolver-args"

# Written only when it differs, as the real resolver does: make re-reads a remade include, so rewriting it every run would never settle.
_RESOLVER = '''\
import sys
from pathlib import Path

Path(".tmp").mkdir(exist_ok=True)
Path(".tmp/resolver-args").write_text(" ".join(sys.argv[1:]), encoding="utf-8")
target = Path(".tmp/profile.mk")
content = """\\
export PROFILE_FLAGS := --profile clickhouse
export DFE_SERVICES := dfe-loader dfe-engine dfe-fetcher-okta-audit
export DFE_BUILDABLE_SERVICES := dfe-engine dfe-fetcher dfe-loader dfe-transform-elastic
export DFE_PER_SOURCE_APPS := dfe-fetcher
export DFE_INSTANCES_RESOLVED := true
export DFE_AUTH_RESOLVED := false
export DFE_OTEL_RESOLVED := false
"""
if not target.is_file() or target.read_text(encoding="utf-8") != content:
    target.write_text(content, encoding="utf-8")
'''


def _build_targets(*, result: subprocess.CompletedProcess) -> list[str] | None:
    """The service names the dry run hands the image builder, None when it prints no build."""
    for line in result.stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[: len(_BUILD)] == _BUILD:
            names = tokens[len(_BUILD) :]
            if "--overlay" in names:
                start = names.index("--overlay")
                del names[start : start + 2]
            return names
    return None


def _make(*, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run make in the copied checkout, with no DFE_ or stack-shaping setting leaking in from the caller."""
    dropped = (
        "COMPOSE_FILE",
        "DEV",
        "LIVE",
        "LIVE_UI",
        "LOCAL",
        "MAKEFLAGS",
        "MAKELEVEL",
        "MFLAGS",
        "SERVICES",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("DFE_")) and key not in dropped
    }
    return subprocess.run(
        args=["make", "--no-print-directory", *args],
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
        env=environment,
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """The Makefile and a stand-in resolver that records its arguments, with no .env."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(dst=root / "Makefile", src=REPO_ROOT / "Makefile")
    (root / "scripts" / "resolve_profile.py").write_text(
        data=_RESOLVER, encoding="utf-8", newline="\n"
    )
    return root


def test_local_builds_a_per_source_app_the_profile_runs(checkout: Path) -> None:
    result = _make(args=["-n", "dev", "LOCAL=dfe-fetcher"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _build_targets(result=result) == ["dfe-fetcher"]


def test_local_refuses_a_per_source_app_the_profile_does_not_run(
    checkout: Path,
) -> None:
    result = _make(args=["-n", "dev", "LOCAL=dfe-transform-elastic"], cwd=checkout)

    assert result.returncode != 0
    assert (
        "'LOCAL' contains names not in the resolved stack: dfe-transform-elastic"
        in result.stderr
    )


def test_local_reaches_the_resolver(checkout: Path) -> None:
    result = _make(args=["-n", "dev", "LOCAL=dfe-engine dfe-fetcher"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    recorded = (checkout / _RESOLVER_ARGS).read_text(encoding="utf-8").split()
    assert recorded == ["--local", "dfe-engine", "--local", "dfe-fetcher"]


def test_no_local_hands_the_resolver_none(checkout: Path) -> None:
    result = _make(args=["-n", "dev"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert "--local" not in (checkout / _RESOLVER_ARGS).read_text(encoding="utf-8")
