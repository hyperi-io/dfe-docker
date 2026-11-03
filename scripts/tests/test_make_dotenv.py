#  Project:      dfe-docker
#  File:         tests/test_make_dotenv.py
#  Purpose:      Assert only the goals that need .env create it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which make goals mint .env, driven through make in a fresh copy.

.env carries seven generated secrets, and make builds a missing included
makefile before it runs any goal, so an unconditional include would mint them for
`make help` and every check, on a laptop and on each CI runner alike.

The copy holds what scripts/init.py needs to really write .env, so a goal that
still reached it would leave one behind here rather than fail for want of a file.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Everything `.env`'s rule and the env-files gate run, and nothing they do not.
_COPIED_FILES = (
    "Makefile",
    "docker-compose.yml",
    ".env.example",
    "scripts/_common.py",
    "scripts/env_files.py",
    "scripts/init.py",
)
_COPIED_DIRS = ("env.example",)

# Dry runs, because the recipes need docker, uvx or a network. Make still builds
# an included makefile under -n, so a dry run reaches .env's rule if the include does.
_READ_ONLY_GOALS = [
    "check",
    "check-compose",
    "check-hardfail",
    "check-dockerfile",
    "check-docs",
    "check-proxy",
    "check-python",
    "check-tests",
]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A fresh copy of what `.env`'s rule needs, with no .env and no env/."""
    for name in _COPIED_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    for name in _COPIED_DIRS:
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def _make(*, cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run make with no DFE_* key and no outer make's flags reaching it."""
    dropped = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DFE_") and key not in dropped
    }
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
    )


def test_help_writes_no_env(checkout: Path) -> None:
    result = _make(cwd=checkout, args=["help"])

    assert result.returncode == 0, result.stderr
    assert not (checkout / ".env").exists()
    assert not (checkout / "env").exists()


@pytest.mark.parametrize("goal", _READ_ONLY_GOALS)
def test_a_check_writes_no_env(checkout: Path, goal: str) -> None:
    result = _make(cwd=checkout, args=["-n", goal])

    assert result.returncode == 0, result.stderr
    assert not (checkout / ".env").exists()
    assert not (checkout / "env").exists()


def test_the_start_gate_mints_env_where_env_dir_already_exists(checkout: Path) -> None:
    # env_files.py runs init only for a missing env/ file, so this is the case
    # the gate's own .env prerequisite is there for.
    shutil.copytree(checkout / "env.example", checkout / "env")

    result = _make(cwd=checkout, args=["env-files"])

    assert result.returncode == 0, result.stderr
    assert (checkout / ".env").is_file()


@pytest.mark.parametrize(
    ("line", "name", "want"),
    [
        pytest.param(
            "DFE_OTEL_COLLECTOR_VERSION=1.2.3   # note",
            "DFE_OTEL_COLLECTOR_VERSION",
            "1.2.3",
            id="inline_comment",
        ),
        pytest.param(
            "DFE_OTEL_COLLECTOR_VERSION=1.2.3\t# note",
            "DFE_OTEL_COLLECTOR_VERSION",
            "1.2.3",
            id="tab_before_comment",
        ),
        pytest.param(
            "DFE_STACK_VERSION=2.2.1  # pin",
            "DFE_STACK_VERSION",
            "2.2.1",
            id="stack_pin",
        ),
        pytest.param(
            "DFE_UI_VERSION_TAG=v1  # tag", "DFE_UI_VERSION_TAG", "v1", id="tag_pin"
        ),
        pytest.param(
            "DFE_OTEL_COLLECTOR_VERSION=1.2.3",
            "DFE_OTEL_COLLECTOR_VERSION",
            "1.2.3",
            id="no_comment",
        ),
    ],
)
def test_a_pin_with_an_inline_comment_reaches_compose_without_its_padding(
    checkout: Path, line: str, name: str, want: str
) -> None:
    (checkout / ".env").write_text(f"{line}\n", encoding="utf-8")
    (checkout / "probe.mk").write_text(
        f"probe:\n\t@printf '[%s]' \"$${name}\"\n", encoding="utf-8"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("DFE_"))
        and key not in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    }
    # A caller that loads .env into its own environment exports the pin padded, which make re-exports
    environment[name] = line.split("=", 1)[1].split("#")[0]

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "BOOTSTRAP_GOALS=probe",
            "-f",
            "Makefile",
            "-f",
            "probe.mk",
            "probe",
        ],
        capture_output=True,
        check=False,
        cwd=checkout,
        encoding="utf-8",
        env=environment,
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"[{want}]"
