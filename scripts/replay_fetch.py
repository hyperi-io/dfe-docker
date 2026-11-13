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

The source's `replay` unit polls this and its live unit polls the feed. While the host its `vars.api_url` names takes a connection, a replayed poll answers with no rows at the `replay` unit's `rows.at`, so live and replayed reports never land together.

The published pack's demo-replay service runs it from the dfe-docker image, whose PyYAML reads the source files.
"""

import argparse
import json
import socket
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import _print, demo_pack_dir, demo_pack_names  # noqa: E402

PORT = 8080
DAY_MS = 86_400_000
REPLAY_UNIT = "replay"
LIVE_CHECK_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 3.0


class ReplayError(Exception):
    """A pack whose replay cannot be served, with what to change."""


@dataclass(slots=True)
class Replay:
    """One source's captured polls, where its rows sit, and the live host that silences it."""

    live: tuple[str, int]
    polls: list[tuple[int, object]]
    rows_at: str
    step_ms: int
    checked: float = field(default=float("-inf"))
    reachable: bool = False


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


def emptied(*, body: object, pointer: str) -> object:
    """`body` with the value at JSON pointer `pointer` replaced by an empty array."""
    if not (pointer.startswith("/")):
        raise ReplayError(f"rows.at {pointer!r} is not a JSON pointer to the rows")
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]
    ]
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


def poll_at(*, now_ms: int, replay: Replay, started_ms: int) -> object:
    """The captured poll this far into the replay, its times moved to now."""
    index = ((now_ms - started_ms) // replay.step_ms) % len(replay.polls)
    polled_ms, body = replay.polls[index]
    return shifted(body=body, offset_ms=now_ms - polled_ms, polled_ms=polled_ms)


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


def _step_ms(*, polls: list[tuple[int, object]]) -> int:
    """The median gap between captured polls, one second for a single poll."""
    gaps = sorted(
        later[0] - earlier[0] for earlier, later in zip(polls, polls[1:], strict=False)
    )
    return max(1, gaps[len(gaps) // 2]) if gaps else 1000


def _replay_rows_at(*, body: Mapping, name: str) -> str:
    """Where a fetcher source's `replay` unit reads its rows."""
    profile = body["fetcher"]["config"]["profile"]
    for endpoint in profile.get("endpoints", []):
        if endpoint.get("unit") == REPLAY_UNIT:
            return str(endpoint.get("rows", {}).get("at", ""))
    raise ReplayError(
        f"{name}.yaml has events to replay and no {REPLAY_UNIT!r} unit to poll them. "
        "Add one whose base_url is `{{ vars.replay_url }}`"
    )


def _live_host(*, body: Mapping, name: str) -> tuple[str, int]:
    """The host and port behind a fetcher source's `vars.api_url`."""
    url = urlsplit(str(body["fetcher"]["config"].get("vars", {}).get("api_url", "")))
    if not (url.hostname):
        raise ReplayError(f"{name}.yaml sets no `vars.api_url` naming its live feed")
    return url.hostname, url.port or (443 if url.scheme == "https" else 80)


def load_replays(*, root: Path) -> dict[str, Replay]:
    """Each fetcher source under root/sources with captured polls under root/events, by name."""
    # Imported here because the tests and the image carry PyYAML and a bare python3 does not
    import yaml

    replays = {}
    for path in sorted((root / "sources").glob("*.yaml")):
        log = root / "events" / path.stem / "demo.log"
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        fetches = isinstance(body, dict) and isinstance(body.get("fetcher"), dict)
        if not (fetches) or not (log.is_file()):
            continue
        polls = _polls(path=log)
        replays[path.stem] = Replay(
            live=_live_host(body=body, name=path.stem),
            polls=polls,
            rows_at=_replay_rows_at(body=body, name=path.stem),
            step_ms=_step_ms(polls=polls),
        )
    return replays


def _reachable(*, replay: Replay) -> bool:
    """Whether the live feed took a connection at the last check, made at most once a minute."""
    now = time.monotonic()
    if now - replay.checked >= LIVE_CHECK_SECONDS:
        try:
            with socket.create_connection(replay.live, timeout=CONNECT_TIMEOUT_SECONDS):
                replay.reachable = True
        except OSError:
            replay.reachable = False
        replay.checked = now
    return replay.reachable


def serve(*, port: int, replays: dict[str, Replay]) -> ThreadingHTTPServer:
    """An HTTP server answering GET /<source> with that source's poll for now."""
    started_ms = int(time.time() * 1000)

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
            if _reachable(replay=replay):
                body = emptied(body=body, pointer=replay.rows_at)
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer(("", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--pack", required=True, help="the pack in resources/demo-<pack> to replay"
    )
    parser.add_argument("--port", default=PORT, type=int, help="the port to answer on")
    args = parser.parse_args(argv)
    if args.pack not in demo_pack_names():
        parser.error(f"no pack {args.pack!r}. The packs are {demo_pack_names()}")
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
            f"while {replay.live[0]} answers"
        )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
