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

The Sources come from resources/demo/demo.yaml. Each vendor table is its engine catalogue entry, created and deployed for its table, then set dormant so no per-vendor transform runs. The intake is one agentless dfe-transform-elastic Source on the receiver's syslog listener, its detect candidates the vendors' catalogue variants. The receiver's syslog listener, UDP and TCP on 514, is switched on through the engine, and `make apply` recreates what the engine says has to change.

The generator is dfe-transform-elastic's own replay script and fixtures, fetched shallow at the tag DFE_TRANSFORM_ELASTIC_VERSION pins so the lines match the detector that runs, or read from DFE_SRC_ROOT/dfe-transform-elastic when that is set. A vendor with a sample set under resources/demo/samples/<package>/ replays that instead of its fixtures. It runs detached in a pinned Python container on the stack's network, looping raw syslog at dfe-receiver until `make demo-stop` or `make down` removes it, and a second run replaces it. The run returns once every vendor table has gained rows, and fails, stopping the stream, when one gains none.

Exit codes:
  0  every vendor table gained rows
  1  the stack cannot run the demo, or a step failed, or a vendor table gained no rows
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

import instances  # noqa: E402
from _common import (  # noqa: E402
    PER_SOURCE_APP_KEYS,
    PER_SOURCE_APPS_VAR,
    REPO_ROOT,
    _load_dotenv,
    _print,
    _profile_mk_value,
    _resolved_services,
)
from _pipeline import ch_int  # noqa: E402
from _sources import hinted_services  # noqa: E402
from build_dev_images import SRC_REMOTE_DEFAULT  # noqa: E402
from post import (  # noqa: E402
    APP_CONFIG_DIR_KEY,
    TARGET_DB,
    TARGET_TABLE,
    _api_get_json,
    _api_post_json,
    _decoded,
    _engine_base,
    _exec_request,
    api_put_json,
)
from seed import ENGINE_FAULTS, SeedError, _token, deploy_fault  # noqa: E402

DEMO_FILE = REPO_ROOT / "resources" / "demo" / "demo.yaml"
TRANSFORM_APP = "dfe-transform-elastic"
TRANSFORM_REPO = "dfe-transform-elastic"
TRANSFORM_VERSION_KEY = "DFE_TRANSFORM_ELASTIC_VERSION"
RECEIVER_SERVICE = "dfe-receiver"
# The receiver's UDP and TCP syslog listeners, which have no handshake to authenticate at, so the receiver refuses to start them without accept_unauthenticated (dfe-receiver src/config/mod.rs)
SYSLOG_SWITCH = {
    "config.syslog.enabled": True,
    "config.syslog.accept_unauthenticated": True,
}
SYSLOG_PORT = 514
# The generator's inputs beside its own directory: the detector's label table and the fixtures it replays
GENERATOR_INPUTS = ("src/detect", "tests/fixtures/unencumbered")
FIXTURES = "tests/fixtures/unencumbered"
FETCH_ROOT = REPO_ROOT / ".tmp" / "demo"
SAMPLES_DIR = REPO_ROOT / "resources" / "demo" / "samples"
# The generator ends a loop only at --count or --duration, so the stream runs ten years unless removed
STREAM_SECONDS = 315_360_000
DEPLOY_TIMEOUT_SECONDS = 90
READY_TIMEOUT_SECONDS = 120.0
READY_INTERVAL_SECONDS = 3.0
LAND_TIMEOUT_SECONDS = 180.0
LAND_INTERVAL_SECONDS = 5.0
GIT_TIMEOUT_SECONDS = 300
VERSION_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class DemoError(Exception):
    """A step of the demo that could not be done, with what to do about it."""


class Demo(typing.NamedTuple):
    """What resources/demo/demo.yaml defines."""

    intake: dict
    sources: list[str]


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


def _container(*, service: str) -> str:
    """The id of this checkout's container for one compose service, or empty when there is none."""
    result = _docker(
        args=[
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project.working_dir={REPO_ROOT}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ]
    )
    ids = result.stdout.split()
    return ids[0] if (result.returncode == 0) and ids else ""


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
    if (not (container)) or (result.returncode != 0) or not (names):
        raise DemoError(
            f"no running {service} container to send to; start the stack first"
        )
    return names[0]


def _patch_json(*, payload: dict, token: str, url: str) -> tuple[int, typing.Any]:
    """PATCH JSON from inside the stack and return (status, decoded body)."""
    status, body = _exec_request(
        method="PATCH", payload=payload, timeout=30, token=token, url=url
    )
    return status, _decoded(body)


def _source_url(*, base: str, name: str) -> str:
    return f"{base}/sources/{quote(safe='', string=name)}"


def _deploy(*, base: str, name: str, token: str) -> dict:
    """Deploy one source, which runs its DDL and creates its topics, and return the reply."""
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


def _held(*, base: str, name: str, token: str) -> list[dict] | None:
    """The engine's replies for a source it already holds, deployed if it never was, or None when it holds none."""
    status, answer = _api_get_json(token=token, url=_source_url(base=base, name=name))
    if status == 404:
        return None
    if status != 200:
        raise DemoError(f"GET /sources/{name} returned HTTP {status}: {answer}")
    _print(msg=f"source {name!r} already exists, left as it is")
    if isinstance(answer, dict) and answer.get("deployed_version"):
        return []
    return [_deploy(base=base, name=name, token=token)]


def _source_variant(*, base: str, label: str, token: str) -> str:
    """The transform variant a stored vendor source runs, read off the source because the catalogue omits an entry once it is stored."""
    status, answer = _api_get_json(token=token, url=_source_url(base=base, name=label))
    transform = answer.get("transform") if isinstance(answer, dict) else None
    variant = transform.get("variant") if isinstance(transform, dict) else None
    if status != 200 or not (variant):
        raise DemoError(
            f"source {label!r} names no transform variant (GET /sources/{label}: "
            f"HTTP {status}), so detection has no candidate for it"
        )
    return str(variant)


def _ensure_table(*, base: str, label: str, token: str) -> list[dict]:
    """Make one vendor's table exist, without a transform running for it, and return the engine's replies."""
    held = _held(base=base, name=label, token=token)
    if held is not None:
        return held
    url = _source_url(base=base, name=label)
    status, created = _api_post_json(
        payload={"entries": [{"entry": label, "intake": "receiver"}]},
        token=token,
        url=f"{base}/sources/from-catalogue",
    )
    rows = created.get("created") if isinstance(created, dict) else None
    stored = {item.get("entry") for item in rows or [] if isinstance(item, dict)}
    # A refused entry comes back in `failed` under 200 or 409, so the status alone does not say it was stored
    if status not in (201, 202) or label not in stored:
        raise DemoError(
            f"creating source {label!r} from the catalogue returned HTTP {status}: {created}"
        )
    _print(msg=f"created source {label!r} from the engine catalogue")
    deployed = _deploy(base=base, name=label, token=token)
    # Dormant keeps the table and stops the per-vendor transform the deploy declared
    status, paused = _patch_json(payload={"state": "dormant"}, token=token, url=url)
    if status != 200:
        raise DemoError(
            f"setting source {label!r} dormant returned HTTP {status}: {paused}"
        )
    _print(
        msg=f"set source {label!r} dormant: its table stays, no transform runs for it"
    )
    return [created, deployed, paused]


def _ensure_intake(*, base: str, body: dict, token: str) -> list[dict]:
    """Create and deploy the agentless intake unless the engine holds it, and return the engine's replies."""
    name = body["source"]
    held = _held(base=base, name=name, token=token)
    if held is not None:
        return held
    status, created = _api_post_json(payload=body, token=token, url=f"{base}/sources")
    if status not in (201, 202):
        raise DemoError(
            f"creating source {name!r} returned HTTP {status}: {created}. A source "
            f"already matching `_source equals syslog` (the starter `syslog` from "
            f"`make sources`) holds the receiver's syslog lines: set it dormant or "
            f"delete it, then run this again"
        )
    _print(msg=f"created source {name!r}")
    return [created, _deploy(base=base, name=name, token=token)]


def _switch_on_syslog(*, base: str, token: str) -> dict:
    """Turn on the receiver's UDP and TCP syslog listener through the engine, and return the reply."""
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
    names = [str(name) for name in receiver.get("instances") or []]
    if status != 200 or not (names):
        raise DemoError(
            f"the engine lists no {RECEIVER_SERVICE} instance (GET /apps: HTTP {status})"
        )
    route = f"{base}/apps/{RECEIVER_SERVICE}/{quote(safe='', string=names[0])}/config"
    status, answer = api_put_json(
        payload={"changes": dict(SYSLOG_SWITCH)}, token=token, url=route
    )
    if status != 200:
        raise DemoError(
            f"the engine refused the receiver's syslog switch: HTTP {status}: {answer}"
        )
    _print(
        msg=f"{RECEIVER_SERVICE} config turns on syslog over UDP and TCP {SYSLOG_PORT}"
    )
    return answer


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
    # environ predates the .env merge: make includes .env itself, and a key it inherits from the environment is exported with the value make parsed
    result = subprocess.run(command, check=False, cwd=REPO_ROOT, env=environ)
    if result.returncode != 0:
        raise DemoError(f"`{' '.join(command)}` failed")


def _syslog_ready() -> bool:
    return _listening(port=SYSLOG_PORT, service=RECEIVER_SERVICE) == {"tcp", "udp"}


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
                f"{' and '.join(waiting)} not ready after {READY_TIMEOUT_SECONDS:.0f}s; "
                f"`docker compose logs` on the service says why. A {TRANSFORM_APP} "
                f"release without agentless mode refuses the intake's config"
            )
        time.sleep(READY_INTERVAL_SECONDS)


def _run_git(*, args: list[str]) -> None:
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


def _fetch_generator(*, generator: str, tag: str) -> Path:
    """A shallow sparse checkout of dfe-transform-elastic at tag, holding the generator and its inputs."""
    checkout = FETCH_ROOT / f"{TRANSFORM_REPO}-{tag}"
    if (checkout / generator).is_file():
        return checkout
    shutil.rmtree(checkout, ignore_errors=True)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    remote = os.environ.get("DFE_SRC_REMOTE", "").strip() or SRC_REMOTE_DEFAULT
    url = f"{remote.rstrip('/')}/{TRANSFORM_REPO}.git"
    _print(msg=f"fetching {TRANSFORM_REPO} {tag} into {checkout}")
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


def generator_tag(*, pin: str) -> str:
    """The git tag a `tag@sha256:digest` image pin names, or empty when it names none."""
    tag = pin.strip().strip("'\"").split("@", 1)[0]
    return tag if VERSION_TAG.fullmatch(tag) else ""


def resolve_generator(*, generator: str) -> Path:
    """The dfe-transform-elastic checkout the generator runs from: DFE_SRC_ROOT's, else a fetch at the pinned tag."""
    raw_root = os.environ.get("DFE_SRC_ROOT", "").strip()
    if raw_root:
        checkout = Path(raw_root).expanduser() / TRANSFORM_REPO
    else:
        tag = generator_tag(pin=os.environ.get(TRANSFORM_VERSION_KEY, ""))
        if not (tag):
            raise DemoError(
                f"{TRANSFORM_VERSION_KEY} names no version; run `make stack` to pin one"
            )
        checkout = _fetch_generator(generator=generator, tag=tag)
    if not ((checkout / generator).is_file()):
        raise DemoError(
            f"{checkout} has no {generator}: the {TRANSFORM_APP} it holds predates "
            f"agentless mode, which ships with the generator. Pin a release that has "
            f"both, or set DFE_SRC_ROOT to a checkout that does"
        )
    return checkout


def stage_feed(*, checkout: Path, feed: Path, generator: str, samples: Path) -> Path:
    """A copy of the generator's tree where each package with a demo sample set replays that set instead of its fixtures."""
    shutil.rmtree(feed, ignore_errors=True)
    for part in (str(Path(generator).parent), *GENERATOR_INPUTS):
        shutil.copytree(checkout / part, feed / part, dirs_exist_ok=True)
    for package in sorted(path for path in samples.glob("*") if path.is_dir()):
        target = feed / FIXTURES / package.name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(package, target)
    return feed


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
    return [
        "docker",
        "run",
        "--detach",
        "--rm",
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


def _stop_stream(*, container: str) -> None:
    """Remove the stream's container, if one runs."""
    _docker(args=["rm", "--force", container])


def load_demo(*, path: Path) -> Demo:
    """Read the intake body and the vendor tables from the demo file."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DemoError(f"{path.name} is not readable YAML: {error}") from error
    intake = doc.get("intake") if isinstance(doc, dict) else None
    sources = doc.get("sources") if isinstance(doc, dict) else None
    if not (isinstance(intake, dict)) or not (isinstance(intake.get("source"), str)):
        raise DemoError(
            f"{path.name} needs an `intake` source body with a `source` name"
        )
    if (
        not (isinstance(sources, list))
        or not (sources)
        or not (all(isinstance(label, str) and label for label in sources))
    ):
        raise DemoError(
            f"{path.name} needs a non-empty `sources` list of catalogue names"
        )
    if not (isinstance(intake.get("transform"), dict)):
        raise DemoError(f"{path.name} `intake` needs a `transform` block")
    return Demo(intake=intake, sources=sources)


def with_candidates(*, intake: dict, variants: list[str]) -> dict:
    """The intake body with its agentless detect candidates set to the vendor variants."""
    transform = dict(intake["transform"])
    transform["detect"] = {**(transform.get("detect") or {}), "candidates": variants}
    return {**intake, "transform": transform}


def declared_instances() -> set[str]:
    """Every per-source instance service dfe-engine declares now."""
    return {
        f"{app}-{name}"
        for app in PER_SOURCE_APP_KEYS
        for name in instances.read_index(path=instances.index_file(service=app))
    }


def runnable(*, declared: set[str], hinted: list[str]) -> list[str]:
    """The hinted services `make apply` can still create: every resident one, and each per-source instance still declared."""
    return [
        service
        for service in hinted
        if service in declared
        or not (any(service.startswith(f"{app}-") for app in PER_SOURCE_APP_KEYS))
    ]


def refusal(*, per_source_apps: list[str], renders: bool) -> str:
    """Why this profile cannot run the demo, empty when it can."""
    if not (renders):
        return (
            "this profile's apps read a committed config, so dfe-engine cannot define "
            "the demo's sources; turn the core footprint on"
        )
    if TRANSFORM_APP not in per_source_apps:
        return (
            f"this profile runs no {TRANSFORM_APP}; use single-kafka or single-grpc "
            f"(DFE_PROFILE in .env), which run it per source"
        )
    return ""


def _counts(*, tables: list[str]) -> dict[str, int | None]:
    return {
        table: ch_int(sql=f"SELECT count() FROM {TARGET_DB}.`{table}`")
        for table in tables
    }


def _await_rows(*, before: dict[str, int | None], vendors: list[str]) -> dict[str, int]:
    """Poll until every vendor table has gained rows, and return each table's gain so far."""
    deadline = time.monotonic() + LAND_TIMEOUT_SECONDS
    while True:
        now = _counts(tables=list(before))
        gained = {table: (now[table] or 0) - (before[table] or 0) for table in before}
        if all(gained[table] > 0 for table in vendors) or time.monotonic() >= deadline:
            return gained
        time.sleep(LAND_INTERVAL_SECONDS)


def _run(*, args: argparse.Namespace) -> int:
    demo = load_demo(path=DEMO_FILE)
    environ = dict(os.environ)
    _load_dotenv()
    if not (_resolved_services()):
        raise DemoError(
            "the profile is not resolved, so which services run is unknown; run this "
            "through make, which writes .tmp/profile.mk"
        )
    reason = refusal(
        per_source_apps=_profile_mk_value(key=PER_SOURCE_APPS_VAR),
        renders=bool(_profile_mk_value(key=APP_CONFIG_DIR_KEY)),
    )
    if reason:
        raise DemoError(reason)
    checkout = resolve_generator(generator=args.generator)
    network = _network(service=RECEIVER_SERVICE)

    base = _engine_base()
    token = _token(base=base)
    replies = []
    variants = []
    for label in demo.sources:
        replies += _ensure_table(base=base, label=label, token=token)
        variants.append(_source_variant(base=base, label=label, token=token))
    intake = with_candidates(intake=demo.intake, variants=variants)
    replies += _ensure_intake(base=base, body=intake, token=token)
    replies.append(_switch_on_syslog(base=base, token=token))

    instance = f"{TRANSFORM_APP}-{intake['source']}"
    declared = instances.read_index(path=instances.index_file(service=TRANSFORM_APP))
    if intake["source"] not in declared:
        raise DemoError(f"dfe-engine declared no {instance} instance for the intake")
    services = runnable(
        declared=declared_instances(), hinted=hinted_services(replies=replies)
    )
    # The switch names no restart once it is set, so a receiver that never restarted after it is found by asking the socket table
    unready = {
        instance: not (_healthy(service=instance)),
        RECEIVER_SERVICE: not (_syslog_ready()),
    }
    services += [svc for svc, down in unready.items() if down and svc not in services]
    if services:
        _apply(dev=args.dev, environ=environ, services=services)
    _await_ready(instance=instance)

    # Lines no detector claims land in the intake's own table, and main is shown to prove none went there
    tables = [*demo.sources, intake["source"], TARGET_TABLE]
    before = _counts(tables=tables)
    feed = stage_feed(
        checkout=checkout,
        feed=FETCH_ROOT / "feed",
        generator=args.generator,
        samples=SAMPLES_DIR,
    )
    _stop_stream(container=args.container)
    command = generator_command(
        container=args.container,
        feed=feed,
        generator=args.generator,
        image=args.image,
        network=network,
        protocol=args.protocol,
        rate=args.rate,
        sources=demo.sources,
    )
    _print(
        msg=f"streaming {', '.join(demo.sources)} syslog over {args.protocol.upper()} at "
        f"{args.rate:g} lines/s in container {args.container}"
    )
    if subprocess.run(command, check=False).returncode != 0:
        raise DemoError(
            "the generator container did not start; docker's error is above"
        )

    gained = _await_rows(before=before, vendors=demo.sources)
    for table, rows in gained.items():
        _print(msg=f"  {TARGET_DB}.{table}: +{rows} rows")
    empty = [table for table in demo.sources if gained[table] <= 0]
    if empty:
        _print(
            msg=f"FAIL no rows landed in {', '.join(f'{TARGET_DB}.{t}' for t in empty)}; "
            f"`docker compose logs {instance}` says what the transform did with them "
            f"and `docker logs {args.container}` what the generator sent"
        )
        _stop_stream(container=args.container)
        return 1
    _print(
        msg="PASS every vendor table gained rows, ready to query in the console or "
        "HyperDX. The stream runs until `make demo-stop`"
    )
    return 0


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
        "--container", required=True, help="the name the stream's container runs under"
    )
    parser.add_argument(
        "--generator",
        required=True,
        help="the replay script's path inside the dfe-transform-elastic checkout",
    )
    parser.add_argument(
        "--image", required=True, help="the pinned Python image the generator runs in"
    )
    parser.add_argument(
        "--protocol", choices=("udp", "tcp"), default="tcp", help="syslog transport"
    )
    parser.add_argument("--rate", default=20.0, type=float, help="lines per second")
    args = parser.parse_args()
    if args.rate <= 0:
        parser.error("--rate must be positive")
    try:
        return _run(args=args)
    except (*ENGINE_FAULTS, DemoError, SeedError) as error:
        _print(msg=f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
