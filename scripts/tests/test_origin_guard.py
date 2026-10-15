#  Project:      dfe-docker
#  File:         tests/test_origin_guard.py
#  Purpose:      Assert publishing the UIs beyond loopback requires DFE_EXTERNAL_ORIGIN
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The DFE_BIND_SCOPE=all guard, driven through make itself.

The guard is a parse-time `$(error)`, so make refuses the goal before it pulls an
image or starts a container -- which is what makes it testable here: a copy of the
Makefile and an empty .env, in a directory of its own, is enough to reach it.

The copy is the isolation. The guard reads DFE_EXTERNAL_ORIGIN from .env or the
environment, so a run against the checkout would answer to whatever the developer
has in theirs.

Where the guard is expected to stay silent these assert its absence rather than a
clean run: the goals that need the origin are the goals that need a resolved
profile too, and a lone Makefile has no scripts/ to resolve one with.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from _common import LOOPBACK_ORIGINS

REPO_ROOT = Path(__file__).resolve().parents[2]

# The sentence the guard is recognised by, quoted from the Makefile.
_GUARD = "DFE_EXTERNAL_ORIGIN must name the address browsers use"

_GUARDED_GOALS = [
    "dev",
    "ci",
    "up",
    "apply",
    "infra",
    "post",
    "test-source",
    "test-flows",
]
# Stopping a stack and checking a file must work whatever the configuration says.
_UNGUARDED_GOALS = ["down", "clean", "creds", "init", "help", "check-compose"]


@pytest.fixture
def makefile(tmp_path: Path) -> Path:
    """A copy of the Makefile, with an empty .env, in a directory of its own."""
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / ".env").write_text("", encoding="utf-8", newline="\n")
    return tmp_path


def _make(
    *, cwd: Path, goal: str, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Dry-run one goal with every DFE_* key dropped, then the overrides applied."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("DFE_")
    }
    environment.update(overrides)
    return subprocess.run(
        ["make", "-n", goal],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("goal", _GUARDED_GOALS)
def test_starting_beyond_loopback_without_an_origin_stops(
    makefile: Path, goal: str
) -> None:
    result = _make(cwd=makefile, goal=goal, DFE_BIND_SCOPE="all")

    assert result.returncode != 0
    assert _GUARD in result.stderr


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "  ",
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://[::1]",
        "https://[::1]",
        "http://0.0.0.0",
        "https://0.0.0.0",
    ],
)
def test_an_origin_only_this_box_can_use_is_no_origin(
    makefile: Path, origin: str
) -> None:
    result = _make(
        cwd=makefile, goal="ci", DFE_BIND_SCOPE="all", DFE_EXTERNAL_ORIGIN=origin
    )

    assert result.returncode != 0
    assert _GUARD in result.stderr


def test_the_makefile_and_the_helper_scripts_hold_one_loopback_set() -> None:
    """The Makefile cannot import the module, so the two lists are held equal here."""
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    declared = next(line for line in lines if line.startswith("LOOPBACK_ORIGINS :="))

    assert set(declared.partition(":=")[2].split()) == LOOPBACK_ORIGINS


def test_the_message_names_what_breaks_and_the_key_to_set(makefile: Path) -> None:
    result = _make(cwd=makefile, goal="ci", DFE_BIND_SCOPE="all")

    assert "frame-ancestors" in result.stderr
    assert "next-auth" in result.stderr
    assert "DFE_EXTERNAL_ORIGIN=http://" in result.stderr


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_an_address_browsers_use_satisfies_it(makefile: Path, source: str) -> None:
    """.env and the environment are both places compose takes the key from."""
    overrides = {"DFE_BIND_SCOPE": "all"}
    if source == "environment":
        overrides["DFE_EXTERNAL_ORIGIN"] = "http://dfe.example.test"
    else:
        (makefile / ".env").write_text(
            "DFE_EXTERNAL_ORIGIN=http://dfe.example.test\n",
            encoding="utf-8",
            newline="\n",
        )

    result = _make(cwd=makefile, goal="ci", **overrides)

    assert _GUARD not in result.stderr


@pytest.mark.parametrize("goal", _UNGUARDED_GOALS)
def test_stopping_and_checking_never_need_the_origin(makefile: Path, goal: str) -> None:
    result = _make(cwd=makefile, goal=goal, DFE_BIND_SCOPE="all")

    assert result.returncode == 0
    assert _GUARD not in result.stderr


def test_a_workstation_needs_no_origin(makefile: Path) -> None:
    result = _make(cwd=makefile, goal="ci")

    assert _GUARD not in result.stderr


# The sentence the shape guard is recognised by, quoted from the Makefile.
_SHAPE_GUARD = "It must be a scheme and a host only, no port"


@pytest.mark.parametrize(
    ("origin", "fault", "fixed"),
    [
        (
            "https://dfe.example.test:3000",
            "carries a port",
            "https://dfe.example.test",
        ),
        ("https://dfe.example.test/", "carries a path", "https://dfe.example.test"),
        (
            "https://dfe.example.test/console",
            "carries a path",
            "https://dfe.example.test",
        ),
        ("http://dfe.example.test?x=1", "carries a query", "http://dfe.example.test"),
        (
            "dfe.example.test",
            "has no http:// or https:// scheme",
            "http://dfe.example.test",
        ),
    ],
)
def test_an_origin_compose_cannot_append_a_port_to_stops_the_start(
    makefile: Path, origin: str, fault: str, fixed: str
) -> None:
    result = _make(cwd=makefile, goal="ci", DFE_EXTERNAL_ORIGIN=origin)

    assert result.returncode != 0
    assert _SHAPE_GUARD in result.stderr
    assert fault in result.stderr
    assert "compose appends the port itself (DFE_UI_PORT" in result.stderr
    assert f"Set DFE_EXTERNAL_ORIGIN={fixed}" in result.stderr


@pytest.mark.parametrize(
    "key", ["DFE_HYPERDX_APP_URL", "DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN"]
)
def test_every_origin_compose_appends_a_port_to_is_held_to_the_same_shape(
    makefile: Path, key: str
) -> None:
    result = _make(cwd=makefile, goal="ci", **{key: "https://dfe.example.test:8443"})

    assert result.returncode != 0
    assert f"{key}='https://dfe.example.test:8443' carries a port" in result.stderr


# A letter outside ASCII, spelled as a code point so this file stays ASCII.
_E_ACUTE = chr(0xE9)


@pytest.mark.parametrize(
    ("origin", "stray"),
    [
        ("https://dfe.example.test;script-src", ";"),
        ("https://dfe.example.test,other.test", ","),
        ("https://dfe.example.test'", "'"),
        ('https://dfe.example.test"', '"'),
        ("https://user@dfe.example.test", "@"),
        ("https://*.example.test", "*"),
        ("https://dfe.example.test%", "%"),
        ("https://dfe.example.test\\", "\\"),
        ("https://dfe.example.test=1", "="),
        (f"https://dfe{_E_ACUTE}.example.test", _E_ACUTE),
        ("http://[::g]", "g"),
        ("http://[]", "[]"),
    ],
)
def test_a_character_no_host_holds_stops_the_start(
    makefile: Path, origin: str, stray: str
) -> None:
    """The origin lands in a CSP source list, where `;` starts a new directive."""
    result = _make(cwd=makefile, goal="ci", DFE_EXTERNAL_ORIGIN=origin)

    assert result.returncode != 0
    assert _SHAPE_GUARD in result.stderr
    assert f"carries characters a host cannot hold: `{stray}`" in result.stderr
    assert "frame-ancestors source list" in result.stderr
    assert "the console answers 500" not in result.stderr


@pytest.mark.parametrize(
    ("origin", "fixed"),
    [
        ("https://dfe.example.test;script-src", "https://<the host browsers use>"),
        ("http://dfe.example.test,other.test", "http://<the host browsers use>"),
    ],
)
def test_the_suggested_fix_never_repeats_the_rejected_host(
    makefile: Path, origin: str, fixed: str
) -> None:
    result = _make(cwd=makefile, goal="ci", DFE_EXTERNAL_ORIGIN=origin)

    assert f"Set DFE_EXTERNAL_ORIGIN={fixed}" in result.stderr
    assert origin.partition("://")[2] not in result.stderr.partition("Set ")[2]


@pytest.mark.parametrize(
    "key",
    ["DFE_EXTERNAL_ORIGIN", "DFE_HYPERDX_APP_URL", "DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN"],
)
def test_every_origin_key_refuses_a_semicolon(makefile: Path, key: str) -> None:
    result = _make(cwd=makefile, goal="ci", **{key: "https://dfe.example.test;x"})

    assert result.returncode != 0
    assert f"{key}='https://dfe.example.test;x' carries characters" in result.stderr


@pytest.mark.parametrize("tls", ["false", "true"])
@pytest.mark.parametrize(
    "origin",
    [
        "https://dfe.example.test",
        "https://DFE-1.Example_Test",
        "http://dfe",
        "http://192.0.2.19",
        "http://[::1]",
        "http://[2001:db8::1]",
        "http://[::ffff:192.0.2.1]",
        "http://[2001:DB8::A]",
    ],
)
def test_a_scheme_and_host_passes_the_shape_guard_with_tls_on_or_off(
    makefile: Path, tls: str, origin: str
) -> None:
    result = _make(
        cwd=makefile, goal="ci", DFE_EXTERNAL_ORIGIN=origin, DFE_PROXY_TLS=tls
    )

    assert _SHAPE_GUARD not in result.stderr


@pytest.mark.parametrize("goal", _UNGUARDED_GOALS)
def test_stopping_and_checking_never_judge_the_origin_shape(
    makefile: Path, goal: str
) -> None:
    result = _make(
        cwd=makefile, goal=goal, DFE_EXTERNAL_ORIGIN="https://dfe.example.test:3000"
    )

    assert result.returncode == 0
    assert _SHAPE_GUARD not in result.stderr
