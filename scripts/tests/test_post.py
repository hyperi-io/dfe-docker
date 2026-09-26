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
deployment dial can take away, the order the claims run in (a new hunt's first
window looks back one cron interval, so it has to be created while this run's
rows are inside it), and that a run which skipped every claim exits non-zero.
"""

from __future__ import annotations

import json

import pytest

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
    assert not post._over_network(_HOST_URL)


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
            "dfe-transform-vrl-filebeat",
            "dfe-ui",
            "hyperdx",
            "otel-collector",
        ],
    )

    assert post._otel_expected_services() == [
        "dfe-engine",
        "dfe-loader",
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


def test_the_hunt_is_created_before_the_slow_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new hunt's first window is one cron interval wide, measured from creation."""
    calls = _stub_claims(monkeypatch)
    _stub_main(monkeypatch)
    monkeypatch.setattr(
        post,
        "_ingest_target",
        lambda *, table: ("dfe-receiver", "http://ingest/ingest"),
    )

    assert post.main() == 0
    assert calls.index("_verify_hunt") < calls.index("_verify_self_monitoring")
    assert calls.index("_verify_hunt") < calls.index("_verify_hyperdx")


def _engine_answers(
    monkeypatch: pytest.MonkeyPatch, *, readyz: dict, schema_status: int
) -> list[str]:
    """Serve /readyz and the schema route from fixed answers; return the paths asked."""
    asked: list[str] = []

    def _get(url, **kwargs):
        asked.append(url)
        if url.endswith("/readyz"):
            return 200, readyz
        return schema_status, {}

    monkeypatch.setattr(post, "http_get_json", _get)
    monkeypatch.setattr(post, "_resolved_services", lambda: [post.ENGINE_SERVICE])
    monkeypatch.setattr(post, "SCHEMA_TIMEOUT_SECONDS", 0.0)
    return asked


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
    def _refused(url, **kwargs):
        raise ConnectionRefusedError(url)

    monkeypatch.setattr(post, "http_get_json", _refused)
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
