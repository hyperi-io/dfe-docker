#  Project:      dfe-docker
#  File:         tests/test_api_put.py
#  Purpose:      Assert the JSON PUT the e2e suite uploads a source's files with
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""dfe-engine takes a program for a source's instance as `PUT /apps/<app>/<instance>/files/<set>/<file>`.

Both PUT helpers are driven against a real HTTP server on loopback, which records the method, body and bearer token it was sent and answers with JSON, with a body that is not JSON or not at all until the test ends.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import _pipeline
import post

# How long a held reply waits for the test to end, past any timeout a test sets
HOLD_SECONDS = 5
ROUTE = "/api/v1/apps/dfe-transform-vrl/e2evrl/files/transforms/stamp_transformed.vrl"


class _Recording(BaseHTTPRequestHandler):
    """Record each request, then answer it as the class attributes say."""

    answer = b'{"size_bytes": 6, "restart_required": []}'
    held = False
    received: list[tuple[str, str, str, bytes]] = []
    released = threading.Event()
    status = 200

    def _record(self) -> None:
        """Keep the method, path, bearer token and body, then answer unless the test holds the reply."""
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append(
            (
                self.command,
                self.path,
                self.headers.get("Authorization", ""),
                self.rfile.read(length),
            )
        )
        if self.held:
            self.released.wait(timeout=HOLD_SECONDS)
            return
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.answer)))
        self.end_headers()
        self.wfile.write(self.answer)

    def do_POST(self) -> None:
        """Record a POST and answer it."""
        self._record()

    def do_PUT(self) -> None:
        """Record a PUT and answer it."""
        self._record()

    def log_message(self, format: str, *args: object) -> None:
        """Keep the server's per-request log line out of the test output."""
        return


@pytest.fixture
def engine_url() -> Iterator[str]:
    """A loopback server standing at the engine's API route, reset for each test."""
    _Recording.answer = b'{"size_bytes": 6, "restart_required": []}'
    _Recording.held = False
    _Recording.received = []
    _Recording.released = threading.Event()
    _Recording.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    threading.Thread(daemon=True, target=server.serve_forever).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}{ROUTE}"
    finally:
        _Recording.released.set()
        server.shutdown()
        server.server_close()


class TestHttpPutJson:
    def test_sends_put_with_the_json_body_and_token(self, engine_url: str) -> None:
        status, body = _pipeline.http_put_json(
            payload={"content": ". = .\n"}, token="tok", url=engine_url
        )

        assert (status, body) == (200, {"size_bytes": 6, "restart_required": []})
        method, path, authorization, sent = _Recording.received[0]
        assert (method, path, authorization) == ("PUT", ROUTE, "Bearer tok")
        assert json.loads(s=sent) == {"content": ". = .\n"}

    def test_an_error_status_is_a_result_with_its_body(self, engine_url: str) -> None:
        _Recording.answer = b'{"detail": "suffix not allowed"}'
        _Recording.status = 422

        status, body = _pipeline.http_put_json(payload={"content": ""}, url=engine_url)

        assert (status, body) == (422, {"detail": "suffix not allowed"})

    def test_a_body_that_is_not_json_comes_back_as_text(self, engine_url: str) -> None:
        _Recording.answer = b"<html>bad gateway</html>"
        _Recording.status = 502

        assert _pipeline.http_put_json(payload={}, url=engine_url) == (
            502,
            "<html>bad gateway</html>",
        )

    def test_a_reply_held_past_the_timeout_raises_once_it_runs_out(
        self, engine_url: str
    ) -> None:
        _Recording.held = True
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            _pipeline.http_put_json(payload={}, timeout=1, url=engine_url)

        assert time.monotonic() - started < HOLD_SECONDS
        assert _Recording.received[0][0] == "PUT"

    def test_post_sends_its_own_method_through_the_same_path(
        self, engine_url: str
    ) -> None:
        _pipeline.http_post_json(payload={"source": "e2evrl"}, url=engine_url)

        assert _Recording.received[0][0] == "POST"


class TestApiPutJson:
    def test_a_host_address_puts_without_docker(self, engine_url: str) -> None:
        status, body = post.api_put_json(
            payload={"content": ". = .\n"}, token="t", url=engine_url
        )

        assert (status, body["size_bytes"]) == (200, 6)
        assert _Recording.received[0][:3] == ("PUT", ROUTE, "Bearer t")

    def test_a_host_address_hands_its_timeout_to_the_request(
        self, engine_url: str
    ) -> None:
        _Recording.held = True
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            post.api_put_json(payload={}, timeout=1, url=engine_url)

        assert time.monotonic() - started < HOLD_SECONDS
