#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/post.py
#  Purpose:      Power-on self test - prove the running stack moves data end to end
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Power-on self test (POST) for a stack that is ALREADY running.

Eight claims. SUBSCRIPTION: where the tier has a bus, the loader is fetching at
least one topic, so the events about to be injected have a consumer at all.
INGEST: uniquely-marked events put in at the ingest edge come out as those exact
rows in ClickHouse. SELF-MONITORING: when the profile runs a collector,
the stack's own telemetry is landing FRESH in the otel database, and each named
service's stdout is landing under its own name. OBSERVABILITY: HyperDX holds the
sources this deployment seeds it and the dashboards the engine ships, read through
the proxy that gives it an identity. CONSOLE: those same marked rows read back
through the engine query API that dfe-ui uses, so the data is not merely stored but
reachable from the surface an operator works in. HUNTS: a hunt created through the
API while the runner is already running is picked up and executed, and its
detections land in the detection table. IDLE APPS: each app the tier starts with
no work is serving and doing nothing, which is the whole point of starting it.
ROUTING: where the engine renders this tier's app config, a source created
through the API reaches the RUNNING receiver, and a record matching its rule
lands in a table of its own. None of the eight is "the containers started" or
"the ports answer".

The INGEST claim is transport-agnostic by construction: it posts to the ingest
edge and reads ClickHouse, so it runs identically on the Kafka and the gRPC
(kafka-less) profiles. A self test that only worked on one transport would be a
self test for the transport, not for the stack. SUBSCRIPTION is the one claim
that is bus-shaped, and it SKIPS where the resolved tier has no bus.

OPT-OUT, not opt-in: it runs unless `DFE_POST_ENABLED=false`. Something that only
runs when you remember to ask for it is not a power-on self test.

Exit codes:
  0  every claim the active profile can make, held (or the POST was skipped for a
     stated reason -- disabled, or the running profile has no ingest component)
  1  a claim failed, or could not be checked

Deliberately NOT a Docker HEALTHCHECK or a component entrypoint step. This
assertion is cross-service and needs the whole stack up, which no single
container can see; and a HEALTHCHECK repeats forever, so it would re-inject test
data into a production ingest path on a timer. See docs and the plan for the
per-component preflight (`selftest`) that WOULD belong in a component image --
that is a scalo contract change, not a Compose one.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

from _common import FALSY, _load_dotenv, _print, _profile_mk_value, _resolved_services
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

# Subscription assertion. A loader whose topic resolver matches nothing stays
# healthy and consumes nothing, which surfaces only as rows that never arrive.
LOADER_SERVICE = "dfe-loader"
LOADER_PROMETHEUS_PORT = "9091"
# rdkafka reports a partition's consumer lag only while it is assigned and
# fetching it, so a topic label here is the subscription, not the config.
LOADER_TOPIC_METRIC = "rdkafka_topic_partition_consumer_lag"
LOADER_TOPIC_LABEL = "topic"
# The resolver re-runs every 60s and rdkafka publishes its statistics every 5s,
# so this covers two missed refreshes rather than an expected duration.
LOADER_TOPIC_TIMEOUT_SECONDS = 150.0
LOADER_TOPIC_INTERVAL_SECONDS = 5.0
# `.profile.mk`'s resolved answer to "does this deployment have a bus", so the
# claim is skipped on the gRPC tiers rather than failing on them.
TRANSPORT_BUS_KEY = "DFE_TRANSPORT_BUS_PRESENT"

TARGET_DB = "dfe"
# The engine's catch-all landing table, DFE_CLICKHOUSE_LANDING_TABLE. A deployment
# that moved it points POST at the same name through DFE_POST_TABLE.
TARGET_TABLE = "main"

# Schema control. dfe-engine applies every ClickHouse object and every bootstrap
# topic at its own startup and names a `schema` check on /readyz that is true
# only once that pass converged. The window covers a cold ClickHouse, which the
# engine retries for DFE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS (180) first.
ENGINE_SERVICE = "dfe-engine"
SCHEMA_READY_CHECK = "schema"
SCHEMA_TIMEOUT_SECONDS = 300.0
SCHEMA_INTERVAL_SECONDS = 5.0

# Self-monitoring assertion. The collector batches on a 5s timeout and the SDKs
# export on their own interval, so the window is generous and the timeout is the
# stuck-dependency backstop.
OTEL_SERVICE = "otel-collector"
OTEL_FRESH_WINDOW_SECONDS = 300
OTEL_TIMEOUT_SECONDS = 120.0
OTEL_INTERVAL_SECONDS = 5.0
# Container stdout reaches the collector over Docker's fluentd log driver, which
# the Makefile wires up alongside the collector unless this is false. Named on
# its own because metrics arriving says nothing about whether logs are.
OTEL_LOGS_TABLE = "otel_logs"
CONTAINER_LOGS_KEY = "DFE_CONTAINER_LOGS_ENABLED"
# The services whose logs a stack has to be able to show. Every DFE service ships
# them; these three are the data path plus the control plane.
CONTAINER_LOG_SERVICES = ("dfe-engine", "dfe-loader", "dfe-receiver")

# HyperDX assertion. The team is created by the first identified request, its
# sources are seeded with it, and the dashboard provisioner runs on a one-minute
# cron -- so this polls rather than probing once.
HYPERDX_SERVICE = "hyperdx"
HYPERDX_SEEDED_SOURCES = (
    "clickhouse_system",
    "hunts",
    "main",
    "otel_logs",
    "otel_metrics",
    "otel_traces",
)
HYPERDX_TIMEOUT_SECONDS = 180.0
HYPERDX_INTERVAL_SECONDS = 5.0

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

# The fire that claims the hunt is not always the fire that matches: the hunt
# carries log_buffer 60, so rows younger than that sit outside the window and are
# picked up by the NEXT minute's fire. Long enough to cover that second fire.
HUNT_DETECTION_TIMEOUT_SECONDS = 150.0
HUNT_DETECTION_INTERVAL_SECONDS = 3.0

# Idle-app assertion: the config that gives each app no work, and the metrics
# port compose publishes it on. dfe-infra apps.yaml declares the idle predicate
# and the app evaluates it; this reads the answer the app publishes.
IDLE_APPS: dict[str, tuple[str, str]] = {
    "dfe-archiver": ("archiver/kafka.yaml", "9093"),
    "dfe-fetcher": ("fetcher/kafka.yaml", "9094"),
    "dfe-transform-elastic": ("transform-elastic/kafka.yaml", "9099"),
    "dfe-transform-vrl": ("transform-vrl/kafka.yaml", "9096"),
}
IDLE_GAUGE = "pipeline_idle"

# Routing assertion. A source created through the API compiles a receiver rule,
# and on this target the engine renders that rule into the file the RUNNING
# receiver polls. The receiver re-reads on a 5s mtime poll and rebuilds its
# router in place, so the wait is that poll plus the trip through the broker and
# the loader's batch -- nothing here waits on a deploy controller.
ROUTING_SERVICES = ("dfe-engine", "dfe-receiver", "dfe-loader")
ROUTING_SOURCE_PREFIX = "postroute"
ROUTING_TIMEOUT_SECONDS = 45.0
ROUTING_INTERVAL_SECONDS = 3.0
# `.profile.mk`'s answer to "does the engine render this tier's app config": the
# claim is only makeable where it does.
APP_CONFIG_DIR_KEY = "DFE_ENGINE_APP_CONFIG_DIR"

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


def _schema_converged() -> bool | None:
    """Whether the engine's `schema` readiness check is true right now.

    None where the engine has not answered at all, so a stack still starting and
    one reporting a failed apply do not read the same.
    """
    bind = os.environ.get("DFE_POST_HOST", "localhost")
    port = os.environ.get("DFE_ENGINE_PORT", "8003")
    try:
        _, body = http_get_json(f"http://{bind}:{port}/readyz", timeout=5)
    except Exception:  # noqa: BLE001 - unreachable is not-yet-answering here
        return None
    if not isinstance(body, dict):
        return None
    return bool((body.get("checks") or {}).get(SCHEMA_READY_CHECK))


def _wait_schema_converged() -> int:
    """Hold until dfe-engine reports its schema pass converged. 0 when it did.

    Replaces the ordering the deleted schema-init container gave: nothing else
    creates a table or a topic, so injecting before that pass converges proves
    nothing about the pipeline and fails on an absent table.
    """
    if ENGINE_SERVICE not in _resolved_services():
        _print(
            msg=f"SKIP  {ENGINE_SERVICE} is not in the active profile -- no schema to wait on"
        )
        return 0

    def _report(attempt, result):
        _print(msg=f"  attempt {attempt}: engine schema check = {result}")

    converged = poll_until(
        _schema_converged,
        timeout=SCHEMA_TIMEOUT_SECONDS,
        interval=SCHEMA_INTERVAL_SECONDS,
        done=lambda result: result is True,
        on_attempt=_report,
    )
    if converged is not True:
        _print(
            msg=f"FAIL  {ENGINE_SERVICE} did not report its schema converged within "
            f"{SCHEMA_TIMEOUT_SECONDS:.0f}s -- GET /api/v1/system/schema on the engine "
            "carries the cause, object by object"
        )
        return 1
    _print(msg=f"PASS  {ENGINE_SERVICE} reports its schema converged")
    return 0


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
    `dfe.main` carries PROJECTIONS, and ClickHouse refuses a lightweight
    DELETE on such a table unless `lightweight_mutation_projection_mode` is
    changed:

        Code: 344. DELETE query is not allowed for table dfe.main because
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

    # Metrics arrive within seconds of a service starting; container logs wait on
    # the collector accepting its first fluentd connection, so both are given the
    # same window and the poll only stops once each half it expects has landed.
    wants_logs = _container_logs_expected()

    def _done(result):
        if result is None or not (any(result.values())):
            return False
        return not (wants_logs) or bool(result.get(OTEL_LOGS_TABLE))

    counts = poll_until(
        _fresh,
        timeout=OTEL_TIMEOUT_SECONDS,
        interval=OTEL_INTERVAL_SECONDS,
        done=_done,
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
    if wants_logs:
        return _verify_container_logs(database=database)
    _print(
        msg=f"SKIP  {CONTAINER_LOGS_KEY} is false -- container stdout is not shipped"
    )
    return 0


def _container_logs_expected() -> bool:
    """Whether this stack ships container stdout to the collector.

    The same two conditions the Makefile chains the log-driver fragment on, so
    the assertion and the wiring cannot disagree about what is running.
    """
    if OTEL_SERVICE not in set(_resolved_services()):
        return False
    return os.environ.get(CONTAINER_LOGS_KEY, "true").strip().lower() not in FALSY


def _verify_container_logs(*, database: str) -> int:
    """Prove each named service's stdout is reaching the otel log table.

    Freshness alone would pass on one container talking, so this asks per service:
    the log driver tags every stream with its container name, and the collector
    turns that tag into ServiceName.
    """
    names = ", ".join(CONTAINER_LOG_SERVICES)
    _print(
        msg=f"Waiting for container stdout in {database}.{OTEL_LOGS_TABLE} ({names})"
    )

    def _per_service():
        found: dict[str, int] = {}
        for service in CONTAINER_LOG_SERVICES:
            count = ch_int(
                f"SELECT count() FROM {database}.{OTEL_LOGS_TABLE} "
                f"WHERE ServiceName = '{escape_literal(service)}' "
                f"AND Timestamp > now() - INTERVAL {OTEL_FRESH_WINDOW_SECONDS} SECOND"
            )
            found[service] = 0 if count is None else count
        return found

    def _report(attempt, result):
        summary = ", ".join(f"{name}={count}" for name, count in sorted(result.items()))
        _print(msg=f"  attempt {attempt}: {summary}")

    counts = poll_until(
        _per_service,
        timeout=OTEL_TIMEOUT_SECONDS,
        interval=OTEL_INTERVAL_SECONDS,
        done=lambda result: all(result.values()),
        on_attempt=_report,
    )

    silent = sorted(name for name, count in counts.items() if not (count))
    if silent:
        _print(
            msg=f"FAIL  no rows in {database}.{OTEL_LOGS_TABLE} for {', '.join(silent)} "
            f"within {OTEL_TIMEOUT_SECONDS:.0f}s -- container stdout is not reaching "
            "the collector"
        )
        return 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    _print(msg=f"PASS  container stdout is landing per service ({summary})")
    return 0


def _service_var(service: str, suffix: str) -> str:
    """The .env / .profile.mk variable naming one service's setting.

    Every per-service key follows it -- DFE_TRANSFORM_VRL_CONFIG,
    DFE_ARCHIVER_PROMETHEUS_PORT -- so it is derived rather than tabulated.
    """
    return f"DFE_{service.removeprefix('dfe-').upper().replace('-', '_')}_{suffix}"


def _metric_value(body: str, name: str) -> float | None:
    """One unlabelled gauge out of a Prometheus exposition body, or None if absent."""
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        if key.split("{", 1)[0] != name:
            continue
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _metric_label_values(body: str, name: str, label: str) -> set[str]:
    """Every value one label takes across a labelled metric's samples.

    Topic and partition labels carry no comma or brace, so splitting the label set
    is enough here and a Prometheus parser is not.
    """
    found: set[str] = set()
    for line in body.splitlines():
        if line.startswith("#") or not (line.startswith(f"{name}{{")):
            continue
        for pair in line[len(name) + 1 : line.rfind("}")].split(","):
            key, _, value = pair.partition("=")
            if key.strip() == label:
                found.add(value.strip().strip('"'))
    return found


def _verify_loader_subscription() -> int:
    """Prove the loader is fetching a topic, on a tier that has a bus.

    The loader discovers its topics from the broker, so a topic that never appears
    -- or one scalo suppresses in favour of a `_load` sibling -- leaves it ready
    and healthy with nothing to read. Asserted before the injection, because after
    it the same fault is indistinguishable from a broken loader or a missing table.
    """
    if _profile_mk_value(key=TRANSPORT_BUS_KEY)[:1] != ["true"]:
        _print(msg="SKIP  this tier has no bus -- the loader is handed its events")
        return 0
    if LOADER_SERVICE not in set(_resolved_services()):
        _print(
            msg=f"SKIP  {LOADER_SERVICE} not in the active profile -- no subscription to prove"
        )
        return 0

    bind = os.environ.get("DFE_POST_HOST", "localhost")
    port = (
        os.environ.get(_service_var(LOADER_SERVICE, "PROMETHEUS_PORT"), "").strip()
        or LOADER_PROMETHEUS_PORT
    )
    base = f"http://{bind}:{port}"
    _print(msg=f"Waiting for {LOADER_SERVICE} to be fetching a topic at {base}")

    def _fetching():
        try:
            body = http_get(f"{base}/metrics", timeout=5)
        except Exception:  # noqa: BLE001 - unreachable and not-yet-serving are one answer
            return set()
        return _metric_label_values(body, LOADER_TOPIC_METRIC, LOADER_TOPIC_LABEL)

    def _report(attempt, result):
        _print(
            msg=f"  attempt {attempt}: topics fetched = {', '.join(sorted(result)) or 'none'}"
        )

    topics = poll_until(
        _fetching,
        timeout=LOADER_TOPIC_TIMEOUT_SECONDS,
        interval=LOADER_TOPIC_INTERVAL_SECONDS,
        on_attempt=_report,
    )
    if not (topics):
        _print(
            msg=f"FAIL  {LOADER_SERVICE} is fetching no topic within "
            f"{LOADER_TOPIC_TIMEOUT_SECONDS:.0f}s -- it resolved an empty subscription, "
            "so events reach the broker and stop there. Read its resolved set with "
            "`docker compose logs dfe-loader` (grep 'Resolved Kafka topics') and see "
            "the `main_load` entry in docs/troubleshooting.md"
        )
        return 1
    _print(msg=f"PASS  {LOADER_SERVICE} is fetching {', '.join(sorted(topics))}")
    return 0


def _idle_apps() -> list[tuple[str, str]]:
    """The (service, metrics port) pairs this run started with an idle config.

    Read off the resolved profile rather than the profile NAME: a data-plane
    profile pointing the same app at a config that gives it work is not one of
    these, and must not be asserted inert.
    """
    running = set(_resolved_services())
    found: list[tuple[str, str]] = []
    for service, (config, default_port) in sorted(IDLE_APPS.items()):
        if service not in running:
            continue
        resolved = _profile_mk_value(key=_service_var(service, "CONFIG"))
        if resolved[:1] != [config]:
            continue
        port = os.environ.get(_service_var(service, "PROMETHEUS_PORT"), "").strip()
        found.append((service, port or default_port))
    return found


def _verify_idle_apps() -> int:
    """Prove each app the tier starts with no work is serving and doing nothing.

    Two-sided on purpose. A container that crash-looped on the idle config fails
    the readiness half; one that is quietly archiving, polling or consuming fails
    the gauge half, and neither shows up as a missing row anywhere else.
    """
    expected = _idle_apps()
    if not expected:
        _print(
            msg="SKIP  this profile starts no app with an idle config -- nothing to prove"
        )
        return 0

    bind = os.environ.get("DFE_POST_HOST", "localhost")
    failed = 0
    for service, port in expected:
        base = f"http://{bind}:{port}"
        _print(msg=f"Waiting for {service} to report {IDLE_GAUGE} at {base}")
        if not (_wait_ready(f"{base}/readyz")):
            _print(
                msg=f"FAIL  {service} runs an idle config but never became ready within "
                f"{READY_TIMEOUT_SECONDS:.0f}s -- it did not start on a config that gives "
                "it no work"
            )
            failed += 1
            continue
        try:
            idle = _metric_value(http_get(f"{base}/metrics", timeout=5), IDLE_GAUGE)
        except Exception:  # noqa: BLE001 - unreachable and unreadable are one answer here
            idle = None
        if idle is None:
            _print(
                msg=f"FAIL  {service} serves no {IDLE_GAUGE} on {base}/metrics, so whether "
                "it is inert cannot be read"
            )
            failed += 1
        elif idle != 1:
            _print(
                msg=f"FAIL  {service} reports {IDLE_GAUGE}={idle:g} -- it was given a config "
                "with no work and is working anyway"
            )
            failed += 1
        else:
            _print(msg=f"PASS  {service} is ready and inert ({IDLE_GAUGE}=1)")
    return failed


def _create_routed_source(*, base: str, name: str, token: str) -> str:
    """Create and deploy a source with a rule of its own. Returns '' or the fault.

    The deploy is what makes the source live: it creates the landing table, ensures
    the topic and pushes the compiled routing into the apps' overlays.
    """
    status, body = http_post_json(
        f"{base}/sources",
        {
            "source": name,
            "display_name": "POST routing self test",
            "match": {"field": "_source", "operator": "equals", "value": name},
            "header": {"type": "common-header/timeseries", "version": "1.0.1"},
            "schema": {"meta_schema": "meta/syslog", "meta_schema_version": "1.0.0"},
        },
        token=token,
    )
    if status != 201:
        return f"POST /sources returned HTTP {status}: {body}"
    status, body = http_post_json(f"{base}/sources/{name}/deploy", {}, token=token)
    if status != 200 or not (isinstance(body, dict) and body.get("applied")):
        return f"POST /sources/{name}/deploy returned HTTP {status}: {body}"
    if body.get("apps_sync_error"):
        return f"the apps could not follow the source: {body['apps_sync_error']}"
    for hint in body.get("restart_required") or []:
        _print(msg=f"      {hint}")
    return ""


def _verify_routing_applied(*, database: str) -> int:
    """Prove a source created through the API reaches the RUNNING receiver.

    The artefact is a row in the new source's own table, not a file on a volume:
    a rendered config the receiver never re-read would satisfy any check that
    looked at the write instead of the landing.
    """
    if not (_profile_mk_value(key=APP_CONFIG_DIR_KEY)):
        _print(
            msg="SKIP  the engine does not render this tier's app config -- its apps "
            "read a committed config, so no API write reaches them"
        )
        return 0
    absent = [
        name for name in ROUTING_SERVICES if name not in set(_resolved_services())
    ]
    if absent:
        _print(
            msg=f"SKIP  {', '.join(absent)} not in the active profile -- no routing to prove"
        )
        return 0

    base = _engine_base()
    token, status, username = _login(base)
    if status != 200 or not (token):
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- this run cannot "
            "create the source it needs to prove the routing reaches the receiver"
        )
        return 1

    name = f"{ROUTING_SOURCE_PREFIX}{_run_id()[-8:]}"
    _print(msg=f"Creating source {name!r} and posting one event that matches its rule")
    fault = _create_routed_source(base=base, name=name, token=token)
    if fault:
        _print(msg=f"FAIL  {fault}")
        _remove(f"{base}/sources/{name}", kind="source", name=name, token=token)
        return 1

    try:
        return _await_routed_row(database=database, name=name)
    finally:
        _remove(f"{base}/sources/{name}", kind="source", name=name, token=token)


def _await_routed_row(*, database: str, name: str) -> int:
    """Post to the receiver until a record lands in the new source's own table."""
    try:
        service, ingest_url = _ingest_target(table=name)
    except (NoIngestComponent, IngestNotReady) as reason:
        _print(msg=f"FAIL  {reason}")
        return 1
    marker = _run_id()

    def _landed():
        # Re-posted every attempt: before the receiver has re-read its config the
        # record takes the catch-all, so the early posts are the wait rather than
        # a failure.
        try:
            http_post(ingest_url, marked_event(marker=marker, source=name))
        except Exception:  # noqa: BLE001 - a refused post is another attempt
            return 0
        return ch_marker_count(database, name, marker) or 0

    def _report(attempt, result):
        _print(msg=f"  attempt {attempt}: rows in {database}.{name} = {result}")

    landed = poll_until(
        _landed,
        timeout=ROUTING_TIMEOUT_SECONDS,
        interval=ROUTING_INTERVAL_SECONDS,
        done=lambda result: bool(result),
        on_attempt=_report,
    )
    if not (landed):
        _print(
            msg=f"FAIL  nothing reached {database}.{name} within "
            f"{ROUTING_TIMEOUT_SECONDS:.0f}s, so the rule the engine compiled for "
            f"{name!r} never reached the running receiver via {service}"
        )
        return 1
    _print(
        msg=f"PASS  the receiver routed {name!r} into {database}.{name} ({landed} row(s))"
    )
    return 0


def _hyperdx_base() -> str:
    """The HyperDX API base URL for this stack, through the proxy holding its origins."""
    bind = os.environ.get("DFE_POST_HOST", "localhost")
    return f"http://{bind}:{os.environ.get('DFE_HYPERDX_API_PORT', '8000')}"


def _verify_hyperdx() -> int:
    """Prove HyperDX holds its seeded sources and the release's provisioned dashboards.

    Both are SERVER-side state, which is the whole point: they are what an operator
    finds on a fresh browser, and what a browser-local HyperDX could never have.
    Read with a console token, because HyperDX verifies the engine's JWT -- so a
    pass also proves that trust works end to end.
    """
    if HYPERDX_SERVICE not in set(_resolved_services()):
        _print(
            msg=f"SKIP  {HYPERDX_SERVICE} not in the active profile -- nothing to prove"
        )
        return 0

    token, status, username = _login(_engine_base())
    if status != 200 or not (token):
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- HyperDX "
            "verifies that token, so this run cannot read it"
        )
        return 1

    base = _hyperdx_base()
    _print(msg=f"Waiting for seeded sources and provisioned dashboards at {base}")

    def _state():
        try:
            source_status, sources = http_get_json(f"{base}/sources", token=token)
            dash_status, dashboards = http_get_json(f"{base}/dashboards", token=token)
        except Exception:  # noqa: BLE001 - any failure is "not ready yet"
            return None
        if source_status != 200 or dash_status != 200:
            return (source_status, dash_status)
        names = {s.get("name") for s in sources or [] if isinstance(s, dict)}
        provisioned = [
            d for d in dashboards or [] if isinstance(d, dict) and d.get("provisioned")
        ]
        return (sorted(n for n in names if n), len(provisioned))

    def _report(attempt, result):
        if result is None:
            _print(msg=f"  attempt {attempt}: the API did not answer")
        elif isinstance(result[0], int):
            _print(msg=f"  attempt {attempt}: HTTP {result[0]}/{result[1]}")
        else:
            _print(
                msg=f"  attempt {attempt}: {len(result[0])} source(s), {result[1]} dashboard(s)"
            )

    def _ready(result):
        if result is None or isinstance(result[0], int):
            return False
        names, provisioned = result
        return set(HYPERDX_SEEDED_SOURCES).issubset(names) and provisioned > 0

    state = poll_until(
        _state,
        timeout=HYPERDX_TIMEOUT_SECONDS,
        interval=HYPERDX_INTERVAL_SECONDS,
        done=_ready,
        on_attempt=_report,
    )

    if state is None or isinstance(state[0], int):
        _print(
            msg=f"FAIL  the HyperDX API at {base} did not accept the console token "
            f"within {HYPERDX_TIMEOUT_SECONDS:.0f}s -- it verifies that token against "
            "the engine's JWKS"
        )
        return 1

    names, provisioned = state
    missing = sorted(set(HYPERDX_SEEDED_SOURCES) - set(names))
    failed = 0
    if missing:
        _print(
            msg=f"FAIL  HyperDX is missing seeded source(s) {', '.join(missing)} -- "
            "the shipped dashboards resolve their tiles by these names"
        )
        failed = 1
    else:
        _print(
            msg=f"PASS  HyperDX holds all {len(HYPERDX_SEEDED_SOURCES)} seeded sources"
        )
    if provisioned < 1:
        _print(
            msg=f"FAIL  HyperDX has no provisioned dashboard within "
            f"{HYPERDX_TIMEOUT_SECONDS:.0f}s -- the engine's dashboards did not reach it"
        )
        failed = 1
    else:
        _print(msg=f"PASS  HyperDX carries {provisioned} provisioned dashboard(s)")
    return failed


def _engine_base() -> str:
    """The engine's /api/v1 base URL for this stack."""
    bind = os.environ.get("DFE_POST_HOST", "localhost")
    return f"http://{bind}:{os.environ.get('DFE_ENGINE_PORT', '8003')}/api/v1"


def _login(base: str) -> tuple[str, int, str]:
    """Log a console account in. Returns (token, status, username).

    An empty token with status 0 means no password was available, which is a
    different fault from a rejected login and reads differently to an operator.
    """
    # DFE_POST_LOGIN_* names any account; the break-glass admin is the fallback.
    username = (
        os.environ.get("DFE_POST_LOGIN_USER", "").strip()
        or os.environ.get("DFE_AUTH_LOCAL_ADMIN_NAME", "admin").strip()
        or "admin"
    )
    password = (
        os.environ.get("DFE_POST_LOGIN_PASSWORD", "").strip()
        or os.environ.get("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "").strip()
    )
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


def _wizard_rotated_admin_password(*, base: str) -> bool:
    """True when the engine's setup-status says the wizard already rotated the break-glass password."""
    try:
        status = json.loads(http_get(f"{base}/auth/setup-status"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(status, dict):
        return False
    initial = status.get("initial_setup") or {}
    return "admin_password" in (initial.get("completed_steps") or [])


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
        if _wizard_rotated_admin_password(base=base):
            _print(
                msg="SKIP  the setup wizard rotated the break-glass password, so no "
                "console credential lives in the env -- set DFE_POST_LOGIN_USER and "
                "DFE_POST_LOGIN_PASSWORD to prove the query path"
            )
            return 0
        _print(msg="FAIL  DFE_AUTH_LOCAL_ADMIN_PASSWORD is unset -- run `make init`")
        return 1

    _print(msg=f"Querying {database}.{table} through the engine API as {username!r}")
    if status != 200 or not (token):
        # An engine seeded before this password was generated holds the old one, and
        # the setup wizard's last step rotates it: either way retrying cannot help.
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- the console "
            "cannot authenticate, so nobody can read this data through dfe-ui. "
            "If the setup wizard rotated the break-glass password, pass the current "
            "one on the command line (shell env beats .env): "
            "DFE_AUTH_LOCAL_ADMIN_PASSWORD=... make post"
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


def _hunt_http_status(*, base: str, hunt: str, token: str) -> int:
    """Return the HTTP status GET /hunts/<name> answers with: 200 present, 404 absent."""
    status, _ = http_get_json(
        f"{base}/hunts/{quote(hunt, safe='')}", token=token, timeout=10
    )
    return status


def _assert_hunt_removed(*, base: str, hunt: str, token: str) -> int:
    """Prove the hunt this run created is gone, so a passing run leaves the stack as it found it."""
    status = _hunt_http_status(base=base, hunt=hunt, token=token)
    if status == 404:
        return 0
    if status == 200:
        _print(
            msg=f"FAIL  GET /hunts/{hunt} still returns the hunt after the delete -- "
            "this run left a hunt behind on the stack"
        )
        return 1
    _print(
        msg=f"      note: GET /hunts/{hunt} returned HTTP {status} after the delete, "
        "so whether the hunt is gone could not be read"
    )
    return 0


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
    if status == 0 and _wizard_rotated_admin_password(base=base):
        _print(
            msg="SKIP  the setup wizard rotated the break-glass password, so no "
            "console credential lives in the env -- set DFE_POST_LOGIN_USER and "
            "DFE_POST_LOGIN_PASSWORD to prove the hunt path"
        )
        return 0
    if status == 0:
        _print(
            msg="FAIL  no password for the console is available -- set "
            "DFE_AUTH_LOCAL_ADMIN_PASSWORD (run `make init`) or DFE_POST_LOGIN_PASSWORD, "
            "so this run can create the hunt it needs"
        )
        return 1
    if status != 200 or not (token):
        _print(
            msg=f"FAIL  login as {username!r} returned HTTP {status} -- the hunt API "
            "rejected the credential, so this run cannot create the hunt it needs"
        )
        return 1

    hunt_name = marker
    rule_name = f"{marker}-rule"
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
            result = _await_hunt(
                base=base, database=database, hunt=hunt_name, token=token
            )
        finally:
            _remove(
                f"{base}/hunts/{hunt_name}", kind="hunt", name=hunt_name, token=token
            )
        return result or _assert_hunt_removed(base=base, hunt=hunt_name, token=token)
    finally:
        _remove(f"{base}/rules/{rule_name}", kind="rule", name=rule_name, token=token)


def _await_hunt(*, base: str, database: str, hunt: str, token: str) -> int:
    """Wait for the runner to load the new hunt, run it, and write its detections."""
    escaped = escape_literal(hunt)
    running, _ = _hunt_status(base=base, token=token)
    # This run's own hunt, not the global count: a concurrent `make post` or an
    # operator deleting an unrelated hunt moves the count either way.
    present = _hunt_http_status(base=base, hunt=hunt, token=token)
    if present != 200:
        _print(
            msg=f"FAIL  GET /hunts/{hunt} returned HTTP {present} straight after the "
            "engine accepted it -- the hunt was not written where the engine reports "
            "hunts from"
        )
        return 1

    seen_running = {"flag": running}

    # The watermark is the runner's fingerprint -- the engine never writes one --
    # so a row for a hunt that did not exist a moment ago is the runner having
    # re-read its dir, with nothing restarted in between.
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

    if matched is None:
        _print(
            msg=f"FAIL  {database}.{HUNT_TARGET_TABLE} could not be read -- the hunt "
            "output table does not exist, so no hunt on this stack can land anywhere"
        )
        return 1
    if matched <= 0:
        _print(
            msg=f"FAIL  the runner ran {hunt!r} but wrote no row to "
            f"{database}.{HUNT_TARGET_TABLE} within {HUNT_DETECTION_TIMEOUT_SECONDS:.0f}s"
        )
        _print(
            msg="      the runner claimed the hunt and completed the run, so it is the "
            "detection write that did not happen -- check the hunt-runner logs and the "
            "rule the hunt names"
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

    # Ahead of every claim below, which all read objects this pass makes.
    if _wait_schema_converged():
        return 1

    try:
        service, ingest_url = _ingest_target(table=table)
    except NoIngestComponent as reason:
        # A genuine skip: some profiles run no ingest component at all
        # (loader-only), so there is nothing to prove end to end. Self-monitoring
        # is a separate pipeline and is still worth asserting.
        _print(msg=f"SKIP  {reason} -- nothing to prove end to end")
        return _verify_self_monitoring() + _verify_hyperdx() + _verify_idle_apps()
    except IngestNotReady as reason:
        _print(msg=f"FAIL  {reason}")
        return 1

    # Fails fast rather than joining the tally below: injecting into a pipeline
    # whose consumer is not attached proves nothing about the pipeline.
    if _verify_loader_subscription():
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
        failed += _verify_hyperdx()
        failed += _verify_ui_query(database=database, marker=marker, table=table)
        failed += _verify_hunt(database=database, marker=marker, table=table)
        failed += _verify_idle_apps()
        failed += _verify_routing_applied(database=database)
        return 1 if failed else 0

    _print(
        msg=f"FAIL  only {matched}/{EVENT_COUNT} marked event(s) reached "
        f"{database}.{table} within {LAND_TIMEOUT_SECONDS:.0f}s"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
