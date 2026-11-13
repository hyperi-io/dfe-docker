#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/demo.py
#  Purpose:      Send live vendor syslog through the running stack, detected per line by dfe-transform-elastic and landed one table per vendor
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Run the agentless syslog demo against the RUNNING stack.

The Sources are the POST /sources bodies under resources/demo/sources, one to a file, each taking its role from its body. The one with `transform.mode: agentless` is the intake: a dfe-transform-elastic Source on the receiver's syslog listener whose detect candidates are the vendor variants. One with a `fetcher` mapping pulls from its upstream into its own table beside the receiver's push. Every other is a vendor table holding its engine catalogue entry as `from-catalogue` writes it for the receiver intake, which the run checks against the engine's copy. The engine's own PyYAML reads each file, because this script runs on a bare python3.

A Source the engine lacks is created without its `state`, because a Source created dormant gets no table. One the engine holds whose working version differs from its file is written over with the file. Each is then deployed where that is outstanding and set to the `state` its file names, active when it names none. A dormant vendor table keeps filling from the intake with no per-vendor transform running.

The receiver's syslog listener, UDP and TCP on 514, is switched on through the engine and `make apply` recreates what the engine says has to change along with the intake's instance or the fetcher where either is not yet healthy. A fetcher the profile cannot run or the engine cannot set up is skipped with a warning, while an intake or vendor step that fails stops the run.

The generator is dfe-transform-elastic's own replay script and fixtures, read from DFE_SRC_ROOT/dfe-transform-elastic on a `make dev` stack and otherwise fetched shallow at the tag DFE_TRANSFORM_ELASTIC_VERSION pins so the lines match the detector that runs. Each vendor's resources/demo/events/<source>/ replaces the fixtures of the package its variant names. It runs detached in a pinned Python container on the stack's network, looping raw syslog at dfe-receiver until `make demo-stop` or `make down` removes it. A second run replaces it.

The run returns once every vendor table has gained rows. It fails, stopping the stream, when one gains none or the generator has exited. A fetcher table with no rows yet only warns, because a fetcher's first poll waits one interval.

Exit codes:
  0  every vendor table gained rows while the generator ran
  1  the stack cannot run the demo, a step failed, the generator exited or a vendor table gained no rows
  2  usage error
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import typing
from pathlib import Path
from urllib.parse import quote

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import _stack_state  # noqa: E402
import instances  # noqa: E402
from _common import (  # noqa: E402
    ENGINE_COMPONENT,
    FETCHER_SERVICE,
    PER_SOURCE_APP_KEYS,
    PER_SOURCE_APPS_VAR,
    REPO_ROOT,
    TRANSFORM_APP_PREFIX,
    _load_dotenv,
    _load_env_file,
    _print,
    _profile_mk_value,
    _rel_path,
    _resolved_services,
    in_stack,
)
from _pipeline import clickhouse_url  # noqa: E402
from _sources import hinted_services  # noqa: E402
from build_dev_images import SRC_REMOTE_DEFAULT  # noqa: E402
from post import (  # noqa: E402
    ADMIN_NAME_KEY,
    ADMIN_PASSWORD_KEY,
    APP_CONFIG_DIR_KEY,
    ENGINE_SERVICE,
    TARGET_DB,
    TARGET_TABLE,
    _api_get_json,
    _api_post_json,
    _decoded,
    _engine_base,
    _ensure_org,
    _exec_request,
    api_put_json,
)
from seed import (  # noqa: E402
    DEPLOY_TIMEOUT_SECONDS,
    ENGINE_FAULTS,
    KINDS,
    SeedError,
    _objects,
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
# The organisation and account that complete the engine's first-run setup
DEMO_ORG = "demo"
DEMO_ANALYST = "demo-analyst"
RECEIVER_SERVICE = "dfe-receiver"
# dfe-engine main reads the catalogue from its dfe-schemas wheel and older images from the seeded /app/schemas, so this tries both in that order, at any depth because newer wheels file each entry under its tag folder
CATALOGUE_READ = "\n".join(
    (
        "import json, pathlib, sys, dfe_schemas, yaml",
        "name = sys.argv[1] + '.yaml'",
        "for root in (dfe_schemas.schemas_root(), pathlib.Path('/app/schemas')):",
        "    path = next(iter(sorted((root / 'catalogue').rglob(name))), None)",
        "    if path is not None:",
        "        sys.stdout.write(json.dumps(yaml.safe_load(path.read_text(encoding='utf-8')), default=str))",
        "        break",
    )
)
# One YAML document on stdin as JSON on stdout with any refusal on stderr. A value JSON cannot carry, such as a date, is refused
YAML_TO_JSON = "\n".join(
    (
        "import json, sys, yaml",
        "try:",
        "    json.dump(yaml.safe_load(sys.stdin), sys.stdout)",
        "except (TypeError, yaml.YAMLError) as error:",
        "    sys.exit(str(error))",
    )
)
# What `from-catalogue` adds to an entry on the receiver intake (dfe-engine source/catalogue.py write_request_for and _match_for)
CATALOGUE_INTAKE = "receiver"
RECEIVER_MATCH_FIELD = "_source"
# The field an Elastic Agent's envelope names its source by, which a Beats source matches on and its events carry
BEATS_MATCH_FIELD = "data_stream.dataset"
# What replays a pack's Beats sources at the receiver: events/<REPLAY_TOOL> REPLAY_COMMAND --target --sources --loop --duration
REPLAY_TOOL = "generate.py"
REPLAY_COMMAND = "replay"
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
ARCHIVER_SERVICE = "dfe-archiver"
# Where dfe-archiver writes (file:// in config/archiver/kafka.yaml), which throwaway containers mount to count, show, prune and purge what it archived
ARCHIVE_MOUNT = "/var/lib/dfe-archiver/archive"
ARCHIVE_STORAGE = (
    '{{range .Mounts}}{{if eq .Destination "' + ARCHIVE_MOUNT + '"}}'
    "{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}"
)
ARCHIVE_TIMEOUT_SECONDS = 180.0
# Where the fetcher keeps its cursors, files per source, which outlive the source and would start a re-created one from the old window
CURSOR_MOUNT = "/var/lib/dfe-fetcher"
# Removes the cursor files of the sources named after the fetcher's directory, run with the cursor volume at /c
CURSOR_REMOVER = (
    "import pathlib, sys\n"
    "root = pathlib.Path('/c', sys.argv[1])\n"
    "for name in sys.argv[2:]:\n"
    "    for path in root.glob(f'{sys.argv[1]}.{name}.*'):\n"
    "        path.unlink()\n"
)
CURSOR_STORAGE = (
    '{{range .Mounts}}{{if eq .Destination "' + CURSOR_MOUNT + '"}}'
    "{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}"
)
ARCHIVE_TOOL = DEMO_DIR / "archive.py"
ARCHIVE_ROOT = "/a"
MANIFEST_FILE = DEMO_DIR / "demo.json"
# The sources this checkout's demo created, which is all `make demo-stop` deletes, so a source of the same name that someone else made is never taken out by `make down`
INSTALLED_FILE = FETCH_ROOT / "installed.json"
CONSOLE_DIR = DEMO_DIR / "console"
# dfe-hyperdx's API as dfe-engine reaches it, which accepts the engine's own token. Everything the pack puts in the console is a plain, deletable object whose name starts with CONSOLE_PREFIX
HYPERDX_API = "http://dfe-hyperdx:8000"
CONSOLE_PREFIX = "demo-"
# A pack's hunts and the rules they run, one engine create body per file as resources/seed holds them
# The upstream licence texts beside a pack's sample events, which no source replays
EVENTS_LICENSES_DIR = "LICENSES"
HUNTS_DIR_NAME = "hunts"
RULES_DIR_NAME = "rules"
# How long a rule or hunt create is tried again while the engine answers 5xx or not at all
HUNT_INSTALL_TIMEOUT_SECONDS = 60.0
HUNT_INSTALL_INTERVAL_SECONDS = 5.0
HYPERDX_SERVICE = "dfe-hyperdx"
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
    """A pack's Sources by role with the events directory of each vendor, Beats and JSON source. A pack with no vendor has no intake."""

    beats: list[dict]
    events: dict[str, Path]
    fetchers: list[dict]
    intake: dict | None
    json: list[dict]
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


def _archive_storage() -> str | None:
    """The volume or host path the archiver writes to, None when its container cannot be read."""
    container = _container(service=ARCHIVER_SERVICE)
    if not (container):
        return None
    mounted = _docker(args=["inspect", "--format", ARCHIVE_STORAGE, container])
    storage = mounted.stdout.strip()
    return storage if (mounted.returncode == 0) and storage else None


def _archive_tool(
    *, args: list[str], image: str, storage: str, write: bool = False
) -> subprocess.CompletedProcess:
    """Run resources/demo/archive.py in a throwaway container holding the archive volume."""
    return _docker(
        args=[
            "run",
            "--rm",
            "--volume",
            f"{storage}:{ARCHIVE_ROOT}{'' if write else ':ro'}",
            "--volume",
            f"{ARCHIVE_TOOL}:/archive.py:ro",
            image,
            "python3",
            "-I",
            "/archive.py",
            *args,
            "--root",
            ARCHIVE_ROOT,
        ],
        timeout=120,
    )


def _archived_objects(*, image: str, sources: list[str]) -> int | None:
    """How many files the archiver has written for the sources, None when they cannot be counted."""
    storage = _archive_storage()
    if storage is None:
        return None
    counted = _archive_tool(
        args=["count", "--sources", ",".join(sources)], image=image, storage=storage
    )
    try:
        return int(counted.stdout.strip())
    except ValueError:
        return None


def _archive_report(*, image: str, sources: list[str]) -> str:
    """The names and sizes of the newest archived objects and the first line of the newest."""
    storage = _archive_storage()
    if storage is None:
        return ""
    shown = _archive_tool(
        args=["show", "--sources", ",".join(sources)], image=image, storage=storage
    )
    return shown.stdout.strip()


def _await_archive(*, image: str, sources: list[str]) -> int | None:
    """Poll until the archive holds a file for the sources, then return how many it holds, None when it cannot be read."""
    deadline = time.monotonic() + ARCHIVE_TIMEOUT_SECONDS
    while True:
        now = _archived_objects(image=image, sources=sources)
        if (now is None) or (now > 0) or (time.monotonic() >= deadline):
            return now
        time.sleep(LAND_INTERVAL_SECONDS)


def _cursor_storage() -> str | None:
    """The volume holding the fetcher's cursors, None when no fetcher runs."""
    container = _container(service=FETCHER_SERVICE)
    if not (container):
        return None
    mounted = _docker(
        args=[
            "inspect",
            "--format",
            CURSOR_STORAGE,
            container,
        ]
    )
    return mounted.stdout.strip() or None


def _console_files(*, console: Path = CONSOLE_DIR, kind: str) -> list[dict]:
    """The console objects of one kind (dashboards or searches) in a pack's console directory, in name order."""
    bodies = []
    for path in sorted((console / kind).glob(pattern="*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DemoError(f"{path.name!r} is not readable JSON: {error}") from error
        if not (isinstance(body, dict)) or body.get("name") != path.stem:
            raise DemoError(
                f"{path.name!r} needs an object whose `name` is {path.stem!r}"
            )
        bodies.append(body)
    return bodies


def _console_url(*, path: str) -> str:
    """Where a browser on this host opens a dfe-hyperdx page."""
    origin = os.environ.get("DFE_HYPERDX_APP_URL", "").strip() or "http://localhost"
    port = os.environ.get("DFE_HYPERDX_APP_PORT", "").strip() or "8090"
    return f"{origin.rstrip('/')}:{port}{path}"


def _hyperdx(
    *, method: str, path: str, payload: dict | None, token: str
) -> tuple[int, typing.Any]:
    """One call to dfe-hyperdx's API from inside the stack, returning (status, decoded body)."""
    status, body = _exec_request(
        method=method,
        payload=payload,
        timeout=30,
        token=token,
        url=f"{HYPERDX_API}{path}",
    )
    return status, _decoded(raw=body)


def _hyperdx_list(*, path: str, token: str) -> list[dict]:
    status, answer = _hyperdx(method="GET", path=path, payload=None, token=token)
    if status != 200 or not (isinstance(answer, list)):
        raise DemoError(f"GET {path} on dfe-hyperdx returned HTTP {status}: {answer}")
    return [item for item in answer if isinstance(item, dict)]


def _remove_console(*, token: str) -> int:
    """Delete every dashboard and saved search whose name starts with the demo prefix, and return how many went."""
    removed = 0
    for path in ("/dashboards", "/saved-search"):
        for item in _hyperdx_list(path=path, token=token):
            name = str(item.get("name", ""))
            if not (name.startswith(CONSOLE_PREFIX)) or item.get("provisioned"):
                continue
            status, answer = _hyperdx(
                method="DELETE", path=f"{path}/{item['id']}", payload=None, token=token
            )
            if status not in (200, 204, 404):
                raise DemoError(f"deleting {name!r} returned HTTP {status}: {answer}")
            removed += 1
    return removed


def _install_console(*, console: Path = CONSOLE_DIR, token: str) -> list[str]:
    """Replace the pack's dashboards and saved searches in the console and return the lines that say where to open them.

    The files name a source by its name, and tiles that run SQL take the connection of that source.
    """
    sources = {
        item["name"]: item for item in _hyperdx_list(path="/sources", token=token)
    }
    _remove_console(token=token)
    links = []
    for kind, path, front in (
        ("dashboards", "/dashboards", "/dashboards"),
        ("searches", "/saved-search", "/search"),
    ):
        for body in _console_files(console=console, kind=kind):
            payload = resolve_console(body=body, sources=sources)
            status, made = _hyperdx(
                method="POST", path=path, payload=payload, token=token
            )
            if status != 200 or not (isinstance(made, dict)) or not (made.get("id")):
                raise DemoError(
                    f"creating {kind[:-1]} {body['name']!r} returned HTTP {status}: {made}"
                )
            tail = (
                "?isLive=true"
                if kind == "searches" and body["name"].endswith("-live")
                else ""
            )
            links.append(
                f"{body['name']}: {_console_url(path=f'{front}/{made['id']}{tail}')}"
            )
    return links


def _delete_source(*, base: str, name: str, token: str) -> None:
    """Delete one source through the engine, which takes its instance, receiver rule and topics with it. One the engine lacks is already gone."""
    status, answer = _exec_request(
        method="DELETE",
        payload=None,
        timeout=DEPLOY_TIMEOUT_SECONDS,
        token=token,
        url=_source_url(base=base, name=name),
    )
    if status not in (200, 202, 404):
        raise DemoError(
            f"deleting source {name!r} returned HTTP {status}: {_decoded(raw=answer)}"
        )
    if status != 404:
        _print(msg=f"deleted source {name!r}")


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
) -> dict[str, int | None]:
    """Poll until every vendor table has gained rows or the generator has exited, then return each table's gain so far, None where it cannot be read."""
    deadline = time.monotonic() + LAND_TIMEOUT_SECONDS
    while True:
        gained = row_gains(before=before, now=_counts(tables=list(before)))
        done = all((gained[table] or 0) > 0 for table in vendors)
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
        entry = json.loads(s=result.stdout)
    except json.JSONDecodeError as error:
        return f"the {ENGINE_SERVICE!r} catalogue entry {name!r} is not readable JSON: {error}"
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
        # `archive` is the demo's own choice on top of the catalogue entry
        drift = catalogue_drift(
            catalogue=entry, source={**body, "archive": WRITE_DEFAULTS["archive"]}
        )
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
    other = answer.get("conflicting_source") if isinstance(answer, dict) else None
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


def _complete_setup(*, base: str, token: str) -> None:
    """Make the organisation and the real user the engine's first-run setup asks for.

    Without them every console page opens on the setup wizard. Nobody logs in as the analyst, so its password is random and never shown.
    """
    org = _ensure_org(base=base, token=token, name=DEMO_ORG)
    if org.fault:
        raise DemoError(f"the demo organisation: {org.fault}")
    account = {
        "username": DEMO_ANALYST,
        "password": secrets.token_urlsafe(32),
        "name": "Demo analyst",
        "org": DEMO_ORG,
    }
    status, answer = _api_post_json(f"{base}/auth/accounts", account, token=token)
    if status not in (201, 409):
        raise DemoError(
            f"creating account {DEMO_ANALYST!r} returned HTTP {status}: {answer}"
        )
    _print(msg=f"setup complete: organisation {DEMO_ORG!r}, account {DEMO_ANALYST!r}")


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


def _docker(
    *, args: list[str], stdin: str | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run one docker command and capture its text output."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        text=True,
        timeout=timeout,
    )


def _engine_python(*, code: str, stdin: str) -> subprocess.CompletedProcess:
    """Run Python in the engine's container, for the PyYAML it carries, or on this interpreter inside the stack, where the dfe-docker image carries its own."""
    if in_stack():
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            input=stdin,
            text=True,
        )
    container = _container(service=ENGINE_SERVICE)
    if not (container) or not (_running(container=container)):
        raise DemoError(
            f"{ENGINE_SERVICE!r} is not running and reading the demo's YAML needs it. "
            f"Start the stack first"
        )
    return _docker(args=["exec", "-i", container, "python", "-c", code], stdin=stdin)


def _ensure_fetchers(
    *, base: str, fetchers: list[dict], per_source_apps: list[str], token: str
) -> tuple[list[dict], list[dict]]:
    """Bring each fetcher source to its file, warning for and skipping one that cannot be, then return the ones set up and the engine's replies."""
    if fetchers and (FETCHER_SERVICE not in per_source_apps):
        names = ", ".join(repr(body["source"]) for body in fetchers)
        _print(
            msg=f"WARNING this profile runs no {FETCHER_SERVICE!r}, so fetcher "
            f"source(s) {names} are skipped. single-kafka runs it"
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
    archive: tuple[list[str], str],
    before: dict[str, int | None],
    container: str,
    fetchers: list[dict],
    instance: str,
    vendors: list[str],
) -> int:
    """Wait for the vendor rows and the archived files, report every table and return the demo's exit code."""
    archiving, image = archive
    gained = _await_rows(before=before, container=container, vendors=vendors)
    for table, rows in gained.items():
        shown = "cannot be read" if rows is None else f"+{rows} rows"
        _print(msg=f"  {TARGET_DB}.{table}: {shown}")
    # A fetcher's table is reported whole, since its cursor holds back what a previous run already pulled
    landed = _counts(tables=[body["source"] for body in fetchers])
    for table, rows in landed.items():
        if rows is not None:
            _print(msg=f"  {TARGET_DB}.{table}: {rows} rows")
    for warning in fetcher_warnings(counts=landed, fetchers=fetchers):
        _print(msg=warning)
    unread = ", ".join(
        repr(f"{TARGET_DB}.{table}") for table in vendors if gained[table] is None
    )
    empty = ", ".join(
        repr(f"{TARGET_DB}.{table}")
        for table in vendors
        if gained[table] is not None and gained[table] <= 0
    )
    archived = None
    if archiving and not (empty or unread):
        archived = _await_archive(image=image, sources=archiving)
        _print(
            msg=f"  {ARCHIVER_SERVICE} archive of {', '.join(archiving)}: "
            f"{'unreadable' if archived is None else f'{archived} files'}"
        )
        for line in _archive_report(image=image, sources=archiving).splitlines():
            _print(msg=f"    {line}")
    unarchived = bool(archiving) and not (empty or unread) and not ((archived or 0) > 0)
    # Lines a previous stream sent can still land, so rows alone do not prove this run's generator worked
    if not (_running(container=container)):
        _print(
            msg=f"FAIL the generator in {container!r} exited, so the rows above may be "
            f"a previous stream's. Its last output:\n"
            f"{_generator_output(container=container)}"
        )
    elif unread:
        _print(
            msg=f"FAIL ClickHouse at {clickhouse_url()} answered no count for {unread}, "
            "so whether rows landed is unknown. CLICKHOUSE_URL names the address to read it at"
        )
    elif empty:
        _print(
            msg=f"FAIL no rows landed in {empty}. `docker compose logs {instance}` says "
            f"what the transform did with them. The generator's last output:\n"
            f"{_generator_output(container=container)}"
        )
    elif unarchived:
        _print(
            msg=f"FAIL {ARCHIVER_SERVICE} wrote no files for {', '.join(archiving)} "
            f"in {ARCHIVE_TIMEOUT_SECONDS:.0f}s. `docker compose logs {ARCHIVER_SERVICE}` "
            f"says why. An archiver that predates the DFE_ARCHIVER_ env names (the one "
            f"stack 2.2.1 pins, v1.7.39) never reaches the broker, so pin a newer "
            f"{ARCHIVER_SERVICE} in .env"
        )
    else:
        kept = " and the archiver wrote files" if archiving else ""
        _print(
            msg=f"PASS every vendor table gained rows{kept}, ready to query in the "
            f"console's Search. The stream runs until `make demo-stop` or a receiver "
            f"restart"
        )
        return 0
    _stop_stream(container=container)
    return 1


def _parsed_yaml(*, label: str, text: str) -> object:
    """One YAML document as the engine's PyYAML reads it, refused under label when it cannot."""
    result = _engine_python(code=YAML_TO_JSON, stdin=text)
    if result.returncode != 0:
        raise DemoError(f"{label} is not readable YAML: {result.stderr.strip()}")
    return json.loads(s=result.stdout)


def _patch_json(*, payload: dict, token: str, url: str) -> tuple[int, typing.Any]:
    """PATCH JSON from inside the stack and return (status, decoded body)."""
    status, body = _exec_request(
        method="PATCH", payload=payload, timeout=30, token=token, url=url
    )
    return status, _decoded(raw=body)


def _read_source(*, path: Path) -> dict:
    """One demo source file's POST /sources body, refused unless its `source` is the file's name."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DemoError(f"{path.name!r} is not readable YAML: {error}") from error
    body = _parsed_yaml(label=repr(path.name), text=text)
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
    """What a demo source is for: `fetcher` with a fetcher mapping, `intake` in agentless mode, `beats` matched on the Beats dataset, `json` matched on another field with no transform, which lands the JSON its pack replays as sent, else `vendor`."""
    if isinstance(body.get("fetcher"), dict):
        return "fetcher"
    transform = body.get("transform")
    if isinstance(transform, dict) and transform.get("mode") == "agentless":
        return "intake"
    match = body.get("match")
    if isinstance(match, dict) and match.get("field") == BEATS_MATCH_FIELD:
        return "beats"
    if isinstance(match, dict) and transform is None:
        return "json"
    return "vendor"


def _run(*, args: argparse.Namespace) -> int:
    """Install the demo pack on the running stack and return the exit code. A pack that does not install whole is removed again."""
    manifest = _read_manifest(path=MANIFEST_FILE)
    INSTALLED_FILE.parent.mkdir(exist_ok=True, parents=True)
    INSTALLED_FILE.write_text(
        json.dumps({"sources": manifest["sources"]}), encoding="utf-8"
    )
    try:
        code = _install(args=args, manifest=manifest)
    except (*ENGINE_FAULTS, DemoError, SeedError) as error:
        _print(msg=f"FAIL {error}")
        code = 1
    if code != 0:
        _print(msg="removing what the demo put in, so no part of it is left behind")
        _remove_pack(args=args, manifest=manifest)
    return code


def _install(*, args: argparse.Namespace, manifest: dict) -> int:
    """Create every source of the pack, start the stream and the pruner and wait for the rows and archived files."""
    environ = dict(os.environ)
    _load_env_file(
        component=ENGINE_COMPONENT, keys=(ADMIN_NAME_KEY, ADMIN_PASSWORD_KEY)
    )
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
    demo = load_demo(events=EVENTS_DIR, sources=SOURCES_DIR)
    check_manifest(demo=demo, manifest=manifest)
    if demo.intake is None:
        raise DemoError(
            f"{str(SOURCES_DIR)!r} holds no intake for `make demo` to stream to"
        )
    checkout = _resolve_generator(dev=args.dev, generator=args.generator)
    network = _network(service=RECEIVER_SERVICE)
    kept, dropped = archivable(
        archiver_runs=ARCHIVER_SERVICE in _resolved_services(),
        bodies=[*demo.vendors, demo.intake],
    )
    for name in dropped:
        _print(
            msg=f"WARNING source {name!r} archives in its file and this profile runs no "
            f"{ARCHIVER_SERVICE!r}, so it is created without archiving"
        )
    demo = demo._replace(intake=kept[-1], vendors=kept[:-1])
    _check_catalogue(vendors=demo.vendors)

    base = _engine_base()
    token = _token(base=base)
    _complete_setup(base=base, token=token)
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
    # Every fetcher source runs in the one dfe-fetcher, from the file dfe-engine merges
    pollers = [FETCHER_SERVICE] if fetchers else []
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
    if HYPERDX_SERVICE in _resolved_services():
        for line in _install_console(token=token):
            _print(msg=f"  console {line}")
    else:
        _print(
            msg=f"WARNING this profile runs no {HYPERDX_SERVICE!r}, so the demo's "
            f"dashboards and saved searches are not created"
        )

    vendors = [body["source"] for body in demo.vendors]
    # Lines no detector claims land in the intake's own table, while main is shown to prove none went there
    tables = [*vendors, demo.intake["source"], TARGET_TABLE]
    # A previous run's stream stops before the count, though lines it already sent can still land after it
    _stop_stream(container=args.container)
    before = _counts(tables=tables)
    archiving = [
        body["source"] for body in [*demo.vendors, demo.intake] if body.get("archive")
    ]
    storage = _archive_storage() if archiving else None
    if storage is not None:
        _start_pruner(
            archive=manifest["archive"],
            container=args.pruner,
            image=args.image,
            storage=storage,
        )
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
        archive=(archiving, args.image),
        before=before,
        container=args.container,
        fetchers=fetchers,
        instance=instance,
        vendors=vendors,
    )


def _read_manifest(*, path: Path) -> dict:
    """The demo pack's manifest, refused unless it names its sources and the archive's retention."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DemoError(f"{path.name!r} is not readable JSON: {error}") from error
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    archive = manifest.get("archive") if isinstance(manifest, dict) else None
    if not (isinstance(sources, list)) or not (
        all(isinstance(name, str) and name for name in sources)
    ):
        raise DemoError(f"{path.name!r} needs a `sources` list of source names")
    keys = ("max_megabytes", "retention_minutes", "sweep_minutes")
    if (
        not (isinstance(archive, dict))
        or not (isinstance(archive.get("sources"), list))
        or not (
            all(
                isinstance(archive.get(key), int | float) and archive[key] > 0
                for key in keys
            )
        )
    ):
        raise DemoError(
            f"{path.name!r} needs an `archive` mapping with `sources` and positive "
            f"{', '.join(keys)}"
        )
    console = manifest.get("console")
    if not (isinstance(console, dict)) or not (
        all(
            isinstance(console.get(kind), list)
            and all(isinstance(name, str) and name for name in console[kind])
            for kind in ("dashboards", "searches")
        )
    ):
        raise DemoError(
            f"{path.name!r} needs a `console` mapping with `dashboards` and `searches` name lists"
        )
    return manifest


def _installed_sources(*, manifest: dict) -> list[str]:
    """The pack's sources this checkout's demo installed, none when it never ran here."""
    try:
        recorded = json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))["sources"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return []
    return [name for name in manifest["sources"] if name in recorded]


def _remove_pack(*, args: argparse.Namespace, manifest: dict) -> None:
    """Take everything the demo put in out again: the stream, the pruner, the sources, the console objects, the cursors and the archived objects. A step that cannot be done warns, and the rest still run."""
    manifest = {**manifest, "sources": _installed_sources(manifest=manifest)}
    _stop_stream(container=args.container)
    _stop_stream(container=args.pruner)
    cursors = _cursor_storage()
    # A transform left running would recreate the topics the engine deletes with its source. The fetcher drops a deleted source on reload
    for name in manifest["sources"]:
        running = _container(
            service=instances.service_name(instance=name, service=TRANSFORM_APP)
        )
        if running:
            _docker(args=["rm", "--force", running])
    deleted = False
    if not (_container(service=ENGINE_SERVICE)):
        _print(msg="no engine container runs, so there are no sources to remove")
    else:
        try:
            _load_env_file(
                component=ENGINE_COMPONENT, keys=(ADMIN_NAME_KEY, ADMIN_PASSWORD_KEY)
            )
            _load_dotenv()
            base = _engine_base()
            token = _token(base=base)
            if _container(service=HYPERDX_SERVICE):
                _print(
                    msg=f"removed {_remove_console(token=token)} console dashboard(s) and saved search(es)"
                )
            for name in reversed(manifest["sources"]):
                _delete_source(base=base, name=name, token=token)
            deleted = True
        except (*ENGINE_FAULTS, DemoError, SeedError) as error:
            _print(
                msg=f"WARNING the demo's sources were not all removed, because {error}. "
                f"Run `make demo-stop` again once the stack is up"
            )
    if deleted:
        try:
            _apply(dev=args.dev, environ=dict(os.environ), services=[])
        except DemoError as error:
            _print(msg=f"WARNING {error}")
        INSTALLED_FILE.unlink(missing_ok=True)
        if cursors is not None:
            # Each cursor file is `<instance_id>.<source>...`, and dfe-engine binds the one fetcher's instance_id to its app name
            _docker(
                args=[
                    "run",
                    "--rm",
                    "--volume",
                    f"{cursors}:/c",
                    args.image,
                    "python3",
                    "-I",
                    "-c",
                    CURSOR_REMOVER,
                    FETCHER_SERVICE,
                    *manifest["sources"],
                ]
            )
    storage = _archive_storage()
    if storage is not None:
        purged = _archive_tool(
            args=["purge", "--sources", ",".join(manifest["archive"]["sources"])],
            image=args.image,
            storage=storage,
            write=True,
        )
        _print(msg=f"removed {purged.stdout.strip() or 0} archived file(s)")


def _start_pruner(*, archive: dict, container: str, image: str, storage: str) -> None:
    """Run the pruner beside the stack, which keeps the archive to its retention and size cap until the pack is removed."""
    _docker(args=["rm", "--force", container])
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--init",
            "--name",
            container,
            "--volume",
            f"{storage}:{ARCHIVE_ROOT}",
            "--volume",
            f"{ARCHIVE_TOOL}:/archive.py:ro",
            image,
            *pruner_command(archive=archive, tool="/archive.py"),
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        raise DemoError(f"the archive pruner did not start: {result.stderr.strip()}")
    _print(
        msg=f"pruning the archive every {archive['sweep_minutes']:g} min to "
        f"{archive['retention_minutes']:g} min and {archive['max_megabytes']:g} MB"
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
    # The catalogue is a few apps, so one page of the largest size holds it whole
    status, page = _api_get_json(token=token, url=f"{base}/apps?page_size=1000")
    names = receiver_instances(page=page)
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


def archivable(
    *, archiver_runs: bool, bodies: list[dict]
) -> tuple[list[dict], list[str]]:
    """The source bodies as the profile can run them, and the names that lost `archive`.

    On a gRPC profile no archiver runs, and the receiver answers 503 for a source that archives.
    """
    if archiver_runs:
        return bodies, []
    kept = [
        {**body, "archive": False} if body.get("archive") else body for body in bodies
    ]
    return kept, [body["source"] for body in bodies if body.get("archive")]


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


def check_manifest(*, demo: Demo | None, manifest: dict, root: Path = DEMO_DIR) -> None:
    """Refuse a pack whose manifest and files disagree, so nothing is installed that removal would not find. `demo` is None for a pack with no source files."""
    bodies = (
        [] if demo is None else [*demo.vendors, *demo.beats, *demo.json, *demo.fetchers]
    )
    if demo is not None and demo.intake is not None:
        bodies.append(demo.intake)
    found = sorted(body["source"] for body in bodies)
    listed = sorted(manifest["sources"])
    if found != listed:
        raise DemoError(
            f"{MANIFEST_FILE.name!r} lists sources {listed} and "
            f"{str(root / SOURCES_DIR.name)!r} holds {found}. Make them the same set"
        )
    console = root / CONSOLE_DIR.name
    for kind in ("dashboards", "searches"):
        found_console = sorted(
            body["name"] for body in _console_files(console=console, kind=kind)
        )
        if found_console != sorted(manifest["console"][kind]):
            raise DemoError(
                f"{MANIFEST_FILE.name!r} lists {kind} {sorted(manifest['console'][kind])} "
                f"and {str(console / kind)!r} holds {found_console}. Make them the same set"
            )
    hunts = pack_hunts(root=root)
    found_hunts = sorted(name for name, _ in hunts.hunts)
    listed_hunts = sorted(manifest.get("hunts", []))
    if found_hunts != listed_hunts:
        raise DemoError(
            f"{MANIFEST_FILE.name!r} lists hunts {listed_hunts} and "
            f"{str(root / HUNTS_DIR_NAME)!r} holds {found_hunts}. Make them the same set"
        )
    archiving = sorted(body["source"] for body in bodies if body.get("archive"))
    if archiving != sorted(manifest["archive"]["sources"]):
        raise DemoError(
            f"{MANIFEST_FILE.name!r} lists archive sources "
            f"{sorted(manifest['archive']['sources'])} and the files that archive are "
            f"{archiving}. Make them the same set"
        )


class PackHunts(typing.NamedTuple):
    """A pack's rules and hunts as (name, create body), each in file name order."""

    rules: list[tuple[str, dict]]
    hunts: list[tuple[str, dict]]


def pack_hunts(*, root: Path) -> PackHunts:
    """Read a pack's rules and hunts, refusing a hunt that runs a rule the pack does not carry.

    Args:
        root: The pack directory.

    Returns:
        The pack's rules and hunts, both empty for a pack with neither directory.

    Raises:
        DemoError: A file that is not a create body named for its object, or a hunt naming a rule with no file.
    """
    try:
        rules = _objects(directory=root / RULES_DIR_NAME, key=KINDS["rules"].key)
        hunts = _objects(directory=root / HUNTS_DIR_NAME, key=KINDS["hunts"].key)
    except SeedError as error:
        raise DemoError(str(error)) from error
    carried = {name for name, _ in rules}
    for name, body in hunts:
        missing = sorted(set(body.get("rules") or []) - carried)
        if missing:
            raise DemoError(
                f"hunt {name!r} runs rules {missing}, which {str(root / RULES_DIR_NAME)!r} "
                f"does not hold. Add each as <rule>.yaml"
            )
    return PackHunts(rules=rules, hunts=hunts)


def _engine_call(
    *, call: typing.Callable[[], tuple[int, typing.Any]]
) -> tuple[int, typing.Any]:
    """Run one engine call, again while it answers 5xx or not at all, up to HUNT_INSTALL_TIMEOUT_SECONDS.

    Returns:
        The last (status, answer), status 0 with the fault as the answer when the engine never answered.
    """
    deadline = time.monotonic() + HUNT_INSTALL_TIMEOUT_SECONDS
    while True:
        try:
            status, answer = call()
        except ENGINE_FAULTS as error:
            status, answer = 0, str(error)
        if (status != 0) and (status < 500):
            return status, answer
        if time.monotonic() >= deadline:
            return status, answer
        time.sleep(HUNT_INSTALL_INTERVAL_SECONDS)


def install_hunts(*, base: str, root: Path, token: str) -> list[str]:
    """Create each of a pack's rules, then its hunts, that the engine lacks, and return what was done.

    Rules go first because the engine runs a hunt only once every rule it names compiles. One the engine already holds is left as it is, so a re-run creates only what is missing. Run it after the sources are deployed, since a rule's preview reads its source's table.

    A hunt never stops the data path: a 5xx or no answer is tried again to a short deadline, and a final failure or a 4xx becomes a WARNING line. A hunt whose rule did not install is skipped, since the runner would fail on every run of it.

    Args:
        base: The engine's API base.
        root: The pack directory.
        token: An admin token.

    Returns:
        One line per rule and hunt: created, already held, or a WARNING with the engine's answer.
    """
    loaded = pack_hunts(root=root)
    lines = []
    failed: set[str] = set()
    for kind, objects in (("rules", loaded.rules), ("hunts", loaded.hunts)):
        route = KINDS[kind].route
        noun = KINDS[kind].noun
        for name, body in objects:
            missing = sorted(set(body.get("rules") or []) & failed)
            if missing:
                lines.append(
                    f"WARNING {noun} {name!r} skipped: rules {missing} did not install"
                )
                continue
            url = f"{base}{route}/{quote(safe='', string=name)}"
            status, answer = _engine_call(
                call=lambda url=url: _api_get_json(token=token, url=url)
            )
            if status == 200:
                lines.append(f"{noun} {name!r} already exists")
                continue
            if status == 404:
                status, answer = _engine_call(
                    call=lambda body=body, route=route: _api_post_json(
                        f"{base}{route}", body, token=token
                    )
                )
                if status == 201:
                    lines.append(f"created {noun} {name!r}")
                    continue
                step = "creating"
            else:
                step = "looking up"
            failed.add(name)
            lines.append(
                f"WARNING {noun} {name!r} not installed: {step} it got HTTP {status}: {answer}"
            )
    return lines


def resolve_console(*, body: dict, sources: dict[str, dict]) -> dict:
    """A console file as dfe-hyperdx takes it, each source name replaced by the source's id and each SQL tile given its source's connection."""

    def source_id(name: object) -> str:
        found = sources.get(str(name))
        if found is None:
            raise DemoError(
                f"{body['name']!r} names source {name!r}, which dfe-hyperdx does not hold"
            )
        return found["id"]

    resolved = {key: value for key, value in body.items()}
    if "source" in resolved:
        resolved["source"] = source_id(resolved["source"])
    tiles = []
    for tile in resolved.get("tiles", []):
        config = dict(tile["config"])
        owner = sources.get(str(config.get("source")))
        config["source"] = source_id(config.get("source"))
        if config.get("configType") == "sql" and owner is not None:
            config["connection"] = owner["connection"]
        tiles.append({**tile, "config": config})
    if "tiles" in resolved:
        resolved["tiles"] = tiles
    return resolved


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


def pruner_command(*, archive: dict, tool: str) -> list[str]:
    """The command that keeps the archive mounted at ARCHIVE_ROOT to the manifest's retention and size cap, running the archive tool at `tool`."""
    return [
        "python3",
        "-I",
        tool,
        "prune",
        "--root",
        ARCHIVE_ROOT,
        "--sources",
        ",".join(archive["sources"]),
        "--interval-minutes",
        str(archive["sweep_minutes"]),
        "--max-age-minutes",
        str(archive["retention_minutes"]),
        "--max-megabytes",
        str(archive["max_megabytes"]),
    ]


def load_demo(*, events: Path, sources: Path) -> Demo:
    """Read every source file under sources by role and pair each vendor, Beats and JSON source with its directory under events. A pack with no vendor needs no intake."""
    bodies = [
        _read_source(path=path) for path in sorted(sources.glob(pattern="*.yaml"))
    ]
    if not (bodies):
        raise DemoError(
            f"{str(sources)!r} holds no source files. Add one POST /sources body per "
            f"demo source as <source>.yaml"
        )
    beats = [body for body in bodies if _role(body=body) == "beats"]
    fetchers = [body for body in bodies if _role(body=body) == "fetcher"]
    intakes = [body for body in bodies if _role(body=body) == "intake"]
    vendors = [body for body in bodies if _role(body=body) == "vendor"]
    replayed = [body for body in bodies if _role(body=body) == "json"]
    intake = (
        _checked_intake(intakes=intakes, sources=sources, vendors=vendors)
        if intakes or vendors
        else None
    )
    names = [body["source"] for body in [*vendors, *beats, *replayed]]
    held = (
        {
            path.name: path
            for path in events.glob(pattern="*")
            if path.is_dir() and path.name != EVENTS_LICENSES_DIR
        }
        if events.is_dir()
        else {}
    )
    missing = [
        name for name in names if (name not in held) or not (any(held[name].iterdir()))
    ]
    if missing:
        layout = f"{events}/<source>/"
        raise DemoError(
            f"source(s) {', '.join(repr(name) for name in missing)} have no "
            f"events to replay. Add each as {layout!r} holding its lines"
        )
    orphans = sorted(set(held) - set(names))
    if orphans:
        raise DemoError(
            f"{str(events)!r} holds events for {', '.join(repr(name) for name in orphans)} "
            f"with no vendor, Beats or JSON source of that name in {str(sources)!r}. Add "
            f"the source or remove the directory"
        )
    for body in [*vendors, *beats, *replayed]:
        _check_events_kind(body=body, events=held[body["source"]])
    if (beats or replayed) and not ((events / REPLAY_TOOL).is_file()):
        raise DemoError(
            f"Beats and JSON source(s) "
            f"{', '.join(repr(body['source']) for body in [*beats, *replayed])} need "
            f"{str(events / REPLAY_TOOL)!r} to replay them with `{REPLAY_COMMAND}`"
        )
    return Demo(
        beats=beats,
        events={name: held[name] for name in names},
        fetchers=fetchers,
        intake=intake,
        json=replayed,
        vendors=vendors,
    )


def _checked_intake(*, intakes: list[dict], sources: Path, vendors: list[dict]) -> dict:
    """The one intake, refused unless it detects exactly the vendors' variants and no two vendors replay over one fixture package."""
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
    return intake


def first_event(*, events: Path) -> dict | None:
    """The first event in a source's events directory as a JSON object, None when it holds no file or that line is not one."""
    files = sorted(path for path in events.iterdir() if path.is_file())
    if not (files):
        return None
    with files[0].open(encoding="utf-8", errors="replace") as handle:
        first = next((line for line in handle if line.strip()), "")
    try:
        record = json.loads(first)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def field_value(*, field: str, record: dict) -> object:
    """The value at a dotted field of a record, walked the way the receiver walks a match field, None where the path breaks."""
    value: object = record
    for part in field.split(sep="."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def beats_dataset(*, events: Path) -> str | None:
    """The `data_stream.dataset` of the first event in a source's events directory, None when its events are not Beats envelopes."""
    record = first_event(events=events)
    dataset = (
        None if record is None else field_value(field=BEATS_MATCH_FIELD, record=record)
    )
    return dataset if isinstance(dataset, str) and dataset else None


def _check_events_kind(*, body: dict, events: Path) -> None:
    """Refuse a source whose events the receiver would not route to it: Beats envelopes for its dataset when it matches on one, JSON carrying its match value when it is a JSON source, raw lines otherwise."""
    name = body["source"]
    dataset = beats_dataset(events=events)
    if _role(body=body) == "json":
        field, wanted = body["match"].get("field"), body["match"].get("value")
        record = first_event(events=events)
        held = None if record is None else field_value(field=str(field), record=record)
        if held != wanted:
            raise DemoError(
                f"{name!r} matches {field} {wanted!r} and the first event in "
                f"{str(events)!r} carries {held!r}. Set {field} on every record"
            )
        return
    if _role(body=body) == "vendor":
        if dataset is not None:
            raise DemoError(
                f"{name!r} replays Beats envelopes for {dataset!r}, which the receiver "
                f"routes on {BEATS_MATCH_FIELD}. Match the source on {BEATS_MATCH_FIELD} "
                f"equals {dataset!r}"
            )
        return
    wanted = body["match"].get("value")
    if dataset != wanted:
        raise DemoError(
            f"{name!r} matches {BEATS_MATCH_FIELD} {wanted!r} and its events in "
            f"{str(events)!r} carry {dataset!r}. Replay Beats envelopes for {wanted!r}"
        )
    transform = body.get("transform")
    engine = transform.get("engine") if isinstance(transform, dict) else None
    if engine != TRANSFORM_APP.removeprefix(TRANSFORM_APP_PREFIX):
        raise DemoError(
            f"Beats source {name!r} runs transform engine {engine!r}. A pack declares "
            f"{TRANSFORM_APP} instances only, so set `transform.engine: "
            f"{TRANSFORM_APP.removeprefix(TRANSFORM_APP_PREFIX)}`"
        )


def row_gains(
    *, before: dict[str, int | None], now: dict[str, int | None]
) -> dict[str, int | None]:
    """The rows each table gained, None for a table ClickHouse gave no count for now, so an unreachable ClickHouse never reads as no rows."""
    return {
        table: None
        if now.get(table) is None
        else (now[table] or 0) - (before.get(table) or 0)
        for table in before
    }


def receiver_instances(*, page: object) -> list[str]:
    """The dfe-receiver instances a GET /apps page lists, none when it is no page or lists no receiver."""
    items = page.get("items") if isinstance(page, dict) else None
    listed = items if isinstance(items, list) else []
    receiver = next(
        (
            entry
            for entry in listed
            if isinstance(entry, dict) and entry.get("service") == RECEIVER_SERVICE
        ),
        {},
    )
    return [str(name) for name in (receiver.get("instances")) or ([])]


def refusal(*, per_source_apps: list[str], renders: bool) -> str:
    """Why this profile cannot run the demo, empty when it can."""
    if not (renders):
        return (
            "this profile's apps read a committed config, so dfe-engine cannot define "
            "the demo's sources. Turn the core footprint on"
        )
    if TRANSFORM_APP not in per_source_apps:
        return (
            f"this profile runs no {TRANSFORM_APP!r}. Use single-kafka "
            f"(DFE_PROFILE in .env), which runs it per source"
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
        "--remove",
        action="store_true",
        help="take the whole demo pack out of the stack instead of installing it",
    )
    parser.add_argument(
        "--container", help="the name the stream's container runs under", required=True
    )
    parser.add_argument(
        "--pruner",
        help="the name the archive pruner's container runs under",
        required=True,
    )
    parser.add_argument(
        "--image", help="the pinned Python image the helpers run in", required=True
    )
    parser.add_argument(
        "--generator",
        help="the replay script's path inside the dfe-transform-elastic checkout",
    )
    parser.add_argument("--protocol", choices=("udp", "tcp"), help="syslog transport")
    parser.add_argument("--rate", help="lines per second", type=float)
    args = parser.parse_args()
    if not (args.remove):
        if not (args.generator) or not (args.protocol) or args.rate is None:
            parser.error(
                message="--generator, --protocol and --rate are required to install"
            )
        if args.rate <= 0:
            parser.error(message="--rate must be positive")
    try:
        if args.remove:
            _remove_pack(args=args, manifest=_read_manifest(path=MANIFEST_FILE))
            return 0
        return _run(args=args)
    except (*ENGINE_FAULTS, DemoError, SeedError) as error:
        _print(msg=f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
