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


def test_a_test_that_ends_keeps_only_the_persistent_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_teardown(monkeypatch)
    monkeypatch.setattr(test_e2e, "exercise_stack", lambda *_args: None)

    test_e2e.run_test(test_e2e.TestContext(), "ci", _case(), ["clickhouse"])

    assert calls == [["clickhouse"]]
