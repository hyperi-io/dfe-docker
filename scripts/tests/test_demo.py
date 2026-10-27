#  Project:      dfe-docker
#  File:         tests/test_demo.py
#  Purpose:      Assert the demo's Sources hold together and how the demo reads its pins, the socket table and the engine's hints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on `make demo` that need no stack: its committed Sources and the pure steps between the engine, the pins and docker."""

import re
from pathlib import Path
from typing import TypedDict

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


class EnsureTableCase(TypedDict):
    """The vendor source the engine holds, if any, and the steps that leave it deployed and dormant."""

    expected: list[str]
    held: dict | None


ENSURE_TABLE_CASES = [
    EnsureTableCase(expected=["create", "deploy", "dormant"], held=None),
    EnsureTableCase(expected=["deploy", "dormant"], held={"state": "active"}),
    EnsureTableCase(
        expected=["dormant"], held={"deployed_version": "1", "state": "active"}
    ),
    EnsureTableCase(expected=[], held={"deployed_version": "1", "state": "dormant"}),
]


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
    assert loaded.sources == ["cisco-asa", "fortinet"]
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


def test_streams_the_generator_detached_and_read_only_on_the_stack_network() -> None:
    command = demo.generator_command(
        container="make-demo-demo-syslog-stream",
        feed=Path("/src/feed"),
        generator="scripts/replay_syslog.py",
        image="python:3@sha256:" + "b" * 64,
        network="make-demo_default",
        protocol="tcp",
        rate=2.5,
        sources=["cisco-asa", "fortinet"],
    )

    assert command[:5] == ["docker", "run", "--detach", "--rm", "--init"]
    assert command[command.index("--name") + 1] == "make-demo-demo-syslog-stream"
    assert command[command.index("--network") + 1] == "make-demo_default"
    assert command[command.index("--volume") + 1] == "/src/feed:/demo:ro"
    assert command[command.index("-I") + 1] == "/demo/scripts/replay_syslog.py"
    assert command[command.index("--target") + 1] == "tcp://dfe-receiver:514"
    assert command[command.index("--sources") + 1] == "cisco-asa,fortinet"
    assert command[command.index("--rate") + 1] == "2.5"
    assert command[command.index("--duration") + 1] == str(demo.STREAM_SECONDS)
    assert "--loop" in command


def test_a_sample_set_replaces_its_package_fixtures_and_leaves_the_rest(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    fixtures = checkout / demo.FIXTURES
    for path, text in {
        "scripts/replay_syslog.py": "print()\n",
        "src/detect/labels.rs": "// labels\n",
        f"{demo.FIXTURES}/cisco_asa/upstream.log": "upstream asa\n",
        f"{demo.FIXTURES}/fortinet/fortigate.log": "upstream fortigate\n",
    }.items():
        (checkout / path).parent.mkdir(parents=True, exist_ok=True)
        (checkout / path).write_text(text, encoding="utf-8")
    samples = tmp_path / "samples"
    (samples / "cisco_asa").mkdir(parents=True)
    (samples / "cisco_asa" / "demo.log").write_text(
        "%ASA-6-302013: x\n", encoding="utf-8"
    )
    feed = tmp_path / "feed"
    (feed / "stale").mkdir(parents=True)

    staged = demo.stage_feed(
        checkout=checkout,
        feed=feed,
        generator="scripts/replay_syslog.py",
        samples=samples,
    )

    asa = sorted(path.name for path in (staged / demo.FIXTURES / "cisco_asa").iterdir())
    assert asa == ["demo.log"]
    assert (staged / demo.FIXTURES / "fortinet" / "fortigate.log").is_file()
    assert (staged / "scripts" / "replay_syslog.py").is_file()
    assert (staged / "src" / "detect" / "labels.rs").is_file()
    assert not (staged / "stale").exists()
    assert (fixtures / "cisco_asa" / "upstream.log").is_file()


@pytest.mark.parametrize(
    ("package", "prefix", "kind"),
    [
        pytest.param("cisco_asa", "%ASA-", re.compile(r"^%ASA-\d-(\d{6}):"), id="asa"),
        pytest.param("fortinet", "date=", re.compile(r'action="(\w+)"'), id="fortinet"),
    ],
)
def test_the_committed_samples_are_varied_lines_on_documentation_addresses(
    package: str, prefix: str, kind: re.Pattern[str]
) -> None:
    lines = (
        (demo.SAMPLES_DIR / package / "demo.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    kinds = {found.group(1) for line in lines if (found := kind.search(line))}
    addresses = set(re.findall(r"\d+\.\d+\.\d+\.\d+", "\n".join(lines)))

    assert len(lines) >= 5000
    assert len(set(lines)) >= 0.95 * len(lines)
    assert all(line.startswith(prefix) for line in lines)
    assert len(kinds) >= 4
    assert len(addresses) >= 300
    assert all(
        address.startswith(("10.", "192.168.", "192.0.2.", "198.51.100.", "203.0.113."))
        for address in addresses
    ), sorted(addresses)


class TestEnsureTable:
    @pytest.mark.parametrize("case", ENSURE_TABLE_CASES)
    def test_matches_expected(
        self, monkeypatch: pytest.MonkeyPatch, case: EnsureTableCase
    ) -> None:
        def fake_create(*, payload: dict, token: str, url: str) -> tuple[int, dict]:
            steps.append("create")
            return 201, {"created": [{"entry": "cisco-asa"}]}

        def fake_deploy(*, base: str, name: str, token: str) -> dict:
            steps.append("deploy")
            return {}

        def fake_set_dormant(*, base: str, label: str, token: str) -> dict:
            steps.append("dormant")
            return {}

        def fake_stored(*, base: str, name: str, token: str) -> dict | None:
            return case["held"]

        steps = []
        monkeypatch.setattr(demo, "_api_post_json", fake_create)
        monkeypatch.setattr(demo, "_deploy", fake_deploy)
        monkeypatch.setattr(demo, "_set_dormant", fake_set_dormant)
        monkeypatch.setattr(demo, "_stored", fake_stored)

        demo._ensure_table(base="http://engine", label="cisco-asa", token="token")

        assert steps == case["expected"]
