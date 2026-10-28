#!/usr/bin/env -S uv run --locked --script
#  Project:      dfe-docker
#  File:         scripts/seed.py
#  Purpose:      Create the starter sources, rules, hunts, orgs and accounts in resources/seed that the running engine lacks and send its fake events
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

"""Create one part of the starter set in resources/seed on the running stack or send its fake events.

The hunts target also creates the rules, because the engine refuses a hunt whose rule does not exist. The accounts target creates the orgs first for the same reason, since the engine refuses an account naming an org it does not hold. Nothing else is created for a target: a rule over a source that is not deployed yet is accepted and reads nothing until it is. A hunt whose table does not exist yet is not created, because the hunt runner fails on every run of it.

Each sources, rules, hunts, orgs or accounts file is the body the engine's create route takes, named for the object it creates. An account file carries no password. A new account gets the one env/engine.env already records for it as DFE_SEED_ACCOUNT_<USERNAME>_PASSWORD. Without one it gets a fresh 24-character password, recorded there before the account is created. So a volume wipe brings the account back on the same password, which `make creds` hands over. No password is ever printed. An object the engine already holds is left as it is, so a re-run creates only what is missing. The one exception is a held source that was never deployed, which is deployed. A source whose fetcher or transform app the profile does not run is skipped. The sources end with a request for the engine to bring every app in step with them, which retries a deploy whose apps did not follow. A restart the engine asks for is printed, not run: `make apply` on a `make dev` stack needs DEV=1, which this script cannot know.

Each events file is a list of records for the source it is named for. They go to dfe-receiver, which routes them by `_source` over whichever transport the stack runs, and the run waits until the source's table holds them. Events for a source the profile cannot run are skipped. Every run sends them again, and needs no login.

The other targets log in as the bootstrap admin with the password in env/engine.env. Once the engine has retired that admin, they ask at the terminal for an account that can write what the target seeds and keep that login only for the run.

Exit codes:
  0  every object the target creates exists, or every event landed
  1  no login, or an object or event could not be read, checked, created, deployed, sent or seen to land
  2  usage error
"""

import argparse
import getpass
import http.client
import json
import re
import subprocess
import sys
import time
import typing
from pathlib import Path
from urllib.parse import quote

import yaml

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    ENGINE_COMPONENT,
    FETCHER_SERVICE,
    PER_SOURCE_APP_KEYS,
    PER_SOURCE_APPS_VAR,
    SEED_DIR,
    SOURCE_NAME,
    TRANSFORM_APP_PREFIX,
    _env_file,
    _env_file_values,
    _load_dotenv,
    _load_env_file,
    _print,
    _profile_mk_value,
    _rel_path,
    _resolved_services,
    seed_password_key,
)
from _pipeline import ch_int, escape_literal, http_post  # noqa: E402
from init import _LEN_CREDENTIAL, _generate_secret  # noqa: E402
from post import (  # noqa: E402
    ADMIN_PASSWORD_KEY,
    APP_CONFIG_DIR_KEY,
    TARGET_DB,
    ApiUnreachable,
    IngestNotReady,
    NoIngestComponent,
    _admin_retired,
    _api_get_json,
    _api_post_json,
    _dotenv_with,
    _engine_base,
    _ingest_target,
    _refresh_access_summary,
    _replace_file,
    _request_token,
    _without_passwords,
    login,
)


class Kind(typing.NamedTuple):
    """How the engine names and reaches one kind of seed object."""

    key: str
    noun: str
    route: str


class SeedError(Exception):
    """A seed object or event that could not be read, checked, created, deployed, sent or seen to land."""


# Heads the seed account passwords this appends to env/engine.env
ACCOUNT_PASSWORD_BANNER = "## Seed account passwords, handed over by `make creds`"
# A deploy runs DDL, topic creation and app overlay commits, and its restart hints are only ever reported once.
DEPLOY_TIMEOUT_SECONDS = 90
EVENTS = "events"
# Every engine call the seeder makes can fail in one of these ways without the engine answering.
ENGINE_FAULTS = (ApiUnreachable, OSError, subprocess.TimeoutExpired)
KINDS = {
    "accounts": Kind(key="username", noun="account", route="/auth/accounts"),
    "hunts": Kind(key="name", noun="hunt", route="/hunts"),
    "orgs": Kind(key="name", noun="org", route="/orgs"),
    "rules": Kind(key="name", noun="rule", route="/rules"),
    "sources": Kind(key="source", noun="source", route="/sources"),
}
LANDING_INTERVAL_SECONDS = 3.0
LANDING_TIMEOUT_SECONDS = 60.0
# A hunt's `global_source_table_name`, the `<database>.<source>` table a source's deploy creates.
HUNT_TABLE = re.compile(
    rf"(?P<database>[A-Za-z_][A-Za-z0-9_]*)\.(?P<table>{SOURCE_NAME.pattern})"
)
# What each target seeds in order, since the engine refuses a hunt or an account naming a rule or org it does not hold
TARGETS = {
    "accounts": ("orgs", "accounts"),
    EVENTS: (EVENTS,),
    "hunts": ("rules", "hunts"),
    "orgs": ("orgs",),
    "rules": ("rules",),
    "sources": ("sources",),
}


def _account_password(*, username: str) -> str:
    """The password a new seed account is created with: the one env/engine.env records for it, else a fresh one recorded there first."""
    key = seed_password_key(username=username)
    recorded = _env_file_values(component=ENGINE_COMPONENT).get(key, "")
    if recorded:
        return recorded
    password = _generate_secret(length=_LEN_CREDENTIAL)
    _record_account_password(key=key, password=password)
    return password


def _action(*, status: int) -> str:
    """What a GET of one object says to do: create it, leave it or report a fault."""
    if status == 200:
        return "exists"
    if status == 404:
        return "create"
    return "fault"


def _await_landing(*, expected: int, source: str) -> None:
    """Wait until the source's table holds expected rows, which proves the receiver routed the events there."""
    deadline = time.monotonic() + LANDING_TIMEOUT_SECONDS
    while True:
        count = _row_count(source=source)
        if count is not None and count >= expected:
            return
        if time.monotonic() >= deadline:
            raise SeedError(
                f"{TARGET_DB}.{source} holds {count} of the {expected} rows expected after "
                f"{LANDING_TIMEOUT_SECONDS:.0f}s, so the receiver did not route the events "
                f"there; run `make events` again once the engine has pushed the source's route"
            )
        time.sleep(LANDING_INTERVAL_SECONDS)


def _create(*, base: str, body: dict, kind: str, name: str, token: str) -> None:
    """Create one object, deploying it when it is a source and handing over its password when it is an account."""
    route = KINDS[kind].route
    status, answer = _api_post_json(payload=body, token=token, url=f"{base}{route}")
    if status != 201:
        fault = f"POST {route!r} returned HTTP {status}: {answer}"
        # A refusal can echo the request, the account's password included
        if body.get("password"):
            fault = _without_passwords(current="", new=body["password"], text=fault)
        raise SeedError(fault)
    _print(msg=f"created {KINDS[kind].noun} {name!r}")
    _print_hints(answer=answer)
    if kind == "sources":
        _deploy(base=base, name=name, token=token)
    if kind == "accounts":
        engine_env = _rel_path(path=_env_file(component=ENGINE_COMPONENT))
        _print(
            msg=f"  its password is {seed_password_key(username=name)} in "
            f"{engine_env}, which `make creds` hands over"
        )
        _refresh_access_summary()


def _deploy(*, base: str, name: str, token: str) -> None:
    """Deploy one source, which creates its landing table and routes its records."""
    path = f"/sources/{quote(safe='', string=name)}/deploy"
    status, answer = _api_post_json(
        payload={}, timeout=DEPLOY_TIMEOUT_SECONDS, token=token, url=f"{base}{path}"
    )
    _print_hints(answer=answer)
    fault = deploy_fault(answer=answer, status=status)
    if fault:
        raise SeedError(fault)
    _print(msg=f"deployed source {name!r}")
    if answer.get("apps_sync_error"):
        _print(
            msg=f"  the apps did not take it ({answer['apps_sync_error']}); the "
            f"reconcile that ends this run retries it"
        )


def _deployed(*, body: object) -> bool:
    """Whether a GET of one source names a deployed version."""
    return isinstance(body, dict) and bool(body.get("deployed_version"))


def _ensure(*, base: str, body: dict, kind: str, name: str, token: str) -> None:
    """Create one object unless the engine holds it, and deploy a source it holds undeployed."""
    if kind == "accounts" and "password" in body:
        raise SeedError(
            f"{name!r} sets a password, which a seed file must not carry: `make "
            f"accounts` mints one into env/engine.env instead"
        )
    path = f"{KINDS[kind].route}/{quote(safe='', string=name)}"
    status, answer = _api_get_json(token=token, url=f"{base}{path}")
    action = _action(status=status)
    if action == "fault":
        raise SeedError(f"GET {path!r} returned HTTP {status}: {answer}")
    if action == "create" and kind == "hunts":
        missing = _missing_table(body=body)
        if missing:
            _print(msg=f"SKIP {KINDS[kind].noun} {name!r}: {missing}")
            return
    if action == "create" and kind == "accounts":
        body = {**body, "password": _account_password(username=name)}
    if action == "create":
        _create(base=base, body=body, kind=kind, name=name, token=token)
        return
    _print(msg=f"{KINDS[kind].noun} {name!r} already exists")
    if kind == "sources" and not (_deployed(body=answer)):
        _deploy(base=base, name=name, token=token)


def _events(*, directory: Path) -> list[tuple[str, list[dict]]]:
    """Read every events file in directory, in name order, as (source, records)."""
    events = []
    for path in sorted(directory.glob(pattern="*.yaml")):
        # The engine's own rule, which also keeps the name safe to quote into SQL
        if not (SOURCE_NAME.fullmatch(string=path.stem)):
            raise SeedError(f"{path.name!r} is not named for a valid source")
        records = _read(path=path)
        if not (isinstance(records, list)) or not (
            all(isinstance(record, dict) for record in records)
        ):
            raise SeedError(f"{path.name!r} must hold a list of records")
        events.append((path.stem, records))
    return events


def _hunt_table(*, body: dict) -> tuple[str, str]:
    """The database and table one hunt reads."""
    value = body.get("global_source_table_name")
    found = HUNT_TABLE.fullmatch(string=value) if isinstance(value, str) else None
    if not (found):
        raise SeedError(
            f"global_source_table_name {value!r} is not a <database>.<source> table"
        )
    return found["database"], found["table"]


def _missing_table(*, body: dict) -> str:
    """Why one hunt is not created yet because its table is missing, or an empty string when the table exists."""
    database, table = _hunt_table(body=body)
    if _table_exists(database=database, table=table):
        return ""
    return (
        f"{f'{database}.{table}'!r} does not exist, so the hunt runner would fail on "
        f"every run of it; `make sources` creates it on a profile that runs its source"
    )


def _objects(*, directory: Path, key: str) -> list[tuple[str, dict]]:
    """Read every seed file in directory, in name order, as (name, request body)."""
    objects = []
    for path in sorted(directory.glob(pattern="*.yaml")):
        body = _read(path=path)
        if not (isinstance(body, dict)):
            raise SeedError(
                f"{path.name!r} holds a {type(body).__name__}, not a request body"
            )
        if body.get(key) != path.stem:
            raise SeedError(
                f"{path.name!r} sets {key!r} to {body.get(key)!r}, and a seed file "
                f"must be named for the object it creates"
            )
        objects.append((path.stem, body))
    return objects


def _print_hints(*, answer: object) -> None:
    """Print each restart the engine says an app needs, verbatim."""
    if not (isinstance(answer, dict)):
        return
    for hint in answer.get("restart_required") or []:
        _print(msg=f"  {hint}")


def _prompted_token(*, base: str, terminal: bool) -> str:
    """Log in as the account the person at the terminal names, on a stack whose bootstrap admin is retired."""
    if not (terminal):
        raise SeedError(
            "the bootstrap admin is retired and there is no terminal to ask for "
            "another login on, so run this from a terminal"
        )
    _print(
        msg="The bootstrap admin is retired. Log in as an account that can write "
        "what this target seeds."
    )
    try:
        username = input("username: ").strip()
        password = getpass.getpass(prompt="password: ")
    except (EOFError, KeyboardInterrupt) as error:
        raise SeedError("no login was given") from error
    token, status, change_required = _request_token(
        base=base, password=password, username=username
    )
    if (status != 200) or not (token):
        raise SeedError(
            _refusal(
                fault=f"login as {username!r} returned HTTP {status}",
                prompted=True,
                status=status,
            )
        )
    if change_required:
        raise SeedError(
            f"{username!r} is still on the password it was issued; change it in the "
            f"console, then run this again"
        )
    return token


def _read(*, path: Path) -> object:
    """Parse one seed file, which has to hold only values JSON can carry."""
    try:
        content = yaml.safe_load(stream=path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SeedError(f"{path.name!r} is not readable YAML: {error}") from error
    try:
        json.dumps(obj=content)
    except (TypeError, ValueError) as error:
        raise SeedError(
            f"{path.name!r} holds a value JSON cannot carry, such as an unquoted "
            f"date: {error}"
        ) from error
    return content


def _reconcile(*, base: str, token: str) -> None:
    """Ask the engine to bring every app in step with the sources, which retries a deploy whose apps did not follow."""
    status, answer = _api_post_json(
        payload={},
        timeout=DEPLOY_TIMEOUT_SECONDS,
        token=token,
        url=f"{base}/sources/reconcile-apps",
    )
    # A stack without gitops keeps no derived app state to reconcile.
    if status == 503:
        return
    if status != 200:
        raise SeedError(
            f"POST '/sources/reconcile-apps' returned HTTP {status}: {answer}"
        )
    _print_hints(answer=answer)


def _record_account_password(*, key: str, password: str) -> None:
    """Record a seed account's password in env/engine.env the way POST records the admin's, atomically and keeping the file's mode."""
    path = _env_file(component=ENGINE_COMPONENT)
    try:
        text = path.read_text(encoding="utf-8")
        _replace_file(
            path=path,
            text=_dotenv_with(
                banner=ACCOUNT_PASSWORD_BANNER, key=key, text=text, value=password
            ),
        )
    except OSError as error:
        raise SeedError(
            f"{key} cannot be recorded in {_rel_path(path=path)} ({error.strerror}), "
            f"so the account is not created"
        ) from error


def _refusal(*, fault: str, prompted: bool, status: int) -> str:
    """A refused login's fault, with the account to use instead where this end can fix it."""
    engine_env = _rel_path(path=_env_file(component=ENGINE_COMPONENT))
    if prompted and status == 401:
        step = "log in as an account that can write what this target seeds, with its current password"
    elif prompted:
        step = ""
    elif status == 0:
        step = f"set {ADMIN_PASSWORD_KEY} in {engine_env}, which `make init` generates"
    elif status == 401:
        step = f"{ADMIN_PASSWORD_KEY} in {engine_env} is not the bootstrap admin's current password, so set it to that and run this again"
    else:
        step = ""
    return f"{fault}; {step}" if step else fault


def _row_count(*, source: str) -> int | None:
    """The rows in one source's table, or None when the table cannot be read."""
    return ch_int(sql=f"SELECT count() FROM {TARGET_DB}.`{source}`")


def _seed_kind(
    *,
    base: str,
    kind: str,
    objects: list[tuple[str, dict]],
    per_source_apps: list[str],
    token: str,
) -> int:
    """Create every object of one kind the engine lacks and this stack can run, and return how many failed."""
    failed = 0
    for name, body in objects:
        try:
            reason = skip_reason(body=body, kind=kind, per_source_apps=per_source_apps)
            if reason:
                _print(msg=f"SKIP {KINDS[kind].noun} {name!r}: {reason}")
                continue
            _ensure(base=base, body=body, kind=kind, name=name, token=token)
        except (*ENGINE_FAULTS, SeedError) as error:
            _print(msg=f"FAIL {KINDS[kind].noun} {name!r}: {error}")
            failed += 1
    if kind == "sources":
        try:
            _reconcile(base=base, token=token)
        except (*ENGINE_FAULTS, SeedError) as error:
            _print(msg=f"FAIL {error}")
            failed += 1
    return failed


def _send(*, records: list[dict], source: str) -> None:
    """Post each record to the receiver labelled for its source, and wait until all of them land in its table."""
    if not (_profile_mk_value(key=APP_CONFIG_DIR_KEY)):
        raise SeedError(
            f"this profile's apps read a committed config, so the receiver has no "
            f"route for source {source!r}"
        )
    service, url = _ingest_target(table=source)
    if service != "dfe-receiver":
        raise SeedError(
            f"events go through dfe-receiver, which routes them by _source, and this "
            f"profile's ingest edge is {service!r}"
        )
    before = _row_count(source=source)
    if before is None:
        raise SeedError(
            f"{TARGET_DB}.{source} cannot be read; run `make sources` first"
        )
    for record in records:
        status = http_post(body=_tagged(record=record, source=source), url=url)
        if (status < 200) or (status >= 300):
            raise SeedError(f"{url!r} answered HTTP {status} for source {source!r}")
    _await_landing(expected=before + len(records), source=source)
    _print(msg=f"sent {len(records)} event(s) to {TARGET_DB}.{source}")


def _send_events(
    *,
    events: list[tuple[str, list[dict]]],
    per_source_apps: list[str],
    sources: dict[str, dict],
) -> int:
    """Send every events file's records whose source this stack can run, and return 1 at the first that cannot be sent or seen to land."""
    try:
        for source, records in events:
            reason = skip_reason(
                body=sources.get(source, {}),
                kind="sources",
                per_source_apps=per_source_apps,
            )
            if reason:
                _print(msg=f"SKIP events for source {source!r}: {reason}")
                continue
            _send(records=records, source=source)
    except (
        http.client.HTTPException,
        IngestNotReady,
        NoIngestComponent,
        OSError,
        SeedError,
    ) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


def _source_apps(*, body: dict) -> list[str]:
    """The per-source apps dfe-engine starts for one source: its fetcher and its transform."""
    apps = []
    if body.get("fetcher"):
        apps.append(FETCHER_SERVICE)
    transform = body.get("transform")
    if isinstance(transform, dict) and transform.get("engine"):
        apps.append(f"{TRANSFORM_APP_PREFIX}{transform['engine']}")
    return apps


def _table_exists(*, database: str, table: str) -> bool:
    """Whether ClickHouse holds one table."""
    count = ch_int(
        sql=f"SELECT count() FROM system.tables WHERE database = "
        f"'{escape_literal(value=database)}' AND name = '{escape_literal(value=table)}'"
    )
    if count is None:
        raise SeedError(
            f"ClickHouse cannot be read, so whether {f'{database}.{table}'!r} "
            f"exists is unknown"
        )
    return count > 0


def _tagged(*, record: dict, source: str) -> str:
    """The JSON body for one record, labelled with the source the receiver routes it to."""
    return json.dumps(obj={**record, "_source": source})


def _token(*, base: str) -> str:
    """A token for the seed writes: the bootstrap admin's, or once it is retired, one for an account named at the terminal."""
    if _admin_retired(base=base):
        return _prompted_token(base=base, terminal=sys.stdin.isatty())
    signed_in = login(base=base)
    if signed_in.fault:
        raise SeedError(
            _refusal(fault=signed_in.fault, prompted=False, status=signed_in.status)
        )
    return signed_in.token


def deploy_fault(*, answer: object, status: int) -> str:
    """Why a deploy answer leaves its source not live, empty when it is live."""
    if (
        (status != 200)
        or not (isinstance(answer, dict))
        or (answer.get("applied") is not True)
    ):
        return f"the deploy answered HTTP {status}: {answer!r}"
    if answer.get("topics_failed"):
        return (
            f"the deploy could not create topics {answer['topics_failed']}, so the "
            f"source has nowhere to land"
        )
    return ""


def skip_reason(*, body: dict, kind: str, per_source_apps: list[str]) -> str:
    """Why this profile cannot run one object, empty when it can."""
    if kind != "sources":
        return ""
    for app in _source_apps(body=body):
        if app not in per_source_apps:
            key = PER_SOURCE_APP_KEYS.get(app)
            return (
                f"this profile sets no {key}: true, so no {app} runs it"
                if (key)
                else f"no profile runs {app}"
            )
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "target",
        choices=sorted(TARGETS),
        help="what to seed: one kind of object, hunts with their rules, accounts with their orgs or events to send",
    )
    kinds = TARGETS[parser.parse_args().target]
    _load_env_file(component=ENGINE_COMPONENT, keys=(ADMIN_PASSWORD_KEY,))
    _load_dotenv()
    if not (_resolved_services()):
        _print(
            msg="FAIL the profile is not resolved, so which services run is unknown; "
            "run this through make, which writes .tmp/profile.mk"
        )
        return 1
    per_source_apps = _profile_mk_value(key=PER_SOURCE_APPS_VAR)

    if EVENTS in kinds:
        try:
            events = _events(directory=SEED_DIR / EVENTS)
            sources = _objects(directory=SEED_DIR / "sources", key=KINDS["sources"].key)
        except SeedError as error:
            _print(msg=f"FAIL {error}")
            return 1
        return _send_events(
            events=events, per_source_apps=per_source_apps, sources=dict(sources)
        )

    try:
        objects = {
            kind: _objects(directory=SEED_DIR / kind, key=KINDS[kind].key)
            for kind in kinds
        }
    except SeedError as error:
        _print(msg=f"FAIL {error}")
        return 1

    base = _engine_base()
    try:
        token = _token(base=base)
    except (*ENGINE_FAULTS, SeedError) as error:
        _print(msg=f"FAIL {error}")
        return 1

    # A hunt is refused while its rule is missing, so the run stops after the first kind that failed.
    for kind, kind_objects in objects.items():
        if _seed_kind(
            base=base,
            kind=kind,
            objects=kind_objects,
            per_source_apps=per_source_apps,
            token=token,
        ):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
