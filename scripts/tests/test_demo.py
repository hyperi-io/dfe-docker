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


class GeneratorTagCase(TypedDict):
    """An image pin and the git tag read from it, empty when it names none."""

    expected: str
    pin: str


class ListeningProtocolsCase(TypedDict):
    """The lines of a /proc/net socket table and the protocols listening on 514 in it."""

    expected: set[str]
    lines: list[str]


class LoadDemoRefusalCase(TypedDict):
    """A demo file the loader refuses and what the refusal says."""

    message: str
    text: str


class RefusalReasonCase(TypedDict):
    """The per-source apps a profile runs, whether its tier renders and what the refusal says."""

    message: str
    per_source_apps: list[str]
    renders: bool


class RunnableCase(TypedDict):
    """The instances still declared, the services the engine hints and the ones `make apply` can create."""

    declared: set[str]
    expected: list[str]
    hinted: list[str]


class SamplesDirPackageCase(TypedDict):
    """One committed sample package, the pattern that reads each line's kind and the prefix every line opens with."""

    kind: re.Pattern[str]
    package: str
    prefix: str


ENSURE_TABLE_CASES = [
    EnsureTableCase(expected=["create", "deploy", "dormant"], held=None),
    EnsureTableCase(expected=["deploy", "dormant"], held={"state": "active"}),
    EnsureTableCase(
        expected=["dormant"], held={"deployed_version": "1", "state": "active"}
    ),
    EnsureTableCase(expected=[], held={"deployed_version": "1", "state": "dormant"}),
]
GENERATOR_TAG_CASES = [
    GeneratorTagCase(expected="v1.1.18", pin="v1.1.18@sha256:" + "a" * 64),
    GeneratorTagCase(expected="v1.2.0", pin="'v1.2.0'"),
    GeneratorTagCase(expected="v1.2.0-rc.1", pin="v1.2.0-rc.1"),
    GeneratorTagCase(expected="", pin=""),
    GeneratorTagCase(expected="", pin="@sha256:" + "a" * 64),
    GeneratorTagCase(expected="", pin="v1;rm -rf /"),
    GeneratorTagCase(expected="", pin="--upload-pack=x"),
]
LISTENING_PROTOCOLS_CASES = [
    ListeningProtocolsCase(expected={"tcp", "udp"}, lines=[TCP_LISTEN_514, UDP_514]),
    ListeningProtocolsCase(expected={"tcp", "udp"}, lines=[TCP6_LISTEN_514, UDP_514]),
    ListeningProtocolsCase(expected={"udp"}, lines=[UDP_514]),
    ListeningProtocolsCase(expected=set(), lines=[TCP_ESTABLISHED_514]),
    ListeningProtocolsCase(expected=set(), lines=[TCP_LISTEN_5140]),
    ListeningProtocolsCase(expected=set(), lines=[]),
    ListeningProtocolsCase(
        expected=set(), lines=["sl  local_address rem_address   st"]
    ),
]
LOAD_DEMO_REFUSAL_CASES = [
    LoadDemoRefusalCase(message="needs an `intake`", text="sources: [a]\n"),
    LoadDemoRefusalCase(
        message="non-empty `sources`",
        text="intake: {source: x, transform: {engine: elastic}}\n",
    ),
    LoadDemoRefusalCase(
        message="non-empty `sources`",
        text="intake: {source: x, transform: {engine: elastic}}\nsources: []\n",
    ),
    LoadDemoRefusalCase(
        message="non-empty `sources`",
        text="intake: {source: x, transform: {engine: elastic}}\nsources: ['']\n",
    ),
    LoadDemoRefusalCase(
        message="needs a `transform`", text="intake: {source: x}\nsources: [a]\n"
    ),
    LoadDemoRefusalCase(message="not readable YAML", text="intake: [\n"),
    LoadDemoRefusalCase(message="needs an `intake`", text="- a\n"),
]
REFUSAL_REASON_CASES = [
    RefusalReasonCase(
        message="runs no dfe-transform-elastic", per_source_apps=[], renders=True
    ),
    RefusalReasonCase(
        message="committed config",
        per_source_apps=["dfe-transform-elastic"],
        renders=False,
    ),
]
RUNNABLE_CASES = [
    RunnableCase(
        declared={"dfe-transform-elastic-demo-syslog"},
        expected=["dfe-transform-elastic-demo-syslog", "dfe-receiver"],
        hinted=[
            "dfe-transform-elastic-cisco-asa",
            "dfe-transform-elastic-demo-syslog",
            "dfe-receiver",
        ],
    ),
    RunnableCase(declared=set(), expected=["dfe-receiver"], hinted=["dfe-receiver"]),
    RunnableCase(declared={"dfe-fetcher-okta"}, expected=[], hinted=[]),
]
SAMPLES_DIR_PACKAGE_CASES = [
    SamplesDirPackageCase(
        kind=re.compile(r"^%ASA-\d-(\d{6}):"), package="cisco_asa", prefix="%ASA-"
    ),
    SamplesDirPackageCase(
        kind=re.compile(r'action="(\w+)"'), package="fortinet", prefix="date="
    ),
]


class TestSamplesDir:
    @pytest.mark.parametrize("case", SAMPLES_DIR_PACKAGE_CASES)
    def test_the_committed_samples_are_varied_lines_on_private_and_documentation_addresses(
        self, case: SamplesDirPackageCase
    ) -> None:
        lines = (
            (demo.SAMPLES_DIR / case["package"] / "demo.log")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        kinds = {
            found.group(1)
            for line in lines
            if (found := case["kind"].search(string=line))
        }
        addresses = set(
            re.findall(pattern=r"\d+\.\d+\.\d+\.\d+", string="\n".join(lines))
        )

        assert len(lines) >= 5000
        assert len(set(lines)) >= 0.95 * len(lines)
        assert all(line.startswith(case["prefix"]) for line in lines)
        assert len(kinds) >= 4
        assert len(addresses) >= 300
        assert all(
            address.startswith(
                ("10.", "192.168.", "192.0.2.", "198.51.100.", "203.0.113.")
            )
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


class TestGeneratorCommand:
    def test_streams_the_generator_detached_and_read_only_on_the_stack_network(
        self,
    ) -> None:
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


class TestGeneratorTag:
    @pytest.mark.parametrize("case", GENERATOR_TAG_CASES)
    def test_matches_expected(self, case: GeneratorTagCase) -> None:
        assert demo.generator_tag(pin=case["pin"]) == case["expected"]


class TestListeningProtocols:
    @pytest.mark.parametrize("case", LISTENING_PROTOCOLS_CASES)
    def test_matches_expected(self, case: ListeningProtocolsCase) -> None:
        protocols = demo.listening_protocols(
            port=514, proc_net="\n".join(case["lines"])
        )

        assert protocols == case["expected"]


class TestLoadDemo:
    def test_the_committed_demo_names_an_agentless_intake_and_valid_tables(
        self,
    ) -> None:
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
            assert SOURCE_NAME.fullmatch(string=name), name

    @pytest.mark.parametrize("case", LOAD_DEMO_REFUSAL_CASES)
    def test_refuses_a_demo_file_it_cannot_run(
        self, tmp_path: Path, case: LoadDemoRefusalCase
    ) -> None:
        path = tmp_path / "demo.yaml"
        path.write_text(data=case["text"], encoding="utf-8")

        with pytest.raises(demo.DemoError, match=case["message"]):
            demo.load_demo(path=path)


class TestRefusal:
    def test_a_profile_that_runs_it_has_no_reason(self) -> None:
        reason = demo.refusal(per_source_apps=["dfe-transform-elastic"], renders=True)

        assert reason == ""

    @pytest.mark.parametrize("case", REFUSAL_REASON_CASES)
    def test_names_why_a_profile_cannot_run_it(self, case: RefusalReasonCase) -> None:
        reason = demo.refusal(
            per_source_apps=case["per_source_apps"], renders=case["renders"]
        )

        assert case["message"] in reason


class TestRunnable:
    @pytest.mark.parametrize("case", RUNNABLE_CASES)
    def test_matches_expected(self, case: RunnableCase) -> None:
        services = demo.runnable(declared=case["declared"], hinted=case["hinted"])

        assert services == case["expected"]


class TestStageFeed:
    def test_a_sample_set_replaces_its_package_fixtures_and_leaves_the_rest(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        fixtures = checkout / demo.FIXTURES
        for path, text in {
            "scripts/replay_syslog.py": "print()\n",
            "src/detect/labels.rs": "// labels\n",
            f"{demo.FIXTURES}/cisco_asa/upstream.log": "upstream asa\n",
            f"{demo.FIXTURES}/fortinet/fortigate.log": "upstream fortigate\n",
        }.items():
            (checkout / path).parent.mkdir(exist_ok=True, parents=True)
            (checkout / path).write_text(data=text, encoding="utf-8")
        samples = tmp_path / "samples"
        (samples / "cisco_asa").mkdir(parents=True)
        (samples / "cisco_asa" / "demo.log").write_text(
            data="%ASA-6-302013: x\n", encoding="utf-8"
        )
        feed = tmp_path / "feed"
        (feed / "stale").mkdir(parents=True)

        staged = demo.stage_feed(
            checkout=checkout,
            feed=feed,
            generator="scripts/replay_syslog.py",
            samples=samples,
        )

        asa = sorted(
            path.name for path in (staged / demo.FIXTURES / "cisco_asa").iterdir()
        )
        assert asa == ["demo.log"]
        assert (staged / demo.FIXTURES / "fortinet" / "fortigate.log").is_file()
        assert (staged / "scripts" / "replay_syslog.py").is_file()
        assert (staged / "src" / "detect" / "labels.rs").is_file()
        assert not ((staged / "stale").exists())
        assert (fixtures / "cisco_asa" / "upstream.log").is_file()


class TestWithCandidates:
    def test_sets_the_candidates_without_touching_the_committed_body(self) -> None:
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
