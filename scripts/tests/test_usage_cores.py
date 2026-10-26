#  Project:      dfe-docker
#  File:         tests/test_usage_cores.py
#  Purpose:      Assert which services cores deployed counts, and how the collector reports them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Cores deployed on Compose, from the CPU limit of each service the stack runs.

dfe-engine's usage rollup reads the Kubernetes shape: a ``k8s.container.cpu_request``
gauge per container beside a Running ``k8s.pod.phase`` reading of the same instant,
and each node's ``system.cpu.logical.count``, joined on ``k8s.node.name``. These
tests hold the derivation to what ``up`` starts and the collector to that shape.

The models below are shaped like ``docker compose config --format json`` output.
The tests that render the real one skip without docker, as test_hyperdx_memory does.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

import check_compose
import resolve_profile
import usage_cores
from _common import COMPOSE_FILE, CONFIG_DIR, REPO_ROOT

_COLLECTOR_CONFIG = CONFIG_DIR / "otel-collector" / "config.yaml"
_COLLECTOR_SERVICE = "otel-collector"
_ENGINE_SERVICE = "dfe-engine"

# What single-kafka runs once it is up: every service but the one-shots, the
# dependencies its named services pull in included.
_SINGLE_KAFKA_RUNNING = {
    "clickhouse",
    "dfe-archiver",
    "dfe-engine",
    "dfe-hunt-runner",
    "dfe-hyperdx-proxy",
    "dfe-loader",
    "dfe-proxy",
    "dfe-receiver",
    "dfe-ui",
    "hyperdx",
    "hyperdx-documentdb",
    "kafka-redpanda",
    "kafka-ui",
    "otel-collector",
}
_SINGLE_KAFKA_ONE_SHOTS = {
    "bearer-tokens-init",
    "contract-dfe-loader",
    "dfe-dashboards",
    "dlq-init",
    "hyperdx-documentdb-tls",
}

# dfe-engine's clickhouse.bootstrap_wait_seconds default (settings.py), and a lease
# lasts twice a peer's wait (schema/lock.py lease_seconds).
_ENGINE_BOOTSTRAP_WAIT_SECONDS = 180
_ENGINE_LEASE_SECONDS = 2 * _ENGINE_BOOTSTRAP_WAIT_SECONDS
# What the start period gave a first start's table creation before the lease was counted.
_FIRST_START_SECONDS = 180

_GO_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


def _service(*, restart: str | None = "unless-stopped", cpus: object = 2.0) -> dict:
    """One service as the resolved model carries it."""
    service: dict = {}
    if restart is not None:
        service["restart"] = restart
    if cpus is not None:
        service["deploy"] = {"resources": {"limits": {"cpus": cpus}}}
    return service


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model these assert on")


def _usage_exports(
    *, argv: list[str], monkeypatch: pytest.MonkeyPatch, profile: str, tmp_path: Path
) -> dict[str, str]:
    """Return the make exports resolve_profile writes for one profile."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    for env_var, _ in resolve_profile.FOOTPRINT_KEYS.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("KAFKA_BACKEND", raising=False)
    monkeypatch.setenv("DFE_PROFILE", profile)

    assert resolve_profile.main(argv) == 0

    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


def _seconds(duration: str) -> float:
    """A Go duration as compose config renders it (`9m0s`), in seconds."""
    match = _GO_DURATION.fullmatch(duration)
    assert match is not None and duration, f"not a duration: {duration!r}"
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def test_a_long_running_service_counts_its_cpu_limit() -> None:
    model = {
        "services": {
            "dfe-loader": _service(cpus=2.0),
            "dfe-proxy": _service(cpus="0.5"),
        }
    }

    assert usage_cores.cpu_limits(model=model, host_cores=32.0) == {
        "dfe-loader": 2.0,
        "dfe-proxy": 0.5,
    }


def test_a_one_shot_is_not_counted() -> None:
    model = {
        "services": {
            "dfe-loader": _service(),
            "dlq-init": _service(restart="no", cpus=0.5),
        }
    }

    assert usage_cores.cpu_limits(model=model, host_cores=32.0) == {"dfe-loader": 2.0}


@pytest.mark.parametrize("restart", [None, "always", "on-failure"])
def test_a_service_that_keeps_running_counts_whatever_restarts_it(
    restart: str | None,
) -> None:
    model = {"services": {"sidecar": _service(restart=restart, cpus=1.0)}}

    assert usage_cores.cpu_limits(model=model, host_cores=32.0) == {"sidecar": 1.0}


@pytest.mark.parametrize(
    "service",
    [
        {"restart": "unless-stopped"},
        {
            "restart": "unless-stopped",
            "deploy": {"resources": {"limits": {"memory": 1}}},
        },
        _service(cpus=0),
        _service(cpus="0"),
    ],
    ids=["no deploy", "memory limit only", "zero", "zero as text"],
)
def test_a_service_with_no_cpu_limit_counts_every_host_core(service: dict) -> None:
    # Docker reads a cpus of 0 as no limit, so the container may take every core.
    model = {"services": {"unlimited": service}}

    assert usage_cores.cpu_limits(model=model, host_cores=12.0) == {"unlimited": 12.0}


def test_an_empty_model_counts_nothing() -> None:
    assert usage_cores.cpu_limits(model={}, host_cores=8.0) == {}
    assert usage_cores.cpu_limits(model={"services": {}}, host_cores=8.0) == {}


@pytest.mark.parametrize("name", ["it's", 'a"b', "back\\slash", "-leading-dash", ""])
def test_a_name_that_could_break_the_sql_literal_is_refused(name: str) -> None:
    # The limits ride inside a single-quoted ClickHouse string in the collector's query.
    model = {"services": {name: _service()}}

    with pytest.raises(ValueError, match="compose charset"):
        usage_cores.cpu_limits(model=model, host_cores=8.0)


def test_a_limit_that_is_not_a_number_is_refused() -> None:
    model = {"services": {"dfe-loader": _service(cpus="two")}}

    with pytest.raises(ValueError):
        usage_cores.cpu_limits(model=model, host_cores=8.0)


def test_the_exports_settle_whatever_order_the_limits_arrive_in() -> None:
    # make re-reads an include it remade, so the same stack must write the same bytes.
    forward = usage_cores.exports(limits={"a": 2.0, "b": 0.5}, node="host-1")
    backward = usage_cores.exports(limits={"b": 0.5, "a": 2.0}, node="host-1")

    assert forward == backward
    assert forward == [
        f'export {usage_cores.USAGE_LIMITS_VAR} := {{"a":2.0,"b":0.5}}',
        f"export {usage_cores.USAGE_NODE_VAR} := host-1",
    ]


def test_no_limits_export_an_empty_object() -> None:
    limits_line, _ = usage_cores.exports(limits={}, node="host-1")

    assert limits_line == f"export {usage_cores.USAGE_LIMITS_VAR} := {{}}"


def test_an_unresolvable_stack_warns_and_exports_no_limits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_docker()

    lines = usage_cores.profile_lines(
        profiles=[], services=["no-such-service"], fragment=None
    )

    assert lines[0] == f"export {usage_cores.USAGE_LIMITS_VAR} := {{}}"
    assert "cores deployed reads nothing" in capsys.readouterr().err


def test_resolve_writes_no_limits_without_the_usage_flag(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Only the goals that start services ask docker compose for the model.
    exports = _usage_exports(
        argv=[], monkeypatch=monkeypatch, profile="single-kafka", tmp_path=tmp_path
    )

    assert usage_cores.USAGE_LIMITS_VAR not in exports
    assert usage_cores.USAGE_NODE_VAR not in exports


def test_a_stack_with_no_collector_resolves_no_limits(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(resolve_profile.OTEL_ENABLED_ENV_VAR, "false")
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setenv("DFE_PROFILE", "single-kafka")

    assert resolve_profile.main(["--usage"]) == 0

    assert usage_cores.USAGE_LIMITS_VAR not in written.read_text(encoding="utf-8")


def test_single_kafka_counts_every_long_running_service_it_starts(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_docker()

    exports = _usage_exports(
        argv=["--usage"],
        monkeypatch=monkeypatch,
        profile="single-kafka",
        tmp_path=tmp_path,
    )

    limits = json.loads(exports[usage_cores.USAGE_LIMITS_VAR])
    assert set(limits) == _SINGLE_KAFKA_RUNNING
    assert not (set(limits) & _SINGLE_KAFKA_ONE_SHOTS)
    assert all(cores > 0 for cores in limits.values())
    assert exports[usage_cores.USAGE_NODE_VAR]


def test_only_the_root_compose_file_sets_a_cpu_limit() -> None:
    # The limits are read from docker-compose.yml and the instances fragment, so an
    # overlay setting cpus would move a limit the usage rollup never sees.
    overlays = sorted(
        path
        for path in REPO_ROOT.glob("docker-compose*.yml")
        if path.name != COMPOSE_FILE.name
    )
    assert overlays, "no compose overlay found -- the glob matched nothing"
    for path in overlays:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*cpus\s*:", text, re.MULTILINE), path.name


def test_the_collector_reads_the_limits_and_node_compose_hands_it() -> None:
    _require_docker()
    env, _ = check_compose._check_env()
    model = check_compose._config_json(env=env, files=[COMPOSE_FILE.name])
    assert model is not None, "the registry compose path did not resolve"

    collector = model["services"][_COLLECTOR_SERVICE]
    for name in (usage_cores.USAGE_LIMITS_VAR, usage_cores.USAGE_NODE_VAR):
        assert name in collector["environment"], name
    proc = [
        volume
        for volume in collector["volumes"]
        if volume.get("source") == "/proc" and volume.get("target") == "/hostfs/proc"
    ]
    assert proc and proc[0].get("read_only") is True


def test_the_collector_reports_the_shape_the_usage_rollup_reads() -> None:
    config = yaml.safe_load(_COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    receivers = config["receivers"]
    processors = config["processors"]
    pipelines = config["service"]["pipelines"]
    node = f"${{env:{usage_cores.USAGE_NODE_VAR}}}"

    assert receivers["host_metrics"]["root_path"] == "/hostfs"
    assert "cpu" in receivers["host_metrics"]["scrapers"]
    assert pipelines["metrics/host"]["receivers"] == ["host_metrics"]

    query = receivers["sql_query/usage"]["queries"][0]
    assert f"${{env:{usage_cores.USAGE_LIMITS_VAR}}}" in query["sql"]
    metrics = {metric["metric_name"]: metric for metric in query["metrics"]}
    assert set(metrics) == {"k8s.container.cpu_request", "k8s.pod.phase"}
    usage = pipelines["metrics/usage"]
    assert usage["receivers"] == ["sql_query/usage"]
    grouped = set(processors["groupbyattrs/usage"]["keys"])
    for metric in metrics.values():
        assert set(metric["attribute_columns"]) == grouped
    assert usage["processors"].index("groupbyattrs/usage") < usage["processors"].index(
        "resource/usage"
    )

    for name, pipeline in (
        ("resource/host", "metrics/host"),
        ("resource/usage", "metrics/usage"),
    ):
        attributes = {
            entry["key"]: entry["value"] for entry in processors[name]["attributes"]
        }
        assert attributes["k8s.node.name"] == node, name
        assert name in pipelines[pipeline]["processors"]


def test_the_e2e_suite_outwaits_a_schema_lease_a_removed_engine_left() -> None:
    # An engine that waits out the lease does not listen, so the suite's readiness poll is
    # the only thing left to give up on it. Read as text: test_e2e.py needs PyYAML to import.
    source = (REPO_ROOT / "scripts" / "test_e2e.py").read_text(encoding="utf-8")
    match = re.search(r"^ENGINE_READY_ATTEMPTS = (\d+)$", source, re.MULTILINE)
    assert match is not None
    poll_seconds = 2

    assert int(match.group(1)) * poll_seconds >= (
        _ENGINE_LEASE_SECONDS + _FIRST_START_SECONDS
    )


def test_the_engine_outlasts_a_schema_lease_a_removed_engine_left() -> None:
    # The next engine waits out the lease before its own apply, and compose fails the
    # stack's start the moment the engine reads unhealthy.
    _require_docker()
    env, _ = check_compose._check_env()
    model = check_compose._config_json(env=env, files=[COMPOSE_FILE.name])
    assert model is not None, "the registry compose path did not resolve"

    healthcheck = model["services"][_ENGINE_SERVICE]["healthcheck"]
    start_period = _seconds(healthcheck["start_period"])

    assert start_period >= _ENGINE_LEASE_SECONDS + _FIRST_START_SECONDS
