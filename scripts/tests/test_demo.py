#  Project:      dfe-docker
#  File:         tests/test_demo.py
#  Purpose:      Assert the demo's Sources hold together and how the demo reads its pins, the socket table and the engine's hints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on `make demo` that need no stack: its committed Sources and the pure steps between the engine, the pins and docker."""

from pathlib import Path

import pytest

import demo
from _common import SOURCE_NAME

TCP_LISTEN_514 = (
    "   0: 00000000:0202 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
)
UDP_514 = "  12: 00000000:0202 00000000:0000 07 00000000:00000000 00:00000000 00000000"
TCP_ESTABLISHED_514 = (
    "   1: AC120005:0202 AC120001:9C40 01 00000000:00000000 00:00000000 00000000"
)
TCP6_LISTEN_514 = "   0: 00000000000000000000000000000000:0202 00000000000000000000000000000000:0000 0A"
TCP_LISTEN_5140 = (
    "   2: 00000000:1414 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
)


def test_the_committed_demo_names_an_agentless_intake_and_valid_tables() -> None:
    loaded = demo.load_demo(path=demo.DEMO_FILE)

    assert loaded.intake["transform"] == {
        "engine": "elastic",
        "mode": "agentless",
        "detect": {"unmatched": "passthrough"},
    }
    assert loaded.intake["match"] == {
        "field": "_source",
        "operator": "equals",
        "value": "syslog",
    }
    assert loaded.sources == ["cisco-asa", "fortinet", "panw", "cisco-ios"]
    for name in [loaded.intake["source"], *loaded.sources]:
        assert SOURCE_NAME.fullmatch(name), name


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("sources: [a]\n", "needs an `intake`", id="no_intake"),
        pytest.param(
            "intake: {source: x, transform: {engine: elastic}}\n",
            "non-empty `sources`",
            id="no_sources",
        ),
        pytest.param(
            "intake: {source: x, transform: {engine: elastic}}\nsources: []\n",
            "non-empty `sources`",
            id="empty_sources",
        ),
        pytest.param(
            "intake: {source: x, transform: {engine: elastic}}\nsources: ['']\n",
            "non-empty `sources`",
            id="blank_source",
        ),
        pytest.param(
            "intake: {source: x}\nsources: [a]\n",
            "needs a `transform`",
            id="no_transform",
        ),
        pytest.param("intake: [\n", "not readable YAML", id="malformed"),
        pytest.param("- a\n", "needs an `intake`", id="not_a_mapping"),
    ],
)
def test_refuses_a_demo_file_it_cannot_run(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(demo.DemoError, match=message):
        demo.load_demo(path=path)


def test_sets_the_candidates_without_touching_the_committed_body() -> None:
    intake = {
        "source": "x",
        "transform": {"engine": "elastic", "detect": {"unmatched": "passthrough"}},
    }

    body = demo.with_candidates(intake=intake, variants=["filebeat.panw.default"])

    assert body["transform"]["detect"] == {
        "unmatched": "passthrough",
        "candidates": ["filebeat.panw.default"],
    }
    assert "candidates" not in intake["transform"]["detect"]


@pytest.mark.parametrize(
    ("pin", "tag"),
    [
        pytest.param("v1.1.18@sha256:" + "a" * 64, "v1.1.18", id="tag_and_digest"),
        pytest.param("'v1.2.0'", "v1.2.0", id="quoted_tag"),
        pytest.param("v1.2.0-rc.1", "v1.2.0-rc.1", id="prerelease"),
        pytest.param("", "", id="unset"),
        pytest.param("@sha256:" + "a" * 64, "", id="digest_only"),
        pytest.param("v1;rm -rf /", "", id="shell_metacharacters"),
        pytest.param("--upload-pack=x", "", id="option_shaped"),
    ],
)
def test_reads_the_git_tag_from_an_image_pin(pin: str, tag: str) -> None:
    assert demo.generator_tag(pin=pin) == tag


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param([TCP_LISTEN_514, UDP_514], {"tcp", "udp"}, id="both"),
        pytest.param([TCP6_LISTEN_514, UDP_514], {"tcp", "udp"}, id="tcp_over_ipv6"),
        pytest.param([UDP_514], {"udp"}, id="udp_only"),
        pytest.param([TCP_ESTABLISHED_514], set(), id="connected_not_listening"),
        pytest.param([TCP_LISTEN_5140], set(), id="other_port"),
        pytest.param([], set(), id="no_sockets"),
        pytest.param(["sl  local_address rem_address   st"], set(), id="header_only"),
    ],
)
def test_reads_the_listeners_from_the_socket_table(
    lines: list[str], expected: set[str]
) -> None:
    assert demo.listening_protocols(port=514, proc_net="\n".join(lines)) == expected


@pytest.mark.parametrize(
    ("hinted", "declared", "expected"),
    [
        pytest.param(
            [
                "dfe-transform-elastic-cisco-asa",
                "dfe-transform-elastic-demo-syslog",
                "dfe-receiver",
            ],
            {"dfe-transform-elastic-demo-syslog"},
            ["dfe-transform-elastic-demo-syslog", "dfe-receiver"],
            id="a_dormant_source_instance_is_dropped",
        ),
        pytest.param(["dfe-receiver"], set(), ["dfe-receiver"], id="resident_kept"),
        pytest.param([], {"dfe-fetcher-okta"}, [], id="nothing_hinted"),
    ],
)
def test_applies_only_the_hinted_services_that_still_exist(
    hinted: list[str], declared: set[str], expected: list[str]
) -> None:
    assert demo.runnable(declared=declared, hinted=hinted) == expected


@pytest.mark.parametrize(
    ("apps", "renders", "message"),
    [
        pytest.param(["dfe-transform-elastic"], True, "", id="single"),
        pytest.param([], True, "runs no dfe-transform-elastic", id="slim"),
        pytest.param(
            ["dfe-transform-elastic"], False, "committed config", id="core_off"
        ),
    ],
)
def test_refuses_a_profile_that_cannot_run_it(
    apps: list[str], renders: bool, message: str
) -> None:
    reason = demo.refusal(per_source_apps=apps, renders=renders)

    assert (message in reason) if message else (reason == "")


def test_runs_the_generator_read_only_on_the_stack_network() -> None:
    command = demo.generator_command(
        checkout=Path("/src/dfe-transform-elastic"),
        duration=30,
        generator="scripts/replay_syslog.py",
        image="python:3@sha256:" + "b" * 64,
        network="make-demo_default",
        protocol="tcp",
        rate=2.5,
        sources=["cisco-asa", "panw"],
    )

    assert command[:4] == ["docker", "run", "--rm", "--init"]
    assert command[command.index("--network") + 1] == "make-demo_default"
    assert (
        command[command.index("--volume") + 1] == "/src/dfe-transform-elastic:/demo:ro"
    )
    assert command[command.index("-I") + 1] == "/demo/scripts/replay_syslog.py"
    assert command[command.index("--target") + 1] == "tcp://dfe-receiver:514"
    assert command[command.index("--sources") + 1] == "cisco-asa,panw"
    assert command[command.index("--rate") + 1] == "2.5"
    assert command[command.index("--duration") + 1] == "30"
    assert "--loop" in command
