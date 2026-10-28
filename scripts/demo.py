#!/usr/bin/env -S uv run --locked --script
#  Project:      dfe-docker
#  File:         scripts/demo.py
#  Purpose:      Send live vendor syslog through the running stack, detected per line by dfe-transform-elastic and landed one table per vendor
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml==6.0.3",
# ]
# ///

"""Run the agentless syslog demo against the RUNNING stack.

The Sources are the POST /sources bodies under resources/demo/sources, one to a file, each taking its role from its body. The one with `transform.mode: agentless` is the intake: a dfe-transform-elastic Source on the receiver's syslog listener whose detect candidates are the vendor variants. One with a `fetcher` mapping pulls from its upstream into its own table beside the receiver's push. Every other is a vendor table holding its engine catalogue entry as `from-catalogue` writes it for the receiver intake, which the run checks against the engine's copy.

A Source the engine lacks is created without its `state`, because a Source created dormant gets no table. One the engine holds whose working version differs from its file is written over with the file. Each is then deployed where that is outstanding and set to the `state` its file names, active when it names none. A dormant vendor table keeps filling from the intake with no per-vendor transform running.

The receiver's syslog listener, UDP and TCP on 514, is switched on through the engine and `make apply` recreates what the engine says has to change along with any intake or fetcher instance not yet healthy. A fetcher the profile cannot run or the engine cannot set up is skipped with a warning, while an intake or vendor step that fails stops the run.

The generator is dfe-transform-elastic's own replay script and fixtures, read from DFE_SRC_ROOT/dfe-transform-elastic on a `make dev` stack and otherwise fetched shallow at the tag DFE_TRANSFORM_ELASTIC_VERSION pins so the lines match the detector that runs. Each vendor's resources/demo/events/<source>/ replaces the fixtures of the package its variant names. It runs detached in a pinned Python container on the stack's network, looping raw syslog at dfe-receiver until `make demo-stop` or `make down` removes it. A second run replaces it.

The run returns once every vendor table has gained rows. It fails, stopping the stream, when one gains none or the generator has exited. A fetcher table with no rows yet only warns, because a fetcher's first poll waits one interval.

Exit codes:
  0  every vendor table gained rows while the generator ran
  1  the stack cannot run the demo, a step failed, the generator exited or a vendor table gained no rows
  2  usage error
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import typing
from pathlib import Path
from urllib.parse import quote

import yaml

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import _stack_state  # noqa: E402
import instances  # noqa: E402
from _common import (  # noqa: E402
    FETCHER_SERVICE,
    PER_SOURCE_APP_KEYS,
    PER_SOURCE_APPS_VAR,
    REPO_ROOT,
    _load_dotenv,
    _print,
    _profile_mk_value,
    _rel_path,
    _resolved_services,
)
from _sources import hinted_services  # noqa: E402
from build_dev_images import SRC_REMOTE_DEFAULT  # noqa: E402
from post import (  # noqa: E402
    APP_CONFIG_DIR_KEY,
    ENGINE_SERVICE,
    TARGET_DB,
    TARGET_TABLE,
    _api_get_json,
    _api_post_json,
    _decoded,
    _engine_base,
    _exec_request,
    api_put_json,
)
from seed import (  # noqa: E402
    DEPLOY_TIMEOUT_SECONDS,
    ENGINE_FAULTS,
    SeedError,
    _row_count,
    _token,
    deploy_fault,
)

DEMO_DIR = REPO_ROOT / "resources" / "demo"
EVENTS_DIR = DEMO_DIR / "events"
SOURCES_DIR = DEMO_DIR / "sources"
TRANSFORM_APP = "dfe-transform-elastic"
TRANSFORM_REPO = "dfe-transform-elastic"
TRANSFORM_VERSION_KEY = "DFE_TRANSFORM_ELASTIC_VERSION"
RECEIVER_SERVICE = "dfe-receiver"
# dfe-engine main reads the catalogue from its dfe-schemas wheel and older images from the seeded /app/schemas, so this tries both in that order
CATALOGUE_READ = "\n".join(
    (
        "import pathlib, sys, dfe_schemas",
        "name = sys.argv[1] + '.yaml'",
        "for root in (dfe_schemas.schemas_root(), pathlib.Path('/app/schemas')):",
        "    path = root / 'catalogue' / name",
        "    if path.is_file():",
        "        sys.stdout.write(path.read_text(encoding='utf-8'))",
        "        break",
    )
)
# What `from-catalogue` adds to an entry on the receiver intake (dfe-engine source/catalogue.py write_request_for and _match_for)
CATALOGUE_INTAKE = "receiver"
RECEIVER_MATCH_FIELD = "_source"
# What `write_request_for` sets on every entry it compiles and a source that omits them gets anyway
WRITE_DEFAULTS = {"archive": False, "transport": None}
# The `state` a create gives a source, which a file naming none wants
DEFAULT_STATE = "active"
# The 409 code a write gets when another enabled source already has its receiver match (dfe-engine api/v1/sources.py)
MATCH_CONFLICT = "match_conflict"
# The receiver's UDP and TCP syslog listeners, which have no handshake to authenticate at, so the receiver refuses to start them without accept_unauthenticated (dfe-receiver src/config/mod.rs)
SYSLOG_SWITCH = {
    "config.syslog.enabled": True,
    "config.syslog.accept_unauthenticated": True,
}
SYSLOG_PORT = 514
FIXTURES = "tests/fixtures/unencumbered"
# The generator's inputs beside its own directory: the detector's label table and the fixtures it replays
GENERATOR_INPUTS = ("src/detect", FIXTURES)
FETCH_ROOT = REPO_ROOT / ".tmp" / "demo"
# The generator ends a loop only at --count or --duration, so the stream runs ten years unless removed
STREAM_SECONDS = 315_360_000
READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 3.0
LAND_TIMEOUT_SECONDS = 180.0
LAND_INTERVAL_SECONDS = 5.0
GIT_TIMEOUT_SECONDS = 300
VERSION_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class DemoError(Exception):
    """A step of the demo that could not be done, with what to do about it."""


class Demo(typing.NamedTuple):
    """The Sources under resources/demo/sources by role with each vendor source's events directory."""

    events: dict[str, Path]
    fetchers: list[dict]
    intake: dict
    vendors: list[dict]


def _apply(*, dev: bool, environ: dict[str, str], services: list[str]) -> None:
    """Recreate the named services with `make apply`, the command the engine's hints name."""
    command = [
        "make",
        "--no-print-directory",
        "apply",
        f"SERVICES={' '.join(services)}",
    ]
    if dev:
        command.append("DEV=1")
    _print(msg=f"running {' '.join(command)}")
    # environ predates the .env merge: make includes .env itself and exports a key it inherits from the environment with the value make parsed
    result = subprocess.run(command, check=False, cwd=REPO_ROOT, env=environ)
    if result.returncode != 0:
        raise DemoError(f"`{' '.join(command)}` failed")


def _await_ready(*, instance: str) -> None:
    """Wait until the receiver listens for syslog and the intake's transform passes its healthcheck."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        waiting = (
            [] if _syslog_ready() else [f"{RECEIVER_SERVICE} syslog on {SYSLOG_PORT}"]
        )
        if not (_healthy(service=instance)):
            waiting.append(instance)
        if not (waiting):
            return
        if time.monotonic() >= deadline:
            raise DemoError(
                f"{' and '.join(waiting)} not ready after {READY_TIMEOUT_SECONDS:.0f}s. "
                f"`docker compose logs` on the service says why. A {TRANSFORM_APP} "
                f"release without agentless mode refuses the intake's config"
            )
        time.sleep(READY_INTERVAL_SECONDS)


def _await_rows(
    *, before: dict[str, int | None], container: str, vendors: list[str]
) -> dict[str, int]:
    """Poll until every vendor table has gained rows or the generator has exited, then return each table's gain so far."""
    deadline = time.monotonic() + LAND_TIMEOUT_SECONDS
    while True:
        now = _counts(tables=list(before))
        gained = {
            table: ((now[table]) or (0)) - ((before[table]) or (0)) for table in before
        }
        done = all(gained[table] > 0 for table in vendors)
        if (
            (done)
            or (time.monotonic() >= deadline)
            or not (_running(container=container))
        ):
            return gained
        time.sleep(LAND_INTERVAL_SECONDS)


def _catalogue(*, name: str) -> dict | str:
    """The engine's catalogue entry for one source name, or why it cannot be read."""
    container = _container(service=ENGINE_SERVICE)
    if not (container):
        return f"there is no {ENGINE_SERVICE!r} container to read it from"
    result = _docker(args=["exec", container, "python", "-c", CATALOGUE_READ, name])
    if result.returncode != 0:
        return f"reading it from {ENGINE_SERVICE!r} failed: {result.stderr.strip()!r}"
    if not (result.stdout.strip()):
        return f"{ENGINE_SERVICE!r} ships no catalogue entry named {name!r}"
    try:
        entry = yaml.safe_load(stream=result.stdout)
    except yaml.YAMLError as error:
        return f"the {ENGINE_SERVICE!r} catalogue entry {name!r} is not readable YAML: {error}"
    if not (isinstance(entry, dict)):
        return f"the {ENGINE_SERVICE!r} catalogue entry {name!r} is not a mapping"
    return entry


def _check_catalogue(*, vendors: list[dict]) -> None:
    """Warn for each vendor source that differs from the engine's catalogue entry or cannot be checked against one."""
    for body in vendors:
        path = _rel_path(path=SOURCES_DIR / f"{body['source']}.yaml")
        entry = _catalogue(name=body["source"])
        if isinstance(entry, str):
            _print(
                msg=f"WARNING {path!r} is unchecked against the engine catalogue: {entry}"
            )
            continue
        drift = catalogue_drift(catalogue=entry, source=body)
        if drift:
            keys = ", ".join(repr(key) for key in drift)
            _print(
                msg=f"WARNING {path!r} differs from the engine's catalogue entry in "
                f"{keys}, so it is not what `from-catalogue` creates on the "
                f"{CATALOGUE_INTAKE!r} intake"
            )


def _conflict_hint(*, answer: object, status: int) -> str:
    """What to do about a write the engine refused for a receiver match another source has, empty for any other refusal."""
    code = answer.get("code") if isinstance(answer, dict) else None
    if (status != 409) or (code != MATCH_CONFLICT):
        return ""
    context = answer.get("context") if isinstance(answer, dict) else None
    other = context.get("conflicting_source") if isinstance(context, dict) else None
    return (
        f". Source {other!r} already matches those records: set it dormant or "
        f"delete it, then run this again"
    )


def _container(*, service: str) -> str:
    """The id of this checkout's container for one compose service, empty when there is none."""
    result = _docker(
        args=[
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={_stack_state.WORKING_DIR_LABEL}={REPO_ROOT}",
            "--filter",
            f"label={_stack_state.SERVICE_LABEL}={service}",
        ]
    )
    ids = result.stdout.split()
    return ids[0] if result.returncode == 0 and ids else ""


def _counts(*, tables: list[str]) -> dict[str, int | None]:
    """The rows in each table, None for a table that cannot be read."""
    return {table: _row_count(source=table) for table in tables}


def _create(*, base: str, body: dict, token: str) -> dict:
    """Create one source from its file, then return the reply."""
    name = body["source"]
    status, created = _api_post_json(
        payload=create_body(body=body), token=token, url=f"{base}/sources"
    )
    if status not in (201, 202):
        raise DemoError(
            f"creating source {name!r} returned HTTP {status}: {created}"
            f"{_conflict_hint(answer=created, status=status)}"
        )
    _print(msg=f"created source {name!r}")
    return created


def _current_deployed(*, held: dict) -> bool:
    """Whether the engine has deployed the working version of a source it holds."""
    deployed = held.get("deployed_version")
    return bool(deployed) and deployed == held.get("current")


def _declared_fetchers(
    *, declared: dict[str, list[str]], fetchers: list[dict]
) -> list[dict]:
    """The fetcher sources the engine declared an instance for, warning for and skipping the rest."""
    kept = []
    for body in fetchers:
        name = body["source"]
        if name in declared.get(FETCHER_SERVICE, []):
            kept.append(body)
            continue
        instance = instances.service_name(instance=name, service=FETCHER_SERVICE)
        _print(
            msg=f"WARNING fetcher source {name!r} is skipped: dfe-engine declared no "
            f"{instance!r} instance for it"
        )
    return kept


def _deploy(*, base: str, name: str, token: str) -> dict:
    """Deploy one source, which runs its DDL and creates its topics, then return the reply."""
    status, answer = _api_post_json(
        payload={},
        timeout=DEPLOY_TIMEOUT_SECONDS,
        token=token,
        url=f"{_source_url(base=base, name=name)}/deploy",
    )
    fault = deploy_fault(answer=answer, status=status)
    if fault:
        raise DemoError(f"source {name!r}: {fault}")
    _print(msg=f"deployed source {name!r}")
    return answer


def _docker(*, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run one docker command and capture its text output."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=timeout,
    )


def _ensure_fetchers(
    *, base: str, fetchers: list[dict], per_source_apps: list[str], token: str
) -> tuple[list[dict], list[dict]]:
    """Bring each fetcher source to its file, warning for and skipping one that cannot be, then return the ones set up and the engine's replies."""
    if fetchers and (FETCHER_SERVICE not in per_source_apps):
        names = ", ".join(repr(body["source"]) for body in fetchers)
        _print(
            msg=f"WARNING this profile runs no {FETCHER_SERVICE!r}, so fetcher "
            f"source(s) {names} are skipped. single-kafka and single-grpc run it"
        )
        return [], []
    kept = []
    replies = []
    for body in fetchers:
        try:
            replies += _ensure_source(base=base, body=body, token=token)
        except DemoError as error:
            _print(msg=f"WARNING fetcher source {body['source']!r} is skipped: {error}")
            continue
        kept.append(body)
    return kept, replies


def _ensure_source(*, base: str, body: dict, token: str) -> list[dict]:
    """Bring one source to what its file defines, created or updated, deployed and in its `state`, then return the engine's replies."""
    name = body["source"]
    held = _stored(base=base, name=name, token=token)
    replies = []
    for step in ensure_steps(body=body, held=held):
        if step == "create":
            replies.append(_create(base=base, body=body, token=token))
        elif step == "update":
            drift = held_drift(body=body, held=held or {})
            replies.append(_update(base=base, body=body, drift=drift, token=token))
        elif step == "deploy":
            replies.append(_deploy(base=base, name=name, token=token))
        else:
            replies.append(
                _set_state(base=base, name=name, state=_state(body=body), token=token)
            )
    return replies


def _fetch_generator(*, generator: str, tag: str) -> Path:
    """A shallow sparse checkout of dfe-transform-elastic at tag, holding the generator and its inputs."""
    checkout = FETCH_ROOT / f"{TRANSFORM_REPO}-{tag}"
    if (checkout / generator).is_file():
        return checkout
    shutil.rmtree(ignore_errors=True, path=checkout)
    checkout.parent.mkdir(exist_ok=True, parents=True)
    remote = (os.environ.get("DFE_SRC_REMOTE", "").strip()) or (SRC_REMOTE_DEFAULT)
    url = f"{remote.rstrip('/')}/{TRANSFORM_REPO}.git"
    _print(msg=f"fetching {TRANSFORM_REPO} {tag!r} into {str(checkout)!r}")
    _run_git(
        args=[
            "clone",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "--sparse",
            f"--branch={tag}",
            "--",
            url,
            str(checkout),
        ]
    )
    paths = [str(Path(generator).parent), *GENERATOR_INPUTS]
    _run_git(args=["-C", str(checkout), "sparse-checkout", "set", *paths])
    return checkout


def _generator_output(*, container: str) -> str:
    """The last lines the generator's container wrote."""
    sent = _docker(args=["logs", "--tail", "20", container])
    return (sent.stdout + sent.stderr).strip()


def _healthy(*, service: str) -> bool:
    """Whether the service's container is running and its healthcheck passes."""
    container = _container(service=service)
    if not (container):
        return False
    result = _docker(
        args=[
            "inspect",
            "--format",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ]
    )
    return result.stdout.split() == ["running", "healthy"]


def _listening(*, port: int, service: str) -> set[str]:
    """The protocols the service's container listens on port with."""
    container = _container(service=service)
    if not (container):
        return set()
    paths = [f"/proc/net/{name}" for name in ("tcp", "tcp6", "udp", "udp6")]
    # A kernel without IPv6 has no tcp6 or udp6, so cat's exit status says nothing
    result = _docker(args=["exec", container, "cat", *paths])
    return listening_protocols(port=port, proc_net=result.stdout)


def _network(*, service: str) -> str:
    """The first docker network the service's container is attached to."""
    container = _container(service=service)
    result = _docker(
        args=[
            "inspect",
            "--format",
            "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
            container,
        ]
    )
    names = result.stdout.split()
    if not (container) or (result.returncode != 0) or not (names):
        raise DemoError(
            f"no running {service!r} container to send to. Start the stack first"
        )
    return names[0]


def _outcome(
    *,
    before: dict[str, int | None],
    container: str,
    fetchers: list[dict],
    instance: str,
    vendors: list[str],
) -> int:
    """Wait for the vendor rows, report every table and return the demo's exit code."""
    gained = _await_rows(before=before, container=container, vendors=vendors)
    for table, rows in gained.items():
        _print(msg=f"  {TARGET_DB}.{table}: +{rows} rows")
    # A fetcher's table is reported whole, since its cursor holds back what a previous run already pulled
    landed = _counts(tables=[body["source"] for body in fetchers])
    for table, rows in landed.items():
        if rows is not None:
            _print(msg=f"  {TARGET_DB}.{table}: {rows} rows")
    for warning in fetcher_warnings(counts=landed, fetchers=fetchers):
        _print(msg=warning)
    empty = ", ".join(
        repr(f"{TARGET_DB}.{table}") for table in vendors if gained[table] <= 0
    )
    # Lines a previous stream sent can still land, so rows alone do not prove this run's generator worked
    if not (_running(container=container)):
        _print(
            msg=f"FAIL the generator in {container!r} exited, so the rows above may be "
            f"a previous stream's. Its last output:\n"
            f"{_generator_output(container=container)}"
        )
    elif empty:
        _print(
            msg=f"FAIL no rows landed in {empty}. `docker compose logs {instance}` says "
            f"what the transform did with them. The generator's last output:\n"
            f"{_generator_output(container=container)}"
        )
    else:
        _print(
            msg="PASS every vendor table gained rows, ready to query in the console or "
            "HyperDX. The stream runs until `make demo-stop` or a receiver restart"
        )
        return 0
    _stop_stream(container=container)
    return 1


def _patch_json(*, payload: dict, token: str, url: str) -> tuple[int, typing.Any]:
    """PATCH JSON from inside the stack and return (status, decoded body)."""
    status, body = _exec_request(
        method="PATCH", payload=payload, timeout=30, token=token, url=url
    )
    return status, _decoded(raw=body)


def _read_source(*, path: Path) -> dict:
    """One demo source file's POST /sources body, refused unless its `source` is the file's name."""
    try:
        body = yaml.safe_load(stream=path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise DemoError(f"{path.name!r} is not readable YAML: {error}") from error
    if not (isinstance(body, dict)):
        raise DemoError(f"{path.name!r} needs a POST /sources body, which is a mapping")
    if body.get("source") != path.stem:
        raise DemoError(
            f"{path.name!r} names source {body.get('source')!r}. A demo source file "
            f"is named after the `source` it holds, so rename one to match"
        )
    return body


def _resolve_generator(*, dev: bool, generator: str) -> Path:
    """The dfe-transform-elastic checkout the generator runs from: DFE_SRC_ROOT's on a `make dev` stack, else a fetch at the pinned tag."""
    raw_root = os.environ.get("DFE_SRC_ROOT", "").strip() if dev else ""
    if raw_root:
        checkout = Path(raw_root).expanduser() / TRANSFORM_REPO
    else:
        tag = generator_tag(pin=os.environ.get(TRANSFORM_VERSION_KEY, ""))
        if not (tag):
            raise DemoError(
                f"{TRANSFORM_VERSION_KEY!r} names no version. Run `make stack` to pin one"
            )
        checkout = _fetch_generator(generator=generator, tag=tag)
    if not ((checkout / generator).is_file()):
        raise DemoError(
            f"{str(checkout)!r} has no {generator!r}: the {TRANSFORM_APP} it holds "
            f"predates agentless mode, which ships with the generator. Pin a release "
            f"that has both or set DFE_SRC_ROOT to a checkout that does"
        )
    return checkout


def _role(*, body: dict) -> str:
    """What a demo source is for: `fetcher` with a fetcher mapping, `intake` in agentless mode, else `vendor`."""
    if isinstance(body.get("fetcher"), dict):
        return "fetcher"
    transform = body.get("transform")
    if isinstance(transform, dict) and transform.get("mode") == "agentless":
        return "intake"
    return "vendor"


def _run(*, args: argparse.Namespace) -> int:
    """Run the demo against the running stack and return its exit code."""
    demo = load_demo(events=EVENTS_DIR, sources=SOURCES_DIR)
    environ = dict(os.environ)
    _load_dotenv()
    if not (_resolved_services()):
        raise DemoError(
            "the profile is not resolved, so which services run is unknown. Run this "
            "through make, which writes .tmp/profile.mk"
        )
    per_source_apps = _profile_mk_value(key=PER_SOURCE_APPS_VAR)
    reason = refusal(
        per_source_apps=per_source_apps,
        renders=bool(_profile_mk_value(key=APP_CONFIG_DIR_KEY)),
    )
    if reason:
        raise DemoError(reason)
    checkout = _resolve_generator(dev=args.dev, generator=args.generator)
    network = _network(service=RECEIVER_SERVICE)
    _check_catalogue(vendors=demo.vendors)

    base = _engine_base()
    token = _token(base=base)
    replies = []
    for body in [*demo.vendors, demo.intake]:
        replies += _ensure_source(base=base, body=body, token=token)
    fetchers, polled = _ensure_fetchers(
        base=base, fetchers=demo.fetchers, per_source_apps=per_source_apps, token=token
    )
    replies += polled
    replies.append(_switch_on_syslog(base=base, token=token))

    instance = instances.service_name(
        instance=demo.intake["source"], service=TRANSFORM_APP
    )
    declared = instances.declared()
    if demo.intake["source"] not in declared.get(TRANSFORM_APP, []):
        raise DemoError(f"dfe-engine declared no {instance!r} instance for the intake")
    fetchers = _declared_fetchers(declared=declared, fetchers=fetchers)
    pollers = [
        instances.service_name(instance=body["source"], service=FETCHER_SERVICE)
        for body in fetchers
    ]
    services = runnable(
        declared=set(instances.services(instances=declared)),
        hinted=hinted_services(replies=replies),
    )
    unready = {
        service: not (_healthy(service=service)) for service in [instance, *pollers]
    }
    # The switch names no restart once it is set, so a receiver that never restarted after it is found by asking the socket table
    unready[RECEIVER_SERVICE] = not (_syslog_ready())
    services += [svc for svc, down in unready.items() if down and svc not in services]
    if services:
        _apply(dev=args.dev, environ=environ, services=services)
    _await_ready(instance=instance)

    vendors = [body["source"] for body in demo.vendors]
    # Lines no detector claims land in the intake's own table, while main is shown to prove none went there
    tables = [*vendors, demo.intake["source"], TARGET_TABLE]
    # A previous run's stream stops before the count, though lines it already sent can still land after it
    _stop_stream(container=args.container)
    before = _counts(tables=tables)
    feed = stage_feed(
        checkout=checkout,
        events={
            fixture_package(source=body): demo.events[body["source"]]
            for body in demo.vendors
        },
        feed=FETCH_ROOT / "feed",
        generator=args.generator,
    )
    command = generator_command(
        container=args.container,
        feed=feed,
        generator=args.generator,
        image=args.image,
        network=network,
        protocol=args.protocol,
        rate=args.rate,
        sources=vendors,
    )
    _print(
        msg=f"streaming {', '.join(vendors)} syslog over {args.protocol.upper()} at "
        f"{args.rate:g} lines/s in container {args.container!r}"
    )
    if subprocess.run(command, check=False).returncode != 0:
        raise DemoError(
            "the generator container did not start. Docker's error is above"
        )
    return _outcome(
        before=before,
        container=args.container,
        fetchers=fetchers,
        instance=instance,
        vendors=vendors,
    )


def _run_git(*, args: list[str]) -> None:
    """Run one git command, failing the demo with git's error when it does."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise DemoError(f"git {args[0]} failed: {result.stderr.strip()}")


def _running(*, container: str) -> bool:
    """Whether the named container is running."""
    result = _docker(args=["inspect", "--format", "{{.State.Running}}", container])
    return result.stdout.strip() == "true"


def _set_state(*, base: str, name: str, state: str, token: str) -> dict:
    """Set one deployed source to the `state` its file names, then return the reply."""
    url = _source_url(base=base, name=name)
    status, answer = _patch_json(payload={"state": state}, token=token, url=url)
    if status != 200:
        raise DemoError(
            f"setting source {name!r} to {state!r} returned HTTP {status}: {answer}"
            f"{_conflict_hint(answer=answer, status=status)}"
        )
    effect = ": its table stays, no transform runs for it" if state == "dormant" else ""
    _print(msg=f"set source {name!r} to {state!r}{effect}")
    return answer


def _source_url(*, base: str, name: str) -> str:
    """The engine's route for one source."""
    return f"{base}/sources/{quote(safe='', string=name)}"


def _state(*, body: dict) -> str:
    """The `state` a source file wants, the one a create gives when it names none."""
    return (body.get("state")) or (DEFAULT_STATE)


def _stop_stream(*, container: str) -> None:
    """Remove the stream's container, if one runs."""
    _docker(args=["rm", "--force", container])


def _stored(*, base: str, name: str, token: str) -> dict | None:
    """The engine's record of one source, None when it holds none."""
    status, answer = _api_get_json(token=token, url=_source_url(base=base, name=name))
    if status == 404:
        return None
    if status != 200:
        raise DemoError(f"GET /sources/{name} returned HTTP {status}: {answer}")
    _print(msg=f"source {name!r} already exists")
    return answer if isinstance(answer, dict) else {}


def _subset(*, held: object, wanted: object) -> bool:
    """Whether held carries everything wanted sets, a mapping key by key and anything else whole."""
    if isinstance(wanted, dict):
        return isinstance(held, dict) and all(
            _subset(held=held.get(key), wanted=value) for key, value in wanted.items()
        )
    return held == wanted


def _switch_on_syslog(*, base: str, token: str) -> dict:
    """Turn on the receiver's UDP and TCP syslog listener through the engine and return the reply."""
    status, apps = _api_get_json(token=token, url=f"{base}/apps")
    listed = apps if isinstance(apps, list) else []
    receiver = next(
        (
            entry
            for entry in listed
            if isinstance(entry, dict) and entry.get("service") == RECEIVER_SERVICE
        ),
        {},
    )
    names = [str(name) for name in (receiver.get("instances")) or ([])]
    if (status != 200) or not (names):
        raise DemoError(
            f"the engine lists no {RECEIVER_SERVICE!r} instance (GET /apps: HTTP {status})"
        )
    route = f"{base}/apps/{RECEIVER_SERVICE}/{quote(safe='', string=names[0])}/config"
    status, answer = api_put_json(
        payload={"changes": SYSLOG_SWITCH}, token=token, url=route
    )
    if status != 200:
        raise DemoError(
            f"the engine refused the receiver's syslog switch: HTTP {status}: {answer}"
        )
    _print(
        msg=f"{RECEIVER_SERVICE!r} config turns on syslog over UDP and TCP {SYSLOG_PORT}"
    )
    return answer


def _syslog_ready() -> bool:
    """Whether the receiver listens for syslog over both TCP and UDP."""
    return _listening(port=SYSLOG_PORT, service=RECEIVER_SERVICE) == {"tcp", "udp"}


def _update(*, base: str, body: dict, drift: list[str], token: str) -> dict:
    """Write one held source's file over the engine's working version, then return the reply."""
    name = body["source"]
    status, answer = api_put_json(
        payload=create_body(body=body),
        token=token,
        url=_source_url(base=base, name=name),
    )
    if status != 200:
        raise DemoError(
            f"updating source {name!r} to its file returned HTTP {status}: {answer}"
            f"{_conflict_hint(answer=answer, status=status)}"
        )
    keys = ", ".join(repr(key) for key in drift)
    _print(msg=f"updated source {name!r} to its file, which differed in {keys}")
    return answer


def _variant(*, source: dict) -> str:
    """The transform variant a vendor source runs, refused when it names none."""
    transform = source.get("transform")
    variant = transform.get("variant") if isinstance(transform, dict) else None
    if not (isinstance(variant, str)) or not (variant):
        raise DemoError(
            f"vendor source {source.get('source')!r} needs a `transform.variant`, "
            f"which the intake's detect candidates name it by"
        )
    return variant


def catalogue_drift(*, catalogue: dict, source: dict) -> list[str]:
    """The top-level keys where a source differs from what `from-catalogue` writes for its catalogue entry on the receiver intake, `state` aside."""
    name = catalogue.get("source")
    entry = {key: value for key, value in catalogue.items() if key != "catalogue"}
    created = {
        **entry,
        **WRITE_DEFAULTS,
        "from_catalogue_intake": CATALOGUE_INTAKE,
        "from_catalogue_item": name,
        "match": {"field": RECEIVER_MATCH_FIELD, "operator": "equals", "value": name},
    }
    written = {**WRITE_DEFAULTS, **create_body(body=source)}
    return sorted(
        key
        for key in created.keys() | written.keys()
        if created.get(key) != written.get(key)
    )


def create_body(*, body: dict) -> dict:
    """The POST or PUT body for one source file: all of it bar `state`, which only the lifecycle route sets here."""
    return {key: value for key, value in body.items() if key != "state"}


def ensure_steps(*, body: dict, held: dict | None) -> list[str]:
    """The engine calls that bring one source to its file, given the engine's record of it: `create` or `update`, then `deploy`, then `state`."""
    steps = []
    if held is None:
        steps.append("create")
    elif held_drift(body=body, held=held):
        steps.append("update")
    # A deploy follows every write, since an update that bumps the version leaves it undeployed and a re-deploy changes only what differs
    if (steps) or not (_current_deployed(held=held or {})):
        steps.append("deploy")
    held_state = DEFAULT_STATE if held is None else held.get("state")
    # A run stopped between the deploy and this leaves the source active, so a re-run finishes it
    if held_state != _state(body=body):
        steps.append("state")
    return steps


def fetcher_warnings(
    *, counts: dict[str, int | None], fetchers: list[dict]
) -> list[str]:
    """A WARNING line for each fetcher table that holds no rows yet or cannot be read."""
    warnings = []
    for body in fetchers:
        table = f"{TARGET_DB}.{body['source']}"
        rows = counts.get(body["source"])
        if rows is None:
            warnings.append(
                f"WARNING {table!r} cannot be read, so whether its fetcher landed rows "
                f"is unknown"
            )
            continue
        if rows > 0:
            continue
        config = body["fetcher"].get("config")
        interval = config.get("interval_secs") if isinstance(config, dict) else None
        wait = f"{interval}s" if interval else "one interval"
        warnings.append(
            f"WARNING no rows in {table!r} yet. A fetcher's first poll waits {wait} "
            f"and none land when the stack has no egress to its upstream"
        )
    return warnings


def fixture_package(*, source: dict) -> str:
    """The replayer's fixture package a vendor source's events stand in for, the middle part of its transform variant."""
    variant = _variant(source=source)
    parts = variant.split(sep=".")
    if (len(parts) < 3) or not (parts[1]):
        raise DemoError(
            f"vendor source {source.get('source')!r} has transform.variant "
            f"{variant!r}, not <shipper>.<package>.<name>"
        )
    return parts[1]


def generator_command(
    *,
    container: str,
    feed: Path,
    generator: str,
    image: str,
    network: str,
    protocol: str,
    rate: float,
    sources: list[str],
) -> list[str]:
    """The detached docker run that loops the vendor lines at dfe-receiver's syslog listener."""
    mount = "/demo"
    # No --rm, so the logs of a generator that exited stay readable until `make demo-stop` or the next run removes it
    return [
        "docker",
        "run",
        "--detach",
        "--init",
        "--name",
        container,
        "--network",
        network,
        "--volume",
        f"{feed}:{mount}:ro",
        image,
        "python3",
        "-I",
        f"{mount}/{generator}",
        "--target",
        f"{protocol}://{RECEIVER_SERVICE}:{SYSLOG_PORT}",
        "--sources",
        ",".join(sources),
        "--rate",
        f"{rate:g}",
        "--loop",
        "--duration",
        str(STREAM_SECONDS),
    ]


def generator_tag(*, pin: str) -> str:
    """The git tag a `tag@sha256:digest` image pin names, empty when it names none."""
    tag = pin.strip().strip("'\"").split(maxsplit=1, sep="@")[0]
    return tag if VERSION_TAG.fullmatch(string=tag) else ""


def held_drift(*, body: dict, held: dict) -> list[str]:
    """The top-level keys a source file sets that the engine's working version holds otherwise, `state` aside."""
    versions = held.get("versions")
    working = versions.get(held.get("current")) if isinstance(versions, dict) else None
    # The top-level copies of versioned keys are the deployed version's, so the working version's own win
    record = {**held, **working} if isinstance(working, dict) else held
    return sorted(
        key
        for key, value in create_body(body=body).items()
        if not (_subset(held=record.get(key), wanted=value))
    )


def listening_protocols(*, port: int, proc_net: str) -> set[str]:
    """The protocols with a socket listening on port, read from /proc/net/{tcp,tcp6,udp,udp6} text."""
    suffix = f":{port:04X}"
    found = set()
    for line in proc_net.splitlines():
        fields = line.split()
        # A TCP socket in LISTEN is state 0A, an unconnected UDP socket 07
        if len(fields) > 3 and fields[1].endswith(suffix) and fields[3] in ("0A", "07"):
            found.add("tcp" if fields[3] == "0A" else "udp")
    return found


def load_demo(*, events: Path, sources: Path) -> Demo:
    """Read every source file under sources by role and pair each vendor source with its directory under events."""
    bodies = [
        _read_source(path=path) for path in sorted(sources.glob(pattern="*.yaml"))
    ]
    if not (bodies):
        raise DemoError(
            f"{str(sources)!r} holds no source files. Add one POST /sources body per "
            f"demo source as <source>.yaml"
        )
    fetchers = [body for body in bodies if _role(body=body) == "fetcher"]
    intakes = [body for body in bodies if _role(body=body) == "intake"]
    vendors = [body for body in bodies if _role(body=body) == "vendor"]
    if len(intakes) != 1:
        found = (", ".join(repr(f"{body['source']}.yaml") for body in intakes)) or (
            "none"
        )
        raise DemoError(
            f"{str(sources)!r} needs exactly one intake, a source with "
            f"`transform.mode: agentless`. It holds {found}"
        )
    intake = intakes[0]
    if not (vendors):
        raise DemoError(
            f"{str(sources)!r} holds no vendor source for the intake to detect into. "
            f"Add one with a `transform.variant`"
        )
    variants = sorted(_variant(source=body) for body in vendors)
    detect = intake["transform"].get("detect")
    candidates = detect.get("candidates") if isinstance(detect, dict) else None
    listed = (
        sorted(str(item) for item in candidates) if isinstance(candidates, list) else []
    )
    if listed != variants:
        raise DemoError(
            f"{intake['source'] + '.yaml'!r} lists `transform.detect.candidates` "
            f"{listed}. Set them to the vendor sources' variants, {variants}"
        )
    owners = {}
    for body in vendors:
        package = fixture_package(source=body)
        if package in owners:
            raise DemoError(
                f"vendor sources {owners[package]!r} and {body['source']!r} both "
                f"replay over fixture package {package!r}, so one set of events would "
                f"never be sent. Give each a variant naming its own package"
            )
        owners[package] = body["source"]
    held = (
        {path.name: path for path in events.glob(pattern="*") if path.is_dir()}
        if events.is_dir()
        else {}
    )
    names = [body["source"] for body in vendors]
    missing = [
        name for name in names if (name not in held) or not (any(held[name].iterdir()))
    ]
    if missing:
        layout = f"{events}/<source>/"
        raise DemoError(
            f"vendor source(s) {', '.join(repr(name) for name in missing)} have no "
            f"events to replay. Add each as {layout!r} holding its lines"
        )
    orphans = sorted(set(held) - set(names))
    if orphans:
        raise DemoError(
            f"{str(events)!r} holds events for {', '.join(repr(name) for name in orphans)} "
            f"with no vendor source of that name in {str(sources)!r}. Add the source "
            f"or remove the directory"
        )
    return Demo(
        events={name: held[name] for name in names},
        fetchers=fetchers,
        intake=intake,
        vendors=vendors,
    )


def refusal(*, per_source_apps: list[str], renders: bool) -> str:
    """Why this profile cannot run the demo, empty when it can."""
    if not (renders):
        return (
            "this profile's apps read a committed config, so dfe-engine cannot define "
            "the demo's sources. Turn the core footprint on"
        )
    if TRANSFORM_APP not in per_source_apps:
        return (
            f"this profile runs no {TRANSFORM_APP!r}. Use single-kafka or single-grpc "
            f"(DFE_PROFILE in .env), which run it per source"
        )
    return ""


def runnable(*, declared: set[str], hinted: list[str]) -> list[str]:
    """The hinted services `make apply` can still create: every resident one and each per-source instance still declared."""
    return [
        service
        for service in hinted
        if (service in declared)
        or not (any(service.startswith(f"{app}-") for app in PER_SOURCE_APP_KEYS))
    ]


def stage_feed(
    *, checkout: Path, events: dict[str, Path], feed: Path, generator: str
) -> Path:
    """A copy of the generator's tree where each fixture package in events replays its events directory instead of its fixtures."""
    shutil.rmtree(ignore_errors=True, path=feed)
    for part in (str(Path(generator).parent), *GENERATOR_INPUTS):
        shutil.copytree(dirs_exist_ok=True, dst=feed / part, src=checkout / part)
    for package, source in sorted(events.items()):
        target = feed / FIXTURES / package
        # The replayer sends a source's lines from the fixture package it maps the source to, so events staged where it ships none are never sent
        if not (target.is_dir()):
            raise DemoError(
                f"{str(checkout)!r} ships no {str(Path(FIXTURES) / package)!r} for "
                f"{str(source)!r} to replace, so the replay script cannot send those "
                f"events. Name a variant whose package {TRANSFORM_REPO} has fixtures for"
            )
        shutil.rmtree(ignore_errors=True, path=target)
        shutil.copytree(dst=target, src=source)
    return feed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="recreate from the local images a `make dev` stack runs",
    )
    parser.add_argument(
        "--container", help="the name the stream's container runs under", required=True
    )
    parser.add_argument(
        "--generator",
        help="the replay script's path inside the dfe-transform-elastic checkout",
        required=True,
    )
    parser.add_argument(
        "--image", help="the pinned Python image the generator runs in", required=True
    )
    parser.add_argument(
        "--protocol", choices=("udp", "tcp"), help="syslog transport", required=True
    )
    parser.add_argument("--rate", help="lines per second", required=True, type=float)
    args = parser.parse_args()
    if args.rate <= 0:
        parser.error(message="--rate must be positive")
    try:
        return _run(args=args)
    except (*ENGINE_FAULTS, DemoError, SeedError) as error:
        _print(msg=f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
