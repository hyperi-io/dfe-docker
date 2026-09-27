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

import re
from pathlib import Path

import check_compose
import init
import instances
from _common import COMPOSE_FILE

_FRAGMENTS = "-f docker-compose.yml -f docker-compose.override.yml"

# A service key in docker-compose.yml: two spaces in, alone on its line.
_SERVICE_KEY_RE = re.compile(r"^  ([A-Za-z0-9._-]+):$", re.MULTILINE)


def test_a_checkout_with_no_env_is_checked_as_its_first_start(dotenv: Path) -> None:
    # No check-* target writes .env, so the check mints its secrets in memory.
    values = check_compose._first_start_dotenv()

    assert not dotenv.exists()
    assert all(values.get(key) for key in init.GENERATED_SECRETS)


def test_a_checkout_with_an_env_is_checked_on_its_own(dotenv: Path) -> None:
    dotenv.write_text("DFE_ENV=production\n", encoding="utf-8", newline="\n")

    assert check_compose._first_start_dotenv() == {"DFE_ENV": "production"}


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


def _seeding(connections: str) -> dict:
    return {
        "services": {"hyperdx": {"environment": {"DEFAULT_CONNECTIONS": connections}}}
    }


def test_hyperdx_dialling_the_wanted_url_passes():
    config = _seeding('[{"name":"platform","host":"https://ch.example.invalid:8443"}]')

    assert (
        check_compose._hyperdx_connection_mismatches(
            config=config, want="https://ch.example.invalid:8443"
        )
        == []
    )


def test_hyperdx_left_on_the_bundled_container_fails_an_external_host():
    """The literal connection HyperDX kept whatever CLICKHOUSE_HOST said."""
    config = _seeding('[{"name":"platform","host":"http://clickhouse:8123"}]')

    failures = check_compose._hyperdx_connection_mismatches(
        config=config, want="http://ch.example.invalid:8123"
    )

    assert failures == [
        "hyperdx: DEFAULT_CONNECTIONS dials ['http://clickhouse:8123'], "
        "want ['http://ch.example.invalid:8123']"
    ]


def test_hyperdx_on_plain_http_fails_a_tls_host():
    config = _seeding('[{"name":"platform","host":"http://ch.example.invalid:8443"}]')

    assert check_compose._hyperdx_connection_mismatches(
        config=config, want="https://ch.example.invalid:8443"
    )


def test_a_second_hyperdx_connection_fails_rather_than_hiding_behind_the_first():
    config = _seeding(
        '[{"host":"http://clickhouse:8123"},{"host":"http://other.invalid:8123"}]'
    )

    assert check_compose._hyperdx_connection_mismatches(
        config=config, want="http://clickhouse:8123"
    )


def test_unparseable_hyperdx_connections_fail_rather_than_passing():
    config = _seeding("[{not json")

    failures = check_compose._hyperdx_connection_mismatches(
        config=config, want="http://clickhouse:8123"
    )

    assert len(failures) == 1
    assert "is not JSON" in failures[0]


def test_hyperdx_missing_from_the_stack_fails_rather_than_passing():
    failures = check_compose._hyperdx_connection_mismatches(
        config={"services": {}}, want="http://clickhouse:8123"
    )

    assert failures == ["hyperdx: dials ClickHouse but is not in the stack"]


def test_a_committed_service_named_like_a_generated_instance_fails():
    """Compose merged a `filebeat` source's instance into the static one."""
    failures = check_compose._instance_name_collisions(
        services={"dfe-transform-vrl-filebeat": {}}
    )

    assert len(failures) == 1
    assert failures[0].startswith("dfe-transform-vrl-filebeat: the dfe-transform-vrl")
    assert "'filebeat'" in failures[0]


def test_the_apps_and_their_e2e_instances_take_no_instance_name():
    services = {
        name: {}
        for name in (
            "dfe-fetcher",
            "dfe-loader",
            "dfe-transform-e2e-vrl-filebeat",
            "dfe-transform-vrl",
            "kafka-ui",
        )
    }

    assert check_compose._instance_name_collisions(services=services) == []


def test_no_committed_service_can_take_a_generated_instance_name():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    block = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = {name: {} for name in _SERVICE_KEY_RE.findall(block)}

    assert "dfe-transform-vrl" in services
    assert check_compose._instance_name_collisions(services=services) == []


# An env/ directory for the custom env rule, as `docker compose config` names it.
_ENV = Path("/stack/env")


def _reads(*names: str, instance_of: str = "") -> dict:
    """One service reading the env files named, in order, as the model carries them."""
    service: dict = {"env_file": [{"path": str(_ENV / n)} for n in names]}
    if instance_of:
        service["labels"] = {instances.INSTANCE_LABEL: instance_of}
    return service


def test_an_app_reading_the_short_custom_name_fails_twice():
    """The engine writes dfe-loader.custom.env, so loader.custom.env reached nothing."""
    failures = check_compose._custom_env_mismatches(
        services={"dfe-loader": _reads("loader.env", "loader.custom.env")},
        env_dir=_ENV,
    )

    assert len(failures) == 2
    assert "loader.custom.env, which dfe-engine never writes" in failures[0]
    assert "reads no dfe-loader.custom.env" in failures[1]


def test_an_app_reading_its_own_custom_file_last_passes():
    services = {
        "dfe-loader": _reads("loader.env", "dfe-loader.custom.env"),
        "kafka-ui": _reads(),
    }

    assert check_compose._custom_env_mismatches(services=services, env_dir=_ENV) == []


def test_an_app_whose_own_file_is_not_last_fails():
    failures = check_compose._custom_env_mismatches(
        services={"dfe-loader": _reads("dfe-loader.custom.env", "loader.env")},
        env_dir=_ENV,
    )

    assert failures == [
        "dfe-loader: reads loader.env after dfe-loader.custom.env, so it outvotes a "
        "key set through dfe-engine"
    ]


def test_an_instance_reads_its_apps_file_then_its_own():
    services = {
        "dfe-fetcher-okta": _reads(
            "fetcher.env",
            "dfe-fetcher.custom.env",
            "dfe-fetcher-okta.custom.env",
            instance_of="dfe-fetcher",
        )
    }

    assert check_compose._custom_env_mismatches(services=services, env_dir=_ENV) == []


def test_an_instance_with_no_file_of_its_own_fails():
    """A per-source extraEnv lands in the instance's file, which it never read."""
    failures = check_compose._custom_env_mismatches(
        services={
            "dfe-fetcher-okta": _reads(
                "fetcher.env", "dfe-fetcher.custom.env", instance_of="dfe-fetcher"
            )
        },
        env_dir=_ENV,
    )

    assert failures == [
        "dfe-fetcher-okta: reads no dfe-fetcher-okta.custom.env, so a key set "
        "through dfe-engine never reaches it"
    ]


def test_a_service_the_engine_does_not_render_reads_no_custom_file():
    """The engine writes nothing for a static e2e instance, under any name."""
    for name in ("transform-vrl-filebeat.custom.env", "dfe-transform-vrl.custom.env"):
        failures = check_compose._custom_env_mismatches(
            services={
                "dfe-transform-e2e-vrl-filebeat": _reads(
                    "transform-vrl-filebeat.env", name
                )
            },
            env_dir=_ENV,
        )

        assert len(failures) == 1
        assert f"{name}, which dfe-engine never writes for it" in failures[0]


def test_a_custom_file_outside_env_fails():
    failures = check_compose._custom_env_mismatches(
        services={
            "dfe-loader": {
                "env_file": [
                    {"path": "/elsewhere/dfe-loader.custom.env"},
                    {"path": str(_ENV / "dfe-loader.custom.env")},
                ]
            }
        },
        env_dir=_ENV,
    )

    assert len(failures) == 1
    assert failures[0].startswith("dfe-loader: reads /elsewhere/dfe-loader.custom.env")


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
