#  Project:      dfe-docker
#  File:         tests/test_kafka_host_ports.py
#  Purpose:      Assert the broker publishes only the host listener
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The broker's in-network listener, 9092, stays off the host.

It advertises `kafka:9092`, so a client on the host that connected to it would be sent
to a name it cannot resolve, and it collides with the Kafka a dev box usually already
runs on 9092. The host listener is 19092, which advertises `localhost`. A text read,
because these tests run with no PyYAML.
"""

import re

import pytest

from _common import COMPOSE_FILE

# The host publish lines of the two broker services, container port last.
_PUBLISH = re.compile(
    r"^\s+- \$\{DFE_BIND_HOST:-127\.0\.0\.1\}:\$\{(?P<key>\w+):-\d+\}:(?P<port>\d+)\s*$",
    re.M,
)


def _broker_publishes() -> list[tuple[str, str]]:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    return [
        (match["key"], match["port"])
        for match in _PUBLISH.finditer(compose)
        if match["key"].startswith("KAFKA_")
    ]


@pytest.mark.parametrize(
    ("what", "holds"),
    [
        pytest.param(
            "the in-network listener port", lambda p: p[1] != "9092", id="no_9092"
        ),
        pytest.param(
            "the retired KAFKA_PLAINTEXT_PORT key",
            lambda p: p[0] != "KAFKA_PLAINTEXT_PORT",
            id="no_old_key",
        ),
    ],
)
def test_the_broker_does_not_publish(what, holds):
    published = _broker_publishes()

    assert published, "the broker publishes its host listener"
    assert all(holds(entry) for entry in published), f"{what} is published: {published}"


def test_each_broker_backend_publishes_the_host_listener_once():
    assert _broker_publishes() == [("KAFKA_PLAINTEXT_HOST_PORT", "19092")] * 2
