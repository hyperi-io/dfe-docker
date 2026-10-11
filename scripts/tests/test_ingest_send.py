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
    """Answers each POST after HOLD_SECONDS, 503 for a body asking to be refused."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(HOLD_SECONDS)
        self.send_response(503 if b"refuse" in body else 200)
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def receiver_url():
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


def test_concurrent_senders_share_the_hold(receiver_url: str) -> None:
    statuses, elapsed = _timed(receiver_url, workers=EVENTS)

    assert statuses == [200] * EVENTS
    assert elapsed < EVENTS * HOLD_SECONDS / 2


def test_each_answer_stays_with_its_own_request(receiver_url: str) -> None:
    bodies = ['{"n": 0}', '{"refuse": 1}', '{"n": 2}']

    assert _pipeline.post_all(receiver_url, bodies, workers=3) == [200, 503, 200]


def test_a_request_nothing_answers_reports_status_zero() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), _HoldingReceiver) as closed:
        port = closed.server_address[1]

    assert _pipeline.post_all(f"http://127.0.0.1:{port}/", ["{}"], workers=1) == [0]
