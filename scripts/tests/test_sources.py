#  Project:      dfe-docker
#  File:         tests/test_sources.py
#  Purpose:      Assert how the e2e suite reads a test's sources block and what it asks of dfe-engine, compose, ClickHouse and the broker for them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A test's `sources:` block names the sources the suite makes through dfe-engine. Everything the suite derives from it is checked here without a stack.

The engine names each source's instance `dfe-transform-<engine>-<source>`, binds it on the bus to `<source>_land` under a consumer group of the same name, then reads back the requests the suite sends it. The committed e2e-tests.yaml is read too, so a source test the suite would refuse fails here first.
"""

from pathlib import Path
from typing import TypedDict

import pytest
import yaml

import _pipeline
import _sources
import instances
import resolve_profile
from _common import COMPOSE_FILE, REPO_ROOT, SERVICE_PROFILES_FILE

E2E_TESTS_FILE = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"
# The ECS layers dfe-engine's elastic catalogue builds a filebeat.cisco_ios.default table on
ECS_SCHEMA = (
    ("additional_fields", "additional/cisco_ios/log"),
    ("derived_schema", "derived/cisco_ios/log"),
    ("meta_schema", "meta/elastic/ecs"),
)
ELASTIC = _sources.Source(
    engine="elastic",
    files=(),
    name="e2eelastic",
    schema=ECS_SCHEMA,
    variant="filebeat.cisco_ios.default",
)
PROGRAM = "tests/e2e/programs/stamp_transformed.vrl"
PROGRAM_TEXT = '._tags.e2e_transform = "vrl"\n'
SYSLOG_SEED = REPO_ROOT / "resources" / "seed" / "sources" / "syslog.yaml"
VECTOR = _sources.Source(engine="vector", files=(), name="e2evector")
VRL = _sources.Source(engine="vrl", files=(("transforms", PROGRAM),), name="e2evrl")
# What dfe-engine is told about the paths of its own tier, whichever profile the test runs
_ENGINE_PATHS = {
    "DFE_APP_CONTRACT_DIR": "/app/content/contract",
    "DFE_DEPLOYMENT_APP_CONFIG_BASE_DIR": "/app/app-config-base",
    "DFE_DEPLOYMENT_APP_CONFIG_DIR": "/app/app-config",
    "DFE_DEPLOYMENT_APP_CONFIG_MOUNT": "/etc/dfe/apps",
    "DFE_SOURCE_CATALOGUE_FILE": "/app/content/catalogue/sources.yaml",
}


class BindingsCase(TypedDict):
    """The sources a test makes and the group each instance commits as on its landing topic."""

    expected: dict[str, str]
    sources: list[_sources.Source]


class BindingsUnassignedCase(TypedDict):
    """The groups the broker says hold each topic, then the bindings still not joined."""

    assigned: dict[str, set[str]]
    expected: list[str]


class BindingsUncommittedCase(TypedDict):
    """The broker's lag by group and topic, then the bindings that have committed nothing."""

    expected: list[str]
    lag: dict[str, dict[str, int]]


class CreateRequestCase(TypedDict):
    """One source, then the schema and transform its `POST /sources` body carries."""

    schema: dict[str, str]
    source: _sources.Source
    transform: dict[str, str]


class EnabledAppsCase(TypedDict):
    """One committed profile and the apps it runs per source."""

    expected: list[str]
    profile: str


class EngineEnvironmentCase(TypedDict):
    """One profile, its transport and everything the engine is told about the tier."""

    expected: dict[str, str]
    profile: str
    transport: str


class EngineOverrideCase(TypedDict):
    """The settings a test hands dfe-engine, the configs it renders over and the override lines for them."""

    environment: dict[str, str]
    expected: str
    volumes: list[str]


class HintedServicesCase(TypedDict):
    """Engine replies and the services their restart hints name."""

    expected: list[str]
    replies: list[object]


class InstanceGroupCase(TypedDict):
    """A service an outage kills and the group it commits as when it is the vrl source's instance."""

    expected: str | None
    service: str


class KafkaBootstrapCase(TypedDict):
    """A test's transport and the broker its apps and dfe-engine are pointed at."""

    expected: str
    transport: str


class MergedTopicsCase(TypedDict):
    """The topics a test names, its transport and the topics it expects once the vrl source's join them."""

    expected: list[str]
    expected_topics: list[str]
    transport: str


class OutageProblemsCase(TypedDict):
    """The service an outage stops and what is wrong with it for the vrl source test."""

    expected: list[str]
    service: str


class ParseCase(TypedDict):
    """A sources block read from a project holding the stamp program, then the sources it gives."""

    expected: list[_sources.Source]
    raw: object


class ParseRefusalCase(TypedDict):
    """A sources block the suite refuses and the problem it names."""

    message: str
    raw: object


class RenderedConfigCase(TypedDict):
    """One service with its instance or none, then where it reads its rendered config."""

    expected: str
    instance: str
    service: str


class RestartPlanCase(TypedDict):
    """The services the engine's hints name, the services reading its render, a transport and what the suite does about each."""

    expected: _sources.RestartPlan
    hinted: list[str]
    rendered_services: list[str]
    transport: str


class SourceCase(TypedDict):
    """One transform engine and source name, then the app and instance they make."""

    app: str
    engine: str
    instance: str
    name: str


class StampCountsCase(TypedDict):
    """What one stamp query printed and the counts read off it."""

    expected: tuple[int, int] | None
    raw: str


class StampProblemsCase(TypedDict):
    """The counts a stamp query gave and what the check finds wrong with them."""

    counts: tuple[int, int] | None
    expected: list[str]


class StampingSourceCase(TypedDict):
    """The sources a test makes, its table and the source whose program stamps that table's rows."""

    expected: _sources.Source | None
    sources: list[_sources.Source]
    table: str


class TargetProblemsCase(TypedDict):
    """The labels a test sends records under and what is wrong with them for the vrl source test."""

    expected: list[str]
    outage_sources: dict[str, str]
    table: str


class UnindexedCase(TypedDict):
    """What dfe-engine's index lists and the instances still missing from it for the vrl source test."""

    expected: list[str]
    found: dict[str, list[str]]


BINDINGS_CASES = [
    BindingsCase(expected={}, sources=[]),
    BindingsCase(expected={"dfe-transform-vrl-e2evrl": "e2evrl_land"}, sources=[VRL]),
    BindingsCase(
        expected={
            "dfe-transform-elastic-e2eelastic": "e2eelastic_land",
            "dfe-transform-vector-e2evector": "e2evector_land",
            "dfe-transform-vrl-e2evrl": "e2evrl_land",
        },
        sources=[VRL, VECTOR, ELASTIC],
    ),
]
BINDINGS_UNASSIGNED_CASES = [
    BindingsUnassignedCase(
        assigned={"dfe-transform-vrl-e2evrl": {"e2evrl_land"}}, expected=[]
    ),
    BindingsUnassignedCase(
        assigned={},
        expected=["'e2evrl_land': group 'dfe-transform-vrl-e2evrl' has not joined"],
    ),
    BindingsUnassignedCase(
        assigned={"dfe-transform-vrl-e2evrl": set()},
        expected=["'e2evrl_land': group 'dfe-transform-vrl-e2evrl' has not joined"],
    ),
    BindingsUnassignedCase(
        assigned={
            "dfe-transform-vrl-e2evrl": {"main_land"},
            "other": {"e2evrl_land"},
        },
        expected=["'e2evrl_land': group 'dfe-transform-vrl-e2evrl' has not joined"],
    ),
]
BINDINGS_UNCOMMITTED_CASES = [
    BindingsUncommittedCase(
        expected=[], lag={"dfe-transform-vrl-e2evrl": {"e2evrl_land": 4}}
    ),
    BindingsUncommittedCase(
        expected=["'e2evrl_land': group 'dfe-transform-vrl-e2evrl' has not committed"],
        lag={"dfe-loader": {"e2evrl_land": 0}},
    ),
    BindingsUncommittedCase(
        expected=["'e2evrl_land': group 'dfe-transform-vrl-e2evrl' has not committed"],
        lag={"dfe-transform-vrl-e2evrl": {}},
    ),
]
CREATE_REQUEST_CASES = [
    CreateRequestCase(
        schema={"meta_schema": "meta/syslog", "meta_schema_version": "1.0.0"},
        source=VRL,
        transform={"engine": "vrl"},
    ),
    CreateRequestCase(
        schema={
            "additional_fields": "additional/cisco_ios/log",
            "derived_schema": "derived/cisco_ios/log",
            "meta_schema": "meta/elastic/ecs",
        },
        source=ELASTIC,
        transform={"engine": "elastic", "variant": "filebeat.cisco_ios.default"},
    ),
]
ENABLED_APPS_CASES = [
    EnabledAppsCase(expected=[], profile="slim-grpc"),
    EnabledAppsCase(expected=[], profile="slim-kafka"),
    EnabledAppsCase(
        expected=["dfe-fetcher", "dfe-transform-vector", "dfe-transform-vrl"],
        profile="single-grpc",
    ),
    EnabledAppsCase(
        expected=[
            "dfe-fetcher",
            "dfe-transform-elastic",
            "dfe-transform-vector",
            "dfe-transform-vrl",
        ],
        profile="single-kafka",
    ),
]
ENGINE_ENVIRONMENT_CASES = [
    EngineEnvironmentCase(
        expected={
            **_ENGINE_PATHS,
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "",
            "DFE_PROFILE": "docker-single",
            "DFE_TRANSPORT_BUS_PRESENT": "false",
            "DFE_TRANSPORT_DEFAULT": "direct",
        },
        profile="single-grpc",
        transport="grpc",
    ),
    EngineEnvironmentCase(
        expected={
            **_ENGINE_PATHS,
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
            "DFE_PROFILE": "docker-single",
            "DFE_TRANSPORT_BUS_PRESENT": "true",
            "DFE_TRANSPORT_DEFAULT": "bus",
        },
        profile="single-kafka",
        transport="kafka",
    ),
    EngineEnvironmentCase(
        expected={
            **_ENGINE_PATHS,
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "",
            "DFE_PROFILE": "docker-slim",
            "DFE_TRANSPORT_BUS_PRESENT": "false",
            "DFE_TRANSPORT_DEFAULT": "direct",
        },
        profile="slim-grpc",
        transport="grpc",
    ),
]
ENGINE_OVERRIDE_CASES = [
    EngineOverrideCase(environment={}, expected="", volumes=[]),
    EngineOverrideCase(
        environment={"DFE_TRANSPORT_DEFAULT": "bus", "DFE_HYPERDX_ENABLED": "true"},
        expected=(
            "  dfe-engine:\n"
            "    environment:\n"
            '      DFE_HYPERDX_ENABLED: "true"\n'
            '      DFE_TRANSPORT_DEFAULT: "bus"\n'
        ),
        volumes=[],
    ),
    EngineOverrideCase(
        environment={},
        expected=(
            "  dfe-engine:\n"
            "    volumes:\n"
            "      - ./config/loader/grpc.yaml:/app/app-config-base/dfe-loader.yaml:ro\n"
        ),
        volumes=["./config/loader/grpc.yaml:/app/app-config-base/dfe-loader.yaml:ro"],
    ),
]
HINTED_SERVICES_CASES = [
    HintedServicesCase(expected=[], replies=[]),
    HintedServicesCase(expected=[], replies=["not a reply", None, {}]),
    HintedServicesCase(
        expected=["dfe-receiver", "dfe-transform-vrl-e2evrl"],
        replies=[
            {
                "restart_required": [
                    "restart required: make apply SERVICES=dfe-receiver"
                ]
            },
            {
                "restart_required": [
                    "recreate required: make apply SERVICES=dfe-transform-vrl-e2evrl",
                    "restart required: make apply SERVICES=dfe-receiver",
                ]
            },
        ],
    ),
    HintedServicesCase(
        expected=[], replies=[{"restart_required": ["restart dfe-receiver by hand"]}]
    ),
    HintedServicesCase(expected=[], replies=[{"restart_required": None}]),
]
INSTANCE_GROUP_CASES = [
    InstanceGroupCase(
        expected="dfe-transform-vrl-e2evrl", service="dfe-transform-vrl-e2evrl"
    ),
    InstanceGroupCase(expected=None, service="dfe-loader"),
    InstanceGroupCase(expected=None, service="dfe-transform-vrl-other"),
]
KAFKA_BOOTSTRAP_CASES = [
    KafkaBootstrapCase(expected="", transport="grpc"),
    KafkaBootstrapCase(expected="kafka:9092", transport="kafka"),
]
MERGED_TOPICS_CASES = [
    MergedTopicsCase(
        expected=["main_land", "e2evrl_land", "e2evrl_load"],
        expected_topics=["main_land"],
        transport="kafka",
    ),
    MergedTopicsCase(
        expected=["e2evrl_land", "e2evrl_load"],
        expected_topics=["e2evrl_land"],
        transport="kafka",
    ),
    MergedTopicsCase(expected=[], expected_topics=[], transport="grpc"),
    MergedTopicsCase(
        expected=["main_land"], expected_topics=["main_land"], transport="grpc"
    ),
]
OUTAGE_PROBLEMS_CASES = [
    OutageProblemsCase(expected=[], service="dfe-loader"),
    OutageProblemsCase(expected=[], service="kafka"),
    OutageProblemsCase(expected=[], service="dfe-transform-vrl-e2evrl"),
    OutageProblemsCase(
        expected=[
            "outage stops 'dfe-transform-vrl-other', which is no instance of a source "
            "this test makes (it makes dfe-transform-vrl-e2evrl)"
        ],
        service="dfe-transform-vrl-other",
    ),
    OutageProblemsCase(
        expected=[
            "outage stops 'dfe-fetcher-e2evrl', which is no instance of a source this "
            "test makes (it makes dfe-transform-vrl-e2evrl)"
        ],
        service="dfe-fetcher-e2evrl",
    ),
]
PARSE_CASES = [
    ParseCase(expected=[], raw=None),
    ParseCase(expected=[], raw=[]),
    ParseCase(
        expected=[VRL],
        raw=[
            {
                "files": {"transforms": [PROGRAM]},
                "name": "e2evrl",
                "transform": {"engine": "vrl"},
            }
        ],
    ),
    ParseCase(
        expected=[VECTOR, ELASTIC],
        raw=[
            {"name": "e2evector", "transform": {"engine": "vector", "variant": None}},
            {
                "name": "e2eelastic",
                "schema": dict(ECS_SCHEMA),
                "transform": {
                    "engine": "elastic",
                    "variant": "filebeat.cisco_ios.default",
                },
            },
        ],
    ),
]
PARSE_REFUSAL_CASES = [
    ParseRefusalCase(message="'sources' must be a list", raw={"name": "e2evrl"}),
    ParseRefusalCase(message="sources[0] must be a mapping", raw=["e2evrl"]),
    ParseRefusalCase(
        message="source name 'e2e_vrl' must be lowercase letters and digits",
        raw=[{"name": "e2e_vrl", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source name 'e2e-vrl' must be lowercase letters and digits",
        raw=[{"name": "e2e-vrl", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source name 'E2evrl' must be lowercase letters and digits",
        raw=[{"name": "E2evrl", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source name 'e2evrl-' must be lowercase letters and digits",
        raw=[{"name": "e2evrl-", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message=f"source name '{'a' * 41}' must be lowercase letters and digits",
        raw=[{"name": "a" * 41, "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source name None must be lowercase letters and digits",
        raw=[{"transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' is declared twice",
        raw=[
            {"name": "e2evrl", "transform": {"engine": "vrl"}},
            {"name": "e2evrl", "transform": {"engine": "vector"}},
        ],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' has unknown key(s) program",
        raw=[{"name": "e2evrl", "program": "x", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' names no 'transform' with an 'engine'",
        raw=[{"name": "e2evrl"}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' transform engine 'wasm' is not one of elastic, vector, vrl",
        raw=[{"name": "e2evrl", "transform": {"engine": "wasm"}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' transform has unknown key(s) program",
        raw=[{"name": "e2evrl", "transform": {"engine": "vrl", "program": "x"}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' transform 'variant' must be a name",
        raw=[{"name": "e2evrl", "transform": {"engine": "vrl", "variant": 3}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' 'files' must map each file set to a list of fixtures",
        raw=[{"files": [PROGRAM], "name": "e2evrl", "transform": {"engine": "vrl"}}],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' file set 'transforms' lists no fixture",
        raw=[
            {
                "files": {"transforms": []},
                "name": "e2evrl",
                "transform": {"engine": "vrl"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' fixture 'config/receiver/grpc.yaml' is outside tests/e2e/",
        raw=[
            {
                "files": {"transforms": ["config/receiver/grpc.yaml"]},
                "name": "e2evrl",
                "transform": {"engine": "vrl"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' fixture 'tests/e2e/../../config/x.vrl' is outside tests/e2e/",
        raw=[
            {
                "files": {"transforms": ["tests/e2e/../../config/x.vrl"]},
                "name": "e2evrl",
                "transform": {"engine": "vrl"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2evrl' fixture 'tests/e2e/programs/absent.vrl' does not exist",
        raw=[
            {
                "files": {"transforms": ["tests/e2e/programs/absent.vrl"]},
                "name": "e2evrl",
                "transform": {"engine": "vrl"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2eelastic' 'schema' must map each schema setting to its value",
        raw=[
            {
                "name": "e2eelastic",
                "schema": "meta/elastic/ecs",
                "transform": {"engine": "elastic"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2eelastic' schema has unknown key(s) ttl",
        raw=[
            {
                "name": "e2eelastic",
                "schema": {"meta_schema": "meta/elastic/ecs", "ttl": "30"},
                "transform": {"engine": "elastic"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2eelastic' schema names no 'meta_schema'",
        raw=[
            {
                "name": "e2eelastic",
                "schema": {"derived_schema": "derived/cisco_ios/log"},
                "transform": {"engine": "elastic"},
            }
        ],
    ),
    ParseRefusalCase(
        message="source 'e2eelastic' schema 'meta_schema' must be a name",
        raw=[
            {
                "name": "e2eelastic",
                "schema": {"meta_schema": ""},
                "transform": {"engine": "elastic"},
            }
        ],
    ),
]
RENDERED_CONFIG_CASES = [
    RenderedConfigCase(
        expected="/etc/dfe/apps/dfe-receiver/config.yaml",
        instance="",
        service="dfe-receiver",
    ),
    RenderedConfigCase(
        expected="/etc/dfe/apps/dfe-loader/loader.yaml",
        instance="",
        service="dfe-loader",
    ),
    RenderedConfigCase(
        expected="/etc/dfe/apps/dfe-transform-vrl/e2evrl/config.yaml",
        instance="e2evrl",
        service="dfe-transform-vrl",
    ),
]
RESTART_PLAN_CASES = [
    RestartPlanCase(
        expected=_sources.RestartPlan(
            ignored=("dfe-loader",),
            problems=(),
            recreated=("dfe-receiver",),
            started=("dfe-transform-vrl-e2evrl",),
        ),
        hinted=["dfe-receiver", "dfe-transform-vrl-e2evrl", "dfe-loader"],
        rendered_services=["dfe-receiver"],
        transport="grpc",
    ),
    RestartPlanCase(
        expected=_sources.RestartPlan(
            ignored=(),
            problems=(
                "dfe-engine named no restart of dfe-receiver after making a direct "
                "source, so its records would skip the transform",
            ),
            recreated=(),
            started=("dfe-transform-vrl-e2evrl",),
        ),
        hinted=["dfe-transform-vrl-e2evrl"],
        rendered_services=["dfe-receiver"],
        transport="grpc",
    ),
    RestartPlanCase(
        expected=_sources.RestartPlan(
            ignored=(),
            problems=(),
            recreated=(),
            started=("dfe-transform-vrl-e2evrl",),
        ),
        hinted=[],
        rendered_services=["dfe-receiver"],
        transport="kafka",
    ),
    RestartPlanCase(
        expected=_sources.RestartPlan(
            ignored=("dfe-receiver",),
            problems=(),
            recreated=(),
            started=("dfe-transform-vrl-e2evrl",),
        ),
        hinted=["dfe-receiver"],
        rendered_services=[],
        transport="grpc",
    ),
]
SOURCE_CASES = [
    SourceCase(
        app="dfe-transform-vrl",
        engine="vrl",
        instance="dfe-transform-vrl-e2evrl",
        name="e2evrl",
    ),
    SourceCase(
        app="dfe-transform-vector",
        engine="vector",
        instance="dfe-transform-vector-e2evector",
        name="e2evector",
    ),
    SourceCase(
        app="dfe-transform-elastic",
        engine="elastic",
        instance="dfe-transform-elastic-e2eelastic",
        name="e2eelastic",
    ),
]
STAMP_COUNTS_CASES = [
    StampCountsCase(expected=(12, 12), raw="12\t12"),
    StampCountsCase(expected=(3, 1), raw="3\t1\n"),
    StampCountsCase(expected=None, raw=""),
    StampCountsCase(expected=None, raw="3"),
    StampCountsCase(expected=None, raw="Code: 47. DB::Exception: Unknown identifier"),
]
STAMP_PROBLEMS_CASES = [
    StampProblemsCase(counts=(5, 5), expected=[]),
    StampProblemsCase(
        counts=(5, 3),
        expected=[
            "2/5 rows of this run in 'e2evrl' lack the `_tags.e2e_transform` "
            "'dfe-transform-vrl-e2evrl' sets, so they reached the table around it"
        ],
    ),
    StampProblemsCase(
        counts=(0, 0),
        expected=[
            "no row of this run could be read in 'e2evrl', so whether "
            "'dfe-transform-vrl-e2evrl' ran on its records is unknown"
        ],
    ),
    StampProblemsCase(
        counts=None,
        expected=[
            "no row of this run could be read in 'e2evrl', so whether "
            "'dfe-transform-vrl-e2evrl' ran on its records is unknown"
        ],
    ),
]
STAMPING_SOURCE_CASES = [
    StampingSourceCase(expected=VRL, sources=[VRL], table="e2evrl"),
    StampingSourceCase(expected=VRL, sources=[VECTOR, VRL, ELASTIC], table="e2evrl"),
    StampingSourceCase(expected=None, sources=[VRL], table="main"),
    StampingSourceCase(expected=None, sources=[VECTOR], table="e2evector"),
    StampingSourceCase(expected=None, sources=[VRL, ELASTIC], table="e2eelastic"),
    StampingSourceCase(expected=None, sources=[], table="main"),
]
TARGET_PROBLEMS_CASES = [
    TargetProblemsCase(expected=[], outage_sources={}, table="e2evrl"),
    TargetProblemsCase(
        expected=[], outage_sources={"e2evrl": "events.jsonl"}, table="e2evrl"
    ),
    TargetProblemsCase(
        expected=["'table' 'main' names no source this test makes (e2evrl)"],
        outage_sources={},
        table="main",
    ),
    TargetProblemsCase(
        expected=["outage source 'main' names no source this test makes (e2evrl)"],
        outage_sources={"e2evrl": "events.jsonl", "main": "events.jsonl"},
        table="e2evrl",
    ),
]
UNINDEXED_CASES = [
    UnindexedCase(expected=[], found={"dfe-transform-vrl": ["e2evrl"]}),
    UnindexedCase(expected=["dfe-transform-vrl-e2evrl"], found={}),
    UnindexedCase(
        expected=["dfe-transform-vrl-e2evrl"],
        found={"dfe-transform-vrl": ["aws-cloudtrail"]},
    ),
    UnindexedCase(
        expected=["dfe-transform-vrl-e2evrl"],
        found={"dfe-transform-vector": ["e2evrl"]},
    ),
]


def _engine_service() -> dict:
    """The committed dfe-engine service, as compose reads its keys before interpolation."""
    services = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]
    return services["dfe-engine"]


def _index(*, app: str, names: list[str]) -> None:
    """Write one app's instance index where the suite reads it, as dfe-engine does."""
    path = instances.index_file(service=app)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")


def _project(*, tmp_path: Path) -> Path:
    """A project directory holding the stamp program where the committed one sits."""
    fixture = tmp_path / PROGRAM
    fixture.parent.mkdir(parents=True)
    fixture.write_text(PROGRAM_TEXT, encoding="utf-8")
    return tmp_path


def _source_tests() -> list[dict]:
    """Every committed e2e test that makes sources."""
    tests = yaml.safe_load(E2E_TESTS_FILE.read_text(encoding="utf-8"))["tests"]
    return [test for test in tests if test.get("sources")]


class TestSource:
    @pytest.mark.parametrize("case", SOURCE_CASES)
    def test_matches_expected(self, case: SourceCase) -> None:
        source = _sources.Source(engine=case["engine"], files=(), name=case["name"])

        assert (source.app, source.instance) == (case["app"], case["instance"])

    def test_every_engine_is_a_per_source_app_the_profiles_know(self) -> None:
        assert _sources.ENGINES == ("elastic", "vector", "vrl")


class TestBindingConsumers:
    def test_each_source_adds_one_consumer_to_its_landing_topic(self) -> None:
        consumers = _sources.binding_consumers(sources=[VRL, VECTOR, ELASTIC])

        assert consumers == {
            "e2evrl_land": 1,
            "e2evector_land": 1,
            "e2eelastic_land": 1,
        }


class TestBindings:
    @pytest.mark.parametrize("case", BINDINGS_CASES)
    def test_matches_expected(self, case: BindingsCase) -> None:
        assert _sources.bindings(sources=case["sources"]) == case["expected"]


class TestBindingsUnassigned:
    @pytest.mark.parametrize("case", BINDINGS_UNASSIGNED_CASES)
    def test_matches_expected(self, case: BindingsUnassignedCase) -> None:
        problems = _sources.bindings_unassigned(
            assigned=case["assigned"], bindings=_sources.bindings(sources=[VRL])
        )

        assert problems == case["expected"]


class TestBindingsUncommitted:
    @pytest.mark.parametrize("case", BINDINGS_UNCOMMITTED_CASES)
    def test_matches_expected(self, case: BindingsUncommittedCase) -> None:
        problems = _sources.bindings_uncommitted(
            bindings=_sources.bindings(sources=[VRL]), lag=case["lag"]
        )

        assert problems == case["expected"]


class TestCreateRequest:
    @pytest.mark.parametrize("case", CREATE_REQUEST_CASES)
    def test_matches_expected(self, case: CreateRequestCase) -> None:
        name = case["source"].name

        body = _sources.create_request(source=case["source"])

        assert body == {
            "display_name": f"E2E {name}",
            "header": {"type": "common-header/timeseries", "version": "1.0.1"},
            "match": {"field": "_source", "operator": "equals", "value": name},
            "schema": case["schema"],
            "source": name,
            "transform": case["transform"],
        }

    def test_the_header_and_schema_are_the_seeded_receiver_sources(self) -> None:
        seeded = yaml.safe_load(SYSLOG_SEED.read_text(encoding="utf-8"))

        body = _sources.create_request(source=VRL)

        assert (body["header"], body["schema"]) == (seeded["header"], seeded["schema"])
        assert body["match"]["field"] == seeded["match"]["field"]

    def test_a_body_carries_no_mutable_module_state(self) -> None:
        body = _sources.create_request(source=VRL)
        body["header"]["version"] = "9.9.9"
        body["schema"]["meta_schema"] = "meta/other"

        again = _sources.create_request(source=VRL)

        assert again["header"]["version"] == "1.0.1"
        assert again["schema"]["meta_schema"] == "meta/syslog"


class TestDeployPath:
    def test_names_the_source_deploy_route(self) -> None:
        assert _sources.deploy_path(source=VRL) == "/sources/e2evrl/deploy"


class TestEnabledApps:
    @pytest.mark.parametrize("case", ENABLED_APPS_CASES)
    def test_matches_expected(self, case: EnabledAppsCase) -> None:
        apps = _sources.enabled_apps(
            profile_name=case["profile"],
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8"),
        )

        assert apps == case["expected"]

    def test_refuses_a_key_the_resolver_refuses(self) -> None:
        text = (
            "profiles:\n  single-grpc:\n    transport: grpc\n"
            "    transform_elastic_enabled: true\n"
        )

        with pytest.raises(resolve_profile.ProfileError, match="kafka transport only"):
            _sources.enabled_apps(profile_name="single-grpc", text=text)

    def test_a_profile_with_the_core_footprint_off_runs_none(self) -> None:
        text = (
            "profiles:\n  single-kafka:\n    transport: kafka\n    core: false\n"
            "    transform_vrl_enabled: true\n"
        )

        assert _sources.enabled_apps(profile_name="single-kafka", text=text) == []


class TestEngineBaseVolumes:
    def test_mounts_each_committed_config_where_the_engine_reads_its_base(
        self,
    ) -> None:
        volumes = _sources.engine_base_volumes(
            services={
                "dfe-loader": "config/loader/grpc.yaml",
                "dfe-receiver": "config/receiver/grpc.yaml",
            }
        )

        assert volumes == [
            "./config/loader/grpc.yaml:/app/app-config-base/dfe-loader.yaml:ro",
            "./config/receiver/grpc.yaml:/app/app-config-base/dfe-receiver.yaml:ro",
        ]

    def test_each_target_replaces_a_mount_the_engine_service_declares(self) -> None:
        declared = _engine_service()["volumes"]
        services = {
            service: f"config/{service}.yaml"
            for service in ("dfe-archiver", "dfe-loader", "dfe-receiver")
        }

        volumes = _sources.engine_base_volumes(services=services)

        for volume in volumes:
            target = volume.split(":")[1]
            assert any(f":{target}:" in mount for mount in declared), target


class TestEngineEnvironment:
    @pytest.mark.parametrize("case", ENGINE_ENVIRONMENT_CASES)
    def test_matches_expected(self, case: EngineEnvironmentCase) -> None:
        environment = _sources.engine_environment(
            profile_name=case["profile"], transport=case["transport"]
        )

        assert environment == case["expected"]

    def test_every_key_is_one_the_engine_service_sets(self) -> None:
        declared = set(_engine_service()["environment"])

        environment = _sources.engine_environment(
            profile_name="single-kafka", transport="kafka"
        )

        assert set(environment) <= declared


class TestEngineOverride:
    @pytest.mark.parametrize("case", ENGINE_OVERRIDE_CASES)
    def test_matches_expected(self, case: EngineOverrideCase) -> None:
        override = _sources.engine_override(
            environment=case["environment"],
            service="dfe-engine",
            volumes=case["volumes"],
        )

        assert override == case["expected"]

    def test_compose_reads_both_blocks_under_the_engine_service(self) -> None:
        override = _sources.engine_override(
            environment={"DFE_TRANSPORT_DEFAULT": "bus"},
            service="dfe-engine",
            volumes=[
                "./config/loader/grpc.yaml:/app/app-config-base/dfe-loader.yaml:ro"
            ],
        )

        parsed = yaml.safe_load(f"services:\n{override}")

        assert parsed == {
            "services": {
                "dfe-engine": {
                    "environment": {"DFE_TRANSPORT_DEFAULT": "bus"},
                    "volumes": [
                        "./config/loader/grpc.yaml:/app/app-config-base/dfe-loader.yaml:ro"
                    ],
                }
            }
        }


class TestHintedServices:
    @pytest.mark.parametrize("case", HINTED_SERVICES_CASES)
    def test_matches_expected(self, case: HintedServicesCase) -> None:
        assert _sources.hinted_services(replies=case["replies"]) == case["expected"]


class TestIndexed:
    def test_lists_only_the_sources_the_index_names(self) -> None:
        _index(app="dfe-transform-vrl", names=["aws-cloudtrail", "e2evrl"])
        _index(app="dfe-transform-vector", names=["aws-guardduty"])

        found = _sources.indexed(sources=[VRL, VECTOR, ELASTIC])

        assert found == {"dfe-transform-vrl": ["e2evrl"]}

    def test_no_index_lists_nothing(self) -> None:
        assert _sources.indexed(sources=[VRL]) == {}

    def test_what_it_lists_is_a_fragment_declaring_each_instance(self) -> None:
        _index(app="dfe-transform-vrl", names=["e2evrl"])
        _index(app="dfe-transform-vector", names=["e2evector"])

        found = _sources.indexed(sources=[VRL, VECTOR])

        assert instances.services(instances=found) == [VECTOR.instance, VRL.instance]


class TestInstanceGroup:
    @pytest.mark.parametrize("case", INSTANCE_GROUP_CASES)
    def test_matches_expected(self, case: InstanceGroupCase) -> None:
        group = _sources.instance_group(service=case["service"], sources=[VRL])

        assert group == case["expected"]


class TestKafkaBootstrap:
    @pytest.mark.parametrize("case", KAFKA_BOOTSTRAP_CASES)
    def test_matches_expected(self, case: KafkaBootstrapCase) -> None:
        broker = _sources.kafka_bootstrap(transport=case["transport"])

        assert broker == case["expected"]


class TestMergedTopics:
    @pytest.mark.parametrize("case", MERGED_TOPICS_CASES)
    def test_matches_expected(self, case: MergedTopicsCase) -> None:
        topics = _sources.merged_topics(
            expected_topics=case["expected_topics"],
            sources=[VRL],
            transport=case["transport"],
        )

        assert topics == case["expected"]

    def test_leaves_the_list_it_is_given_as_it_was(self) -> None:
        declared = ["main_land"]

        _sources.merged_topics(
            expected_topics=declared, sources=[VRL], transport="kafka"
        )

        assert declared == ["main_land"]


class TestOutageProblems:
    @pytest.mark.parametrize("case", OUTAGE_PROBLEMS_CASES)
    def test_matches_expected(self, case: OutageProblemsCase) -> None:
        problems = _sources.outage_problems(service=case["service"], sources=[VRL])

        assert problems == case["expected"]


class TestParse:
    @pytest.mark.parametrize("case", PARSE_CASES)
    def test_matches_expected(self, tmp_path: Path, case: ParseCase) -> None:
        sources, problems = _sources.parse(
            project_dir=_project(tmp_path=tmp_path), raw=case["raw"]
        )

        assert (sources, problems) == (case["expected"], [])

    @pytest.mark.parametrize("case", PARSE_REFUSAL_CASES)
    def test_refuses(self, tmp_path: Path, case: ParseRefusalCase) -> None:
        sources, problems = _sources.parse(
            project_dir=_project(tmp_path=tmp_path), raw=case["raw"]
        )

        assert any(problem.startswith(case["message"]) for problem in problems), (
            problems
        )
        assert len(sources) < max(1, len(case["raw"]))

    def test_every_committed_source_test_parses_and_its_profile_runs_it(
        self,
    ) -> None:
        profiles = SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        tests = _source_tests()

        for test in tests:
            sources, problems = _sources.parse(
                project_dir=REPO_ROOT, raw=test["sources"]
            )
            enabled = _sources.enabled_apps(profile_name=test["profile"], text=profiles)
            outage = _sources.outage_problems(
                service=test["outage"]["service"], sources=sources
            )
            targets = _sources.target_problems(
                outage_sources=(test["outage"].get("sources")) or ({}),
                sources=sources,
                table=test["table"],
            )

            assert (problems, outage, targets) == ([], [], []), test["name"]
            assert {source.app for source in sources} <= set(enabled), test["name"]
        assert sorted(test["name"] for test in tests) == [
            "kafka-outage",
            "loader-outage-grpc-transform-vrl",
            "transform-vrl-kill-grpc",
            "transform-vrl-outage-grpc",
        ]

    def test_every_committed_vrl_source_test_owes_the_stamp(self) -> None:
        for test in _source_tests():
            sources, _ = _sources.parse(project_dir=REPO_ROOT, raw=test["sources"])

            stamping = _sources.stamping_source(sources=sources, table=test["table"])

            assert stamping == VRL, test["name"]

    def test_the_committed_elastic_source_takes_the_catalogue_schema(self) -> None:
        tests = {test["name"]: test for test in _source_tests()}

        sources, _ = _sources.parse(
            project_dir=REPO_ROOT, raw=tests["kafka-outage"]["sources"]
        )

        assert ELASTIC in sources


class TestRenderedConfig:
    @pytest.mark.parametrize("case", RENDERED_CONFIG_CASES)
    def test_matches_expected(self, case: RenderedConfigCase) -> None:
        path = _sources.rendered_config(
            instance=case["instance"], service=case["service"]
        )

        assert path == case["expected"]

    def test_an_instance_reads_the_file_its_generated_service_names(self) -> None:
        fragment = instances.fragment(instances={VRL.app: [VRL.name]})

        want = _sources.rendered_config(instance=VRL.name, service=VRL.app)

        assert f'command: ["--config", "{want}"]' in fragment


class TestRestartPlan:
    @pytest.mark.parametrize("case", RESTART_PLAN_CASES)
    def test_matches_expected(self, case: RestartPlanCase) -> None:
        plan = _sources.restart_plan(
            found={VRL.app: [VRL.name]},
            hinted=case["hinted"],
            rendered_services=case["rendered_services"],
            transport=case["transport"],
        )

        assert plan == case["expected"]


class TestSourcePath:
    def test_names_the_source_route(self) -> None:
        assert _sources.source_path(source=VRL) == "/sources/e2evrl"


class TestStampCounts:
    @pytest.mark.parametrize("case", STAMP_COUNTS_CASES)
    def test_matches_expected(self, case: StampCountsCase) -> None:
        assert _sources.stamp_counts(raw=case["raw"]) == case["expected"]


class TestStampProblems:
    @pytest.mark.parametrize("case", STAMP_PROBLEMS_CASES)
    def test_matches_expected(self, case: StampProblemsCase) -> None:
        problems = _sources.stamp_problems(
            counts=case["counts"], source=VRL, table="e2evrl"
        )

        assert problems == case["expected"]


class TestStampQueries:
    def test_counts_this_runs_rows_beside_those_carrying_the_stamp(self) -> None:
        queries = _sources.stamp_queries(
            database="dfe", marker="e2e-1-kafka-outage", source=VRL, table="e2evrl"
        )

        assert queries == [
            "SELECT count(), countIf(toString(`_tags`.e2e_transform) = 'vrl') "
            "FROM dfe.e2evrl WHERE startsWith(toString(`_tags`.marker), "
            "'e2e-1-kafka-outage') FORMAT TabSeparated",
            "SELECT count(), countIf(JSONExtractString(toString(`_tags`), "
            "'e2e_transform') = 'vrl') FROM dfe.e2evrl WHERE "
            "startsWith(JSONExtractString(toString(`_tags`), 'marker'), "
            "'e2e-1-kafka-outage') FORMAT TabSeparated",
        ]

    def test_a_marker_with_a_quote_stays_inside_its_literal(self) -> None:
        queries = _sources.stamp_queries(
            database="dfe", marker="e2e-'x", source=VRL, table="e2evrl"
        )

        assert all("'e2e-\\'x'" in query for query in queries), queries

    def test_the_marker_reads_back_as_the_landing_check_reads_it(self) -> None:
        marked = [
            template.format(key="marker") for template in _sources.TAG_EXPRESSIONS
        ]

        assert marked == list(_pipeline.MARKER_EXPRESSIONS)


class TestStampingSource:
    @pytest.mark.parametrize("case", STAMPING_SOURCE_CASES)
    def test_matches_expected(self, case: StampingSourceCase) -> None:
        source = _sources.stamping_source(sources=case["sources"], table=case["table"])

        assert source == case["expected"]

    def test_the_committed_program_sets_the_stamp_the_check_reads(self) -> None:
        program = (REPO_ROOT / PROGRAM).read_text(encoding="utf-8")

        assert f'._tags.{_sources.STAMP_KEY} = "{VRL.engine}"' in program


class TestTargetProblems:
    @pytest.mark.parametrize("case", TARGET_PROBLEMS_CASES)
    def test_matches_expected(self, case: TargetProblemsCase) -> None:
        problems = _sources.target_problems(
            outage_sources=case["outage_sources"], sources=[VRL], table=case["table"]
        )

        assert problems == case["expected"]


class TestTopics:
    def test_each_source_lands_then_loads_in_the_order_declared(self) -> None:
        assert _sources.topics(sources=[VRL, ELASTIC]) == [
            "e2evrl_land",
            "e2evrl_load",
            "e2eelastic_land",
            "e2eelastic_load",
        ]

    def test_no_sources_adds_no_topic(self) -> None:
        assert _sources.topics(sources=[]) == []


class TestUnindexed:
    @pytest.mark.parametrize("case", UNINDEXED_CASES)
    def test_matches_expected(self, case: UnindexedCase) -> None:
        missing = _sources.unindexed(found=case["found"], sources=[VRL])

        assert missing == case["expected"]


class TestUploadRequests:
    def test_uploads_each_fixture_under_its_own_name(self, tmp_path: Path) -> None:
        requests = _sources.upload_requests(
            project_dir=_project(tmp_path=tmp_path), source=VRL
        )

        assert requests == [
            (
                "/apps/dfe-transform-vrl/e2evrl/files/transforms/stamp_transformed.vrl",
                {"content": PROGRAM_TEXT},
            )
        ]

    def test_a_name_with_a_space_is_quoted(self, tmp_path: Path) -> None:
        fixture = tmp_path / "tests" / "e2e" / "two words.vrl"
        fixture.parent.mkdir(parents=True)
        fixture.write_text(". = .\n", encoding="utf-8")
        source = _sources.Source(
            engine="vrl",
            files=(("transforms", "tests/e2e/two words.vrl"),),
            name="e2evrl",
        )

        requests = _sources.upload_requests(project_dir=tmp_path, source=source)

        assert requests[0][0].endswith("/files/transforms/two%20words.vrl")

    def test_a_source_with_no_files_uploads_nothing(self, tmp_path: Path) -> None:
        assert _sources.upload_requests(project_dir=tmp_path, source=VECTOR) == []
