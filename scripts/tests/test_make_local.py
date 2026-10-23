#  Project:      dfe-docker
#  File:         scripts/tests/test_make_local.py
#  Purpose:      Assert which names `LOCAL=` accepts and what it hands the resolver, read off a dry run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What `make dev LOCAL=...` accepts and builds, in a copy of the Makefile.

A per-source app is never in DFE_SERVICES, only its instances are, so LOCAL also accepts the apps the profile runs per source. The resolver is handed the per-source apps built locally, because only it can put a local image on an instance: what LOCAL names, else every one the profile runs on `make dev`, else what the last dev build recorded on `make apply`.

Only the goals that start services also hand it `--usage`, because that run asks docker compose for each service's CPU limit.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = ["python3", "scripts/build_dev_images.py"]
_RECORD = "docker-compose.local.yml"
_RESOLVER_ARGS = Path(".tmp") / "resolver-args"
# What a goal that starts services adds, asserted apart from the LOCAL arguments.
_USAGE = "--usage"

# Each run appends its arguments as a line, because make runs the resolver again on each pass and in each sub-make. Written only when it differs, as the real resolver does: make re-reads a remade include, so rewriting it every run would never settle
_RESOLVER = '''\
import sys
from pathlib import Path

Path(".tmp").mkdir(exist_ok=True)
with Path(".tmp/resolver-args").open("a", encoding="utf-8") as recorded:
    recorded.write(" ".join(sys.argv[1:]) + "\\n")
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


def _build_line(*, result: subprocess.CompletedProcess) -> list[str] | None:
    """The image builder's arguments in the dry run, None when it prints no build."""
    for line in result.stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[: len(_BUILD)] == _BUILD:
            return tokens[len(_BUILD) :]
    return None


def _build_targets(*, result: subprocess.CompletedProcess) -> list[str] | None:
    """The service names the dry run hands the image builder, None when it prints no build."""
    names = _build_line(result=result)
    if names is None:
        return None
    for flag in ("--overlay", "--record"):
        if flag in names:
            start = names.index(flag)
            del names[start : start + 2]
    return names


def _resolves(*, checkout: Path) -> list[list[str]]:
    """The arguments of every resolver run, in order."""
    lines = (checkout / _RESOLVER_ARGS).read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines]


def _local_args(args: list[str]) -> list[str]:
    """One run's arguments without the usage flag."""
    return [arg for arg in args if arg != _USAGE]


def _first_resolve(*, checkout: Path) -> list[str]:
    """The LOCAL arguments the resolver was handed by the goal's own make, before any sub-make ran it."""
    return _local_args(_resolves(checkout=checkout)[0])


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
    """The Makefile, a stand-in resolver that records its arguments and the network precheck, with no .env."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(dst=root / "Makefile", src=REPO_ROOT / "Makefile")
    (root / "scripts" / "resolve_profile.py").write_text(
        data=_RESOLVER, encoding="utf-8", newline="\n"
    )
    # `make apply` runs it while make parses, and a precheck that cannot run refuses the goal
    shutil.copy(
        dst=root / "scripts" / "proxy_tls.py",
        src=REPO_ROOT / "scripts" / "proxy_tls.py",
    )
    return root


def test_local_builds_a_per_source_app_the_profile_runs(checkout: Path) -> None:
    result = _make(args=["-n", "dev", "LOCAL=dfe-fetcher"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _build_targets(result=result) == ["dfe-fetcher"]


def test_local_builds_no_per_source_app_it_does_not_name(checkout: Path) -> None:
    result = _make(args=["-n", "dev", "LOCAL=dfe-engine"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _build_targets(result=result) == ["dfe-engine"]
    assert _first_resolve(checkout=checkout) == ["--local", "dfe-engine"]


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
    assert _first_resolve(checkout=checkout) == [
        "--local",
        "dfe-engine",
        "--local",
        "dfe-fetcher",
    ]


def test_no_local_builds_every_per_source_app_and_records_the_build(
    checkout: Path,
) -> None:
    result = _make(args=["-n", "dev"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert "dfe-fetcher" in _build_targets(result=result)
    line = _build_line(result=result)
    assert line[line.index("--record") + 1] == _RECORD


def test_no_local_puts_every_per_source_instance_on_its_local_image(
    checkout: Path,
) -> None:
    result = _make(args=["-n", "dev"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _first_resolve(checkout=checkout) == ["--local-per-source"]


@pytest.mark.parametrize("dev", ["", "1"])
def test_apply_puts_each_instance_back_on_what_the_dev_build_recorded(
    checkout: Path, dev: str
) -> None:
    result = _make(
        args=["-n", "apply", "SERVICES=dfe-fetcher-okta-audit", f"DEV={dev}"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    runs = _resolves(checkout=checkout)
    assert runs
    assert all(_local_args(run) == ["--local-overlay", _RECORD] for run in runs)


def test_apply_with_local_hands_the_resolver_what_it_names(checkout: Path) -> None:
    result = _make(
        args=["-n", "apply", "SERVICES=dfe-fetcher-okta-audit", "LOCAL=dfe-fetcher"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    assert _first_resolve(checkout=checkout) == ["--local", "dfe-fetcher"]


def test_any_other_goal_resolves_from_what_the_dev_build_recorded(
    checkout: Path,
) -> None:
    result = _make(args=["-n", "post"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _first_resolve(checkout=checkout) == ["--local-overlay", _RECORD]


def test_a_registry_start_hands_the_resolver_none(checkout: Path) -> None:
    result = _make(args=["-n", "ci"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _first_resolve(checkout=checkout) == []


@pytest.mark.parametrize("goal", ["ci", "dev", "up", "apply"])
def test_a_goal_that_starts_services_asks_for_the_usage_limits(
    checkout: Path, goal: str
) -> None:
    result = _make(args=["-n", goal], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _USAGE in _resolves(checkout=checkout)[0]


@pytest.mark.parametrize("goal", ["post", "ps"])
def test_a_goal_that_starts_nothing_never_asks_compose_for_the_limits(
    checkout: Path, goal: str
) -> None:
    result = _make(args=["-n", goal], cwd=checkout)

    assert result.returncode == 0, result.stderr
    runs = _resolves(checkout=checkout)
    assert runs, "the resolver never ran"
    assert not any(_USAGE in run for run in runs)


def test_a_registry_start_removes_the_dev_build_record_before_it_starts(
    checkout: Path,
) -> None:
    result = _make(args=["-n", "ci"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    lines = [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
    removal = lines.index(["rm", "-f", _RECORD])
    start = next(
        number
        for number, tokens in enumerate(lines)
        if tokens[:2] == ["docker", "compose"] and "up" in tokens
    )
    assert removal < start
