#  Project:      dfe-docker
#  File:         tests/test_proxy_routes.py
#  Purpose:      Hold the console origin's route table and path normalisation to what the edge may publish
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""dfe-proxy's console origin answers 404 for dfe-ui's metrics scrape, and matches on the canonical path.

dfe-ui serves /metrics unauthenticated and the console origin faces every browser, so the proxy answers it, and everything under it, before dfe-ui sees the request. Envoy matches the path as sent unless told to normalise it, so `/%6Detrics` and `//metrics` would reach dfe-ui past an exact match; the console's connection manager normalises first. Normalisation rewrites the path the backend receives as well, so it is confined to that one origin and the rest of its route table is pinned as it was.
"""

from pathlib import Path

import pytest

import _envoy

PROXY_DIR = Path(__file__).resolve().parents[2] / "config" / "proxy"
_CONSOLE_CONFIGS = ["envoy.yaml", "envoy.tls.yaml"]
_ALL_CONFIGS = [*_CONSOLE_CONFIGS, "hyperdx.yaml", "hyperdx.tls.yaml"]
_CONSOLE_LISTENER = "ingress"
_NORMALISING = ("normalize_path", "merge_slashes")
# The console's routes in match order, as (match, cluster, direct status).
_CONSOLE_ROUTES = [
    ({"path": "/livez"}, "", "200"),
    ({"path_separated_prefix": "/metrics", "case_sensitive": "false"}, "", "404"),
    ({"prefix": "/api/v1/"}, "dfe-engine", ""),
    ({"prefix": "/"}, "dfe-ui", ""),
]


def _managers(name: str) -> list[_envoy.ConnectionManager]:
    return _envoy.connection_managers(
        text=(PROXY_DIR / name).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("name", _CONSOLE_CONFIGS)
def test_the_console_hides_metrics_and_routes_everything_else_unchanged(
    name: str,
) -> None:
    console = [m for m in _managers(name) if m.listener == _CONSOLE_LISTENER]

    assert len(console) == 1
    assert len(console[0].virtual_hosts) == 1
    routes = console[0].virtual_hosts[0].routes
    assert [(r.match, r.cluster, r.status) for r in routes] == _CONSOLE_ROUTES


@pytest.mark.parametrize("name", _CONSOLE_CONFIGS)
def test_the_console_matches_and_forwards_the_canonical_path(name: str) -> None:
    console = [m for m in _managers(name) if m.listener == _CONSOLE_LISTENER]

    assert console
    for manager in console:
        assert {key: manager.settings.get(key) for key in _NORMALISING} == {
            "normalize_path": "true",
            "merge_slashes": "true",
        }


@pytest.mark.parametrize("name", _ALL_CONFIGS)
def test_no_hyperdx_origin_rewrites_the_path_it_forwards(name: str) -> None:
    others = [m for m in _managers(name) if m.listener != _CONSOLE_LISTENER]

    assert others
    for manager in others:
        assert not (set(_NORMALISING) & set(manager.settings)), manager.listener
