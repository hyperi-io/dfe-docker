#  Project:      dfe-docker
#  File:         tests/test_ingest_send.py
#  Purpose:      Assert the e2e suite's data file is sent concurrently, each answer kept
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The receiver holds each answer until the next hop has the record.

A stand-in receiver that holds every request for a fixed time shows the cost:
one sender at a time pays the hold per event, concurrent senders share it.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import _pipeline

HOLD_SECONDS = 0.2
EVENTS = 10


class _HoldingReceiver(BaseHTTPRequestHandler):
    """Answers each POST after HOLD_SECONDS, 503 for a body asking to be refused.

    It counts the requests it is holding at once, and keeps the most it held.
    """

    lock = threading.Lock()
    holding = 0
    most_held = 0
    authorizations: list[str] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.lock:
            type(self).authorizations.append(self.headers.get("Authorization", ""))
            type(self).holding += 1
            type(self).most_held = max(type(self).most_held, type(self).holding)
        time.sleep(HOLD_SECONDS)
        with self.lock:
            type(self).holding -= 1
        self.send_response(503 if b"refuse" in body else 200)
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The receiver's ingest token, so no test execs into a running stack."""
    monkeypatch.setattr(_pipeline, "ingest_token", lambda: "tok-1")


@pytest.fixture
def receiver_url():
    _HoldingReceiver.holding = 0
    _HoldingReceiver.most_held = 0
    _HoldingReceiver.authorizations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HoldingReceiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/ingest"
    finally:
        server.shutdown()
        server.server_close()


def _timed(url: str, workers: int) -> tuple[list[int], float]:
    bodies = [f'{{"n": {n}}}' for n in range(EVENTS)]
    started = time.monotonic()
    statuses = _pipeline.post_all(url, bodies, workers=workers)
    return statuses, time.monotonic() - started


def test_one_sender_at_a_time_pays_the_hold_per_event(receiver_url: str) -> None:
    statuses, elapsed = _timed(receiver_url, workers=1)

    assert statuses == [200] * EVENTS
    assert elapsed >= EVENTS * HOLD_SECONDS
    assert _HoldingReceiver.most_held == 1


def test_concurrent_senders_share_the_hold(receiver_url: str) -> None:
    statuses, _ = _timed(receiver_url, workers=EVENTS)

    assert statuses == [200] * EVENTS
    assert _HoldingReceiver.most_held > 1


def test_each_answer_stays_with_its_own_request(receiver_url: str) -> None:
    bodies = ['{"n": 0}', '{"refuse": 1}', '{"n": 2}']

    assert _pipeline.post_all(receiver_url, bodies, workers=3) == [200, 503, 200]
    # A receiver an ingest credential put in bearer mode refuses a post with no token.
    assert _HoldingReceiver.authorizations == ["Bearer tok-1"] * 3


def test_a_request_nothing_answers_reports_status_zero() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), _HoldingReceiver) as closed:
        port = closed.server_address[1]

    assert _pipeline.post_all(f"http://127.0.0.1:{port}/", ["{}"], workers=1) == [0]


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("edgestream-dev1:abc123\n", "abc123"),
        ("a:one\nb:two\n", "one"),
        ("\n", ""),
        ("", ""),
        ("broken-line\nid:t2\n", "t2"),
        ("id:\n", ""),
    ],
    ids=["one", "first-of-two", "no-credential", "empty", "skips-junk", "no-token"],
)
def test_the_sender_takes_the_first_token_the_listener_lists(
    text: str, token: str
) -> None:
    assert _pipeline.bearer_token(text) == token


@pytest.mark.parametrize(
    ("answers", "tokens", "execs"),
    [
        (["rx1\n", "id:t1\n", "rx1\n", "id:t2\n"], ["t1", "t1"], 2),
        (["", "rx1\n", "id:t1\n"], ["", "t1"], 3),
        (["rx1\n", "", "rx1\n", "id:t1\n"], ["", "t1"], 4),
    ],
    ids=["found-once", "no-receiver-then-found", "failed-exec-then-found"],
)
def test_an_ingest_token_is_kept_once_found_and_asked_again_when_empty(
    monkeypatch: pytest.MonkeyPatch, answers: list[str], tokens: list[str], execs: int
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(_pipeline, "_ingest_token_found", [])
    replies = iter(answers)
    calls: list[tuple[str, ...]] = []

    def _docker(*args: str) -> str:
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(_pipeline, "_docker_output", _docker)

    assert [_pipeline.ingest_token(), _pipeline.ingest_token()] == tokens
    assert len(calls) == execs
