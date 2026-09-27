#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         tests/test_check_compose.py
#  Purpose:      Prove the dev-path reader keeps every compose line it matches,
#                so a goal printing two of them is asserted twice
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for check_compose's pure halves: the `make -n dev` reader and the rules
it applies to a resolved compose model.

The rest of check_compose.py shells out to `docker compose` and `make`, which
needs a resolved stack. These halves are parsers and rules over the output, so
they are testable on a string or a dict -- and they decide how much each guard
actually looks at.
"""

from __future__ import annotations

import check_compose

_FRAGMENTS = "-f docker-compose.yml -f docker-compose.override.yml"


def test_it_reads_the_files_off_each_subcommand():
    found = check_compose._dev_compose_files(
        output=f"docker compose {_FRAGMENTS} pull\ndocker compose {_FRAGMENTS} up -d\n"
    )

    assert sorted(found) == ["pull", "up"]
    assert found["up"] == [["docker-compose.yml", "docker-compose.override.yml"]]


def test_a_second_line_does_not_hide_the_first():
    """One `up -d` with a fragment missing used to pass if a later one had it."""
    found = check_compose._dev_compose_files(
        output=(
            "docker compose -f docker-compose.yml up -d dfe-engine\n"
            f"docker compose {_FRAGMENTS} up -d\n"
        )
    )

    assert found["up"] == [
        ["docker-compose.yml"],
        ["docker-compose.yml", "docker-compose.override.yml"],
    ]


def test_a_goal_that_prints_no_compose_line_reads_as_absent():
    assert check_compose._dev_compose_files(output="echo nothing to do\n") == {}


def _logged(**options: str) -> dict:
    return {"logging": {"driver": "fluentd", "options": options}}


def test_a_shipped_service_that_does_not_wait_for_the_ack_fails():
    """Without the ack, lines sent before the collector listens are counted as sent."""
    config = {"services": {"dfe-loader": _logged(**{"fluentd-async": "true"})}}

    failures = check_compose._log_driver_failures(config=config)

    assert len(failures) == 1
    assert "dfe-loader" in failures[0]
    assert "fluentd-request-ack" in failures[0]


def test_a_shipped_service_that_does_not_queue_fails():
    config = {"services": {"dfe-loader": _logged(**{"fluentd-request-ack": "true"})}}

    failures = check_compose._log_driver_failures(config=config)

    assert [f for f in failures if "fluentd-async" in f] == failures
    assert len(failures) == 1


def _every_image_volume_mounted() -> dict:
    return {
        "services": {
            name: {
                "volumes": [
                    {"type": "volume", "source": f"v{index}", "target": path}
                    for index, path in enumerate(paths)
                ]
            }
            for name, paths in check_compose._IMAGE_VOLUMES.items()
        }
    }


def test_an_image_volume_compose_leaves_unmounted_fails():
    """An unmounted /state strands one anonymous volume per down/up cycle."""
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-ferretdb"]["volumes"] = []

    failures = check_compose._unmounted_image_volume_failures(config=config)

    assert len(failures) == 1
    assert "hyperdx-ferretdb" in failures[0]
    assert "/state" in failures[0]


def test_an_anonymous_compose_volume_is_still_unmounted():
    """`- /state` with no source is the same anonymous volume, declared in compose."""
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-ferretdb"]["volumes"] = [
        {"type": "volume", "target": "/state"}
    ]

    assert check_compose._unmounted_image_volume_failures(config=config)


def test_named_volumes_and_binds_both_count_as_mounted():
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-ferretdb"]["volumes"] = [
        {"type": "bind", "source": "/srv/ferret-state", "target": "/state"}
    ]

    assert check_compose._unmounted_image_volume_failures(config=config) == []


def test_a_listed_service_missing_from_the_stack_fails_rather_than_passing():
    config = _every_image_volume_mounted()
    del config["services"]["kafka-apache"]

    failures = check_compose._unmounted_image_volume_failures(config=config)

    assert failures == ["kafka-apache: in _IMAGE_VOLUMES but not in the stack"]


def _dialling(*, host: str, http: str, native: str) -> dict:
    engine_keys = {
        "DFE_CLICKHOUSE_HOST": host,
        "DFE_CLICKHOUSE_PORT": http,
        "DFE_CLICKHOUSE_NATIVE_PORT": native,
    }
    return {
        "services": {
            "dfe-engine": {"environment": dict(engine_keys)},
            "dfe-hunt-runner": {"environment": dict(engine_keys)},
            "otel-collector": {
                "environment": {"CLICKHOUSE_HOST": host, "CLICKHOUSE_PORT": native}
            },
        }
    }


def test_every_dialler_on_the_container_ports_passes():
    config = _dialling(host="clickhouse", http="8123", native="9000")

    assert (
        check_compose._clickhouse_dial_mismatches(
            config=config, want=("clickhouse", "8123", "9000")
        )
        == []
    )


def test_a_dialler_on_the_moved_host_port_fails():
    """The shape #75 reported: the engine dialled the publish, not the container."""
    config = _dialling(host="clickhouse", http="8123", native="9000")
    config["services"]["dfe-engine"]["environment"]["DFE_CLICKHOUSE_PORT"] = "18123"

    failures = check_compose._clickhouse_dial_mismatches(
        config=config, want=("clickhouse", "8123", "9000")
    )

    assert failures == ["dfe-engine: DFE_CLICKHOUSE_PORT=18123, want 8123"]


def test_the_collector_is_held_to_the_native_port_it_dials():
    config = _dialling(host="clickhouse", http="8123", native="9000")
    config["services"]["otel-collector"]["environment"]["CLICKHOUSE_PORT"] = "19000"

    failures = check_compose._clickhouse_dial_mismatches(
        config=config, want=("clickhouse", "8123", "9000")
    )

    assert failures == ["otel-collector: CLICKHOUSE_PORT=19000, want 9000"]


def test_a_dialler_missing_from_the_stack_fails_rather_than_passing():
    config = _dialling(host="clickhouse", http="8123", native="9000")
    del config["services"]["dfe-hunt-runner"]

    failures = check_compose._clickhouse_dial_mismatches(
        config=config, want=("clickhouse", "8123", "9000")
    )

    assert failures == ["dfe-hunt-runner: dials ClickHouse but is not in the stack"]


def test_a_service_that_queues_and_waits_passes_and_json_file_is_not_checked():
    config = {
        "services": {
            "dfe-loader": _logged(
                **{"fluentd-async": "true", "fluentd-request-ack": "true"}
            ),
            "clickhouse": {"logging": {"driver": "json-file"}},
            "kafka-ui": {},
        }
    }

    assert check_compose._log_driver_failures(config=config) == []
