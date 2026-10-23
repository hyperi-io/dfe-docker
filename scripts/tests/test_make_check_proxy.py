#  Project:      dfe-docker
#  File:         tests/test_make_check_proxy.py
#  Purpose:      Assert check-proxy validates on the Envoy the stack pins, read off a dry run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""check-proxy takes its Envoy from DFE_PROXY_VERSION, else from the stack scripts/stack.py resolves."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_DIGEST_PIN = f"v1.39.2@sha256:{'a' * 64}"
_STACK_LOOKUP = ["python3", "scripts/stack.py", "--print", "DFE_PROXY_VERSION"]
_SUBSTITUTION = "version=$("


def _dry_run(*, args: list[str], cwd: Path) -> str:
    """Return what `make -n check-proxy` prints, with no DFE_* key, VERSION or outer make flags reaching it."""
    dropped = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "VERSION")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("DFE_")) and key not in dropped
    }
    result = subprocess.run(
        args=["make", "--no-print-directory", "-n", "check-proxy", *args],
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
        env=environment,
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _version_command(*, recipe: str) -> list[str]:
    """Return the command whose output check-proxy runs Envoy at, split as the shell would."""
    start = recipe.index(_SUBSTITUTION) + len(_SUBSTITUTION)
    end = recipe.index(") ||", start)
    return shlex.split(recipe[start:end])


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A copy of the Makefile with no .env, which is how CI checks the repo out."""
    (tmp_path / "Makefile").write_text(
        data=(REPO_ROOT / "Makefile").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def test_a_set_dfe_proxy_version_is_the_envoy_it_validates_on(checkout: Path) -> None:
    recipe = _dry_run(args=[f"DFE_PROXY_VERSION={_DIGEST_PIN}"], cwd=checkout)

    assert _version_command(recipe=recipe) == ["echo", _DIGEST_PIN]
    assert '"envoyproxy/envoy:$version" envoy --mode validate' in recipe


def test_the_pin_make_stack_writes_to_env_is_read_without_its_annotation(
    checkout: Path,
) -> None:
    (checkout / ".env").write_text(
        data=f"DFE_PROXY_VERSION={_DIGEST_PIN}  # envoyproxy/envoy\n", encoding="utf-8"
    )

    recipe = _dry_run(args=[], cwd=checkout)

    assert _version_command(recipe=recipe) == ["echo", _DIGEST_PIN]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], _STACK_LOOKUP),
        (["VERSION=2.2.1-rc.17"], [*_STACK_LOOKUP, "2.2.1-rc.17"]),
        (["DFE_STACK_VERSION=2.2.0"], [*_STACK_LOOKUP, "2.2.0"]),
        (["DFE_PROXY_VERSION="], _STACK_LOOKUP),
    ],
)
def test_an_unset_dfe_proxy_version_is_read_from_the_stack(
    checkout: Path, args: list[str], expected: list[str]
) -> None:
    recipe = _dry_run(args=args, cwd=checkout)

    assert _version_command(recipe=recipe) == expected
