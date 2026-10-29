#  Project:      dfe-docker
#  File:         tests/test_kafka_host_ports.py
#  Purpose:      Assert the broker publishes only the host listener
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The broker's in-network listener, 9092, stays off the host and 19092 is its only publish."""

import re
from collections.abc import Callable

import pytest

from _common import COMPOSE_FILE

# The two broker services' host publish lines, container port last, read as text since these tests run without PyYAML
_PUBLISH = re.compile(
    r"^\s+- \$\{DFE_BIND_HOST:-127\.0\.0\.1\}:\$\{(?P<key>\w+):-\d+\}:(?P<port>\d+)\s*$",
    flags=re.M,
)


def _broker_publishes() -> list[tuple[str, str]]:
    """The (variable, container port) of each broker host publish in docker-compose.yml."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    return [
        (match["key"], match["port"])
        for match in _PUBLISH.finditer(compose)
        if match["key"].startswith("KAFKA_")
    ]


def _not_in_network_port(*, entry: tuple[str, str]) -> bool:
    """Whether a publish leaves the in-network listener's 9092 off the host."""
    return entry[1] != "9092"


def _not_retired_key(*, entry: tuple[str, str]) -> bool:
    """Whether a publish is set by something other than the retired KAFKA_PLAINTEXT_PORT."""
    return entry[0] != "KAFKA_PLAINTEXT_PORT"


@pytest.mark.parametrize(
    ("what", "holds"),
    [
        pytest.param(
            "the in-network listener port", _not_in_network_port, id="no_9092"
        ),
        pytest.param(
            "the retired KAFKA_PLAINTEXT_PORT key", _not_retired_key, id="no_old_key"
        ),
    ],
)
def test_the_broker_does_not_publish(what: str, holds: Callable[..., bool]) -> None:
    published = _broker_publishes()

    assert published, "the broker publishes its host listener"
    assert all(holds(entry=entry) for entry in published), (
        f"{what} is published: {published}"
    )


def test_each_broker_backend_publishes_the_host_listener_once() -> None:
    assert _broker_publishes() == [("KAFKA_PLAINTEXT_HOST_PORT", "19092")] * 2
