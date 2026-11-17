#  Project:      dfe-docker
#  File:         scripts/tests/test_replay_fetch.py
#  Purpose:      Tests for scripts/replay_fetch.py, the captured-poll server a pack's fetcher sources replay from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/replay_fetch.py: the time shift, the silenced poll, the live feed check, the committed packs it loads, and a real round trip over HTTP."""

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import replay_fetch
from _common import demo_pack_dir

POLLED_MS = 1_794_605_497_000
REPLAY_UNIT = {
    "base_url": "{{ vars.replay_url }}",
    "path": "/air",
    "rows": {"at": "/ac", "decoder": "json_at"},
    "unit": "replay",
}
LIVE_UNIT = {
    "path": "/v2/point/{{ vars.lat }}",
    "query": {"from": "{{ int(window.end) - 600000 }}", "radius": "{{ vars.radius }}"},
    "rows": {"at": "/ac", "decoder": "json_at"},
    "unit": "point",
}


def _source(*, endpoints: list[dict]) -> str:
    """A fetcher source file whose live feed is api.example and whose profile has these endpoints."""
    body = {
        "fetcher": {
            "config": {
                "profile": {
                    "base_url": "{{ vars.api_url }}",
                    "endpoints": endpoints,
                    "headers": {"Accept": "application/json"},
                },
                "vars": {
                    "api_url": "https://api.example",
                    "lat": "-33.9",
                    "radius": "100",
                },
            }
        },
        "source": "air",
    }
    return json.dumps(body)


def _pack(root: Path, *, endpoints: list[dict], lines: list[str]) -> Path:
    """A pack holding one fetcher source `air` and its captured polls."""
    (root / "sources").mkdir(parents=True)
    (root / "sources" / "air.yaml").write_text(
        _source(endpoints=endpoints), encoding="utf-8"
    )
    (root / "events" / "air").mkdir(parents=True)
    (root / "events" / "air" / "demo.log").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )
    return root


def _replay(*, url: str = "http://127.0.0.1:1/air") -> replay_fetch.Replay:
    """Two captured polls, silenced while `url` answers with rows at /ac."""
    polls = [
        (POLLED_MS, {"ac": [{"hex": "a"}], "now": POLLED_MS}),
        (POLLED_MS + 10_000, {"ac": [{"hex": "b"}], "now": POLLED_MS + 10_000}),
    ]
    probe = replay_fetch.Probe(url=url, headers={}, rows_at="/ac")
    return replay_fetch.Replay(
        live=("127.0.0.1", 1), probe=probe, polls=polls, rows_at="/ac", step_ms=10_000
    )


def _closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def _live_feed(*, status: int, payload: bytes) -> Iterator[tuple[str, dict]]:
    """A local live feed answering every GET with `answer`, which a test may change."""
    answer = {"status": status, "payload": payload}

    class Feed(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(answer["status"])
            self.send_header("Content-Length", str(len(answer["payload"])))
            self.end_headers()
            self.wfile.write(answer["payload"])

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v2", answer
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def served() -> Iterator[tuple[replay_fetch.ThreadingHTTPServer, dict]]:
    replays: dict[str, replay_fetch.Replay] = {}
    server = replay_fetch.serve(port=0, replays=replays)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, replays
    finally:
        server.shutdown()
        server.server_close()


def _get(server: replay_fetch.ThreadingHTTPServer, path: str) -> tuple[int, object]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as answer:  # noqa: S310
            return answer.status, json.load(answer)
    except urllib.error.HTTPError as error:
        return error.code, None


class TestShifted:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (POLLED_MS, POLLED_MS + 60_000),
            (POLLED_MS - 86_400_000, POLLED_MS - 86_400_000 + 60_000),
            (POLLED_MS - 86_400_001, POLLED_MS - 86_400_001),
            (230_987_940, 230_987_940),
            (0, 0),
            (True, True),
            (float(POLLED_MS), float(POLLED_MS)),
            ("2026-11-13T21:31:47Z", "2026-11-13T21:31:47Z"),
            (None, None),
        ],
        ids=[
            "poll-time",
            "a-day-before",
            "past-a-day",
            "mmsi",
            "zero",
            "bool",
            "float",
            "text",
            "null",
        ],
    )
    def test_moves_only_epoch_milliseconds_near_the_poll(
        self, expected: object, value: object
    ) -> None:
        body = {"features": [{"properties": {"t": value}}]}

        moved = replay_fetch.shifted(body=body, offset_ms=60_000, polled_ms=POLLED_MS)

        assert moved == {"features": [{"properties": {"t": expected}}]}


class TestEmptied:
    @pytest.mark.parametrize(
        ("pointer", "body", "expected"),
        [
            ("/ac", {"ac": [1, 2], "now": 5}, {"ac": [], "now": 5}),
            ("/features", {"features": [{"a": 1}]}, {"features": []}),
            ("/a/0/b", {"a": [{"b": [1]}]}, {"a": [{"b": []}]}),
            ("/a~1b", {"a/b": [1]}, {"a/b": []}),
            ("", [{"ip_address": "192.0.2.1"}], []),
        ],
        ids=["top", "geojson", "nested", "escaped", "whole-body"],
    )
    def test_blanks_the_rows_and_keeps_the_rest(
        self, body: dict, expected: dict, pointer: str
    ) -> None:
        assert replay_fetch.emptied(body=body, pointer=pointer) == expected
        assert body != expected

    @pytest.mark.parametrize(
        ("pointer", "body", "message"),
        [
            ("ac", {"ac": []}, "not a JSON pointer"),
            ("/n/x", {"n": 3}, "leads into a int"),
        ],
        ids=["relative", "into-a-number"],
    )
    def test_refuses_a_pointer_it_cannot_follow(
        self, body: dict, message: str, pointer: str
    ) -> None:
        with pytest.raises(replay_fetch.ReplayError, match=message):
            replay_fetch.emptied(body=body, pointer=pointer)


class TestPollAt:
    @pytest.mark.parametrize(
        ("elapsed_ms", "hex_code"),
        [(0, "a"), (9_999, "a"), (10_000, "b"), (20_000, "a"), (35_000, "b")],
    )
    def test_serves_the_capture_as_far_in_as_the_replay_has_run(
        self, elapsed_ms: int, hex_code: str
    ) -> None:
        now_ms = POLLED_MS + 3_600_000
        replay = _replay()

        body = replay_fetch.poll_at(
            now_ms=now_ms, replay=replay, started_ms=now_ms - elapsed_ms
        )

        assert isinstance(body, dict)
        assert body["ac"] == [{"hex": hex_code}]
        assert body["now"] == now_ms


class TestLoadReplays:
    def test_reads_the_committed_demo_pack(self) -> None:
        replays = replay_fetch.load_replays(root=demo_pack_dir(pack=""))

        assert sorted(replays) == ["feodo-c2"]
        assert replays["feodo-c2"].rows_at == ""
        assert replays["feodo-c2"].live == ("feodotracker.abuse.ch", 443)
        assert replays["feodo-c2"].probe.url == (
            "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
        )
        body = replays["feodo-c2"].polls[0][1]
        assert isinstance(body, list)
        assert body
        assert replay_fetch.emptied(body=body, pointer="") == []

    def test_reads_the_committed_theatre_pack(self) -> None:
        replays = replay_fetch.load_replays(root=demo_pack_dir(pack="theatre"))

        assert sorted(replays) == ["adsb-tracks", "ais-tracks"]
        assert replays["adsb-tracks"].rows_at == "/ac"
        assert replays["adsb-tracks"].live == ("api.adsb.lol", 443)
        assert replays["ais-tracks"].rows_at == "/features"
        assert replays["ais-tracks"].live == ("meri.digitraffic.fi", 443)
        assert replays["adsb-tracks"].probe.url == (
            "https://api.adsb.lol/v2/point/-33.94/151.18/100"
        )
        assert replays["adsb-tracks"].probe.rows_at == "/ac"
        # The query value that needs the poll window is left out, the rest is asked
        assert replays["ais-tracks"].probe.url == (
            "https://meri.digitraffic.fi/api/ais/v1/locations"
            "?latitude=60.1&longitude=24.95&radius=60"
        )
        assert replays["ais-tracks"].probe.headers["Accept-Encoding"] == "gzip"
        for replay in replays.values():
            # Ten minutes of polls ten seconds apart
            assert replay.step_ms == pytest.approx(10_000, abs=1_000)
            assert len(replay.polls) >= 55

    def test_asks_the_live_unit_with_what_vars_can_fill(self, tmp_path: Path) -> None:
        line = f'{{"polled_ms": {POLLED_MS}, "body": {{}}}}'
        root = _pack(tmp_path, endpoints=[REPLAY_UNIT, LIVE_UNIT], lines=[line])

        replays = replay_fetch.load_replays(root=root)

        assert replays["air"].probe == replay_fetch.Probe(
            url="https://api.example/v2/point/-33.9?radius=100",
            headers={"Accept": "application/json"},
            rows_at="/ac",
        )

    def test_skips_a_fetcher_with_no_capture(self, tmp_path: Path) -> None:
        root = _pack(tmp_path, endpoints=[REPLAY_UNIT, LIVE_UNIT], lines=["{}"])
        (root / "events" / "air" / "demo.log").unlink()

        assert replay_fetch.load_replays(root=root) == {}

    @pytest.mark.parametrize(
        ("endpoints", "lines", "message"),
        [
            ([REPLAY_UNIT, LIVE_UNIT], [], "holds no captured polls"),
            ([REPLAY_UNIT, LIVE_UNIT], ["{"], "line 1 is not a captured poll"),
            (
                [REPLAY_UNIT, LIVE_UNIT],
                ['{"body": {}}'],
                "line 1 is not a captured poll",
            ),
            ([], [f'{{"polled_ms": {POLLED_MS}, "body": {{}}}}'], "no 'replay' unit"),
            (
                [REPLAY_UNIT],
                [f'{{"polled_ms": {POLLED_MS}, "body": {{}}}}'],
                "no live unit to ask",
            ),
            (
                [REPLAY_UNIT, {**LIVE_UNIT, "path": "/{{ int(window.end) }}"}],
                [f'{{"polled_ms": {POLLED_MS}, "body": {{}}}}'],
                "need more than `vars`",
            ),
        ],
        ids=[
            "empty",
            "malformed",
            "no-poll-time",
            "no-replay-unit",
            "no-live-unit",
            "unfillable-path",
        ],
    )
    def test_refuses_a_replay_it_cannot_serve(
        self, endpoints: list[dict], lines: list[str], message: str, tmp_path: Path
    ) -> None:
        root = _pack(tmp_path, endpoints=endpoints, lines=lines)

        with pytest.raises(replay_fetch.ReplayError, match=message):
            replay_fetch.load_replays(root=root)


class TestServe:
    @pytest.mark.parametrize(
        ("status", "payload", "silent"),
        [
            (200, b'{"ac": [{"hex": "z"}]}', True),
            (200, b'{"ac": []}', False),
            (200, b'{"msg": "No error"}', False),
            (200, b"not json", False),
            (503, b'{"ac": [{"hex": "z"}]}', False),
            (None, b"", False),
        ],
        ids=[
            "answers-with-rows",
            "answers-empty",
            "answers-without-the-rows-key",
            "answers-text",
            "answers-an-error",
            "unreachable",
        ],
    )
    def test_serves_the_capture_unless_the_live_feed_has_rows(
        self,
        served: tuple[replay_fetch.ThreadingHTTPServer, dict],
        silent: bool,
        status: int | None,
        payload: bytes,
    ) -> None:
        server, replays = served
        # None stands for a feed nothing listens for
        with _live_feed(status=status or 200, payload=payload) as (url, _):
            replays["air"] = _replay(
                url=url if status else f"http://127.0.0.1:{_closed_port()}/v2"
            )

            code, body = _get(server, "/air")

        assert code == 200
        assert isinstance(body, dict)
        assert body["ac"] == ([] if silent else [{"hex": "a"}])
        assert body["now"] > POLLED_MS

    def test_asks_the_live_feed_again_on_the_wait_its_last_answer_sets(
        self, served: tuple[replay_fetch.ThreadingHTTPServer, dict]
    ) -> None:
        server, replays = served
        with _live_feed(status=200, payload=b'{"ac": []}') as (url, answer):
            replays["air"] = _replay(url=url)
            assert _get(server, "/air")[1]["ac"] == [{"hex": "a"}]

            # Rows appear, but the empty answer holds until its wait has run out
            answer["payload"] = b'{"ac": [{"hex": "z"}]}'
            assert _get(server, "/air")[1]["ac"] == [{"hex": "a"}]

            replays["air"].checked -= replay_fetch.NO_ROWS_CHECK_SECONDS
            assert _get(server, "/air")[1]["ac"] == []

            # The rows go again, and the longer wait for a feed with rows holds the silence
            answer["payload"] = b'{"ac": []}'
            replays["air"].checked -= replay_fetch.NO_ROWS_CHECK_SECONDS
            assert _get(server, "/air")[1]["ac"] == []

            replays["air"].checked -= replay_fetch.LIVE_ROWS_CHECK_SECONDS
            assert _get(server, "/air")[1]["ac"] == [{"hex": "a"}]

    def test_refuses_a_source_it_holds_no_capture_for(
        self, served: tuple[replay_fetch.ThreadingHTTPServer, dict]
    ) -> None:
        server, _ = served

        assert _get(server, "/sea") == (404, None)
