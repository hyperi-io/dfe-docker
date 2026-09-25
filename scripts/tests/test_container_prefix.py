#  Project:      dfe-docker
#  File:         tests/test_container_prefix.py
#  Purpose:      Assert the container-prefix fragment renames every container and nothing else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The prefix fragment, read against the committed compose file."""

from pathlib import Path

import pytest

import container_prefix
from _common import COMPOSE_FILE


def test_every_fixed_container_name_in_the_compose_file_is_read() -> None:
    names = container_prefix.committed_names()
    fixed = COMPOSE_FILE.read_text(encoding="utf-8").count("\n    container_name: ")

    assert names["clickhouse"] == "dfe-clickhouse"
    assert names["dfe-engine"] == "dfe-engine"
    assert names["otel-collector"] == "dfe-otel-collector"
    assert len(names) == fixed


def test_only_the_services_block_is_read(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "x-anchor: &a\n  container_name: not-a-service\n"
        "services:\n"
        "  app:\n"
        "    image: x\n"
        "    container_name: dfe-app\n"
        "  unnamed:\n"
        "    image: y\n"
        "volumes:\n"
        "  data:\n"
        "    container_name: not-a-service-either\n",
        encoding="utf-8",
    )

    assert container_prefix.committed_names(compose_file=compose) == {"app": "dfe-app"}


def test_the_fragment_renames_each_service_under_its_own_key() -> None:
    text = container_prefix.fragment("e2e-", {"dfe-loader": "dfe-loader"})

    assert "  dfe-loader:\n    container_name: e2e-dfe-loader\n" in text


def test_an_instance_is_renamed_with_the_committed_services(tmp_path: Path) -> None:
    target = tmp_path / "docker-compose.prefix.yml"

    assert container_prefix.write("kt-", ["dfe-transform-vrl-meraki"], path=target)

    text = target.read_text(encoding="utf-8")
    assert "container_name: kt-dfe-transform-vrl-meraki\n" in text
    assert "container_name: kt-dfe-receiver\n" in text


def test_no_prefix_removes_a_fragment_left_by_an_earlier_one(tmp_path: Path) -> None:
    target = tmp_path / "docker-compose.prefix.yml"
    target.write_text("services: {}\n", encoding="utf-8")

    assert not container_prefix.write("", [], path=target)
    assert not target.exists()


@pytest.mark.parametrize("prefix", ["-e2e", "e2e/", "e 2e", "_e2e"])
def test_a_prefix_docker_would_refuse_is_refused_here(prefix: str) -> None:
    with pytest.raises(container_prefix.PrefixError, match="cannot start a container"):
        container_prefix.validate(prefix)


@pytest.mark.parametrize("prefix", ["e2e-", "kt-", "stack2.", "a"])
def test_a_usable_prefix_passes(prefix: str) -> None:
    assert container_prefix.validate(prefix) == prefix
