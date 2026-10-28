#  Project:      dfe-docker
#  File:         tests/test_port_check.py
#  Purpose:      Assert the start gate names a taken host port and the variable that moves it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""scripts/port_check.py: published ports from a compose config, bind probe, variable lookup."""

import socket
from collections.abc import Iterator

import pytest

import port_check

COMPOSE = """\
    ports:
      - ${DFE_BIND_HOST:-127.0.0.1}:${CLICKHOUSE_HTTP_PORT:-8123}:8123
      # - ${DFE_BIND_HOST:-127.0.0.1}:${COMMENTED_PORT:-5555}:5555
      - ${DFE_BIND_HOST:-127.0.0.1}:${CLICKHOUSE_NATIVE_PORT:-9000}:9000
"""


@pytest.fixture
def held_port() -> Iterator[int]:
    """A loopback TCP port something is listening on."""
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        yield holder.getsockname()[1]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {8123: "CLICKHOUSE_HTTP_PORT", 9000: "CLICKHOUSE_NATIVE_PORT"}),
        (
            {"CLICKHOUSE_HTTP_PORT": "18123"},
            {18123: "CLICKHOUSE_HTTP_PORT", 9000: "CLICKHOUSE_NATIVE_PORT"},
        ),
    ],
)
def test_variables_for_follows_env_then_default(env: dict, expected: dict) -> None:
    assert port_check.variables_for(compose_text=COMPOSE, env=env) == expected


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, []),
        ({"services": {"a": {}}}, []),
        ({"services": {"a": {"ports": [{"target": 80}]}}}, []),
        (
            {
                "services": {
                    "a": {"ports": [{"host_ip": "127.0.0.1", "published": "8123"}]},
                    "b": {"ports": [{"published": "53", "protocol": "udp"}]},
                }
            },
            [("a", "127.0.0.1", 8123, "tcp"), ("b", "0.0.0.0", 53, "udp")],
        ),
    ],
)
def test_published_ports(config: dict, expected: list) -> None:
    assert port_check.published_ports(config=config) == expected


def test_conflicts_names_the_taken_port_and_its_variable(held_port: int) -> None:
    ports = [("clickhouse", "127.0.0.1", held_port, "tcp")]
    lines = port_check.conflicts(ports=ports, names={held_port: "CLICKHOUSE_HTTP_PORT"})
    assert len(lines) == 1
    assert "CLICKHOUSE_HTTP_PORT" in lines[0]
    assert str(held_port) in lines[0]


def test_conflicts_is_empty_for_a_free_port() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert port_check.conflicts(ports=[("a", "127.0.0.1", free, "tcp")], names={}) == []
