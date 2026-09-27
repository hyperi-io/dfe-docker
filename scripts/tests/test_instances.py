#  Project:      dfe-docker
#  File:         tests/test_instances.py
#  Purpose:      Assert the compose service count follows the sources, not a constant
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a deployment actually runs for an app that runs one container per source.

The gap these close: Compose declared one dfe-fetcher, so a deployment with two
CrowdStrike accounts ran one of them. The count now comes from the index
dfe-engine writes when it renders each instance's config, so two sources are two
containers and no source is none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import instances

FETCHER = "dfe-fetcher"
VRL = "dfe-transform-vrl"


def _index(env_dir: Path, service: str, *names: str) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{service}{instances.INDEX_SUFFIX}").write_text(
        "".join(f"{name}\n" for name in names), encoding="utf-8"
    )


def test_two_sources_of_one_connector_are_two_services(tmp_path: Path) -> None:
    _index(tmp_path, FETCHER, "crowdstrike-eu", "crowdstrike-us")

    found = instances.declared(env_dir=tmp_path)

    assert instances.services(found) == [
        "dfe-fetcher-crowdstrike-eu",
        "dfe-fetcher-crowdstrike-us",
    ]


def test_a_deployment_with_no_source_declares_nothing(tmp_path: Path) -> None:
    _index(tmp_path, FETCHER)

    assert instances.declared(env_dir=tmp_path) == {}


def test_a_deployment_that_never_rendered_declares_nothing(tmp_path: Path) -> None:
    assert instances.declared(env_dir=tmp_path / "absent") == {}


def test_every_app_with_an_index_is_carried(tmp_path: Path) -> None:
    # Which apps run one per source is apps.yaml's to say, so the generator
    # discovers them rather than holding a list of its own.
    _index(tmp_path, FETCHER, "okta-audit")
    _index(tmp_path, VRL, "filebeat")

    assert instances.services(instances.declared(env_dir=tmp_path)) == [
        "dfe-fetcher-okta-audit",
        "dfe-transform-vrl-filebeat",
    ]


def test_each_service_reads_its_own_instance_config() -> None:
    text = instances.fragment({FETCHER: ["crowdstrike-eu", "crowdstrike-us"]})

    assert '"/etc/dfe/apps/dfe-fetcher/crowdstrike-eu/fetcher.yaml"' in text
    assert '"/etc/dfe/apps/dfe-fetcher/crowdstrike-us/fetcher.yaml"' in text


def test_each_service_extends_the_committed_one() -> None:
    # One definition of the image, volumes, limits and healthcheck.
    text = instances.fragment(
        {FETCHER: ["okta-audit"]}, compose_file="docker-compose.yml"
    )

    assert (
        "    extends:\n      file: docker-compose.yml\n      service: dfe-fetcher"
        in text
    )


def test_no_instance_publishes_a_host_port() -> None:
    # N containers cannot share one host port, and nothing outside the stack
    # addresses an instance directly.
    text = instances.fragment({FETCHER: ["a", "b"]})

    assert text.count("ports: !reset []") == 2


def test_each_container_is_named_for_its_source() -> None:
    text = instances.fragment({FETCHER: ["okta-audit"]})

    assert "  dfe-fetcher-okta-audit:" in text
    assert "OTEL_SERVICE_NAME: dfe-fetcher-okta-audit" in text


def test_each_instance_container_carries_the_stack_prefix() -> None:
    # A container name is daemon-wide, so two stacks collide here as well as on
    # the committed services.
    text = instances.fragment({FETCHER: ["okta-audit"]})

    assert "container_name: ${DFE_CONTAINER_PREFIX:-}dfe-fetcher-okta-audit" in text


def test_an_app_this_repo_has_no_service_for_is_named(tmp_path: Path) -> None:
    _index(tmp_path, "dfe-nonesuch", "somewhere")

    assert instances.unknown_apps(instances.declared(env_dir=tmp_path)) == [
        "dfe-nonesuch"
    ]


def test_an_app_with_a_config_file_but_no_compose_service_is_named(
    tmp_path: Path,
) -> None:
    # extends naming a service that is not there fails the whole compose file.
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  dfe-loader:\n    image: x\n", encoding="utf-8")
    _index(tmp_path, FETCHER, "okta-audit")

    found = instances.declared(env_dir=tmp_path)

    assert instances.unknown_apps(found, compose_file=compose) == [FETCHER]


def test_the_fragment_is_removed_when_the_last_source_goes(tmp_path: Path) -> None:
    # A stale file would declare containers for sources this deployment lost.
    target = tmp_path / "docker-compose.instances.yml"
    assert instances.write({FETCHER: ["okta-audit"]}, path=target) is True

    assert instances.write({}, path=target) is False
    assert not target.exists()


@pytest.mark.parametrize("service", sorted(instances.SERVICE_CONFIG_FILE))
def test_every_app_the_generator_knows_is_a_committed_compose_service(
    service: str,
) -> None:
    assert instances.extendable(service)
