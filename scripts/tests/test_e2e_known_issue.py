#  Project:      dfe-docker
#  File:         tests/test_e2e_known_issue.py
#  Purpose:      Assert a known issue absorbs only its own failures, and archivers are stopped with a real compose action
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`known_issue` turns the failures an issue describes into one skip, and nothing else.

A harness failure (a failed unpause, a failed start, an unhealthy service) must still fail the run. The archive fallback stops each archiver through compose, so the action has to reach it as a command and not be split into letters.
"""

from types import SimpleNamespace

import pytest

import _outage
import test_e2e

ISSUE = "hyperi-io/dfe-receiver#278"
CUT = "[t] 16/22282 requests got no HTTP answer (during: none (RemoteDisconnected) x16)"
LOST = "[t] 3/900 accepted records never reached 'dfe.main' within 300s"
UNPAUSE = "[t] could not unpause 'dfe-loader': boom"


def _test() -> SimpleNamespace:
    return SimpleNamespace(name="t", outage=SimpleNamespace(known_issue=ISSUE))


def _run(*, labels: list[str]) -> test_e2e.TestContext:
    ctx = test_e2e.TestContext()
    for label in ("[t] earlier failure",):
        test_e2e.mark_fail(ctx=ctx, label=label)
    before = len(ctx.failures)
    for label in labels:
        test_e2e.mark_fail(ctx=ctx, label=label)
    test_e2e.settle_known_issue(ctx=ctx, failures_before=before, test=_test())
    return ctx


@pytest.mark.parametrize("labels", [[CUT], [LOST], [CUT, LOST]])
def test_the_failures_an_issue_describes_become_one_skip(labels: list[str]) -> None:
    ctx = _run(labels=labels)

    assert ctx.skipped == 1
    assert ctx.failed == 1
    assert ctx.failures == ["[t] earlier failure"]


@pytest.mark.parametrize(
    "labels", [[UNPAUSE], [CUT, UNPAUSE], [UNPAUSE, "[t] 'dfe-loader' not healthy"]]
)
def test_any_other_failure_still_fails_the_run(labels: list[str]) -> None:
    ctx = _run(labels=labels)

    assert (UNPAUSE in ctx.failures) or ("[t] 'dfe-loader' not healthy" in ctx.failures)
    assert CUT not in ctx.failures
    assert ctx.failed == 1 + len([m for m in labels if m != CUT])


def test_a_clean_run_fails_so_the_key_is_removed() -> None:
    ctx = _run(labels=[])

    assert ctx.skipped == 0
    assert ctx.failed == 2
    assert "looks fixed" in ctx.failures[-1]


def test_a_stop_reaches_compose_as_one_action(monkeypatch) -> None:
    def run_cmd(args, capture=False, cwd=None):
        ran.append(list(args))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    ran = []
    monkeypatch.setattr(test_e2e, "run_cmd", run_cmd)
    ctx = test_e2e.TestContext()

    assert test_e2e.lifecycle_step(
        action=("stop",), ctx=ctx, service="dfe-archiver", test=_test()
    )
    assert ran == [["docker", "compose", "--profile", "*", "stop", "dfe-archiver"]]


def test_a_short_archive_stops_every_archiver_gracefully(monkeypatch) -> None:
    def _archived(*, test):
        return {1: 1}

    def _never(*args, **kwargs):
        return None

    def run_cmd(args, capture=False, cwd=None):
        stopped.append(list(args))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    stopped = []
    plan = _outage.outage_plan(
        default_interval=0.0,
        default_seconds=60,
        default_workers=1,
        outage={
            "service": "dfe-archiver",
            "archive": {"services": ["dfe-archiver", "dfe-archiver-2"], "path": "/a"},
        },
    )
    test = SimpleNamespace(marker="m", name="t", outage=plan, table="main")
    sent = [
        _outage.Sent(
            error="",
            phase="during",
            seconds=0.1,
            seq=1,
            source="main",
            started=1.0,
            status=200,
        )
    ]
    monkeypatch.setattr(test_e2e, "run_cmd", run_cmd)
    monkeypatch.setattr(test_e2e, "poll_until", _never)
    monkeypatch.setattr(test_e2e, "archived_records", _archived)

    ctx = test_e2e.TestContext()
    test_e2e.verify_archived(ctx=ctx, sent=sent, test=test)

    assert [call[-2:] for call in stopped] == [
        ["stop", "dfe-archiver"],
        ["stop", "dfe-archiver-2"],
    ]
    assert ctx.failed == 0
    assert ctx.passed == 1


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {"code": "value_error", "detail": [{"input": {"password": "SECRET-pw"}}]},
            "HTTP 422 (value_error)",
        ),
        ({"detail": "x"}, "HTTP 422 (no code)"),
        ("<html>proxy error</html>", "HTTP 422 (no code)"),
    ],
    ids=["code", "no-code", "not-json"],
)
def test_a_refusal_is_reported_by_status_and_code_never_its_body(
    answer: object, expected: str
) -> None:
    reported = test_e2e.problem_code(answer=answer, status=422)

    assert reported == expected
    assert "SECRET" not in reported
