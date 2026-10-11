#  Project:      dfe-docker
#  File:         tests/test_script_locks.py
#  Purpose:      Assert every inline-metadata script is pinned exactly and locked to those pins
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The scripts that declare packages inline run with `uv run --locked`.

So each one needs a lock beside it that records exactly the pins it declares,
and `make check-locks` has to check it. A script that gains inline metadata, or a
pin that moves without a relock, fails here without needing uv.
"""

import re
import tomllib
from pathlib import Path

import pytest

from _common import REPO_ROOT

SCRIPTS_DIR = REPO_ROOT / "scripts"
# The reference expression from the inline script metadata specification.
METADATA_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<specifier>.*)$")


def _metadata(path: Path) -> dict | None:
    for block in METADATA_BLOCK.finditer(path.read_text(encoding="utf-8")):
        if block.group("type") == "script":
            lines = block.group("content").splitlines(keepends=True)
            return tomllib.loads(
                "".join(
                    line[2:] if line.startswith("# ") else line[1:] for line in lines
                )
            )
    return None


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


SCRIPTS = sorted(path for path in SCRIPTS_DIR.glob("*.py") if _metadata(path))


def test_the_e2e_and_flow_scripts_declare_their_packages() -> None:
    assert [path.name for path in SCRIPTS] == ["test_e2e.py", "test_flows.py"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_each_pin_is_an_exact_version(script: Path) -> None:
    for dependency in _metadata(script)["dependencies"]:
        assert REQUIREMENT.match(dependency)["specifier"].startswith("=="), dependency


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_lock_records_exactly_the_scripts_pins(script: Path) -> None:
    lock_path = script.with_name(f"{script.name}.lock")
    assert lock_path.is_file(), f"no lock: run `uv lock --script scripts/{script.name}`"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    metadata = _metadata(script)

    declared = {
        (_normalised(match["name"]), match["specifier"].replace(" ", ""))
        for match in map(REQUIREMENT.match, metadata["dependencies"])
    }
    locked = {
        (_normalised(entry["name"]), entry.get("specifier", ""))
        for entry in lock["manifest"]["requirements"]
    }

    assert locked == declared
    assert lock["requires-python"] == metadata["requires-python"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_file_in_the_lock_carries_a_hash(script: Path) -> None:
    lock = tomllib.loads(
        script.with_name(f"{script.name}.lock").read_text(encoding="utf-8")
    )
    for package in lock["package"]:
        files = [package.get("sdist") or {}, *(package.get("wheels") or [])]
        assert all(
            entry.get("hash", "").startswith("sha256:") for entry in files if entry
        )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_make_check_locks_checks_each_script(script: Path) -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert f"lock --check --script scripts/{script.name}" in makefile
    assert f"run --locked --script ./scripts/{script.name}" in makefile
