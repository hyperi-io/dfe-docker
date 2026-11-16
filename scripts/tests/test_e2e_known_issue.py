#  Project:      dfe-docker
#  File:         tests/test_e2e_known_issue.py
#  Purpose:      Assert a known issue absorbs only its own failures, and archivers are stopped with a real compose action
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`known_issue` turns the failures an issue describes into one skip, and nothing else.

A harness failure (a failed unpause, a failed start, an unhealthy service) must
still fail the run. The archive fallback stops each archiver through compose, so
the action has to reach it as a command and not be split into letters.
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


def _run(labels: list[str]) -> test_e2e.TestContext:
    ctx = test_e2e.TestContext()
    for label in ("[t] earlier failure",):
        test_e2e.mark_fail(ctx, label)
    before = len(ctx.failures)
    for label in labels:
        test_e2e.mark_fail(ctx, label)
    test_e2e.settle_known_issue(ctx, _test(), before)
    return ctx


@pytest.mark.parametrize("labels", [[CUT], [LOST], [CUT, LOST]])
def test_the_failures_an_issue_describes_become_one_skip(labels: list[str]) -> None:
    ctx = _run(labels)

    assert ctx.skipped == 1
    assert ctx.failed == 1
    assert ctx.failures == ["[t] earlier failure"]


@pytest.mark.parametrize(
    "labels", [[UNPAUSE], [CUT, UNPAUSE], [UNPAUSE, "[t] 'dfe-loader' not healthy"]]
)
def test_any_other_failure_still_fails_the_run(labels: list[str]) -> None:
    ctx = _run(labels)

    assert UNPAUSE in ctx.failures or "[t] 'dfe-loader' not healthy" in ctx.failures
    assert CUT not in ctx.failures
    assert ctx.failed == 1 + len([m for m in labels if m != CUT])


def test_a_clean_run_fails_so_the_key_is_removed() -> None:
    ctx = _run([])

    assert ctx.skipped == 0
    assert ctx.failed == 2
    assert "looks fixed" in ctx.failures[-1]


def test_a_stop_reaches_compose_as_one_action(monkeypatch) -> None:
    ran: list[list[str]] = []

    def run_cmd(args, capture=False, cwd=None):
        ran.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_e2e, "run_cmd", run_cmd)
    ctx = test_e2e.TestContext()

    assert test_e2e.lifecycle_step(ctx, _test(), ("stop",), "dfe-archiver")
    assert ran == [["docker", "compose", "--profile", "*", "stop", "dfe-archiver"]]


def test_a_short_archive_stops_every_archiver_gracefully(monkeypatch) -> None:
    stopped: list[list[str]] = []

    def run_cmd(args, capture=False, cwd=None):
        stopped.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    plan = _outage.outage_plan(
        {
            "service": "dfe-archiver",
            "archive": {"services": ["dfe-archiver", "dfe-archiver-2"], "path": "/a"},
        },
        60,
        default_workers=1,
        default_interval=0.0,
    )
    test = SimpleNamespace(name="t", outage=plan, table="main", marker="m")
    sent = [
        _outage.Sent(
            seq=1,
            source="main",
            phase="during",
            status=200,
            error="",
            started=1.0,
            seconds=0.1,
        )
    ]
    monkeypatch.setattr(test_e2e, "run_cmd", run_cmd)
    monkeypatch.setattr(test_e2e, "poll_until", lambda *a, **k: None)
    monkeypatch.setattr(test_e2e, "archived_records", lambda test: {1: 1})

    ctx = test_e2e.TestContext()
    test_e2e.verify_archived(ctx, test, sent)

    assert [call[-2:] for call in stopped] == [
        ["stop", "dfe-archiver"],
        ["stop", "dfe-archiver-2"],
    ]
    assert ctx.failed == 0
    assert ctx.passed == 1
