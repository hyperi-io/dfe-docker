#  Project:      dfe-docker
#  File:         tests/test_run_test_teardown.py
#  Purpose:      Assert an e2e test takes its stack down however it ends
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""run_test tears the stack down in a finally, so an exception leaves nothing running.

The harness imports PyYAML, which the unit-test job does not install, so these
skip there and run wherever `make test-e2e` can.
"""

import pytest

pytest.importorskip("yaml")

import test_e2e  # noqa: E402


def _case() -> test_e2e.TestCase:
    return test_e2e.TestCase(
        name="unit",
        profile="grpc-receiver",
        compose_profiles=[],
        services={},
        data_file="tests/e2e/data/events.jsonl",
        database="dfe",
        table="main",
        marker="e2e-1-unit",
    )


def _record_teardown(monkeypatch: pytest.MonkeyPatch) -> list:
    calls = []
    monkeypatch.setattr(
        test_e2e, "stack_down", lambda keep_services=None: calls.append(keep_services)
    )
    return calls


def test_an_exception_mid_test_takes_the_whole_stack_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_teardown(monkeypatch)

    def _explode(*_args: object) -> None:
        raise RuntimeError("harness defect after stack_up")

    monkeypatch.setattr(test_e2e, "exercise_stack", _explode)

    with pytest.raises(RuntimeError):
        test_e2e.run_test(test_e2e.TestContext(), "ci", _case(), ["clickhouse"])

    assert calls == [None]


def test_an_exception_keeps_a_service_that_was_running_before_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_teardown(monkeypatch)

    def _explode(*_args: object) -> None:
        raise RuntimeError("harness defect after stack_up")

    monkeypatch.setattr(test_e2e, "exercise_stack", _explode)

    with pytest.raises(RuntimeError):
        test_e2e.run_test(
            test_e2e.TestContext(), "ci", _case(), ["clickhouse"], ["clickhouse"]
        )

    assert calls == [["clickhouse"]]


def test_a_test_that_ends_keeps_only_the_persistent_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_teardown(monkeypatch)
    monkeypatch.setattr(test_e2e, "exercise_stack", lambda *_args: None)

    test_e2e.run_test(test_e2e.TestContext(), "ci", _case(), ["clickhouse"])

    assert calls == [["clickhouse"]]


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path, *, running: list[str]) -> list:
    """Run main over one passing test, with `running` up before the run starts."""
    config = tmp_path / "e2e-tests.yaml"
    config.write_text(
        "global:\n  persistent_services:\n    - clickhouse\n"
        "tests:\n  - name: unit\n    profile: grpc-receiver\n",
        encoding="utf-8",
    )
    calls = _record_teardown(monkeypatch)
    monkeypatch.setattr(test_e2e, "TEST_CONFIG", config)
    monkeypatch.setattr(test_e2e, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(test_e2e, "parse_args", lambda: _Args())
    monkeypatch.setattr(test_e2e, "require_command", lambda _name: None)
    monkeypatch.setattr(test_e2e, "stack_is_up", lambda: False)
    monkeypatch.setattr(test_e2e, "resolve_test_case", lambda *_args: _case())
    monkeypatch.setattr(test_e2e, "build_images", lambda *_args: None)
    monkeypatch.setattr(test_e2e, "exercise_stack", lambda *_args: None)
    monkeypatch.setattr(
        test_e2e,
        "container_state",
        lambda service: _Running() if service in running else None,
    )
    test_e2e.main()
    return calls


class _Args:
    tests: list[str] = []
    outages = False


class _Running:
    status = "running"


def test_a_run_takes_down_the_clickhouse_it_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = _run_main(monkeypatch, tmp_path, running=[])

    assert calls == [["clickhouse"], None]


def test_a_run_leaves_up_the_clickhouse_it_found_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = _run_main(monkeypatch, tmp_path, running=["clickhouse"])

    assert calls == [["clickhouse"], ["clickhouse"]]
