#  Project:      dfe-docker
#  File:         tests/test_post.py
#  Purpose:      Assert what the power-on self test claims, and how it reaches the stack
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The parts of POST that decide whether a run means anything.

Three things are asserted here and nothing else needs a stack to check them: the
UI-class APIs are read over the compose network rather than a host port that a
deployment dial can take away, the order the claims run in (the loader's
subscription is only visible once events have landed), and that a run which
skipped every claim exits non-zero. The hunt claim's own steps are driven against
a stand-in engine and ClickHouse, so its verdict is shown failing as well as holding.
"""

from __future__ import annotations

import datetime
import json

import pytest

import _pipeline
import post

_HOST_URL = "http://localhost:8003/api/v1/auth/login"


def test_the_ui_class_apis_are_read_over_the_compose_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A moved host port or an unpublished UI cannot move either API."""
    monkeypatch.setenv("DFE_POST_HOST", "127.0.0.2")
    monkeypatch.setenv("DFE_HYPERDX_API_PORT", "18000")
    monkeypatch.setenv("DFE_ENGINE_PORT", "18003")

    assert post._hyperdx_base() == "http://dfe-hyperdx-proxy:8000"
    assert post._engine_base() == "http://dfe-engine:8000/api/v1"


def test_a_container_address_takes_the_in_network_transport() -> None:
    assert post._over_network(f"{post.HYPERDX_NETWORK_BASE}/sources")
    assert post._over_network(f"{post.ENGINE_NETWORK_BASE}/auth/login")
    assert post._over_network(f"{post.ENGINE_NETWORK_ORIGIN}/readyz")
    assert not post._over_network(_HOST_URL)
    assert not post._over_network("http://localhost:8003/readyz")


def test_the_exec_script_reads_the_key_the_request_is_passed_in() -> None:
    """The script runs in another process, so the two halves of the name must agree."""
    assert post.API_REQUEST_KEY in post._EXEC_REQUEST_SCRIPT


def test_a_request_over_the_network_carries_the_method_body_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def _run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return _completed(stdout="c0ffee\n")
        recorded["args"] = args
        recorded["env"] = kwargs["env"]
        return _completed(stdout='200\n{"access_token": "t"}')

    monkeypatch.setattr(post.subprocess, "run", _run)
    post._api_container.cache_clear()

    status, body = post._api_post_json(
        f"{post.ENGINE_NETWORK_BASE}/auth/login",
        {"username": "admin", "password": "hunter2"},
        token="bearer",
    )

    assert (status, body) == (200, {"access_token": "t"})
    args = recorded["args"]
    assert args[:4] == ["docker", "exec", "-e", post.API_REQUEST_KEY]
    spec = json.loads(recorded["env"][post.API_REQUEST_KEY])
    assert spec["method"] == "POST"
    assert spec["token"] == "bearer"
    assert json.loads(spec["body"]) == {"username": "admin", "password": "hunter2"}


def test_neither_the_password_nor_the_token_reaches_the_exec_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any local user can read another process's argv through `ps`."""
    recorded: dict[str, object] = {}

    def _run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return _completed(stdout="c0ffee\n")
        recorded["args"] = args
        return _completed(stdout="200\n{}")

    monkeypatch.setattr(post.subprocess, "run", _run)
    post._api_container.cache_clear()

    post._api_post_json(
        f"{post.ENGINE_NETWORK_BASE}/auth/login",
        {"password": "pw-7c1e0b"},
        token="tok-3f9a2d",
    )

    argv = " ".join(recorded["args"])
    assert "pw-7c1e0b" not in argv
    assert "tok-3f9a2d" not in argv


def test_the_exec_addresses_the_container_compose_names_not_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare service name is daemon-wide, so it reaches another stack's container."""
    recorded: dict[str, object] = {}

    def _run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            assert post.API_EXEC_SERVICE in args
            return _completed(stdout="c0ffee\n")
        recorded["args"] = args
        return _completed(stdout="200\n{}")

    monkeypatch.setattr(post.subprocess, "run", _run)
    post._api_container.cache_clear()

    post._api_post_json(f"{post.ENGINE_NETWORK_BASE}/auth/login", {})

    args = recorded["args"]
    assert "c0ffee" in args
    assert post.API_EXEC_SERVICE not in args


def test_no_container_for_this_project_is_reported_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Testing the wrong stack is the defect; finding no stack is a fine answer."""
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda args, **kwargs: _completed(stdout="", stderr="no such service"),
    )
    post._api_container.cache_clear()

    with pytest.raises(post.ApiUnreachable, match="no running"):
        post._api_post_json(f"{post.ENGINE_NETWORK_BASE}/auth/login", {})


def test_a_host_address_never_reaches_for_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(post, "http_post_json", lambda *args, **kwargs: (200, {}))
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("a host URL must not shell out"),
    )

    assert post._api_post_json(_HOST_URL, {}) == (200, {})


def test_an_exec_that_did_not_run_is_not_a_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A docker failure must not read as an API answer."""
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda *args, **kwargs: _completed(code=1, stderr="No such container"),
    )

    with pytest.raises(post.ApiUnreachable):
        post._api_get_json(f"{post.HYPERDX_NETWORK_BASE}/sources")


def test_only_the_services_that_push_their_own_metrics_are_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        post,
        "_resolved_services",
        lambda: [
            "dfe-engine",
            "dfe-hunt-runner",
            "dfe-loader",
            "dfe-transform-e2e-vrl-filebeat",
            "dfe-transform-vrl-filebeat",
            "dfe-ui",
            "hyperdx",
            "otel-collector",
        ],
    )

    assert post._otel_expected_services() == [
        "dfe-engine",
        "dfe-loader",
        "dfe-transform-e2e-vrl-filebeat",
        "dfe-transform-vrl-filebeat",
    ]


def test_the_per_service_read_covers_the_metrics_tables_and_not_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traces are sampled, so a service missing from them says nothing."""
    queries: list[str] = []
    monkeypatch.setattr(post, "ch_query", lambda sql: queries.append(sql) or "")

    post._otel_reporting_services(database="dfe")

    assert len(queries) == 1
    for table in post.OTEL_METRICS_TABLES:
        assert f"dfe.{table}" in queries[0]
    assert "otel_traces" not in queries[0]


def test_a_service_that_never_exported_fails_the_self_monitoring_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(post, "OTEL_SERVICE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(
        post, "_resolved_services", lambda: ["dfe-loader", "dfe-engine"]
    )
    monkeypatch.setattr(post, "ch_query", lambda sql: "dfe-loader\n")

    assert post._verify_service_metrics(database="dfe") == 1


def test_every_pushing_service_in_the_tables_holds_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(post, "OTEL_SERVICE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(
        post, "_resolved_services", lambda: ["dfe-loader", "dfe-engine"]
    )
    monkeypatch.setattr(
        post, "ch_query", lambda sql: "dfe-loader\ndfe-engine\nclickhouse\n"
    )

    assert post._verify_service_metrics(database="dfe") == 0


def test_a_run_that_asserted_nothing_exits_non_zero() -> None:
    assert (
        post._report_claims(claims={"ingest": post.SKIPPED, "hunts": post.SKIPPED}) == 1
    )


def test_a_run_that_held_every_claim_it_made_exits_zero() -> None:
    assert post._report_claims(claims={"ingest": post.HELD, "hunts": post.SKIPPED}) == 0


def test_one_failed_claim_fails_the_run() -> None:
    claims = {"ingest": post.HELD, "hunts": post.Claim(asserted=True, failed=1)}

    assert post._report_claims(claims=claims) == 1


def test_a_profile_that_can_prove_nothing_does_not_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grpc-minimal shape: no ingest component, every other claim skipped."""
    _stub_main(monkeypatch)

    def _no_ingest(*, table):
        raise post.NoIngestComponent("active profile has no ingest component")

    monkeypatch.setattr(post, "_ingest_target", _no_ingest)
    for name in ("_verify_self_monitoring", "_verify_hyperdx", "_verify_idle_apps"):
        monkeypatch.setattr(post, name, lambda: post.SKIPPED)

    assert post.main() == 1


def test_the_subscription_is_asked_once_the_events_have_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A topic nothing has been written to has no lag series, so asking first fails a working loader."""
    calls = _stub_claims(monkeypatch)
    _stub_main(monkeypatch)
    monkeypatch.setattr(
        post,
        "_ingest_target",
        lambda *, table: ("dfe-receiver", "http://ingest/ingest"),
    )

    def _posted(*args, **kwargs) -> int:
        calls.append("http_post")
        return 200

    monkeypatch.setattr(post, "http_post", _posted)

    assert post.main() == 0
    assert calls.index("http_post") < calls.index("_verify_loader_subscription")


def test_events_that_never_land_ask_the_subscription_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_claims(monkeypatch)
    _stub_main(monkeypatch)
    monkeypatch.setattr(
        post,
        "_ingest_target",
        lambda *, table: ("dfe-receiver", "http://ingest/ingest"),
    )
    monkeypatch.setattr(post, "ch_marker_count", lambda *args, **kwargs: 0)
    monkeypatch.setattr(post, "LAND_TIMEOUT_SECONDS", 0.0)

    def _posted(*args, **kwargs) -> int:
        calls.append("http_post")
        return 200

    monkeypatch.setattr(post, "http_post", _posted)

    assert post.main() == 1
    assert calls == ["http_post"] * post.EVENT_COUNT + ["_verify_loader_subscription"]


class _Clock:
    """Time that passes only when the code under test sleeps."""

    EPOCH = 1790000000.0

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.EPOCH + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _routed_row_at(
    monkeypatch: pytest.MonkeyPatch, *, bus: bool, lands_at: float | None
) -> _Clock:
    """Make the new source's row land `lands_at` seconds into the wait, or never."""
    clock = _Clock()
    monkeypatch.setattr(_pipeline, "time", clock)
    monkeypatch.setattr(post, "_tier_has_bus", lambda: bus)
    monkeypatch.setattr(
        post,
        "_ingest_target",
        lambda *, table: ("dfe-receiver", "http://ingest/ingest"),
    )
    monkeypatch.setattr(post, "http_post", lambda *args, **kwargs: 200)

    def _rows(*args) -> int:
        return int(lands_at is not None and clock.now >= lands_at)

    monkeypatch.setattr(post, "ch_marker_count", _rows)
    return clock


@pytest.mark.parametrize(
    "lands_at",
    [
        # The loader subscribes 44s after the topic is made, then flushes.
        50.0,
        # It subscribes a whole refresh after the topic is made, then flushes.
        post.LOADER_TOPIC_REFRESH_SECONDS + 15.0,
    ],
)
def test_the_routing_wait_outlasts_the_loaders_next_topic_refresh(
    monkeypatch: pytest.MonkeyPatch, lands_at: float
) -> None:
    _routed_row_at(monkeypatch, bus=True, lands_at=lands_at)

    assert post._await_routed_row(database="dfe", name="postroutetest") == 0


def test_a_loader_that_never_subscribes_fails_the_claim_and_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _routed_row_at(monkeypatch, bus=True, lands_at=None)

    assert post._await_routed_row(database="dfe", name="postroutetest") == 1
    window = post._routing_timeout_seconds(bus=True)
    assert window <= clock.now < window + post.ROUTING_INTERVAL_SECONDS
    assert "never subscribed to its landing topic" in capsys.readouterr().err


def test_only_a_tier_with_a_bus_waits_out_a_topic_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a bus the receiver hands the loader its records, so no topic is listed."""
    gave_up_at: dict[bool, float] = {}
    for bus in (True, False):
        clock = _routed_row_at(monkeypatch, bus=bus, lands_at=None)
        capsys.readouterr()
        assert post._await_routed_row(database="dfe", name="postroutetest") == 1
        gave_up_at[bus] = clock.now

    assert "subscribed" not in capsys.readouterr().err
    assert gave_up_at[True] - gave_up_at[False] >= (
        post.LOADER_TOPIC_REFRESH_SECONDS - post.ROUTING_INTERVAL_SECONDS
    )


@pytest.mark.parametrize(
    ("resolved", "bus"), [(["true"], True), (["false"], False), ([], False)]
)
def test_the_bus_is_read_off_the_resolved_profile(
    monkeypatch: pytest.MonkeyPatch, resolved: list[str], bus: bool
) -> None:
    monkeypatch.setattr(post, "_profile_mk_value", lambda *, key: resolved)

    assert post._tier_has_bus() is bus


def _no_host_port(url, **kwargs):
    pytest.fail(f"the schema wait dialled {url} on the host")


def _engine_answers(
    monkeypatch: pytest.MonkeyPatch, *, readyz: dict, schema_status: int
) -> list[str]:
    """Serve /readyz and the schema route in-network; return the URLs asked."""
    asked: list[str] = []

    def _run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return _completed(stdout="c0ffee\n")
        url = json.loads(kwargs["env"][post.API_REQUEST_KEY])["url"]
        asked.append(url)
        if url.endswith("/readyz"):
            return _completed(stdout=f"200\n{json.dumps(readyz)}")
        return _completed(stdout=f"{schema_status}\n{{}}")

    monkeypatch.setattr(post.subprocess, "run", _run)
    monkeypatch.setattr(post, "http_get_json", _no_host_port)
    post._api_container.cache_clear()
    monkeypatch.setattr(post, "_resolved_services", lambda: [post.ENGINE_SERVICE])
    monkeypatch.setattr(post, "SCHEMA_TIMEOUT_SECONDS", 0.0)
    return asked


def test_the_schema_wait_reads_the_engine_over_the_compose_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DFE_ENGINE_API_EXTERNAL=false leaves no host port, and a moved one is moot."""
    monkeypatch.setenv("DFE_POST_HOST", "127.0.0.2")
    monkeypatch.setenv("DFE_ENGINE_PORT", "18003")
    asked = _engine_answers(
        monkeypatch, readyz={"checks": {"schema": True}}, schema_status=404
    )

    assert post._schema_state() == post.SCHEMA_CONVERGED
    assert asked == ["http://dfe-engine:8000/readyz"]


def test_the_schema_route_is_asked_over_the_compose_network_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _engine_answers(
        monkeypatch, readyz={"checks": {"clickhouse": True}}, schema_status=404
    )

    assert post._schema_state() == post.SCHEMA_UNSERVED
    assert asked == [
        "http://dfe-engine:8000/readyz",
        "http://dfe-engine:8000/api/v1/system/schema",
    ]


def test_an_engine_container_not_up_yet_reads_as_not_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait starts while compose is still starting the engine."""
    monkeypatch.setattr(
        post.subprocess, "run", lambda args, **kwargs: _completed(stdout="")
    )
    monkeypatch.setattr(post, "http_get_json", _no_host_port)
    post._api_container.cache_clear()

    assert post._schema_state() is None


def test_an_engine_that_predates_schema_control_is_skipped_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The v1.20 shape: no schema check on /readyz and no route to report one."""
    _engine_answers(
        monkeypatch, readyz={"checks": {"clickhouse": True}}, schema_status=404
    )

    assert post._wait_schema_converged() == 0
    assert "SKIP  schema convergence NOT checked" in capsys.readouterr().err


def test_a_named_schema_check_that_is_false_fails_even_where_the_route_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip keys on the check being absent, never on the route alone."""
    asked = _engine_answers(
        monkeypatch,
        readyz={"checks": {"clickhouse": True, "schema": False}},
        schema_status=404,
    )

    assert post._wait_schema_converged() == 1
    assert not any(url.endswith(post.SCHEMA_STATUS_PATH) for url in asked)


def test_an_engine_serving_the_route_without_the_check_is_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 means the route exists, so the check is still to come."""
    _engine_answers(
        monkeypatch, readyz={"checks": {"clickhouse": True}}, schema_status=401
    )

    assert post._schema_state() == post.SCHEMA_PENDING
    assert post._wait_schema_converged() == 1


def test_a_converged_schema_check_passes_without_asking_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _engine_answers(
        monkeypatch,
        readyz={"checks": {"clickhouse": True, "schema": True}},
        schema_status=404,
    )

    assert post._wait_schema_converged() == 0
    assert not any(url.endswith(post.SCHEMA_STATUS_PATH) for url in asked)


def test_an_engine_that_never_answers_fails_rather_than_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refused(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return _completed(stdout="c0ffee\n")
        return _completed(code=1, stderr="ConnectionRefusedError: [Errno 111]")

    monkeypatch.setattr(post.subprocess, "run", _refused)
    monkeypatch.setattr(post, "http_get_json", _no_host_port)
    post._api_container.cache_clear()
    monkeypatch.setattr(post, "_resolved_services", lambda: [post.ENGINE_SERVICE])
    monkeypatch.setattr(post, "SCHEMA_TIMEOUT_SECONDS", 0.0)

    assert post._schema_state() is None
    assert post._wait_schema_converged() == 1


class _Completed:
    """Stand-in for a finished `docker exec`."""

    def __init__(self, *, code: int, stdout: str, stderr: str) -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _completed(*, code: int = 0, stdout: str = "", stderr: str = "") -> _Completed:
    return _Completed(code=code, stdout=stdout, stderr=stderr)


def _stub_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let main() run without a stack, a .env or the developer's own environment."""
    monkeypatch.delenv("DFE_POST_ENABLED", raising=False)
    monkeypatch.setattr(post, "_load_dotenv", lambda: None)
    monkeypatch.setattr(post, "_weak_secrets", list)
    monkeypatch.setattr(post, "_cleanup", lambda *args: None)
    # Unstubbed, this polls the engine's /readyz for 300s against whatever the
    # developer happens to have on the host, and passes on a live stack.
    monkeypatch.setattr(post, "_wait_schema_converged", lambda: 0)
    monkeypatch.setattr(post, "http_post", lambda *args, **kwargs: 200)
    monkeypatch.setattr(post, "ch_count", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        post, "ch_marker_count", lambda *args, **kwargs: post.EVENT_COUNT
    )


def _stub_claims(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every claim with one that records that it ran, in order."""
    calls: list[str] = []

    def _recorder(name: str):
        def _claim(*args, **kwargs) -> post.Claim:
            calls.append(name)
            return post.HELD

        return _claim

    for name in (
        "_verify_loader_subscription",
        "_verify_hunt",
        "_verify_self_monitoring",
        "_verify_hyperdx",
        "_verify_ui_query",
        "_verify_idle_apps",
        "_verify_routing_applied",
    ):
        monkeypatch.setattr(post, name, _recorder(name))
    return calls


def test_the_log_query_matches_the_container_name_not_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ServiceName in otel_logs is the fluentd `{{.Name}}` tag, which an override renames."""
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda args, **kwargs: _completed(stdout="accept-dfe-engine\n"),
    )
    post._container_name.cache_clear()

    assert post._container_name("dfe-engine") == "accept-dfe-engine"


def test_a_container_start_is_floored_to_the_second_the_log_driver_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda args, **kwargs: _completed(stdout="2026-09-26T19:29:57.911126595Z\n"),
    )

    started = post._container_started("dfe-loader")

    assert started == int(
        datetime.datetime(2026, 9, 26, 19, 29, 57, tzinfo=datetime.UTC).timestamp()
    )


@pytest.mark.parametrize(
    ("code", "stdout"),
    [(1, ""), (0, "0001-01-01T00:00:00Z\n"), (0, "not a time\n")],
)
def test_a_start_docker_cannot_state_is_none(
    monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda args, **kwargs: _completed(code=code, stdout=stdout),
    )

    assert post._container_started("dfe-loader") is None


def _count_log_queries(
    monkeypatch: pytest.MonkeyPatch, started: int | None
) -> list[str]:
    """Run the container-log claim once and return every query it sent."""
    queries: list[str] = []
    monkeypatch.setattr(post, "OTEL_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(post, "_container_name", lambda service: service)
    monkeypatch.setattr(post, "_container_started", lambda name: started)
    monkeypatch.setattr(post, "ch_int", lambda sql: queries.append(sql) or 1)
    assert post._verify_container_logs(database="dfe") == 0
    return queries


def test_a_quiet_service_is_counted_from_its_own_start_not_a_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader logs at startup and then nothing, so a 300s window ages it out."""
    queries = _count_log_queries(monkeypatch, 1790000000)

    assert len(queries) == len(post.CONTAINER_LOG_SERVICES)
    for sql in queries:
        assert "Timestamp >= toDateTime(1790000000)" in sql
        assert "now() - INTERVAL" not in sql


def test_a_container_with_no_readable_start_falls_back_to_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _count_log_queries(monkeypatch, None)

    for sql in queries:
        assert f"now() - INTERVAL {post.OTEL_FRESH_WINDOW_SECONDS} SECOND" in sql


def test_an_unresolvable_service_falls_back_to_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stack that renames nothing, or a compose that cannot answer, still reads."""
    monkeypatch.setattr(
        post.subprocess,
        "run",
        lambda args, **kwargs: _completed(code=1, stdout="", stderr="no such service"),
    )
    post._container_name.cache_clear()

    assert post._container_name("dfe-engine") == "dfe-engine"


# The row each of the hunt's events lands as.
_HUNT_UUIDS = {"matching-000": "u-m0", "other-000": "u-o0", "other-001": "u-o1"}


class _HuntStack:
    """The engine's hunt routes and the ClickHouse reads the hunt claim makes.

    The first window the runner commits ends half a second before the hunt's
    events load, so only a later window scans them: the queued run, or the
    scheduled fire a minute on. `detected` names the events the hunt writes a
    detection for.
    """

    FIRST_WINDOW = int(_Clock.EPOCH)
    SCHEDULED_FIRE = FIRST_WINDOW + 60
    LAST_LOAD_MS = FIRST_WINDOW * 1000 + 500

    def __init__(
        self,
        clock: _Clock,
        *,
        detected: list[str],
        running: bool = True,
        picks_up: bool = True,
        run_status: int = 202,
    ) -> None:
        self.clock = clock
        self.detected = detected
        self.running = running
        self.picks_up = picks_up
        self.run_status = run_status
        self.hunts: dict[str, int | None] = {}
        self.rules: list[dict] = []
        self.sent: list[dict] = []
        self.queued: list[int] = []

    def _last_run(self, name: str) -> int | None:
        last_run = self.hunts[name]
        if last_run is not None and self.clock.time() >= self.SCHEDULED_FIRE:
            return max(last_run, self.SCHEDULED_FIRE)
        return last_run

    def get_json(self, url: str, token: str = "", timeout: int = 30):
        route = url.removeprefix(post.ENGINE_NETWORK_BASE)
        if route == "/hunts/status":
            return 200, {"running": self.running, "runners": int(self.running)}
        if route.startswith("/hunts?search="):
            items = [
                {"name": name, "last_run": self._last_run(name), "run_requested": False}
                for name in self.hunts
            ]
            return 200, {"items": items}
        name = route.removeprefix("/hunts/")
        return (200, {"name": name}) if name in self.hunts else (404, {})

    def post_json(self, url: str, payload: dict, token: str = "", timeout: int = 30):
        route = url.removeprefix(post.ENGINE_NETWORK_BASE)
        if route == "/auth/login":
            return 200, {"access_token": "t"}
        if route == "/rules":
            self.rules.append(payload)
            stored = {"source_db": "dfe", "source_table": "main"}
            return 201, {"rule": stored, "sql_errors": []}
        if route == "/hunts":
            self.hunts[payload["name"]] = self.FIRST_WINDOW if self.picks_up else None
            return 201, payload
        name = route.removeprefix("/hunts/").removesuffix("/run")
        if self.run_status != 202:
            return self.run_status, {"detail": "not implemented"}
        fire = int(self.clock.time())
        self.queued.append(fire)
        self.hunts[name] = fire
        return 202, {"queued": True, "requested_fire": fire, "poll_seconds": 15.0}

    def delete(self, url: str, token: str = "", timeout: int = 10) -> int:
        self.hunts.pop(url.rsplit("/", 1)[-1], None)
        return 204

    def ingest(self, url: str, body: str, *args, **kwargs) -> int:
        self.sent.append(json.loads(body))
        return 200

    def query(self, sql: str) -> str:
        if "toUnixTimestamp64Milli" in sql:
            return str(self.LAST_LOAD_MS)
        if "matched_uuid" in sql:
            rule = self.rules[-1]["name"]
            return "".join(f"{_HUNT_UUIDS[e]}\t{rule}\thigh\n" for e in self.detected)
        return "".join(f"{uuid}\t{label}\n" for label, uuid in _HUNT_UUIDS.items())

    def count(self, sql: str) -> int:
        return len(self.detected)


def _hunt_stack(monkeypatch: pytest.MonkeyPatch, **kwargs) -> _HuntStack:
    """Serve the hunt claim from a stand-in stack, on a clock only its sleeps move."""
    clock = _Clock()
    monkeypatch.setattr(_pipeline, "time", clock)
    monkeypatch.setattr(post, "time", clock)
    stack = _HuntStack(clock, **kwargs)
    monkeypatch.setattr(post, "_resolved_services", lambda: list(post.HUNT_SERVICES))
    monkeypatch.setattr(post, "_api_get_json", stack.get_json)
    monkeypatch.setattr(post, "_api_post_json", stack.post_json)
    monkeypatch.setattr(post, "_api_delete", stack.delete)
    monkeypatch.setattr(post, "http_post", stack.ingest)
    monkeypatch.setattr(post, "ch_query", stack.query)
    monkeypatch.setattr(post, "ch_int", stack.count)
    for name in ("DFE_POST_LOGIN_USER", "DFE_POST_LOGIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(post.ADMIN_PASSWORD_KEY, "a-password")
    return stack


def _hunt_claim() -> post.Claim:
    return post._verify_hunt(
        database="dfe", ingest_url="http://ingest/ingest", marker="m", table="main"
    )


def test_the_hunt_claim_holds_on_exactly_the_matching_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = _hunt_stack(monkeypatch, detected=["matching-000"])

    assert _hunt_claim() == post.HELD
    assert len(stack.queued) == 1
    assert stack.hunts == {}
    assert "set by the queued run" in capsys.readouterr().err


def test_the_hunt_reads_only_its_own_events_not_the_ingest_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console claim counts the ingest marker's rows, so the hunt's carry their own."""
    stack = _hunt_stack(monkeypatch, detected=["matching-000"])

    _hunt_claim()

    assert "= 'm-hunt'" in stack.rules[0]["user_sql"]
    assert {event["_tags"]["marker"] for event in stack.sent} == {"m-hunt"}
    assert len(stack.sent) == sum(len(events) for events in post.HUNT_EVENTS.values())


def test_a_detected_near_miss_fails_the_hunt_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An over-broad rule: every failed login, root or not."""
    _hunt_stack(monkeypatch, detected=["matching-000", "other-000"])

    assert _hunt_claim() == post.Claim(asserted=True, failed=1)
    assert (
        "events the rule must not match were detected: other-000"
        in capsys.readouterr().err
    )


def test_a_missed_matching_event_fails_the_hunt_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hunt_stack(monkeypatch, detected=[])

    assert _hunt_claim() == post.Claim(asserted=True, failed=1)
    assert "matching events with no detection: matching-000" in capsys.readouterr().err


def test_a_run_now_that_does_not_queue_fails_and_the_schedule_is_still_judged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hunt_stack(monkeypatch, detected=["matching-000"], run_status=501)

    assert _hunt_claim() == post.Claim(asserted=True, failed=1)
    err = capsys.readouterr().err
    assert "POST /hunts/m-hunt/run returned HTTP 501" in err
    assert "set by a scheduled fire" in err
    assert "holds exactly the 1 matching event(s)" in err


def test_a_hunt_the_runner_never_takes_up_fails_the_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = _hunt_stack(monkeypatch, detected=[], picks_up=False)

    assert _hunt_claim() == post.Claim(asserted=True, failed=1)
    assert "the runner committed no window for 'm-hunt'" in capsys.readouterr().err
    assert stack.sent == []
    assert stack.hunts == {}


def test_no_live_runner_fails_the_claim_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = _hunt_stack(monkeypatch, detected=[], running=False)

    assert _hunt_claim() == post.Claim(asserted=True, failed=1)
    assert "reported no live hunt runner" in capsys.readouterr().err
    assert stack.rules == []


def test_the_hunt_events_carry_a_match_and_near_misses() -> None:
    """Without both sets the verdict cannot tell an exact rule from an over-broad one."""
    assert post.HUNT_EVENTS[post._detection.MATCHING]
    assert post.HUNT_EVENTS[post._detection.OTHER]
