#  Project:      dfe-docker
#  File:         tests/test_origins.py
#  Purpose:      Assert which origins the console may be framed from and is driven on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two consumers of DFE_EXTERNAL_ORIGIN that have to agree about the console.

dfe-proxy re-serves HyperDX under a frame-ancestors list, and an operator reads
the console on loopback whatever address the deployment publishes it as -- so a
list carrying the external origin alone blanks the embedded views on the box
itself. The source runner is the other: it drives the console, and it drives it
at the address testers use.

The compose value is read out of docker-compose.yml and expanded here over the
`${NAME:-default}` form, which is the only interpolation that line uses.
"""

from __future__ import annotations

import re

import pytest

import test_source
from _common import COMPOSE_FILE

_ANCESTOR_KEY = "DFE_HYPERDX_EMBED_ANCESTOR"
_INTERPOLATION = re.compile(r"\$\{([A-Z0-9_]+):-([^{}]*)\}")


def _ancestor_template() -> str:
    """The compose line the proxy's frame-ancestors source list is built from."""
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition(": ")
        if key == _ANCESTOR_KEY:
            return value
    raise AssertionError(f"docker-compose.yml sets no {_ANCESTOR_KEY}")


def _expand(*, template: str, values: dict[str, str]) -> str:
    """Resolve `${NAME:-default}` innermost first, as compose interpolates it."""
    while (match := _INTERPOLATION.search(template)) is not None:
        replacement = values.get(match.group(1), "").strip() or match.group(2)
        template = template[: match.start()] + replacement + template[match.end() :]
    return template


def _ancestors(**values: str) -> list[str]:
    return _expand(template=_ancestor_template(), values=values).split()


def test_an_operator_on_the_box_is_always_an_allowed_ancestor() -> None:
    assert "http://localhost:3000" in _ancestors()
    assert "http://127.0.0.1:3000" in _ancestors()


def test_the_external_origin_joins_the_list_rather_than_replacing_it() -> None:
    found = _ancestors(DFE_EXTERNAL_ORIGIN="http://10.0.0.5")

    assert "http://10.0.0.5:3000" in found
    assert "http://localhost:3000" in found
    assert "http://127.0.0.1:3000" in found


def test_every_ancestor_carries_the_ui_port() -> None:
    """The console is framed from the port it is served on, not the embed port."""
    assert _ancestors(DFE_EXTERNAL_ORIGIN="http://10.0.0.5", DFE_UI_PORT="8443") == [
        "http://localhost:8443",
        "http://127.0.0.1:8443",
        "http://10.0.0.5:8443",
    ]


def test_the_per_surface_override_still_wins() -> None:
    found = _ancestors(
        DFE_EXTERNAL_ORIGIN="http://10.0.0.5",
        DFE_HYPERDX_APP_URL="http://dfe.example.test",
    )

    assert "http://dfe.example.test:3000" in found
    assert "http://10.0.0.5:3000" not in found


@pytest.mark.parametrize(
    ("environment", "bound", "expected"),
    [
        ({}, "", "http://127.0.0.1"),
        ({"DFE_EXTERNAL_ORIGIN": "http://localhost"}, "", "http://127.0.0.1"),
        ({"DFE_BIND_SCOPE": "all"}, "0.0.0.0", "http://localhost"),
        (
            {"DFE_BIND_SCOPE": "all", "DFE_EXTERNAL_ORIGIN": "http://192.0.2.19"},
            "0.0.0.0",
            "http://192.0.2.19",
        ),
        (
            {"DFE_EXTERNAL_ORIGIN": "https://dfe.example.test/"},
            "",
            "https://dfe.example.test",
        ),
    ],
)
def test_the_source_runner_drives_the_address_testers_use(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    bound: str,
    expected: str,
) -> None:
    for key in ("DFE_EXTERNAL_ORIGIN", "DFE_BIND_SCOPE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert test_source._ui_origin(bound=bound, host="localhost") == expected
