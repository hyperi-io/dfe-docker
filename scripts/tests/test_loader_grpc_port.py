#  Project:      dfe-docker
#  File:         tests/test_loader_grpc_port.py
#  Purpose:      Assert every sender dials the port dfe-loader listens on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Every committed sender dials the port dfe-loader's Push listener binds.

dfe-engine compiles the receiver's loader destination from its app manifest, while
the loader's bind comes from `config/loader/grpc.yaml`. A mismatch is silent: the
receiver answers HTTP 202 and drops every record into a closed port. A text read,
because these tests run with no PyYAML.
"""

import re

from _common import COMPOSE_FILE, REPO_ROOT

# dfe-engine's appmgmt/apps.yaml declares dfe-loader `endpoints.push.port: 6000`.
MANIFEST_PUSH_PORT = 6000

_LOADER_CONFIG = REPO_ROOT / "config" / "loader" / "grpc.yaml"
_SENDER_DIRS = (REPO_ROOT / "config", REPO_ROOT / "tests" / "e2e" / "config")


def _loader_listen_port() -> int:
    body = _LOADER_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s+listen:\s*\S+:(\d+)\s*$", body, re.M)
    assert match, f"{_LOADER_CONFIG} names no grpc listen address"
    return int(match.group(1))


def _dialled_loader_ports() -> list[tuple[str, int]]:
    dialled: list[tuple[str, int]] = []
    for directory in _SENDER_DIRS:
        for path in sorted(directory.rglob("*.yaml")):
            body = path.read_text(encoding="utf-8")
            name = str(path.relative_to(REPO_ROOT))
            dialled.extend(
                (name, int(port)) for port in re.findall(r"dfe-loader:(\d+)", body)
            )
    return dialled


def test_the_loader_listens_where_the_engine_points_its_senders() -> None:
    assert _loader_listen_port() == MANIFEST_PUSH_PORT


def test_every_committed_sender_dials_the_loaders_listen_port() -> None:
    listen = _loader_listen_port()
    dialled = _dialled_loader_ports()

    assert dialled, "no committed config dials dfe-loader, so this test proves nothing"
    assert [(name, port) for name, port in dialled if port != listen] == []


def test_compose_publishes_the_port_the_loader_listens_on() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    published = re.findall(r"\$\{DFE_LOADER_GRPC_PORT:-\d+\}:(\d+)", compose)

    assert published == [str(_loader_listen_port())]
