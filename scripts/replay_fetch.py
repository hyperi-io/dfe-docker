#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/replay_fetch.py
#  Purpose:      Serve a demo pack's captured fetcher polls to dfe-fetcher, so a host with no egress still fills the pack's fetcher tables
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Serve a demo pack's captured polls over HTTP on the stack's own network.

Each fetcher source under the pack's sources/ with an events/<source>/demo.log is served at GET /<source>. A demo.log line is one captured poll, `{"polled_ms": ..., "body": ...}`, and the poll served is the one captured as far into the capture as the time since this started, round again at the end. Every integer in the body within a day of its poll time is an epoch-millisecond time and is moved forward by the age of the capture, so the reports read as current.

The source's `replay` unit polls this and its live unit polls the feed. While the live feed answers with rows at the live unit's `rows.at`, a replayed poll answers with no rows at the `replay` unit's `rows.at`, so live and replayed reports never land together. A feed that is down, answers an error or answers with fewer rows than half the captured polls' median leaves the captured poll to land, so a pack never depends on a third-party API having the traffic its hunts, dashboards and searches read. Sydney's night curfew leaves a quiet feed with a handful of aircraft and none of the flights they select.

The feed is asked with the live unit's own request, minus any query value that needs more than `vars`, at most once every LIVE_ROWS_CHECK_SECONDS while it has rows and once every NO_ROWS_CHECK_SECONDS while it has none. While a feed has rows that spends 48 requests a day, which with the live unit's 432 stays inside the 500 adsb.lol allows an address with no key.

The published pack's demo-replay service runs it from the dfe-docker image, whose PyYAML reads the source files.
"""

import argparse
import gzip
import http.client
import json
import re
import sys
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

# Bytecode goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import _print, demo_pack_dir, demo_pack_names  # noqa: E402

PORT = 8080
DAY_MS = 86_400_000
REPLAY_UNIT = "replay"
LIVE_ROWS_CHECK_SECONDS = 1800.0
NO_ROWS_CHECK_SECONDS = 300.0
PROBE_TIMEOUT_SECONDS = 5.0
# Where a replay unit the file leaves out is served from
REPLAY_URL = f"http://demo-replay:{PORT}"
VARS_TOKEN = re.compile(r"\{\{\s*vars\.(\w+)\s*\}\}")


class ReplayError(Exception):
    """A pack whose replay cannot be served, with what to change."""


@dataclass(frozen=True, slots=True)
class Probe:
    """The request that asks a live feed whether it has rows, and where they sit in its answer."""

    url: str
    headers: Mapping[str, str]
    rows_at: str


@dataclass(slots=True)
class Replay:
    """One source's captured polls, where its rows sit and the live feed that silences it."""

    live: tuple[str, int]
    probe: Probe
    polls: list[tuple[int, object]]
    rows_at: str
    step_ms: int
    min_live_rows: int = 1
    checked: float = field(default=float("-inf"))
    live_rows: bool = False


def _at(*, body: object, pointer: str) -> object:
    """The value at JSON pointer `pointer` in `body`, the whole body for the empty pointer, None where the body has no such place."""
    node = body
    for part in _pointer_parts(pointer=pointer):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return node


def _has_rows(*, minimum: int = 1, probe: Probe) -> bool:
    """Whether the live feed answers now with at least `minimum` rows, False for any failure to answer."""
    request = urllib.request.Request(probe.url, headers=dict(probe.headers))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as answer:  # noqa: S310
            raw = answer.read()
            if answer.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        rows = _at(body=json.loads(raw), pointer=probe.rows_at)
    except (OSError, EOFError, ValueError, http.client.HTTPException):
        return False
    return isinstance(rows, list) and len(rows) >= max(1, minimum)


def _live_host(*, body: Mapping, name: str) -> tuple[str, int]:
    """The host and port behind a fetcher source's `vars.api_url`."""
    url = urlsplit(str(body["fetcher"]["config"].get("vars", {}).get("api_url", "")))
    if not (url.hostname):
        raise ReplayError(f"{name}.yaml sets no `vars.api_url` naming its live feed")
    return url.hostname, (url.port) or (443 if url.scheme == "https" else 80)


def _live_probe(*, body: Mapping, name: str) -> Probe:
    """The request that asks a fetcher source's live unit whether the feed has rows."""
    config = body["fetcher"]["config"]
    profile = config["profile"]
    variables = config.get("vars", {})
    for endpoint in profile.get("endpoints", []):
        if endpoint.get("unit") == REPLAY_UNIT:
            continue
        base = _render(
            text=str(endpoint.get("base_url", profile.get("base_url", ""))),
            variables=variables,
        )
        path = _render(text=str(endpoint.get("path", "")), variables=variables)
        if (
            (base is None)
            or (path is None)
            or (urlsplit(base).scheme not in {"http", "https"})
        ):
            raise ReplayError(
                f"{name}.yaml's live unit {endpoint.get('unit')!r} has a base_url and "
                "path that need more than `vars` and an http(s) scheme"
            )
        values = {
            key: _render(text=str(value), variables=variables)
            for key, value in endpoint.get("query", {}).items()
        }
        query = urlencode(
            {key: value for key, value in values.items() if value is not None}
        )
        rows_at = _rows_at(endpoint=endpoint)
        _pointer_parts(pointer=rows_at)
        return Probe(
            headers={
                str(key): str(value)
                for key, value in profile.get("headers", {}).items()
            },
            rows_at=rows_at,
            url=f"{base.rstrip('/')}{path}{'?' + query if query else ''}",
        )
    raise ReplayError(f"{name}.yaml has a {REPLAY_UNIT!r} unit and no live unit to ask")


def _live_serves_rows(*, replay: Replay) -> bool:
    """Whether the live feed had rows at its last check, which repeats on a longer wait while it has them."""
    now = time.monotonic()
    wait = NO_ROWS_CHECK_SECONDS if not (replay.live_rows) else LIVE_ROWS_CHECK_SECONDS
    if now - replay.checked >= wait:
        replay.live_rows = _has_rows(minimum=replay.min_live_rows, probe=replay.probe)
        replay.checked = now
    return replay.live_rows


def _pointer_parts(*, pointer: str) -> list[str]:
    """The keys and indexes a JSON pointer walks through."""
    if pointer == "":
        return []
    if not (pointer.startswith("/")):
        raise ReplayError(f"rows.at {pointer!r} is not a JSON pointer to the rows")
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]
    ]


def _polls(*, path: Path) -> list[tuple[int, object]]:
    """The captured polls in one demo.log, in capture order."""
    polls = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            entry = json.loads(line)
            polls.append((int(entry["polled_ms"]), entry["body"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ReplayError(
                f"{str(path)!r} line {number} is not a captured poll, "
                f'{{"polled_ms": ..., "body": ...}}: {error}'
            ) from error
    if not (polls):
        raise ReplayError(f"{str(path)!r} holds no captured polls")
    return polls


def _render(*, text: str, variables: Mapping) -> str | None:
    """`text` with each `{{ vars.name }}` filled in, None where it holds any other template or an unset name."""

    def value(hit: re.Match) -> str:
        return str(variables[hit.group(1)])

    try:
        filled = VARS_TOKEN.sub(value, text)
    except KeyError:
        return None
    return None if "{{" in filled else filled


def _replay_rows_at(*, body: Mapping, name: str) -> str:
    """Where a fetcher source's `replay` unit reads its rows, the whole body when it sets no `at`, which only a `json_array` unit may do."""
    profile = body["fetcher"]["config"]["profile"]
    for endpoint in profile.get("endpoints", []):
        if endpoint.get("unit") == REPLAY_UNIT:
            return _rows_at(endpoint=endpoint)
    raise ReplayError(
        f"{name}.yaml has events to replay and no {REPLAY_UNIT!r} unit to poll them. "
        "Add one whose base_url is `{{ vars.replay_url }}`"
    )


def _rows_at(*, endpoint: Mapping) -> str:
    """Where a unit's rows sit, the JSON pointer in `rows.at`. Only a `json_array` unit has none, because its whole body is the rows."""
    rows = endpoint.get("rows", {})
    at = str(rows.get("at", ""))
    if at == "" and rows.get("decoder") != "json_array":
        raise ReplayError(
            f"rows.at {at!r} is not a JSON pointer to the rows of unit {endpoint.get('unit')!r}, "
            "and only a json_array decoder reads the whole body as its rows"
        )
    return at


def _median_rows(*, polls: list[tuple[int, object]], pointer: str) -> int:
    """The median number of rows a captured poll holds at JSON pointer `pointer`."""
    counts = sorted(
        len(rows) if isinstance(rows := _at(body=body, pointer=pointer), list) else 0
        for _, body in polls
    )
    return counts[len(counts) // 2]


def _step_ms(*, polls: list[tuple[int, object]]) -> int:
    """The median gap between captured polls, one second for a single poll."""
    gaps = sorted(
        later[0] - earlier[0] for earlier, later in zip(polls, polls[1:], strict=False)
    )
    return max(1, gaps[len(gaps) // 2]) if gaps else 1000


def emptied(*, body: object, pointer: str) -> object:
    """`body` with the value at JSON pointer `pointer` replaced by an empty array."""
    parts = _pointer_parts(pointer=pointer)
    if not (parts):
        return []
    copy = json.loads(json.dumps(body))
    node = copy
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, list):
        node[int(parts[-1])] = []
    elif isinstance(node, dict):
        node[parts[-1]] = []
    else:
        raise ReplayError(f"rows.at {pointer!r} leads into a {type(node).__name__}")
    return copy


def load_replays(*, root: Path) -> dict[str, Replay]:
    """Each fetcher source under root/sources with captured polls under root/events, by name."""
    # PyYAML is in the image and the tests, not a bare python3
    import yaml

    replays = {}
    for path in sorted((root / "sources").glob("*.yaml")):
        log = root / "events" / path.stem / "demo.log"
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        fetches = isinstance(body, dict) and isinstance(body.get("fetcher"), dict)
        if not (fetches) or not (log.is_file()):
            continue
        body = with_replay_unit(body=body)
        polls = _polls(path=log)
        live = _live_host(body=body, name=path.stem)
        rows_at = _replay_rows_at(body=body, name=path.stem)
        probe = _live_probe(body=body, name=path.stem)
        replays[path.stem] = Replay(
            live=live,
            polls=polls,
            probe=probe,
            rows_at=rows_at,
            step_ms=_step_ms(polls=polls),
            min_live_rows=_median_rows(polls=polls, pointer=rows_at) // 2,
        )
    return replays


def poll_at(*, now_ms: int, replay: Replay, started_ms: int) -> object:
    """The captured poll this far into the replay, its times moved to now."""
    index = ((now_ms - started_ms) // replay.step_ms) % len(replay.polls)
    polled_ms, body = replay.polls[index]
    return shifted(body=body, offset_ms=now_ms - polled_ms, polled_ms=polled_ms)


def serve(*, port: int, replays: dict[str, Replay]) -> ThreadingHTTPServer:
    """An HTTP server answering GET /<source> with that source's poll for now."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            name = urlsplit(self.path).path.strip("/")
            replay = replays.get(name)
            if replay is None:
                self.send_error(404, f"no replay for {name!r}")
                return
            body = poll_at(
                now_ms=int(time.time() * 1000), replay=replay, started_ms=started_ms
            )
            if _live_serves_rows(replay=replay):
                body = emptied(body=body, pointer=replay.rows_at)
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    started_ms = int(time.time() * 1000)
    return ThreadingHTTPServer(("", port), Handler)


def shifted(*, body: object, offset_ms: int, polled_ms: int) -> object:
    """`body` with each integer within a day of `polled_ms` moved by `offset_ms`."""
    if isinstance(body, dict):
        return {
            key: shifted(body=value, offset_ms=offset_ms, polled_ms=polled_ms)
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [
            shifted(body=value, offset_ms=offset_ms, polled_ms=polled_ms)
            for value in body
        ]
    if isinstance(body, int) and not (isinstance(body, bool)):
        return body + offset_ms if abs(body - polled_ms) <= DAY_MS else body
    return body


def with_replay_unit(*, body: Mapping) -> dict:
    """The fetcher source with a `replay` unit, added from its first unit where the file has none: that unit's rows read from `vars.replay_url` at /<source>. A source that polls the live feed only stays that way under `make demo`, which runs no replay service."""
    copy = json.loads(json.dumps(body))
    config = copy["fetcher"]["config"]
    endpoints = config["profile"].setdefault("endpoints", [])
    if any(endpoint.get("unit") == REPLAY_UNIT for endpoint in endpoints):
        return copy
    if not (endpoints):
        return copy
    endpoints.insert(
        0,
        {
            "base_url": "{{ vars.replay_url }}",
            "path": f"/{copy['source']}",
            "rows": endpoints[0].get("rows", {}),
            "unit": REPLAY_UNIT,
        },
    )
    config.setdefault("vars", {}).setdefault("replay_url", REPLAY_URL)
    return copy


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--pack",
        help="the pack in resources/demo-<pack> to replay, empty for the demo",
        required=True,
    )
    parser.add_argument("--port", default=PORT, help="the port to answer on", type=int)
    args = parser.parse_args(argv)
    if args.pack and args.pack not in demo_pack_names():
        parser.error(
            message=f"no pack {args.pack!r}. The packs are {demo_pack_names()}"
        )
    try:
        replays = load_replays(root=demo_pack_dir(pack=args.pack))
    except (OSError, ReplayError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    if not (replays):
        _print(msg=f"FAIL pack {args.pack!r} holds no fetcher source with events")
        return 1
    server = serve(port=args.port, replays=replays)
    for name, replay in replays.items():
        _print(
            msg=f"replaying {len(replay.polls)} polls of {name!r} at /{name}, silent "
            f"while {replay.live[0]} answers with rows"
        )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
