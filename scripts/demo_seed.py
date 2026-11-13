#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/demo_seed.py
#  Purpose:      Seed the published demo stack from one-shots on its own network: its sources, the replayer's feed and the console
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Seed the published demo stack from containers on its own network.

The demo compose file scripts/publish_compose.py writes runs this from the dfe-docker image with DFE_IN_STACK set, so each engine and dfe-hyperdx call goes to the service by name and the image's own PyYAML reads the source files. `--pack <name>` seeds from resources/demo-<name> in place of resources/demo. One command per one-shot:

  sources  create, deploy and set the state of every source under the pack's sources/, then turn on the receiver's syslog listener where the pack has an intake and create the rules and hunts under the pack's rules/ and hunts/. dfe-receiver and the per-source instances wait for it to exit 0. A pack with no source files only completes the engine's first-run setup.
  feed     fetch dfe-transform-elastic's replay script at the tag DFE_TRANSFORM_ELASTIC_VERSION pins and stage it under --feed with the pack's events in place of its fixtures, which the stream replays.
  console  create the pack's dashboards and saved searches, waiting up to CONSOLE_TIMEOUT_SECONDS for dfe-hyperdx to hold the sources they read.

sources and console log in as the break-glass account, because the bootstrap admin keeps its first-login password until the person running the demo replaces it. The stack is the demo's own, so every source they touch is one sources created.

Exit codes:
  0  done
  1  not inside the stack, or a step failed
  2  usage error
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import demo  # noqa: E402
from _common import (  # noqa: E402
    FETCHER_SERVICE,
    IN_STACK_KEY,
    _print,
    demo_pack_dir,
    demo_pack_names,
    in_stack,
)
from post import ENGINE_NETWORK_BASE, _request_token  # noqa: E402
from seed import ENGINE_FAULTS, SeedError  # noqa: E402

BREAKGLASS_USERNAME = "breakglass"
BREAKGLASS_PASSWORD_KEY = "DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD"
# dfe-hyperdx holds a source only once the engine has deployed it, so the console waits for that rather than failing first time
CONSOLE_TIMEOUT_SECONDS = 600.0
CONSOLE_INTERVAL_SECONDS = 10.0


def _token(*, base: str) -> str:
    """A token for the break-glass account, the demo's one admin that needs no password change."""
    password = os.environ.get(BREAKGLASS_PASSWORD_KEY, "").strip()
    if not (password):
        raise demo.DemoError(
            f"{BREAKGLASS_PASSWORD_KEY} is unset, so the demo cannot log in"
        )
    token, status, _ = _request_token(
        base=base, password=password, username=BREAKGLASS_USERNAME
    )
    if (status != 200) or not (token):
        raise demo.DemoError(f"login as {BREAKGLASS_USERNAME!r} returned HTTP {status}")
    return token


def _loaded(*, root: Path) -> demo.Demo | None:
    """The pack's sources by role, None for a pack with no source files, refused unless they and the manifest agree."""
    sources = root / demo.SOURCES_DIR.name
    loaded = (
        demo.load_demo(events=root / demo.EVENTS_DIR.name, sources=sources)
        if any(sources.glob(pattern="*.yaml"))
        else None
    )
    demo.check_manifest(
        demo=loaded,
        manifest=demo._read_manifest(path=root / demo.MANIFEST_FILE.name),
        root=root,
    )
    return loaded


def seed_sources(*, root: Path) -> None:
    """Bring every pack source to its file and turn on the syslog listener the intake reads."""
    loaded = _loaded(root=root)
    token = _token(base=ENGINE_NETWORK_BASE)
    demo._complete_setup(base=ENGINE_NETWORK_BASE, token=token)
    if loaded is None:
        _print(msg=f"{str(root)!r} holds no source files, so none were created")
        return
    intake = [] if loaded.intake is None else [loaded.intake]
    for body in [*loaded.vendors, *loaded.beats, *loaded.json, *intake]:
        demo._ensure_source(base=ENGINE_NETWORK_BASE, body=body, token=token)
    demo._ensure_fetchers(
        base=ENGINE_NETWORK_BASE,
        fetchers=loaded.fetchers,
        per_source_apps=[FETCHER_SERVICE],
        token=token,
    )
    if intake:
        demo._switch_on_syslog(base=ENGINE_NETWORK_BASE, token=token)
    for line in demo.install_hunts(base=ENGINE_NETWORK_BASE, root=root, token=token):
        _print(msg=line)


def stage_feed(*, feed: Path, generator: str, root: Path) -> None:
    """Stage the replay script at the pinned tag under `feed`, each vendor's events in place of its fixtures."""
    loaded = _loaded(root=root)
    if loaded is None:
        raise demo.DemoError(f"{str(root)!r} holds no vendor source to replay")
    checkout = demo._resolve_generator(dev=False, generator=generator)
    staged = demo.stage_feed(
        checkout=checkout,
        events={
            demo.fixture_package(source=body): loaded.events[body["source"]]
            for body in loaded.vendors
        },
        feed=feed,
        generator=generator,
    )
    _print(msg=f"staged {generator!r} and the demo's events in {str(staged)!r}")


def install_console(*, root: Path) -> None:
    """Create the pack's dashboards and saved searches once dfe-hyperdx holds every source they read."""
    token = _token(base=ENGINE_NETWORK_BASE)
    deadline = time.monotonic() + CONSOLE_TIMEOUT_SECONDS
    while True:
        try:
            links = demo._install_console(
                console=root / demo.CONSOLE_DIR.name, token=token
            )
        except demo.DemoError as error:
            if time.monotonic() >= deadline:
                raise
            _print(msg=f"waiting for dfe-hyperdx: {error}")
            time.sleep(CONSOLE_INTERVAL_SECONDS)
            continue
        for line in links:
            _print(msg=f"  console {line}")
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--pack",
        default="",
        help="seed the named pack in resources/demo-<pack>, the demo's own when unset",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="create and deploy the demo's sources")
    feed = commands.add_parser("feed", help="stage the replay script and events")
    feed.add_argument(
        "--feed", type=Path, required=True, help="where the stream reads it"
    )
    feed.add_argument(
        "--generator",
        required=True,
        help="the replay script's path inside the dfe-transform-elastic checkout",
    )
    commands.add_parser("console", help="create the demo's dashboards and searches")
    args = parser.parse_args(argv)
    if args.pack and args.pack not in demo_pack_names():
        parser.error(
            f"no pack {args.pack!r}. The packs are {demo_pack_names()}, each a resources/demo-<pack> directory"
        )
    root = demo_pack_dir(pack=args.pack)
    if not (in_stack()):
        _print(
            msg=f"FAIL {args.command} runs in the published demo's own containers, which set {IN_STACK_KEY}. On a checkout run `make demo`"
        )
        return 1
    try:
        if args.command == "sources":
            seed_sources(root=root)
        elif args.command == "feed":
            stage_feed(feed=args.feed, generator=args.generator, root=root)
        else:
            install_console(root=root)
    except (*ENGINE_FAULTS, demo.DemoError, SeedError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
