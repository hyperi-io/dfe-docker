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
import shutil
from pathlib import Path

import pytest

import check_compose
import init
import instances
from _common import COMPOSE_FILE, REPO_ROOT

_FRAGMENTS = "-f docker-compose.yml -f docker-compose.override.yml"
_PROXY_DIR = REPO_ROOT / "config" / "proxy"
# One strip list as the configs write it, at the indentation of a virtual host's keys.
_STRIP_LIST = (
    "                      request_headers_to_remove:\n"
    "                        - X-Forwarded-Email\n"
    "                        - X-Forwarded-Groups\n"
    "                        - X-OIDC-Subject\n"
    "                        - X-OIDC-Groups\n"
)

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
    """An unmounted /data strands one anonymous volume per down/up cycle."""
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-documentdb"]["volumes"] = []

    failures = check_compose._unmounted_image_volume_failures(config=config)

    assert len(failures) == 1
    assert "hyperdx-documentdb" in failures[0]
    assert "/data" in failures[0]


def test_an_anonymous_compose_volume_is_still_unmounted():
    """`- /data` with no source is the same anonymous volume, declared in compose."""
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-documentdb"]["volumes"] = [
        {"type": "volume", "target": "/data"}
    ]

    assert check_compose._unmounted_image_volume_failures(config=config)


def test_named_volumes_and_binds_both_count_as_mounted():
    config = _every_image_volume_mounted()
    config["services"]["hyperdx-documentdb"]["volumes"] = [
        {"type": "bind", "source": "/srv/documentdb-data", "target": "/data"}
    ]

    assert check_compose._unmounted_image_volume_failures(config=config) == []


def test_a_listed_service_missing_from_the_stack_fails_rather_than_passing():
    config = _every_image_volume_mounted()
    del config["services"]["kafka-apache"]

    failures = check_compose._unmounted_image_volume_failures(config=config)

    assert failures == ["kafka-apache: in _IMAGE_VOLUMES but not in the stack"]


def _table_images() -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """Every table service on a stand-in ref, and each ref declaring what the table lists."""
    images = {name: f"{name}:pinned" for name in check_compose._IMAGE_VOLUMES}
    declared = {
        images[name]: frozenset(paths)
        for name, paths in check_compose._IMAGE_VOLUMES.items()
    }
    return images, declared


def test_local_images_declaring_exactly_the_table_pass():
    images, declared = _table_images()
    images["dfe-loader"] = "dfe-loader:pinned"
    declared["dfe-loader:pinned"] = frozenset()

    failures, checked, skipped = check_compose._image_volume_drift(
        images=images, inspect=declared.get
    )

    assert failures == []
    assert (checked, skipped) == (len(images), 0)


def test_a_path_the_image_declares_and_the_table_lacks_fails():
    images, declared = _table_images()
    declared["hyperdx-documentdb:pinned"] = frozenset({"/data", "/var/lib/documentdb"})

    failures, _, _ = check_compose._image_volume_drift(
        images=images, inspect=declared.get
    )

    assert failures == [
        "hyperdx-documentdb: hyperdx-documentdb:pinned declares VOLUME "
        "/var/lib/documentdb, which _IMAGE_VOLUMES does not list -- add it there so "
        "the mount check holds compose to it"
    ]


def test_a_service_missing_from_the_table_whose_image_declares_a_volume_fails():
    """The leak the table alone cannot see: a new service on an image with a VOLUME."""
    failures, checked, _ = check_compose._image_volume_drift(
        images={"dfe-newcomer": "newcomer:1"},
        inspect={"newcomer:1": frozenset({"/data"})}.get,
    )

    assert checked == 1
    assert len(failures) == 1
    assert failures[0].startswith("dfe-newcomer: newcomer:1 declares VOLUME /data")


def test_a_path_the_table_lists_and_the_image_does_not_declare_fails():
    images, declared = _table_images()
    declared["kafka-apache:pinned"] = frozenset({"/var/lib/kafka/data"})

    failures, _, _ = check_compose._image_volume_drift(
        images=images, inspect=declared.get
    )

    assert failures == [
        f"kafka-apache: _IMAGE_VOLUMES lists {path}, which kafka-apache:pinned does "
        "not declare -- drop the stale entry"
        for path in ("/etc/kafka/secrets", "/mnt/shared/config")
    ]


def test_an_image_the_daemon_does_not_hold_is_skipped_not_failed():
    images, declared = _table_images()
    del declared["clickhouse:pinned"]

    failures, checked, skipped = check_compose._image_volume_drift(
        images=images, inspect=declared.get
    )

    assert failures == []
    assert (checked, skipped) == (len(images) - 1, 1)


def test_with_no_docker_daemon_every_image_is_skipped():
    images, _ = _table_images()

    assert check_compose._image_volume_drift(images=images, inspect=None) == (
        [],
        0,
        len(images),
    )


def test_one_image_several_services_run_is_inspected_once():
    asked: list[str] = []

    def inspect(image: str) -> frozenset[str]:
        asked.append(image)
        return frozenset()

    failures, checked, _ = check_compose._image_volume_drift(
        images={"dfe-archiver": "archiver:1", "dfe-archiver-e2e": "archiver:1"},
        inspect=inspect,
    )

    assert failures == []
    assert checked == 2
    assert asked == ["archiver:1"]


def test_no_docker_on_the_path_skips_the_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    assert check_compose._local_image_inspector() is None


def test_a_docker_with_no_daemon_skips_the_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("needs the docker binary, which is what reaches for the daemon")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path / 'absent.sock'}")

    assert check_compose._local_image_inspector() is None


def test_an_image_the_daemon_does_not_hold_reads_as_not_local():
    inspect = check_compose._local_image_inspector()
    if inspect is None:
        pytest.skip("no docker daemon answered, so no image can be inspected")

    assert inspect("dfe-compose-check.invalid/absent:0.0.0") is None


def test_every_local_table_image_declares_exactly_the_paths_the_table_lists():
    """The table measured against the pinned images themselves, where this host holds one."""
    inspect = check_compose._local_image_inspector()
    if inspect is None:
        pytest.skip("no docker daemon answered, so no image can be inspected")
    env, _ = check_compose._check_env()
    config = check_compose._image_volume_model(env=env)
    assert config is not None, "the registry compose path did not resolve"
    images = {
        name: image
        for name, image in check_compose._service_images(config=config).items()
        if name in check_compose._IMAGE_VOLUMES
    }

    failures, checked, _ = check_compose._image_volume_drift(
        images=images, inspect=inspect
    )

    if not checked:
        pytest.skip("no image the table names is local, and this test pulls none")
    assert failures == []


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


def _reads(*names: str, instance_of: str = "") -> dict:
    """One service that read the env files named, in order, over the sentinels."""
    environment: dict[str, str] = {}
    for index, name in enumerate(names):
        environment[check_compose._SENTINEL_LAST] = name
        if name.endswith(".custom.env"):
            environment[f"{check_compose._SENTINEL_READ}{index}"] = name
    service: dict = {"environment": environment}
    if instance_of:
        service["labels"] = {instances.INSTANCE_LABEL: instance_of}
    return service


def test_an_app_reading_the_short_custom_name_fails_twice():
    """The engine writes dfe-loader.custom.env, so loader.custom.env reached nothing."""
    failures = check_compose._custom_env_mismatches(
        services={"dfe-loader": _reads("loader.env", "loader.custom.env")}
    )

    assert len(failures) == 2
    assert "env/loader.custom.env, which dfe-engine never writes" in failures[0]
    assert "reads no env/dfe-loader.custom.env" in failures[1]


def test_an_app_reading_its_own_custom_file_last_passes():
    services = {
        "dfe-loader": _reads("loader.env", "dfe-loader.custom.env"),
        "kafka-ui": _reads(),
    }

    assert check_compose._custom_env_mismatches(services=services) == []


def test_an_app_whose_own_file_is_not_last_fails():
    failures = check_compose._custom_env_mismatches(
        services={"dfe-loader": _reads("dfe-loader.custom.env", "loader.env")}
    )

    assert failures == [
        "dfe-loader: reads env/loader.env after env/dfe-loader.custom.env, so it "
        "outvotes a key set through dfe-engine"
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

    assert check_compose._custom_env_mismatches(services=services) == []


def test_an_instance_with_no_file_of_its_own_fails():
    """A per-source extraEnv lands in the instance's file, which it never read."""
    failures = check_compose._custom_env_mismatches(
        services={
            "dfe-fetcher-okta": _reads(
                "fetcher.env", "dfe-fetcher.custom.env", instance_of="dfe-fetcher"
            )
        }
    )

    assert failures == [
        "dfe-fetcher-okta: reads no env/dfe-fetcher-okta.custom.env, so a key set "
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
            }
        )

        assert len(failures) == 1
        assert f"env/{name}, which dfe-engine never writes for it" in failures[0]


def _proxy_text(name: str) -> str:
    return (_PROXY_DIR / name).read_text(encoding="utf-8")


def _gated(*, app: str, api: str) -> dict:
    return {
        "services": {
            "oauth2-proxy-hyperdx": {"environment": {"OAUTH2_PROXY_UPSTREAMS": app}},
            "oauth2-proxy-hyperdx-api": {
                "environment": {"OAUTH2_PROXY_UPSTREAMS": api}
            },
        }
    }


@pytest.mark.parametrize(
    ("name", "hosts"),
    [
        ("hyperdx.yaml", 2),
        ("hyperdx.tls.yaml", 4),
        ("envoy.yaml", 1),
        ("envoy.tls.yaml", 1),
    ],
)
def test_every_route_to_hyperdx_strips_caller_supplied_identity(
    hosts: int, name: str
) -> None:
    """Both HyperDX origins on every chain, and dfe-proxy's embed, which skips dfe-hyperdx-proxy."""
    failures, made = check_compose._identity_strip_failures(
        label=name, text=_proxy_text(name)
    )

    assert failures == []
    assert made == hosts


def test_a_route_to_hyperdx_missing_one_header_fails():
    """The fork's default email header is the one a header-dev HyperDX reads with no setting."""
    original = _proxy_text("hyperdx.yaml")
    text = original.replace("                        - X-Forwarded-Email\n", "", 1)

    failures, _ = check_compose._identity_strip_failures(
        label="hyperdx.yaml", text=text
    )

    assert text != original
    assert len(failures) == 1
    assert failures[0].startswith("hyperdx.yaml hyperdx-app/hyperdx: ")
    assert "['x-forwarded-email']" in failures[0]


def test_a_tls_chain_that_drops_the_strip_fails_while_its_plain_twin_keeps_it():
    original = _proxy_text("hyperdx.tls.yaml")
    text = original.replace(_STRIP_LIST, "", 1)

    failures, made = check_compose._identity_strip_failures(
        label="hyperdx.tls.yaml", text=text
    )

    assert text != original
    assert made == 4
    assert len(failures) == 1
    assert failures[0].startswith("hyperdx.tls.yaml hyperdx-app/hyperdx: ")


def test_a_proxy_that_stamps_an_identity_fails_even_while_it_strips():
    """Strip then add is the fixed-operator stamping the engine-issued token replaced."""
    original = _proxy_text("envoy.yaml")
    stamp = (
        "                      request_headers_to_add:\n"
        "                        - header:\n"
        "                            key: X-Forwarded-Email\n"
        '                            value: "admin@dfe.local"\n'
    )
    text = original.replace(_STRIP_LIST, _STRIP_LIST + stamp, 1)

    failures, _ = check_compose._identity_strip_failures(label="envoy.yaml", text=text)

    assert text != original
    assert failures == [
        "envoy.yaml hyperdx-embed/hyperdx: adds ['x-forwarded-email'], an identity no "
        "proxy in this stack asserts"
    ]


def test_a_config_that_routes_nothing_to_hyperdx_fails_rather_than_passing():
    text = _proxy_text("hyperdx.yaml").replace(
        "address: hyperdx,", "address: elsewhere,"
    )

    failures, made = check_compose._identity_strip_failures(
        label="hyperdx.yaml", text=text
    )

    assert made == 0
    assert failures == [
        "hyperdx.yaml: no virtual host routes to HyperDX, so nothing was checked"
    ]


def test_a_strip_list_written_another_way_is_refused_rather_than_read_as_empty():
    original = _proxy_text("hyperdx.yaml")
    text = original.replace(
        _STRIP_LIST,
        "                      request_headers_to_remove: [X-Forwarded-Email]\n",
        1,
    )

    assert text != original
    with pytest.raises(ValueError, match="request_headers_to_remove must be a block"):
        check_compose._identity_strip_failures(label="hyperdx.yaml", text=text)


def test_hyperdx_gates_that_reach_hyperdx_directly_pass():
    config = _gated(app="http://hyperdx:8090", api="http://hyperdx:8000")

    assert check_compose._gate_upstream_mismatches(config=config) == []


def test_a_hyperdx_gate_routed_through_dfe_hyperdx_proxy_fails():
    """dfe-hyperdx-proxy strips the identity oauth2-proxy sets, so header-dev loses its login."""
    config = _gated(app="http://dfe-hyperdx-proxy:8090", api="http://hyperdx:8000")

    failures = check_compose._gate_upstream_mismatches(config=config)

    assert len(failures) == 1
    assert failures[0].startswith(
        "oauth2-proxy-hyperdx: OAUTH2_PROXY_UPSTREAMS='http://dfe-hyperdx-proxy:8090', "
        "want 'http://hyperdx:8090'"
    )


def test_a_hyperdx_gate_missing_from_the_stack_fails_rather_than_passing():
    config = _gated(app="http://hyperdx:8090", api="http://hyperdx:8000")
    del config["services"]["oauth2-proxy-hyperdx-api"]

    assert check_compose._gate_upstream_mismatches(config=config) == [
        "oauth2-proxy-hyperdx-api: gates HyperDX but is not in the stack"
    ]


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


def _checking(*, enabled: str = "true", send_instance_id: str = "true") -> dict:
    """A resolved model in which every release-check app carries the dial's two values."""
    return {
        "services": {
            name: {
                "environment": {
                    f"{prefix}VERSION_CHECK__ENABLED": enabled,
                    f"{prefix}VERSION_CHECK__SEND_INSTANCE_ID": send_instance_id,
                }
            }
            for name, prefix in check_compose._VERSION_CHECK_PREFIXES.items()
        }
    }


def test_every_app_carrying_the_dial_under_its_own_prefix_passes():
    assert (
        check_compose._version_check_mismatches(
            config=_checking(), enabled="true", send_instance_id="true"
        )
        == []
    )


def test_an_app_that_defaults_the_check_off_fails():
    config = _checking()
    config["services"]["dfe-loader"]["environment"][
        "DFE_LOADER_VERSION_CHECK__ENABLED"
    ] = "false"

    failures = check_compose._version_check_mismatches(
        config=config, enabled="true", send_instance_id="true"
    )

    assert failures == [
        "dfe-loader: DFE_LOADER_VERSION_CHECK__ENABLED=false, want true"
    ]


def test_the_archiver_is_read_under_its_short_prefix_not_dfe_archiver():
    config = _checking()
    environment = config["services"]["dfe-archiver"]["environment"]
    environment["DFE_ARCHIVER_VERSION_CHECK__ENABLED"] = environment.pop(
        "ARCHIVER_VERSION_CHECK__ENABLED"
    )

    failures = check_compose._version_check_mismatches(
        config=config, enabled="true", send_instance_id="true"
    )

    assert failures == [
        "dfe-archiver: ARCHIVER_VERSION_CHECK__ENABLED=unset, want true"
    ]


def test_the_engine_is_read_bare_and_elastic_under_its_own_prefix():
    prefixes = check_compose._VERSION_CHECK_PREFIXES

    assert prefixes["dfe-engine"] == ""
    assert prefixes["dfe-transform-elastic"] == "DFE_TRANSFORM_ELASTIC_"
    assert prefixes["dfe-transform-vrl"] == prefixes["dfe-transform-vector"]
    assert prefixes["dfe-transform-vrl"] == "DFE_TRANSFORM_"


def test_an_app_missing_from_the_stack_fails_the_release_check_rather_than_passing():
    config = _checking()
    del config["services"]["dfe-receiver"]

    failures = check_compose._version_check_mismatches(
        config=config, enabled="true", send_instance_id="true"
    )

    assert failures == ["dfe-receiver: runs the release check but is not in the stack"]


def test_the_opted_out_case_wants_false_from_every_app():
    config = _checking(enabled="false")

    assert (
        check_compose._version_check_mismatches(
            config=config, enabled="false", send_instance_id="true"
        )
        == []
    )
    assert check_compose._version_check_mismatches(
        config=config, enabled="true", send_instance_id="true"
    )


def test_the_first_release_check_case_is_the_default_and_it_is_on():
    label, dials, wanted = check_compose._VERSION_CHECK_CASES[0]

    assert (label, dials, wanted) == ("defaults", {}, ("true", "true"))


def test_every_dfe_app_has_a_release_check_decision():
    assert check_compose._version_check_coverage_failures() == []


def test_a_new_dfe_app_nobody_decided_for_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        check_compose,
        "_DFE_OWNED_SERVICES",
        {*check_compose._DFE_OWNED_SERVICES, "dfe-newcomer"},
    )

    failures = check_compose._version_check_coverage_failures()

    assert len(failures) == 1
    assert failures[0].startswith("dfe-newcomer: no release-check decision")
