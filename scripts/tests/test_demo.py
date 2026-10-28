#  Project:      dfe-docker
#  File:         tests/test_demo.py
#  Purpose:      Assert the demo's Sources and events hold together and how the demo reads its pins, the socket table, the catalogue and the engine's records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on `make demo` that need no stack: its committed Sources and events and the pure steps between the engine, the pins and docker."""

import re
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

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
ASA_VARIANT = "filebeat.cisco_asa.default"
FORTINET_VARIANT = "filebeat.fortinet.default"
# The cisco-asa catalogue entry as dfe-schemas ships it
CATALOGUE_ENTRY = {
    "catalogue": {
        "data_stream": "log",
        "framing": "line",
        "intakes": ["beats", "receiver"],
        "package": "cisco_asa",
        "variants": [ASA_VARIANT],
    },
    "description": "The log data stream of the Elastic cisco_asa integration.",
    "display_name": "cisco_asa.log",
    "match": {"field": "_source", "operator": "equals", "value": "cisco-asa"},
    "schema": {"meta_schema": "meta/elastic/ecs"},
    "source": "cisco-asa",
    "transform": {"engine": "elastic", "variant": ASA_VARIANT},
}
# That entry as `from-catalogue` writes it on the receiver intake, set dormant
CATALOGUE_SOURCE = {
    "description": "The log data stream of the Elastic cisco_asa integration.",
    "display_name": "cisco_asa.log",
    "from_catalogue_intake": "receiver",
    "from_catalogue_item": "cisco-asa",
    "match": {"field": "_source", "operator": "equals", "value": "cisco-asa"},
    "schema": {"meta_schema": "meta/elastic/ecs"},
    "source": "cisco-asa",
    "state": "dormant",
    "transform": {"engine": "elastic", "variant": ASA_VARIANT},
}
FETCHER = {
    "fetcher": {
        "config": {"interval_secs": 60},
        "source_type": "crates_io",
        "topic": "own",
    },
    "source": "registry",
}
INTAKE = {
    "source": "intake",
    "transform": {
        "detect": {"candidates": [ASA_VARIANT]},
        "engine": "elastic",
        "mode": "agentless",
    },
}
VENDOR = {
    "source": "cisco-asa",
    "state": "dormant",
    "transform": {"engine": "elastic", "variant": ASA_VARIANT},
}
EVENTS = {"cisco-asa": ["demo.log"]}
# GET /sources/registry as the engine answers it, the fetcher deployed and active
HELD_FETCHER = {
    "archive": False,
    "current": "1.0.0",
    "deployed_version": "1.0.0",
    "enabled": True,
    "from_catalogue_intake": None,
    "from_catalogue_item": None,
    "match": None,
    "origin": "fetcher",
    "source": "registry",
    "state": "active",
    "transform": None,
    "versions": {
        "1.0.0": {
            "aliases": {},
            "archive": False,
            "fetcher": {
                "config": {"interval_secs": 60},
                "org": None,
                "routes": [],
                "source_type": "crates_io",
                "topic": "own",
            },
            "origin": "fetcher",
            "source_deployment": {"applied": True},
        }
    },
}
# GET /sources/cisco-asa as the engine answers it, the vendor deployed and dormant
HELD_VENDOR = {
    "archive": False,
    "current": "1.0.0",
    "deployed_version": "1.0.0",
    "enabled": False,
    "match": None,
    "source": "cisco-asa",
    "state": "dormant",
    "transform": {"engine": "elastic", "mode": None, "variant": ASA_VARIANT},
    "versions": {
        "1.0.0": {
            "aliases": {},
            "transform": {"engine": "elastic", "mode": None, "variant": ASA_VARIANT},
            "views": [],
        }
    },
}
# A held vendor whose working version 2.0.0 matches its file while the top-level copy is still the deployed 1.0.0's
HELD_VENDOR_DRAFT = {
    **HELD_VENDOR,
    "current": "2.0.0",
    "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.v1"},
    "versions": {
        "1.0.0": {
            "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.v1"}
        },
        "2.0.0": {"transform": {"engine": "elastic", "variant": ASA_VARIANT}},
    },
}


class CatalogueDriftCase(TypedDict):
    """A catalogue entry, a source file and the top-level keys where the file is not what `from-catalogue` writes."""

    catalogue: dict
    expected: list[str]
    source: dict


class EnsureStepsCase(TypedDict):
    """A source file, the engine's record of it if any and the calls that bring the source to the file."""

    body: dict
    expected: list[str]
    held: dict | None


class EventsDirSourceCase(TypedDict):
    """One committed events directory, the pattern that reads each line's kind and the prefix every line opens with."""

    kind: re.Pattern[str]
    prefix: str
    source: str


class FetcherWarningsCase(TypedDict):
    """The rows each fetcher table holds, the fetcher files and a fragment of each WARNING line they give."""

    counts: dict[str, int | None]
    expected: list[str]
    fetchers: list[dict]


class FixturePackageCase(TypedDict):
    """A vendor source's transform variant and the replayer fixture package it names."""

    expected: str
    variant: str


class FixturePackageRefusalCase(TypedDict):
    """A vendor source the fixture package cannot be read from and what the refusal says."""

    message: str
    source: dict


class GeneratorTagCase(TypedDict):
    """An image pin and the git tag read from it, empty when it names none."""

    expected: str
    pin: str


class HeldDriftCase(TypedDict):
    """A source file, the engine's record of it and the top-level keys where the record holds otherwise."""

    body: dict
    expected: list[str]
    held: dict


class ListeningProtocolsCase(TypedDict):
    """The lines of a /proc/net socket table and the protocols listening on 514 in it."""

    expected: set[str]
    lines: list[str]


class LoadDemoRefusalCase(TypedDict):
    """Demo source files and events directories the loader refuses and what the refusal says."""

    events: dict[str, list[str]]
    message: str
    sources: dict[str, dict | str]


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


CATALOGUE_DRIFT_CASES = [
    CatalogueDriftCase(catalogue=CATALOGUE_ENTRY, expected=[], source=CATALOGUE_SOURCE),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=[],
        source=demo.create_body(body=CATALOGUE_SOURCE),
    ),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=[],
        source={**CATALOGUE_SOURCE, "archive": False, "transport": None},
    ),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=["archive"],
        source={**CATALOGUE_SOURCE, "archive": True},
    ),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=["description"],
        source={**CATALOGUE_SOURCE, "description": "Edited by hand"},
    ),
    CatalogueDriftCase(
        catalogue={
            **CATALOGUE_ENTRY,
            "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.v2"},
        },
        expected=["transform"],
        source=CATALOGUE_SOURCE,
    ),
    CatalogueDriftCase(
        catalogue={
            **CATALOGUE_ENTRY,
            "match": {
                "field": "data_stream.dataset",
                "operator": "equals",
                "value": "cisco_asa.log",
            },
        },
        expected=[],
        source=CATALOGUE_SOURCE,
    ),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=["from_catalogue_intake", "from_catalogue_item", "match"],
        source={
            key: value
            for key, value in CATALOGUE_SOURCE.items()
            if not (key.startswith("from_catalogue")) and key != "match"
        },
    ),
    CatalogueDriftCase(
        catalogue=CATALOGUE_ENTRY,
        expected=["source"],
        source={**CATALOGUE_SOURCE, "source": "edge-firewall"},
    ),
]
ENSURE_STEPS_CASES = [
    EnsureStepsCase(body=VENDOR, expected=["create", "deploy", "state"], held=None),
    EnsureStepsCase(
        body=VENDOR,
        expected=["deploy", "state"],
        held={**HELD_VENDOR, "deployed_version": None, "state": "active"},
    ),
    EnsureStepsCase(
        body=VENDOR, expected=["state"], held={**HELD_VENDOR, "state": "active"}
    ),
    EnsureStepsCase(
        body=VENDOR, expected=["state"], held={**HELD_VENDOR, "state": "disabled"}
    ),
    EnsureStepsCase(body=VENDOR, expected=[], held=HELD_VENDOR),
    EnsureStepsCase(
        body={
            **VENDOR,
            "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.v2"},
        },
        expected=["update", "deploy"],
        held=HELD_VENDOR,
    ),
    EnsureStepsCase(body=VENDOR, expected=["deploy"], held=HELD_VENDOR_DRAFT),
    EnsureStepsCase(body=VENDOR, expected=["update", "deploy", "state"], held={}),
    EnsureStepsCase(body=INTAKE, expected=["create", "deploy"], held=None),
    EnsureStepsCase(body=FETCHER, expected=["create", "deploy"], held=None),
    EnsureStepsCase(body=FETCHER, expected=[], held=HELD_FETCHER),
    EnsureStepsCase(
        body={
            **FETCHER,
            "fetcher": {**FETCHER["fetcher"], "config": {"interval_secs": 300}},
        },
        expected=["update", "deploy"],
        held=HELD_FETCHER,
    ),
    EnsureStepsCase(
        body=FETCHER, expected=["state"], held={**HELD_FETCHER, "state": "dormant"}
    ),
]
EVENTS_DIR_SOURCE_CASES = [
    EventsDirSourceCase(
        kind=re.compile(r"^%ASA-\d-(\d{6}):"), prefix="%ASA-", source="cisco-asa"
    ),
    EventsDirSourceCase(
        kind=re.compile(r'action="(\w+)"'), prefix="date=", source="fortinet"
    ),
]
FETCHER_WARNINGS_CASES = [
    FetcherWarningsCase(counts={"registry": 5}, expected=[], fetchers=[FETCHER]),
    FetcherWarningsCase(
        counts={"registry": 0},
        expected=["no rows in 'dfe.registry' yet. A fetcher's first poll waits 60s"],
        fetchers=[FETCHER],
    ),
    FetcherWarningsCase(
        counts={"registry": 0},
        expected=["first poll waits one interval"],
        fetchers=[{"fetcher": {"source_type": "crates_io"}, "source": "registry"}],
    ),
    FetcherWarningsCase(
        counts={"registry": None},
        expected=["'dfe.registry' cannot be read"],
        fetchers=[FETCHER],
    ),
    FetcherWarningsCase(
        counts={}, expected=["'dfe.registry' cannot be read"], fetchers=[FETCHER]
    ),
    FetcherWarningsCase(
        counts={"registry": 3, "releases": None},
        expected=["'dfe.releases' cannot be read"],
        fetchers=[FETCHER, {**FETCHER, "source": "releases"}],
    ),
]
FIXTURE_PACKAGE_CASES = [
    FixturePackageCase(expected="cisco_asa", variant=ASA_VARIANT),
    FixturePackageCase(expected="fortinet", variant=FORTINET_VARIANT),
]
FIXTURE_PACKAGE_REFUSAL_CASES = [
    FixturePackageRefusalCase(
        message="needs a `transform.variant`", source={"source": "x"}
    ),
    FixturePackageRefusalCase(
        message="needs a `transform.variant`",
        source={"source": "x", "transform": {"engine": "elastic", "variant": ""}},
    ),
    FixturePackageRefusalCase(
        message="not <shipper>.<package>.<name>",
        source={"source": "x", "transform": {"variant": "cisco_asa"}},
    ),
    FixturePackageRefusalCase(
        message="not <shipper>.<package>.<name>",
        source={"source": "x", "transform": {"variant": "filebeat.cisco_asa"}},
    ),
    FixturePackageRefusalCase(
        message="not <shipper>.<package>.<name>",
        source={"source": "x", "transform": {"variant": "filebeat..default"}},
    ),
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
HELD_DRIFT_CASES = [
    HeldDriftCase(body=FETCHER, expected=[], held=HELD_FETCHER),
    HeldDriftCase(
        body={
            **FETCHER,
            "fetcher": {**FETCHER["fetcher"], "config": {"interval_secs": 300}},
        },
        expected=["fetcher"],
        held=HELD_FETCHER,
    ),
    HeldDriftCase(body=VENDOR, expected=[], held=HELD_VENDOR),
    HeldDriftCase(body={**VENDOR, "state": "active"}, expected=[], held=HELD_VENDOR),
    HeldDriftCase(
        body={**VENDOR, "description": "Cisco ASA"},
        expected=["description"],
        held=HELD_VENDOR,
    ),
    HeldDriftCase(body=VENDOR, expected=["source", "transform"], held={}),
    HeldDriftCase(body=VENDOR, expected=[], held=HELD_VENDOR_DRAFT),
    HeldDriftCase(
        body={
            **VENDOR,
            "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.v1"},
        },
        expected=["transform"],
        held=HELD_VENDOR_DRAFT,
    ),
    HeldDriftCase(
        body=INTAKE,
        expected=["transform"],
        held={
            "current": "1.0.0",
            "source": "intake",
            "versions": {
                "1.0.0": {
                    "transform": {
                        "detect": {
                            "candidates": [ASA_VARIANT, FORTINET_VARIANT],
                            "unmatched": "passthrough",
                        },
                        "engine": "elastic",
                        "mode": "agentless",
                    }
                }
            },
        },
    ),
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
    LoadDemoRefusalCase(events={}, message="holds no source files", sources={}),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="not readable YAML",
        sources={"intake.yaml": "source: [\n"},
    ),
    LoadDemoRefusalCase(
        events=EVENTS, message="which is a mapping", sources={"intake.yaml": "- a\n"}
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="names source 'intake'",
        sources={"cisco-asa.yaml": VENDOR, "other.yaml": INTAKE},
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="exactly one intake",
        sources={"cisco-asa.yaml": VENDOR},
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="exactly one intake",
        sources={
            "cisco-asa.yaml": VENDOR,
            "intake.yaml": INTAKE,
            "second.yaml": {**INTAKE, "source": "second"},
        },
    ),
    LoadDemoRefusalCase(
        events={},
        message="no vendor source",
        sources={"intake.yaml": INTAKE, "registry.yaml": FETCHER},
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="needs a `transform.variant`",
        sources={
            "cisco-asa.yaml": {
                "source": "cisco-asa",
                "transform": {"engine": "elastic"},
            },
            "intake.yaml": INTAKE,
        },
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="'registry' needs a `transform.variant`",
        sources={
            "cisco-asa.yaml": VENDOR,
            "intake.yaml": INTAKE,
            "registry.yaml": {"fetcher": None, "source": "registry"},
        },
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="transform.detect.candidates",
        sources={
            "cisco-asa.yaml": VENDOR,
            "intake.yaml": {
                **INTAKE,
                "transform": {
                    "detect": {"candidates": [FORTINET_VARIANT]},
                    "mode": "agentless",
                },
            },
        },
    ),
    LoadDemoRefusalCase(
        events=EVENTS,
        message="transform.detect.candidates",
        sources={
            "cisco-asa.yaml": VENDOR,
            "intake.yaml": {**INTAKE, "transform": {"mode": "agentless"}},
        },
    ),
    LoadDemoRefusalCase(
        events={**EVENTS, "edge": ["demo.log"]},
        message="both replay over fixture package 'cisco_asa'",
        sources={
            "cisco-asa.yaml": VENDOR,
            "edge.yaml": {
                "source": "edge",
                "transform": {"variant": "filebeat.cisco_asa.edge"},
            },
            "intake.yaml": {
                **INTAKE,
                "transform": {
                    "detect": {"candidates": [ASA_VARIANT, "filebeat.cisco_asa.edge"]},
                    "mode": "agentless",
                },
            },
        },
    ),
    LoadDemoRefusalCase(
        events={},
        message="no events to replay",
        sources={"cisco-asa.yaml": VENDOR, "intake.yaml": INTAKE},
    ),
    LoadDemoRefusalCase(
        events={"cisco-asa": []},
        message="no events to replay",
        sources={"cisco-asa.yaml": VENDOR, "intake.yaml": INTAKE},
    ),
    LoadDemoRefusalCase(
        events={**EVENTS, "fortinet": ["demo.log"]},
        message="no vendor source of that name",
        sources={"cisco-asa.yaml": VENDOR, "intake.yaml": INTAKE},
    ),
]
REFUSAL_REASON_CASES = [
    RefusalReasonCase(
        message="runs no 'dfe-transform-elastic'",
        per_source_apps=["dfe-fetcher"],
        renders=True,
    ),
    RefusalReasonCase(
        message="committed config",
        per_source_apps=["dfe-fetcher", "dfe-transform-elastic"],
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


def _committed() -> demo.Demo:
    """The committed demo, as `make demo` loads it."""
    return demo.load_demo(events=demo.EVENTS_DIR, sources=demo.SOURCES_DIR)


def _unsorted(*, node: object, path: str) -> list[str]:
    """Every mapping under node whose keys are out of alphabetical order, named by its path."""
    found = []
    if isinstance(node, dict):
        keys = [str(key) for key in node]
        if keys != sorted(keys):
            found.append(f"{path}: {keys}")
        for key, value in node.items():
            found += _unsorted(node=value, path=f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _unsorted(node=value, path=f"{path}[{index}]")
    return found


def _write_demo(
    *, events: dict[str, list[str]], root: Path, sources: dict[str, dict | str]
) -> None:
    """Write each source file under root/sources and each events directory, with its files, under root/events."""
    (root / "sources").mkdir(parents=True)
    for name, body in sources.items():
        text = body if isinstance(body, str) else yaml.safe_dump(data=body)
        (root / "sources" / name).write_text(data=text, encoding="utf-8")
    for name, files in events.items():
        (root / "events" / name).mkdir(parents=True)
        for file in files:
            (root / "events" / name / file).write_text(
                data="%ASA-6-302013: x\n", encoding="utf-8"
            )


class TestEventsDir:
    def test_the_cases_cover_every_committed_vendor(self) -> None:
        sources = sorted(case["source"] for case in EVENTS_DIR_SOURCE_CASES)

        assert sources == sorted(_committed().events)

    @pytest.mark.parametrize("case", EVENTS_DIR_SOURCE_CASES)
    def test_the_committed_events_are_varied_lines_on_private_and_documentation_addresses(
        self, case: EventsDirSourceCase
    ) -> None:
        lines = (
            (demo.EVENTS_DIR / case["source"] / "demo.log")
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


class TestCatalogueDrift:
    @pytest.mark.parametrize("case", CATALOGUE_DRIFT_CASES)
    def test_matches_expected(self, case: CatalogueDriftCase) -> None:
        drift = demo.catalogue_drift(catalogue=case["catalogue"], source=case["source"])

        assert drift == case["expected"]


class TestCreateBody:
    def test_drops_state_and_keeps_the_rest_without_touching_the_file(self) -> None:
        body = demo.create_body(body=VENDOR)

        assert body == {"source": "cisco-asa", "transform": VENDOR["transform"]}
        assert VENDOR["state"] == "dormant"


class TestEnsureSteps:
    @pytest.mark.parametrize("case", ENSURE_STEPS_CASES)
    def test_matches_expected(self, case: EnsureStepsCase) -> None:
        steps = demo.ensure_steps(body=case["body"], held=case["held"])

        assert steps == case["expected"]


class TestFetcherWarnings:
    @pytest.mark.parametrize("case", FETCHER_WARNINGS_CASES)
    def test_matches_expected(self, case: FetcherWarningsCase) -> None:
        warnings = demo.fetcher_warnings(
            counts=case["counts"], fetchers=case["fetchers"]
        )

        assert len(warnings) == len(case["expected"])
        assert all(warning.startswith("WARNING ") for warning in warnings)
        assert all(
            fragment in warning
            for fragment, warning in zip(case["expected"], warnings, strict=True)
        )


class TestFixturePackage:
    @pytest.mark.parametrize("case", FIXTURE_PACKAGE_CASES)
    def test_matches_expected(self, case: FixturePackageCase) -> None:
        package = demo.fixture_package(
            source={"source": "x", "transform": {"variant": case["variant"]}}
        )

        assert package == case["expected"]

    @pytest.mark.parametrize("case", FIXTURE_PACKAGE_REFUSAL_CASES)
    def test_refuses_a_source_whose_variant_names_no_package(
        self, case: FixturePackageRefusalCase
    ) -> None:
        with pytest.raises(demo.DemoError, match=re.escape(pattern=case["message"])):
            demo.fixture_package(source=case["source"])


class TestGeneratorCommand:
    def test_streams_the_generator_detached_and_read_only_on_the_stack_network(
        self,
    ) -> None:
        image = "python:3@sha256:" + "b" * 64
        command = demo.generator_command(
            container="make-demo-demo-syslog-stream",
            feed=Path("/src/feed"),
            generator="scripts/replay_syslog.py",
            image=image,
            network="make-demo_default",
            protocol="tcp",
            rate=2.5,
            sources=["cisco-asa", "fortinet"],
        )

        assert command[:4] == ["docker", "run", "--detach", "--init"]
        assert "--rm" not in command
        assert command[command.index("--name") + 1] == "make-demo-demo-syslog-stream"
        assert command[command.index("--network") + 1] == "make-demo_default"
        assert command[command.index("--volume") + 1] == "/src/feed:/demo:ro"
        assert command[command.index("python3") - 1] == image
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


class TestHeldDrift:
    @pytest.mark.parametrize("case", HELD_DRIFT_CASES)
    def test_matches_expected(self, case: HeldDriftCase) -> None:
        drift = demo.held_drift(body=case["body"], held=case["held"])

        assert drift == case["expected"]


class TestListeningProtocols:
    @pytest.mark.parametrize("case", LISTENING_PROTOCOLS_CASES)
    def test_matches_expected(self, case: ListeningProtocolsCase) -> None:
        protocols = demo.listening_protocols(
            port=514, proc_net="\n".join(case["lines"])
        )

        assert protocols == case["expected"]


class TestLoadDemo:
    def test_sorts_each_source_into_its_role_and_pairs_each_vendor_with_its_events(
        self, tmp_path: Path
    ) -> None:
        _write_demo(
            events=EVENTS,
            root=tmp_path,
            sources={
                "cisco-asa.yaml": VENDOR,
                "intake.yaml": INTAKE,
                "registry.yaml": FETCHER,
            },
        )

        loaded = demo.load_demo(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )

        assert loaded.intake == INTAKE
        assert loaded.vendors == [VENDOR]
        assert loaded.fetchers == [FETCHER]
        assert loaded.events == {"cisco-asa": tmp_path / "events" / "cisco-asa"}

    def test_accepts_candidates_in_any_order(self, tmp_path: Path) -> None:
        fortinet = {"source": "fortinet", "transform": {"variant": FORTINET_VARIANT}}
        intake = {
            **INTAKE,
            "transform": {
                "detect": {"candidates": [FORTINET_VARIANT, ASA_VARIANT]},
                "mode": "agentless",
            },
        }
        _write_demo(
            events={**EVENTS, "fortinet": ["demo.log"]},
            root=tmp_path,
            sources={
                "cisco-asa.yaml": VENDOR,
                "fortinet.yaml": fortinet,
                "intake.yaml": intake,
            },
        )

        loaded = demo.load_demo(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )

        assert loaded.vendors == [VENDOR, fortinet]

    def test_a_fetcher_mapping_outranks_agentless_mode(self, tmp_path: Path) -> None:
        polled = {**FETCHER, "transform": {"mode": "agentless"}}
        _write_demo(
            events=EVENTS,
            root=tmp_path,
            sources={
                "cisco-asa.yaml": VENDOR,
                "intake.yaml": INTAKE,
                "registry.yaml": polled,
            },
        )

        loaded = demo.load_demo(
            events=tmp_path / "events", sources=tmp_path / "sources"
        )

        assert loaded.fetchers == [polled]
        assert loaded.intake == INTAKE

    def test_the_committed_intake_detects_agentless_and_passes_unmatched_lines_through(
        self,
    ) -> None:
        intake = _committed().intake

        assert intake["source"] == "demo-syslog"
        assert intake["match"] == {
            "field": "_source",
            "operator": "equals",
            "value": "syslog",
        }
        assert intake["transform"]["engine"] == "elastic"
        assert intake["transform"]["mode"] == "agentless"
        assert intake["transform"]["detect"]["unmatched"] == "passthrough"

    def test_the_committed_vendors_are_dormant_receiver_tables_of_their_own(
        self,
    ) -> None:
        vendors = _committed().vendors

        assert [body["source"] for body in vendors] == ["cisco-asa", "fortinet"]
        assert all(body["state"] == "dormant" for body in vendors)
        assert all(
            body["match"]
            == {"field": "_source", "operator": "equals", "value": body["source"]}
            for body in vendors
        )
        assert [demo.fixture_package(source=body) for body in vendors] == [
            "cisco_asa",
            "fortinet",
        ]

    def test_the_committed_fetchers_land_in_their_own_tables_inside_their_rate_limits(
        self,
    ) -> None:
        loaded = _committed()
        fetchers = {body["source"]: body["fetcher"] for body in loaded.fetchers}
        releases = fetchers["component-releases"]["config"]

        assert sorted(fetchers) == ["component-releases", "package-registry-crates-io"]
        assert all(fetcher["topic"] == "own" for fetcher in fetchers.values())
        # Unauthenticated GitHub allows 60 requests an hour per IP and each poll spends one per repo
        assert releases["interval_secs"] >= 60 * len(releases["vars"]["repos"])
        for name in [loaded.intake["source"], *loaded.events, *fetchers]:
            assert SOURCE_NAME.fullmatch(string=name), name

    def test_the_committed_sources_keep_their_keys_alphabetical(self) -> None:
        unsorted = []
        for path in sorted(demo.SOURCES_DIR.glob(pattern="*.yaml")):
            body = yaml.safe_load(stream=path.read_text(encoding="utf-8"))
            unsorted += _unsorted(node=body, path=path.name)

        assert unsorted == []

    @pytest.mark.parametrize("case", LOAD_DEMO_REFUSAL_CASES)
    def test_refuses_a_demo_it_cannot_run(
        self, tmp_path: Path, case: LoadDemoRefusalCase
    ) -> None:
        _write_demo(events=case["events"], root=tmp_path, sources=case["sources"])

        with pytest.raises(demo.DemoError, match=re.escape(pattern=case["message"])):
            demo.load_demo(events=tmp_path / "events", sources=tmp_path / "sources")


class TestRefusal:
    def test_a_profile_that_runs_the_transform_has_no_reason(self) -> None:
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
    def test_an_events_directory_replaces_its_package_fixtures_and_leaves_the_rest(
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
        events = tmp_path / "events" / "cisco-asa"
        events.mkdir(parents=True)
        (events / "demo.log").write_text(data="%ASA-6-302013: x\n", encoding="utf-8")
        feed = tmp_path / "feed"
        (feed / "stale").mkdir(parents=True)

        staged = demo.stage_feed(
            checkout=checkout,
            events={"cisco_asa": events},
            feed=feed,
            generator="scripts/replay_syslog.py",
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

    def test_refuses_events_for_a_package_the_checkout_ships_no_fixtures_for(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        for path in ("scripts/replay_syslog.py", f"{demo.FIXTURES}/cisco_asa/x.log"):
            (checkout / path).parent.mkdir(exist_ok=True, parents=True)
            (checkout / path).write_text(data="x\n", encoding="utf-8")
        (checkout / "src" / "detect").mkdir(parents=True)
        events = tmp_path / "events" / "fortinet"
        events.mkdir(parents=True)
        (events / "demo.log").write_text(data="date=x\n", encoding="utf-8")

        with pytest.raises(demo.DemoError, match="ships no"):
            demo.stage_feed(
                checkout=checkout,
                events={"fortinet": events},
                feed=tmp_path / "feed",
                generator="scripts/replay_syslog.py",
            )
