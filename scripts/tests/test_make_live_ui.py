#  Project:      dfe-docker
#  File:         scripts/tests/test_make_live_ui.py
#  Purpose:      Assert what `LIVE_UI=1` changes in the Makefile, read off a dry run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What `make dev LIVE_UI=1` builds, chains and reads, in a copy of the Makefile."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = ["python3", "scripts/build_dev_images.py"]
_CONTAINER_LOGS = "compose/container-logs.yml"
_LIVE_UI = "compose/live-ui.yml"
_NODE_IMAGE = "node:24-trixie-slim@sha256:0123456789abcdef"
_PROBE = "probe-node-image"

# Written only when it differs, as the real resolver does: make re-reads a remade include, so rewriting it every run would never settle.
_RESOLVER = '''\
from pathlib import Path

target = Path(".tmp/profile.mk")
content = """\\
export PROFILE_FLAGS := --profile core
export DFE_SERVICES := dfe-engine dfe-ui dfe-proxy
export DFE_BUILDABLE_SERVICES := dfe-engine dfe-ui
export DFE_INSTANCES_RESOLVED := false
export DFE_AUTH_RESOLVED := false
export DFE_OTEL_RESOLVED := true
"""
if not target.is_file() or target.read_text(encoding="utf-8") != content:
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_text(content, encoding="utf-8")
'''


def _build_targets(*, result: subprocess.CompletedProcess) -> list[str] | None:
    """The service names the dry run hands the image builder; None when it prints no build."""
    for line in result.stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[: len(_BUILD)] == _BUILD:
            return [
                token for token in tokens[len(_BUILD) :] if not (token.startswith("-"))
            ]
    return None


def _chain(*, result: subprocess.CompletedProcess) -> list[str]:
    """The COMPOSE_FILE chain `make print-compose-file` printed."""
    for line in result.stdout.splitlines():
        if line.startswith("COMPOSE_FILE="):
            return line.removeprefix("COMPOSE_FILE=").split(":")
    raise AssertionError(f"no COMPOSE_FILE line in {result.stdout!r}")


def _compose_lines(*, result: subprocess.CompletedProcess) -> list[list[str]]:
    """Every `docker compose` line the dry run printed, split as the shell would."""
    lines = [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
    return [tokens for tokens in lines if tokens[:2] == ["docker", "compose"]]


def _make(
    *, args: list[str], cwd: Path, home: Path | None = None
) -> subprocess.CompletedProcess:
    """Run make in the copied checkout, with no DFE_ or stack-shaping setting leaking in from the caller."""
    # COMPOSE_FILE too: an outer `make check-tests` exports its own chain, which a run that chains nothing would print.
    dropped = (
        "COMPOSE_FILE",
        "DEV",
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
    if home is not None:
        environment["HOME"] = str(home)
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
    """The Makefile, a stand-in resolver and the network precheck `apply` runs, with no .env."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(dst=root / "Makefile", src=REPO_ROOT / "Makefile")
    shutil.copy(
        dst=root / "scripts" / "proxy_tls.py",
        src=REPO_ROOT / "scripts" / "proxy_tls.py",
    )
    (root / "scripts" / "resolve_profile.py").write_text(
        data=_RESOLVER, encoding="utf-8", newline="\n"
    )
    return root


@pytest.fixture
def src_root(tmp_path: Path) -> Path:
    """A DFE_SRC_ROOT whose dfe-ui Dockerfile declares the Node image, then re-declares it."""
    root = tmp_path / "src"
    (root / "dfe-ui").mkdir(parents=True)
    (root / "dfe-ui" / "Dockerfile").write_text(
        data=(
            "# Trixie by name.\n"
            f"ARG NODE_IMAGE={_NODE_IMAGE}\n\n"
            "FROM ${NODE_IMAGE} AS builder\n"
            "ARG NODE_IMAGE\n"
            "ARG NODE_IMAGE=node:shadowed\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    return root


@pytest.mark.parametrize(("live_ui", "built"), [("", True), ("1", False)])
def test_dfe_ui_is_built_only_without_live_ui(
    checkout: Path, src_root: Path, live_ui: str, built: bool
) -> None:
    result = _make(
        args=["-n", "dev", f"LIVE_UI={live_ui}", f"DFE_SRC_ROOT={src_root}"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    targets = _build_targets(result=result)
    assert targets is not None
    assert ("dfe-ui" in targets) is built
    assert "dfe-engine" in targets


def test_live_ui_with_only_dfe_ui_to_start_builds_nothing(
    checkout: Path, src_root: Path
) -> None:
    # The builder refuses an empty list after `make dev` has already run `down`.
    result = _make(
        args=["-n", "dev", "LIVE_UI=1", "SERVICES=dfe-ui", f"DFE_SRC_ROOT={src_root}"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    assert _build_targets(result=result) is None


def test_live_ui_chains_its_overlay_after_the_container_logs_fragment(
    checkout: Path,
) -> None:
    result = _make(args=["print-compose-file", "LIVE_UI=1"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    chain = _chain(result=result)
    assert chain.index(_LIVE_UI) > chain.index(_CONTAINER_LOGS)


def test_live_ui_off_chains_no_overlay(checkout: Path) -> None:
    result = _make(args=["print-compose-file"], cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert _LIVE_UI not in _chain(result=result)


@pytest.mark.parametrize("local", ["", "dfe-engine"])
def test_live_ui_rides_the_dev_path_pull_and_start(
    checkout: Path, src_root: Path, local: str
) -> None:
    result = _make(
        args=["-n", "dev", "LIVE_UI=1", f"LOCAL={local}", f"DFE_SRC_ROOT={src_root}"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    lines = [
        tokens
        for tokens in _compose_lines(result=result)
        if ("pull" in tokens) or ("up" in tokens)
    ]
    assert len(lines) == 2
    for tokens in lines:
        assert _LIVE_UI in tokens


@pytest.mark.parametrize(
    "goal_args", [["ci"], ["up"], ["apply", "SERVICES=dfe-ui"]], ids=" ".join
)
def test_live_ui_never_reaches_a_registry_goal(
    checkout: Path, src_root: Path, goal_args: list[str]
) -> None:
    result = _make(
        args=["-n", *goal_args, "LIVE_UI=1", f"DFE_SRC_ROOT={src_root}"],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    lines = _compose_lines(result=result)
    assert lines
    for tokens in lines:
        assert _LIVE_UI not in tokens


def test_live_ui_refuses_dfe_ui_in_local(checkout: Path, src_root: Path) -> None:
    result = _make(
        args=["-n", "dev", "LIVE_UI=1", "LOCAL=dfe-ui", f"DFE_SRC_ROOT={src_root}"],
        cwd=checkout,
    )

    assert result.returncode != 0
    assert "Drop dfe-ui from LOCAL" in result.stderr


def test_live_ui_refuses_a_missing_checkout_before_anything_runs(
    checkout: Path, tmp_path: Path
) -> None:
    result = _make(
        args=["-n", "dev", "LIVE_UI=1", f"DFE_SRC_ROOT={tmp_path / 'missing'}"],
        cwd=checkout,
    )

    assert result.returncode != 0
    assert "holds no dfe-ui/Dockerfile" in result.stderr
    assert _compose_lines(result=result) == []


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_live_ui_reads_the_node_image_from_the_checkout_dockerfile(
    checkout: Path, src_root: Path, quote: str
) -> None:
    # .env values are quoted by convention; make keeps the quotes compose strips.
    result = _make(
        args=[
            f"--eval={_PROBE}: ; @echo $(DFE_UI_NODE_IMAGE)",
            _PROBE,
            "LIVE_UI=1",
            f"DFE_SRC_ROOT={quote}{src_root}{quote}",
        ],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == _NODE_IMAGE


def test_live_ui_expands_a_tilde_in_the_source_root(
    checkout: Path, src_root: Path
) -> None:
    result = _make(
        args=[
            f"--eval={_PROBE}: ; @echo $(DFE_UI_NODE_IMAGE)",
            _PROBE,
            "LIVE_UI=1",
            f"DFE_SRC_ROOT=~/{src_root.name}",
        ],
        cwd=checkout,
        home=src_root.parent,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == _NODE_IMAGE


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://console.example.test", "console.example.test"),
        ('"https://quoted.example.test"', "quoted.example.test"),
        ("http://localhost", "0.0.0.0"),
        ("http://192.0.2.10", "0.0.0.0"),
        ("http://[fd00::1]", "0.0.0.0"),
        ("", "0.0.0.0"),
    ],
)
def test_live_ui_serves_on_the_console_hostname_only_when_it_is_a_name(
    checkout: Path, origin: str, host: str
) -> None:
    # Next only passes hot reload from the host it serves on; it cannot serve on an IP it does not own.
    result = _make(
        args=[
            f"--eval={_PROBE}: ; @echo [$(DFE_UI_DEV_HOST)]",
            _PROBE,
            "LIVE_UI=1",
            f"DFE_EXTERNAL_ORIGIN={origin}",
        ],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == f"[{host}]"


def test_live_ui_without_a_checkout_leaves_the_node_image_unset(
    checkout: Path, tmp_path: Path
) -> None:
    # Outside the live goals nothing refuses; the overlay's `:?` names DFE_SRC_ROOT if compose ever reads it.
    result = _make(
        args=[
            f"--eval={_PROBE}: ; @echo [$(DFE_UI_NODE_IMAGE)]",
            _PROBE,
            "LIVE_UI=1",
            f"DFE_SRC_ROOT={tmp_path / 'missing'}",
        ],
        cwd=checkout,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "[]"
