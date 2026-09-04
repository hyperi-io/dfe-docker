#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/post.py
#  Purpose:      Power-on self test - prove the running stack moves data end to end
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Power-on self test (POST) for a stack that is ALREADY running.

Four claims. INGEST: uniquely-marked events put in at the ingest edge come out as
those exact rows in ClickHouse. SELF-MONITORING: when the profile runs a collector,
the stack's own telemetry is landing FRESH in the otel database. CONSOLE: those
same marked rows read back through the engine query API that dfe-ui uses, so the
data is not merely stored but reachable from the surface an operator works in.
HUNTS: a hunt created through the API while the runner is already running is picked
up and executed, and its detections land in the detection table. None of the four is
"the containers started" or "the ports answer".

Transport-agnostic by construction. It posts to the ingest edge and reads
ClickHouse, so it works identically on the Kafka and the gRPC (kafka-less)
profiles -- there is nothing in here that knows which is in play, and that is
deliberate. A self test that only worked on one transport would be a self test
for the transport, not for the stack.

OPT-OUT, not opt-in: it runs unless `DFE_POST_ENABLED=false`. Something that only
runs when you remember to ask for it is not a power-on self test.

Exit codes:
  0  the pipeline moved the marked events (or the POST was skipped for a stated
     reason -- disabled, or the running profile has no ingest component)
  1  the events did not land, or landed unverifiably

Deliberately NOT a Docker HEALTHCHECK or a component entrypoint step. This
assertion is cross-service and needs the whole stack up, which no single
container can see; and a HEALTHCHECK repeats forever, so it would re-inject test
data into a production ingest path on a timer. See docs and the plan for the
per-component preflight (`selftest`) that WOULD belong in a component image --
that is a scalo contract change, not a Compose one.
"""

from __future__ import annotations

import os
import sys
import time

from _common import FALSY, _load_dotenv, _print, _resolved_services
from _pipeline import (
    MARKER_EXPRESSIONS,
    ch_count,
    ch_int,
    ch_marker_count,
    escape_literal,
    http_delete,
    http_get,
    http_get_json,
    http_post,
    http_post_json,
    marked_event,
    otel_fresh_counts,
    poll_until,
)

# Kept small on purpose. This proves the path works; the e2e suite is what
# exercises volume. A self test that writes thousands of rows into a production
# landing table every boot is a self test nobody leaves enabled.
EVENT_COUNT = 3

# How long to wait for rows to appear. The pipeline is asynchronous (and via
# Kafka, genuinely so), and the timeout is the stuck-dependency backstop rather
# than an expected duration.
LAND_TIMEOUT_SECONDS = 60.0
LAND_INTERVAL_SECONDS = 3.0

# How long to wait for the ingest component to report ready. `make dev`/`make ci`
# do not pass `--wait` to `docker compose up`, so this runs while containers are
# still warming; a single probe would decide "not ready" a second after boot.
READY_TIMEOUT_SECONDS = 90.0
READY_INTERVAL_SECONDS = 2.0

TARGET_DB = "dfe"
TARGET_TABLE = "default"

# Self-monitoring assertion. The collector batches on a 5s timeout and the SDKs
# export on their own interval, so the window is generous and the timeout is the
# stuck-dependency backstop.
OTEL_SERVICE = "otel-collector"
OTEL_FRESH_WINDOW_SECONDS = 300
OTEL_TIMEOUT_SECONDS = 120.0
OTEL_INTERVAL_SECONDS = 5.0

# Console assertion. dfe-ui holds no ClickHouse credential of its own -- it reads
# through the engine's query API -- so exercising that API with the break-glass
# login is what proves the console can see the data.
UI_QUERY_SERVICES = ("dfe-engine", "dfe-ui")
UI_QUERY_DATASOURCE = "clickhouse:default"
UI_QUERY_TIMEOUT_SECONDS = 30.0
UI_QUERY_INTERVAL_SECONDS = 3.0

# Hunt assertion. The runner runs the engine image off the ENGINE's config volume,
# so a hunt the API writes is a file the runner reads -- which is what makes
# "created while the runner was already up" a claim worth making.
HUNT_SERVICES = ("dfe-engine", "dfe-hunt-runner")
HUNT_TARGET_TABLE = "detection"

# The tightest schedule a hunt config expresses, and the only way in: POST
# /hunts/{name}/run answers 501, so no ad-hoc trigger can shorten the wait.
HUNT_CRON = "* * * * *"

# One runner reload plus the hunt's phase offset, which is a stable hash in
# [0, 0.8*interval) -- up to 48s on a 60s hunt. The stuck-runner backstop.
HUNT_PICKUP_TIMEOUT_SECONDS = 150.0
HUNT_PICKUP_INTERVAL_SECONDS = 3.0

# Short because the run has already finished by then: the worker advances the
# watermark only after its INSERT commits.
HUNT_DETECTION_TIMEOUT_SECONDS = 30.0
HUNT_DETECTION_INTERVAL_SECONDS = 3.0

# Compose defaults that mean "nobody ran `make init`". They are deterministic and
# committed, so a stack running them has a signing key and a database password
# that anyone with the repo already knows.
#
# Each maps to the service that consumes it, so a secret is only enforced when the
# service using it is actually running. Failing a HyperDX password on a profile
# with no HyperDX told an operator "the stack is up, but it did not prove it moves
# data" for a reason with nothing to do with data.
WEAK_SECRET_DEFAULTS = {
    "DFE_UI_NEXTAUTH_SECRET": ("RUN-make-init-TO-GENERATE-A-REAL-SECRET", "dfe-ui"),
    "HYPERDX_POSTGRES_PASSWORD": ("hyperdx", "hyperdx"),
}


def _weak_secrets() -> list[tuple[str, str]]:
    """Return (name, value) for every in-use secret still at its built-in default.

    Scoped to the services the resolved profile actually starts. An unset secret
    for a service nobody runs is not a finding, and reporting it as one buries the
    case that matters.
    """
    running = set(_resolved_services())
    weak = []
    for name, (default, service) in sorted(WEAK_SECRET_DEFAULTS.items()):
        if running and service not in running:
            continue
        value = os.environ.get(name, "").strip()
        if not (value) or value == default:
            weak.append((name, value or default))

    # ClickHouse is not a DFE service (it comes in via the `clickhouse` profile,
    # not DFE_SERVICES), and its weak default is an EMPTY password, so it does not
    # fit the service-map above. `make init` now generates it. Only enforce it for
    # the Docker ClickHouse: with CLICKHOUSE_HOST set the operator points at an
    # external instance and owns its credential, so an empty value here is theirs
    # to make, not a missed `make init`.
    if not (os.environ.get("CLICKHOUSE_HOST", "").strip()):
        ch = os.environ.get("CLICKHOUSE_PASSWORD", "").strip()
        if not (ch):
            weak.append(
                (
                    "CLICKHOUSE_PASSWORD",
                    "<empty> -- full-admin ClickHouse with no password",
                )
            )
    return weak


def _enabled() -> bool:
    """Return False only when POST is explicitly switched off."""
    raw = os.environ.get("DFE_POST_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in FALSY


def _wait_ready(url: str) -> bool:
    """Wait for a readiness endpoint to answer 2xx, up to READY_TIMEOUT_SECONDS.

    A single probe was not enough. `make dev` / `make ci` do not pass `--wait` to
    `docker compose up`, so they return as soon as containers are STARTED, and
    this runs immediately after. A service that is up but still warming answers
    503 on /readyz, which urllib raises. Probing once meant the self test decided
    "not ready" a second after boot, every time.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        try:
            http_get(url, timeout=3)
            return True
        except Exception:  # noqa: BLE001 - not-ready and unreachable look the same here
            if time.monotonic() >= deadline:
                return False
            time.sleep(READY_INTERVAL_SECONDS)


class NoIngestComponent(Exception):
    """The active profile declares no ingest component -- nothing to prove."""


class IngestNotReady(Exception):
    """The profile DOES declare an ingest component and it never became ready."""


def _ingest_target(*, table: str) -> tuple[str, str]:
    """Find the ingest edge of the ACTIVE profile, or None if it has none.

    Driven by the resolved profile, NOT by probing ports. Probing looked simpler
    and was wrong: a stray container from a previous profile answers a probe
    perfectly well while being wired to a pipeline that is no longer running. A
    port probe finds it, injects into it, and reports a self-test failure that is
    really a stale container. (`make down` used to be profile-scoped, which made
    strays routine; it now sweeps every profile, but a stray can still arrive from
    an older checkout or something started by hand.) Ask the profile.

    Order matters: the receiver is the front door when the profile has one. The
    fetcher is the fallback, and its ingest path is per-table.
    """
    bind = os.environ.get("DFE_POST_HOST", "localhost")
    services = _resolved_services()
    if not (services):
        raise IngestNotReady(
            "no resolved profile (.profile.mk missing) -- run a make target first"
        )

    # Not-ready and not-present must NOT collapse into one answer. A profile that
    # DECLARES an ingest component and cannot reach it is a failure; only a
    # profile with no ingest component at all is a legitimate skip. Conflating
    # them means a crash-looping receiver produces a green "nothing to prove",
    # which is the worst outcome available: silence that looks like success.
    if "dfe-receiver" in services:
        port = os.environ.get("DFE_RECEIVER_HTTP_PORT", "8080")
        metrics = os.environ.get("DFE_RECEIVER_PROMETHEUS_PORT", "9090")
        if not (_wait_ready(f"http://{bind}:{metrics}/readyz")):
            raise IngestNotReady(
                "dfe-receiver is in the active profile but never became ready "
                f"within {READY_TIMEOUT_SECONDS:.0f}s"
            )
        return "dfe-receiver", f"http://{bind}:{port}/ingest"

    if "dfe-fetcher" in services:
        port = os.environ.get("DFE_FETCHER_INGEST_PORT", "8082")
        metrics = os.environ.get("DFE_FETCHER_PROMETHEUS_PORT", "9094")
        if not (_wait_ready(f"http://{bind}:{metrics}/readyz")):
            raise IngestNotReady(
                "dfe-fetcher is in the active profile but never became ready "
                f"within {READY_TIMEOUT_SECONDS:.0f}s"
            )
        return "dfe-fetcher", f"http://{bind}:{port}/ingest/{table}"

    raise NoIngestComponent(
        f"active profile has no ingest component (services: {', '.join(services)})"
    )


def _run_id() -> str:
    """Return a marker unique to this POST run - pid plus 4 random bytes."""
    return f"post-{os.getpid()}-{os.urandom(4).hex()}"


def _cleanup(database: str, table: str, marker: str) -> None:
    """Report that this run's synthetic rows remain. It cannot remove them.

    We tried deleting them and it does not work: the engine-provisioned
    `dfe.default` carries PROJECTIONS, and ClickHouse refuses a lightweight
    DELETE on such a table unless `lightweight_mutation_projection_mode` is
    changed:

        Code: 344. DELETE query is not allowed for table dfe.default because
        as it has projections and setting lightweight_mutation_projection_mode
        is set to THROW.

    Changing a server-level mutation setting to tidy up after a self test is a
    worse trade than leaving three rows, so it says so instead. The rows are
    identifiable (`post-<pid>-<hex>` in `_tags.marker`) if you want them gone.

    Writing to the real landing table is deliberate: a dedicated table would
    prove a different pipeline than the one being asserted.
    """
    _print(
        msg=f"      note: {EVENT_COUNT} synthetic row(s) remain in {database}.{table} "
        f"tagged {marker} -- lightweight DELETE is refused on a table with projections"
    )


def _verify_self_monitoring() -> int:
    """Prove the stack's OWN telemetry reaches ClickHouse, when a collector runs.

    The second of the two pipelines a complete stack has to land, matching the
    k8s bootstrap smoke. Skipped, not passed, when no collector is in the profile.
    """
    if OTEL_SERVICE not in set(_resolved_services()):
        _print(
            msg=f"SKIP  {OTEL_SERVICE} not in the active profile -- no self-telemetry to prove"
        )
        return 0

    database = os.environ.get("DFE_OTEL_DATABASE", "dfe")
    _print(
        msg=f"Waiting for self-telemetry in {database} "
        f"(rows newer than {OTEL_FRESH_WINDOW_SECONDS}s)"
    )

    def _fresh():
        return otel_fresh_counts(database, OTEL_FRESH_WINDOW_SECONDS)

    def _report(attempt, result):
        if result is None:
            _print(msg=f"  attempt {attempt}: {database} tables not created yet")
            return
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(result.items())
        )
        _print(msg=f"  attempt {attempt}: {summary}")

    counts = poll_until(
        _fresh,
        timeout=OTEL_TIMEOUT_SECONDS,
        interval=OTEL_INTERVAL_SECONDS,
        done=lambda result: result is not None and any(result.values()),
        on_attempt=_report,
    )

    if counts is None:
        _print(
            msg=f"FAIL  no {database} table could be read within "
            f"{OTEL_TIMEOUT_SECONDS:.0f}s -- the collector never wrote its schema"
        )
        return 1
    landed = {table: count for table, count in counts.items() if count}
    if not (landed):
        _print(
            msg=f"FAIL  {database} exists but carries no rows newer than "
            f"{OTEL_FRESH_WINDOW_SECONDS}s -- the services are not exporting"
        )
        return 1
    summary = ", ".join(f"{table}={count}" for table, count in sorted(landed.items()))
    _print(msg=f"PASS  self-telemetry is streaming into {database} ({summary})")
    return 0


def _engine_base() -> str:
    """The engine's /api/v1 base URL for this stack."""
    bind = os.environ.get("DFE_POST_HOST", "localhost")
    return f"http://{bind}:{os.environ.get('DFE_ENGINE_PORT', '8003')}/api/v1"


def _login(base: str) -> tuple[str, int, str]:
    """Log the break-glass admin in. Returns (token, status, username).

    An empty token with status 0 means the password was never generated, which is
    a different fault from a rejected login and reads differently to an operator.
    """
    username = os.environ.get("DFE_AUTH_LOCAL_ADMIN_NAME", "admin").strip() or "admin"
    password = os.environ.get("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "").strip()
    if not (password):
        return "", 0, username
    status, body = http_post_json(
        f"{base}/auth/login", {"username": username, "password": password}
    )
    token = body.get("access_token", "") if isinstance(body, dict) else ""
    return token, status, username


def _ui_query_count(
    *, base: str, database: str, marker: str, table: str, token: str
) -> int | None:
    """Count this run's rows through the query API, or None if no form was readable.

    Both marker expressions are tried for the same reason ``ch_marker_count`` tries
    them: this repo does not own the landing schema.
    """
    escaped = escape_literal(marker)
    for expression in MARKER_EXPRESSIONS:
        status, body = http_post_json(
            f"{base}/queries/raw",
            {
                "datasource": UI_QUERY_DATASOURCE,
                "query": (
                    f"SELECT count() AS c FROM {database}.{table} "
                    f"WHERE {expression} = '{escaped}'"
                ),
            },
            token=token,
        )
        if status != 200 or not (isinstance(body, dict)):
            continue
        rows = body.get("rows") or []
        if not (rows):
            continue
        try:
            matched = int(next(iter(rows[0].values())))
        except (StopIteration, TypeError, ValueError):
            continue
        if matched > 0:
            return matched
    return None


def _verify_ui_query(*, database: str, marker: str, table: str) -> int:
    """Prove the console's query path returns THIS run's rows.

    Rows in ClickHouse are not the same claim as rows an operator can see: the
    console reaches them through the engine, which authenticates, resolves a
    datasource and applies row-level scoping the loader never touches.
    """
    absent = [
        name for name in UI_QUERY_SERVICES if name not in set(_resolved_services())
    ]
    if absent:
        _print(
            msg=f"SKIP  {', '.join(absent)} not in the active profile -- no console query path to prove"
        )
        return 0

    base = _engine_base()
    token, status, username = _login(base)
    if status == 0:
        _print(msg="FAIL  DFE_AUTH_LOCAL_ADMIN_PASSWORD is unset -- run `make init`")
        return 1

    _print(msg=f"Querying {database}.{table} through the engine API as {username!r}")
    if status != 200 or not (token):
        # An engine seeded before this password was generated holds the old one, so
        # retrying cannot help.
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- the console "
            "cannot authenticate, so nobody can read this data through dfe-ui"
        )
        return 1

    def _report(attempt, result):
        _print(msg=f"  attempt {attempt}: query API rows = {result}")

    matched = poll_until(
        lambda: _ui_query_count(
            base=base, database=database, marker=marker, table=table, token=token
        ),
        timeout=UI_QUERY_TIMEOUT_SECONDS,
        interval=UI_QUERY_INTERVAL_SECONDS,
        done=lambda result: result is not None and result >= EVENT_COUNT,
        on_attempt=_report,
    )

    if matched is None or matched < EVENT_COUNT:
        _print(
            msg=f"FAIL  the query API returned {matched if matched is not None else 0}"
            f"/{EVENT_COUNT} of this run's rows within {UI_QUERY_TIMEOUT_SECONDS:.0f}s "
            "-- the rows are in ClickHouse but the console cannot read them"
        )
        return 1

    _print(
        msg=f"PASS  dfe-ui's query path returned {matched}/{EVENT_COUNT} of this run's rows"
    )
    return 0


def _create_hunt_rule(
    *, base: str, database: str, marker: str, name: str, table: str, token: str
) -> str:
    """Create the detection rule this run's hunt names. Returns '' or the fault."""
    escaped = escape_literal(marker)
    status, body = http_post_json(
        f"{base}/rules",
        {
            "name": name,
            "severity": "high",
            "source_type": "raw",
            "user_sql": (
                f"SELECT * FROM {database}.{table} "
                f"WHERE {MARKER_EXPRESSIONS[0]} = '{escaped}'"
            ),
        },
        token=token,
    )
    return "" if status == 201 else f"POST /rules returned HTTP {status}: {body}"


def _create_hunt(
    *, base: str, database: str, name: str, rule: str, table: str, token: str
) -> str:
    """Create the hunt over this run's rows. Returns '' or the fault."""
    status, body = http_post_json(
        f"{base}/hunts",
        {
            "name": name,
            "cron": HUNT_CRON,
            "log_buffer": 60,
            "customers": ["post"],
            "rules": [rule],
            "global_source_table_name": f"{database}.{table}",
            "global_target_table_name": f"{database}.{HUNT_TARGET_TABLE}",
            "checkpoint_timestamp_field": "_timestamp_load",
        },
        token=token,
    )
    return "" if status == 201 else f"POST /hunts returned HTTP {status}: {body}"


def _remove(url: str, *, kind: str, name: str, token: str) -> None:
    """Delete one thing this run created, and say so when it does not go."""
    status = http_delete(url, token=token)
    if status not in (200, 204):
        _print(msg=f"      note: {kind} {name!r} was not removed (HTTP {status})")


def _hunt_status(*, base: str, token: str) -> tuple[bool, int]:
    """Read (running, hunt_count) off GET /hunts/status, or (False, -1) if unreadable."""
    status, body = http_get_json(f"{base}/hunts/status", token=token, timeout=10)
    if status != 200 or not (isinstance(body, dict)):
        return False, -1
    return bool(body.get("running")), int(body.get("hunt_count", -1))


def _verify_hunt(*, database: str, marker: str, table: str) -> int:
    """Prove a hunt created while the runner is running is picked up and executed.

    Two things have to be true and only one of them is about ClickHouse. The
    runner must SEE a hunt written to the shared config volume seconds ago without
    anything being restarted, and it must then run it into the detection table.
    A stack where hunts only start working after a bounce is a stack where the
    hunts page lies to whoever just used it.
    """
    absent = [name for name in HUNT_SERVICES if name not in set(_resolved_services())]
    if absent:
        _print(
            msg=f"SKIP  {', '.join(absent)} not in the active profile -- no hunt runner to prove"
        )
        return 0

    base = _engine_base()
    token, status, username = _login(base)
    if status != 200 or not (token):
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- the hunt API "
            "cannot be reached, so this run cannot create the hunt it needs"
        )
        return 1

    hunt_name = marker
    rule_name = f"{marker}-rule"
    _, before = _hunt_status(base=base, token=token)
    _print(
        msg=f"Creating rule {rule_name!r} and hunt {hunt_name!r} over this run's rows"
    )

    fault = _create_hunt_rule(
        base=base,
        database=database,
        marker=marker,
        name=rule_name,
        table=table,
        token=token,
    )
    if fault:
        _print(msg=f"FAIL  {fault}")
        return 1

    try:
        fault = _create_hunt(
            base=base,
            database=database,
            name=hunt_name,
            rule=rule_name,
            table=table,
            token=token,
        )
        if fault:
            _print(msg=f"FAIL  {fault}")
            return 1
        try:
            return _await_hunt(
                base=base, database=database, hunt=hunt_name, before=before, token=token
            )
        finally:
            _remove(
                f"{base}/hunts/{hunt_name}", kind="hunt", name=hunt_name, token=token
            )
    finally:
        _remove(f"{base}/rules/{rule_name}", kind="rule", name=rule_name, token=token)


def _await_hunt(*, base: str, database: str, hunt: str, before: int, token: str) -> int:
    """Wait for the runner to load the new hunt, run it, and write its detections."""
    escaped = escape_literal(hunt)
    running, after = _hunt_status(base=base, token=token)
    if after >= 0 and after <= before:
        _print(
            msg=f"FAIL  GET /hunts/status still counts {after} hunt(s) after creating one "
            "-- the engine did not write the hunt where it reports hunts from"
        )
        return 1

    # The watermark is the runner's fingerprint: the engine never writes one, so a
    # row here means the runner loaded a hunt that did not exist a moment ago and
    # ran it, with nothing restarted in between.
    seen_running = {"flag": running}

    def _picked_up():
        live, _ = _hunt_status(base=base, token=token)
        seen_running["flag"] = seen_running["flag"] or live
        return ch_int(
            f"SELECT count() FROM {database}.hunt_watermark WHERE hunt_id = '{escaped}'"
        )

    def _report(attempt, result):
        _print(msg=f"  attempt {attempt}: hunt_watermark rows = {result}")

    _print(msg=f"Waiting for the running hunt-runner to pick up {hunt!r} (no restart)")
    watermarks = poll_until(
        _picked_up,
        timeout=HUNT_PICKUP_TIMEOUT_SECONDS,
        interval=HUNT_PICKUP_INTERVAL_SECONDS,
        done=lambda result: result is not None and result > 0,
        on_attempt=_report,
    )

    if watermarks is None:
        _print(
            msg=f"FAIL  {database}.hunt_watermark could not be read -- the hunt "
            "coordination schema is missing, so no runner has ever started"
        )
        return 1
    if watermarks <= 0:
        _print(
            msg=f"FAIL  the runner did not claim {hunt!r} within "
            f"{HUNT_PICKUP_TIMEOUT_SECONDS:.0f}s -- a hunt created through the API is "
            "not reaching the runner, so hunts only work after a restart"
        )
        return 1
    _print(
        msg=f"PASS  the already-running hunt-runner loaded {hunt!r} and ran it without a restart"
    )

    # `running` is true only while a hunt holds a lease, and a lease is released as
    # soon as the run commits, so a healthy runner reads as running for well under
    # a second per fire. Reported, never asserted -- see the engine issue.
    if seen_running["flag"]:
        _print(msg="PASS  GET /hunts/status reported running: true during the fire")
    else:
        _print(
            msg="      note: GET /hunts/status never reported running: true -- it counts "
            "in-flight leases, not whether a runner process is alive"
        )

    def _detections():
        return ch_int(
            f"SELECT count() FROM {database}.{HUNT_TARGET_TABLE} "
            f"WHERE hunt_name = '{escaped}'"
        )

    def _report_rows(attempt, result):
        _print(msg=f"  attempt {attempt}: {HUNT_TARGET_TABLE} rows = {result}")

    matched = poll_until(
        _detections,
        timeout=HUNT_DETECTION_TIMEOUT_SECONDS,
        interval=HUNT_DETECTION_INTERVAL_SECONDS,
        done=lambda result: result is not None and result > 0,
        on_attempt=_report_rows,
    )

    if not (matched):
        _print(
            msg=f"FAIL  the runner ran {hunt!r} but wrote no row to "
            f"{database}.{HUNT_TARGET_TABLE} within {HUNT_DETECTION_TIMEOUT_SECONDS:.0f}s"
        )
        _print(
            msg="      the hunt YAML the API writes carries `rules`, and the runner "
            "executes a `query` field nothing compiles those rules into "
            "(dfe_engine/hunt_runner/spec_loader.py) -- so a hunt made in the UI "
            "schedules, claims and completes without ever detecting anything"
        )
        return 1

    _print(
        msg=f"PASS  {hunt!r} wrote {matched} row(s) to {database}.{HUNT_TARGET_TABLE}"
    )
    _print(
        msg=f"      note: those row(s) remain, tagged hunt_name = {hunt} -- the hunt and "
        "its rule are removed"
    )
    return 0


def main() -> int:
    _load_dotenv()

    if not (_enabled()):
        _print(msg="SKIP  DFE_POST_ENABLED is false -- power-on self test disabled")
        return 0

    database = os.environ.get("DFE_POST_DATABASE", TARGET_DB)
    table = os.environ.get("DFE_POST_TABLE", TARGET_TABLE)

    weak = _weak_secrets()
    if weak:
        # The compose defaults for these are deterministic, publicly-committed
        # constants, so a stack running them has a signing key anyone can read.
        # Compose cannot hard-fail on them (interpolation is not profile-gated, so
        # it would break `make down` too), which leaves this as the enforcement
        # point -- the self test runs on every `make dev` / `make ci`, which is
        # exactly when an unset secret should be caught.
        for name, value in weak:
            _print(msg=f"FAIL  {name} is still the built-in default ({value!r})")
        _print(msg="      run `make init` to generate real values")
        return 1

    try:
        service, ingest_url = _ingest_target(table=table)
    except NoIngestComponent as reason:
        # A genuine skip: some profiles run no ingest component at all
        # (loader-only), so there is nothing to prove end to end. Self-monitoring
        # is a separate pipeline and is still worth asserting.
        _print(msg=f"SKIP  {reason} -- nothing to prove end to end")
        return _verify_self_monitoring()
    except IngestNotReady as reason:
        _print(msg=f"FAIL  {reason}")
        return 1
    marker = _run_id()
    _print(msg=f"Injecting {EVENT_COUNT} marked event(s) via {service} at {ingest_url}")
    _print(msg=f"Marker: {marker}")

    baseline = ch_count(database, table)
    sent = 0
    for index in range(EVENT_COUNT):
        body = marked_event(marker=marker, source=table, extra={"post_sequence": index})
        try:
            status = http_post(ingest_url, body)
        except Exception as error:  # noqa: BLE001
            _print(msg=f"FAIL  ingest request raised: {error}")
            return 1
        if status < 200 or status >= 300:
            _print(msg=f"FAIL  ingest returned HTTP {status} from {ingest_url}")
            return 1
        sent += 1

    _print(msg=f"Accepted {sent}/{EVENT_COUNT}; waiting for rows in {database}.{table}")

    def _landed():
        return ch_marker_count(database, table, marker)

    def _report(attempt, result):
        _print(msg=f"  attempt {attempt}: marker rows = {result}")

    # `done` is explicit rather than relying on truthiness. The events are three
    # separate POSTs through a batching loader, so seeing 1 of 3 on the first poll
    # is normal -- and 1 is truthy. A truthiness test would stop there and report
    # a failure that another second of waiting resolves.
    matched = poll_until(
        _landed,
        timeout=LAND_TIMEOUT_SECONDS,
        interval=LAND_INTERVAL_SECONDS,
        done=lambda result: result is not None and result >= EVENT_COUNT,
        on_attempt=_report,
    )

    if matched is None:
        # No expression could read a marker. On an EMPTY table that is expected
        # rather than diagnostic -- there is no row for the probe to find, so it
        # cannot tell "schema has no marker column" from "nothing arrived". Say
        # which case this is instead of blaming the schema either way.
        landed = ch_count(database, table) - baseline
        if landed <= 0:
            _print(
                msg=f"FAIL  no rows reached {database}.{table} (row count moved by "
                f"{landed:+d}) within {LAND_TIMEOUT_SECONDS:.0f}s"
            )
        else:
            _print(
                msg=f"FAIL  {landed:+d} row(s) arrived in {database}.{table} but the "
                "marker column could not be read -- this run cannot be identified, "
                "so the schema may not carry `_tags`"
            )
        return 1

    if matched >= EVENT_COUNT:
        _print(
            msg=f"PASS  {matched}/{EVENT_COUNT} marked event(s) landed in {database}.{table}"
        )
        _cleanup(database, table, marker)
        # Every remaining claim runs even when an earlier one fails, so one boot
        # reports every broken pipeline rather than the first one.
        failed = _verify_self_monitoring()
        failed += _verify_ui_query(database=database, marker=marker, table=table)
        failed += _verify_hunt(database=database, marker=marker, table=table)
        return 1 if failed else 0

    _print(
        msg=f"FAIL  only {matched}/{EVENT_COUNT} marked event(s) reached "
        f"{database}.{table} within {LAND_TIMEOUT_SECONDS:.0f}s"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
