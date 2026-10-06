#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/check_compose.py
#  Purpose:      Validate the compose stack resolves on every path, without the stack SSoT
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Validate that docker-compose.yml resolves, on every path we ship.

`docker compose config` is the instrument: it interpolates every variable, applies
profiles and overlays, and fails on anything structurally wrong. It needs no daemon
and no registry access, which is what makes it usable as a gate.

The obstacle is that the stack deliberately hard-fails on unset image pins
(``${DFE_LOADER_VERSION:?...}``), and those pins come from the DFE stack SSoT --
a private artifact CI has no business pulling just to check YAML. So this script
reads the hard-fail keys straight out of the compose file and supplies a
placeholder for each. That keeps the check hermetic, and it means adding a new
hard-fail key can never quietly break CI: the key is discovered, not listed here.

Placeholders are only ever injected for keys the compose file ITSELF declares
mandatory, and only into this process's subprocess environment. A real value
already present in the environment always wins, so a local run with a pinned .env
validates the real pins.

A fresh checkout has no .env, and no check-* target writes one, so there the
check renders in memory the .env `make init` would write: the generated secrets
resolve as a first start would see them, and only the image pins get
placeholders. That is harmless here -- this checks compose STRUCTURE, not that
any particular digest exists.

Paths checked:

- registry: ``-f docker-compose.yml`` alone, the CI and production path.
- dev: the same plus the auto-loaded ``docker-compose.override.yml``, the path
  ``make dev`` takes.
- live: dev plus ``docker-compose.live.yml``, the path ``make dev LIVE=1``
  takes. Its ``${DFE_SRC_ROOT:?...}`` mounts are hard-fail by design, so the
  placeholder injection covers that key the same way it covers image pins.
- local: the registry path plus the overlay ``make dev LOCAL=...`` generates,
  rendered here for one representative component so the generator's output is
  checked, not just its source.

Each is checked against both Kafka backends, because the two are mutually
exclusive and a change can easily satisfy one and break the other -- which is
precisely what happened with the topic-init service that ran a Redpanda image on
the Apache profile.

Beyond resolution, these semantic assertions ride along.

- No service that gates on a health endpoint may carry a CPU ceiling under
  `_MIN_HEALTH_CPUS` -- see that constant for why a lower ceiling takes the
  endpoint dark while the data path keeps working.
- The web-UI exposure dials must do what they claim: the bind scope moves every
  UI port and nothing else, and each unpublish fragment drops that UI's ports and
  no other service's. See `_UI_EXPOSURE`.
- The opt-in auth profile must gate without holes: no stack needs an OIDC setting
  to resolve, an armed profile moves every infra-UI origin behind a proxy, and
  the infra kill switch takes the proxies down with the UIs. See `_AUTH_PROXIES`.
- The committed override must repoint every service that runs a buildable
  component's image, consumers included, or `make dev` runs two builds of one
  component. See `_override_coverage_failures`.
- The overlay `make dev LOCAL=...` generates must put the named component and its
  image consumers on `:local` and leave every other service on its registry pin.
  See `_local_overlay_failures`.
- That overlay must describe THIS run: a build that builds nothing removes it
  rather than leaving the previous run's. See `_overlay_staleness_failures`.
- `make dev LOCAL=...` names its compose files explicitly instead of riding
  COMPOSE_FILE, so it must still chain every overlay fragment that chain does.
  See `_dev_path_fragment_failures`.
- The builder's IMPLICIT_CONSUMERS must match the compose graph, or a `depends_on`
  edge added here silently stops `make dev` building an image the stack starts.
  See `_implicit_consumer_failures`.
- Every profile's transform output topic must be read by that profile's loader,
  and no two transforms may consume one topic. See `_transform_wiring_failures`.
- Every enrichment table a transform config names must resolve to a file through
  that service's own bind mounts. See `_enrichment_table_failures`.
- The engine must never receive an empty or `changeme` admin password outside a
  dev posture, because it refuses to start on one. See `_credential_failures`.
- Moving ClickHouse's host publish must not move the port any service dials it
  on in-network. See `_clickhouse_dial_failures`.
- Every service the log-driver fragment ships must queue its lines and wait for
  the collector's ack. See `_log_driver_failures`.
- Console TLS must change nothing until it is dialled on. Once on, it must move both proxies onto their TLS configs and the certificate and pin the network so the engine trusts dfe-proxy's address alone. See `_proxy_tls_mismatches`.
- Every path a pinned image declares as a VOLUME must be mounted from a named
  volume or a bind. See `_IMAGE_VOLUMES`.
- No committed service may carry a name a generated per-source instance can take.
  See `_instance_name_collisions`.
- Every service dfe-engine renders, and every instance the generator declares,
  must read the custom env file the engine writes for it, last; no service may
  read one the engine does not write. See `_custom_env_mismatches`.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import instances
import proxy_tls
from _common import (
    COMPOSE_FILE,
    COMPOSE_LIVE_FILE,
    COMPOSE_OVERRIDE_FILE,
    CONFIG_DIR,
    CUSTOM_ENV_SUFFIX,
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    ENV_DIR,
    ENV_TEMPLATE_DIR,
    REPO_ROOT,
    SERVICE_CONFIG_FILE,
    SERVICE_PROFILES_FILE,
    _config_enrichment_paths,
    _config_topics,
    _dotenv_values,
    _parse_dotenv,
    _print,
    _required_compose_vars,
    _transform_topics,
)
from build_dev_images import (
    IMPLICIT_CONSUMERS,
    buildable_components,
    local_image_services,
    overlay_text,
)
from init import _render_secrets
from resolve_profile import _parse_yaml

# The overlay `make dev LOCAL=...` generates, rendered for dfe-engine because it
# is the component with IMAGE_CONSUMERS followers. Written under .tmp so compose
# resolves it relative to the repo like the shipped files.
_LOCAL_SAMPLE = ["dfe-engine"]
_LOCAL_RENDER = REPO_ROOT / ".tmp" / "compose-check-local.yml"
# A stack service the builder cannot build, for the overlay staleness assertion.
_UNBUILDABLE_SAMPLE = "kafka-ui"

# Enough to satisfy interpolation and produce a parseable image reference.
_PLACEHOLDER = "0.0.0-compose-check"
# Keys with these suffixes land in bind-mount sources, and compose reads a
# sourceless-looking string there as a NAMED VOLUME ("refers to undefined
# volume") -- so their placeholder must be an absolute path. Any existing one
# does; the repo root is the one path this script can always name.
_PATH_KEY_SUFFIXES = ("_ROOT", "_DIR", "_PATH")

# Profiles are opt-in, so an unprofiled run checks almost nothing. `dfe` and
# `core` carry the services this repo exists to ship.
_BASE_PROFILES = ("clickhouse", "dfe", "core", "hyperdx", "kafka-ui", "otel")
_KAFKA_BACKENDS = ("kafka-redpanda", "kafka-apache")

# Tokio sizes its worker pool from `available_parallelism()`, which reads the
# cgroup CPU quota and FLOORS it. A ceiling under 2.0 therefore leaves a scalo
# service with a single worker thread, and its synchronous Kafka poll loop owns
# that thread -- so the operator surface (/livez, /readyz, /metrics) accepts
# connections and answers none of them while the data path keeps working.
# Observed on dfe-archiver at 1.5; see the x-limits-service comment in
# docker-compose.yml for the A/B that proved it.
#
# Keyed off the health path rather than a service list so a service added later is
# covered without anyone remembering this file exists. Both `*z` paths count: the
# starvation takes the whole observability port dark, so which path a service
# happens to gate on says nothing about its exposure. A service with no
# healthcheck at all still gets nothing, which is the honest limit here.
_MIN_HEALTH_CPUS = 2.0
_HEALTH_PATHS = ("/livez", "/readyz")

# Retired health paths. The whole surface is /livez, /readyz and /metrics, and
# these 404 on every image the stack pins.
_RETIRED_HEALTH_PATHS = ("/healthz", "/health/live", "/health/ready", "/health/startup")

# The web UIs the exposure dials govern, keyed by compose service: the class the
# kill switch reads, and the fragment that unpublishes that UI. PRODUCT is the DFE
# UI and the API it consumes; INFRA is the ops consoles DFE_INFRA_UIS_EXTERNAL
# covers as a set. Every other published port is ingest or a backing service and
# must not move when the UI dials do -- which is what `_ui_exposure_failures`
# asserts.
_UI_EXPOSURE: dict[str, tuple[str, str]] = {
    "dfe-engine": ("product", "docker-compose.unpublish-engine-api.yml"),
    "dfe-proxy": ("product", "docker-compose.unpublish-dfe-ui.yml"),
    # HyperDX is published by the proxy that injects its identity headers, never
    # by the hyperdx container itself.
    "dfe-hyperdx-proxy": ("infra", "docker-compose.unpublish-hyperdx.yml"),
    "kafka-ui": ("infra", "docker-compose.unpublish-kafbat.yml"),
}

# The two addresses DFE_BIND_SCOPE resolves to, localhost first (the default).
_BIND_SCOPE_ADDRS = ("127.0.0.1", "0.0.0.0")

# The opt-in `auth` profile fronts each infra UI with oauth2-proxy. Keyed by the
# UI service: the proxies that front it, and the fragment that unpublishes them.
# One proxy per ORIGIN, because oauth2-proxy serves a single --http-address and
# HyperDX is one UI across two.
_AUTH_PROFILE = "auth"
_AUTH_PROXIES: dict[str, tuple[tuple[str, ...], str]] = {
    "dfe-hyperdx-proxy": (
        ("oauth2-proxy-hyperdx", "oauth2-proxy-hyperdx-api"),
        "docker-compose.unpublish-auth-hyperdx.yml",
    ),
    "kafka-ui": (
        ("oauth2-proxy-kafbat",),
        "docker-compose.unpublish-auth-kafbat.yml",
    ),
}
# The fragments the Makefile chains whenever the profile is armed: each infra UI
# moves behind its proxy, so its own port stops reaching the host.
_AUTH_DIRECT_FRAGMENTS = (
    "docker-compose.unpublish-hyperdx.yml",
    "docker-compose.unpublish-kafbat.yml",
)
# Settings the proxies read. Prefixes, because the rule they guard is that NO
# stack needs any of them: compose interpolates before it filters by profile, so
# one hard-fail key in a profiled service would make an OIDC issuer mandatory for
# every deploy. `_auth_exposure_failures` renders with all of them stripped.
_AUTH_ENV_PREFIXES = ("DFE_OIDC_", "DFE_OAUTH2_PROXY_")
# Representative values for the armed-profile renders. Never leave this process.
_AUTH_ENV = {
    "DFE_OIDC_ISSUER_URL": "https://idp.example.invalid/realms/dfe",
    "DFE_OIDC_CLIENT_ID": "compose-check",
    "DFE_OIDC_CLIENT_SECRET": "compose-check",
    "DFE_OAUTH2_PROXY_COOKIE_SECRET": "0123456789abcdef0123456789abcdef",  # gitleaks:allow
}

# Every overlay fragment the Makefile chains, read back out of its own
# `chain_fragment` calls, so a fragment added there needs no edit here.
_MAKEFILE = REPO_ROOT / "Makefile"
_CHAIN_FRAGMENT_RE = re.compile(r"chain_fragment,([^),]+)\)")
# The chain is a make variable and the LOCAL overlay only exists once a build has
# written it, so neither resolves through `docker compose config` on the fresh
# checkout every check-* target must run on.
_COMPOSE_FILE_GOAL = "print-compose-file"
_COMPOSE_FILE_PREFIX = "COMPOSE_FILE="
# The dials that gate a fragment, set so every one chains. Two come from
# .profile.mk, where only a make command-line variable beats the include.
_FRAGMENT_DIALS = {
    "DFE_AUTH_RESOLVED": "true",
    "DFE_INSTANCES_RESOLVED": "true",
    "DFE_OTEL_RESOLVED": "true",
    "DFE_CONTAINER_LOGS_ENABLED": "true",
    "DFE_INFRA_UIS_EXTERNAL": "false",
    "DFE_UI_EXTERNAL": "false",
    "DFE_ENGINE_API_EXTERNAL": "false",
    "DFE_PROXY_TLS": "true",
}
# Console TLS refuses to start beside the auth profile, so `make dev` runs once per side, each held to the chain its own dials produce. The TLS run also needs what that refusal and the TLS precheck look for: an https origin and a certificate pair openssl accepts.
_DEV_PATH_CERT_DIR = REPO_ROOT / ".tmp" / "compose-check-certs"
_DEV_PATH_BIN_DIR = REPO_ROOT / ".tmp" / "compose-check-bin"
_DEV_PATH_RUNS = {
    "auth": {"DFE_PROXY_TLS": "false"},
    "console TLS": {
        "DFE_AUTH_ENABLED": "false",
        "DFE_AUTH_RESOLVED": "false",
        "DFE_EXTERNAL_ORIGIN": "https://compose-check.invalid",
        "DFE_HYPERDX_APP_URL": "",
        "DFE_PROXY_CERT_DIR": f"./{_DEV_PATH_CERT_DIR.relative_to(REPO_ROOT)}",
    },
}
# The two `docker compose` lines `make dev` runs, by the tokens that identify one.
_DEV_SUBCOMMANDS = {"pull": ("pull",), "up": ("up", "-d")}

# The engine refuses to start on an unset or shipped-default admin password unless
# DFE_ENV names a dev posture, so a compose file that hands it one outside dev
# produces a container that crash-loops on boot. The posture list mirrors
# dfe_engine.settings.is_dev_posture; compose's own `${DFE_ENV:-production}` default
# is what makes the unset case a fault rather than a dev posture, so only a .env
# that says `dev` gets to run on the shipped password.
_ENGINE_SERVICE = "dfe-engine"
_ADMIN_PASSWORD_KEY = "DFE_AUTH_LOCAL_ADMIN_PASSWORD"
_POSTURE_KEY = "DFE_ENV"
_DEFAULT_PASSWORD = "changeme"
_DEV_POSTURES = frozenset({"dev", "development", "local", "test", "ci"})
# An env file compose loads INSTEAD of .env, so a case describes the whole input.
_EMPTY_ENV_FILE = REPO_ROOT / ".tmp" / "compose-check-empty.env"
# (label, DFE_ENV, admin password, must the rule flag it). The negative half: a
# rule that flags nothing passes every stack, so the cases that must NOT flag are
# checked as hard as the ones that must.
_CREDENTIAL_CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("production, no password", "production", "", True),
    (f"production, {_DEFAULT_PASSWORD}", "production", _DEFAULT_PASSWORD, True),
    ("staging, no password", "staging", "", True),
    ("production, minted password", "production", "aMintedValue123", False),
    ("dev, no password", "dev", "", False),
    (f"dev, {_DEFAULT_PASSWORD}", "dev", _DEFAULT_PASSWORD, False),
    ("posture unset, no password", "", "", True),
    (f"posture unset, {_DEFAULT_PASSWORD}", "", _DEFAULT_PASSWORD, True),
    ("posture unset, minted password", "", "aMintedValue123", False),
)

# Each service that dials ClickHouse from inside the stack, by the environment keys
# carrying the host, HTTP port and native port it dials (None where it uses none).
_CLICKHOUSE_DIALLERS: dict[str, tuple[str, str | None, str]] = {
    "dfe-engine": (
        "DFE_CLICKHOUSE_HOST",
        "DFE_CLICKHOUSE_PORT",
        "DFE_CLICKHOUSE_NATIVE_PORT",
    ),
    "dfe-hunt-runner": (
        "DFE_CLICKHOUSE_HOST",
        "DFE_CLICKHOUSE_PORT",
        "DFE_CLICKHOUSE_NATIVE_PORT",
    ),
    "otel-collector": ("CLICKHOUSE_HOST", None, "CLICKHOUSE_PORT"),
}
_CLICKHOUSE_SERVICE = "clickhouse"
# HyperDX dials ClickHouse too, from the URL in the connection it seeds.
_HYPERDX_SERVICE = "hyperdx"
_HYPERDX_CONNECTIONS_KEY = "DEFAULT_CONNECTIONS"
# What `make` exports from resolve_profile.py for CLICKHOUSE_SECURE; this check
# runs without make, so a case sets it directly.
_CLICKHOUSE_SCHEME_KEY = "DFE_CLICKHOUSE_RESOLVED_SCHEME"
# Every key that moves a ClickHouse address, stripped before each case so the
# checkout's own .env cannot decide the answer.
_CLICKHOUSE_ADDRESS_KEYS = frozenset(
    {
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_NATIVE_PORT",
        "CLICKHOUSE_EXTERNAL_HTTP_PORT",
        "CLICKHOUSE_EXTERNAL_NATIVE_PORT",
        "CLICKHOUSE_SECURE",
        _CLICKHOUSE_SCHEME_KEY,
    }
)
_EXTERNAL_CLICKHOUSE = "clickhouse.example.invalid"
# (label, environment, the (host, HTTP, native) every dialler must get, the host
# ports the bundled container must publish, the URL HyperDX's connection must
# name). The publish is asserted too, so a case that moved nothing cannot pass.
_CLICKHOUSE_DIAL_CASES: tuple[
    tuple[str, dict[str, str], tuple[str, str, str], tuple[str, ...], str], ...
] = (
    (
        "defaults",
        {},
        ("clickhouse", "8123", "9000"),
        ("8123", "9000"),
        "http://clickhouse:8123",
    ),
    (
        "host publish moved",
        {"CLICKHOUSE_HTTP_PORT": "18123", "CLICKHOUSE_NATIVE_PORT": "19000"},
        ("clickhouse", "8123", "9000"),
        ("18123", "19000"),
        "http://clickhouse:8123",
    ),
    (
        "external host",
        {"CLICKHOUSE_HOST": _EXTERNAL_CLICKHOUSE},
        (_EXTERNAL_CLICKHOUSE, "8123", "9000"),
        ("8123", "9000"),
        f"http://{_EXTERNAL_CLICKHOUSE}:8123",
    ),
    (
        "external host and ports",
        {
            "CLICKHOUSE_HOST": _EXTERNAL_CLICKHOUSE,
            "CLICKHOUSE_HTTP_PORT": "18123",
            "CLICKHOUSE_NATIVE_PORT": "19000",
            "CLICKHOUSE_EXTERNAL_HTTP_PORT": "8443",
            "CLICKHOUSE_EXTERNAL_NATIVE_PORT": "9440",
        },
        (_EXTERNAL_CLICKHOUSE, "8443", "9440"),
        ("18123", "19000"),
        f"http://{_EXTERNAL_CLICKHOUSE}:8443",
    ),
    (
        "external host over TLS",
        {
            "CLICKHOUSE_HOST": _EXTERNAL_CLICKHOUSE,
            "CLICKHOUSE_EXTERNAL_HTTP_PORT": "8443",
            "CLICKHOUSE_EXTERNAL_NATIVE_PORT": "9440",
            "CLICKHOUSE_SECURE": "true",
            _CLICKHOUSE_SCHEME_KEY: "https",
        },
        (_EXTERNAL_CLICKHOUSE, "8443", "9440"),
        ("8123", "9000"),
        f"https://{_EXTERNAL_CLICKHOUSE}:8443",
    ),
)

# Services this project owns and therefore holds to that surface. hyperdx,
# clickhouse, the brokers and kafka-ui are third-party and keep their own.
_DFE_OWNED_SERVICES = {
    "dfe-archiver",
    "dfe-engine",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-e2e-elastic-cisco-ios",
    "dfe-transform-e2e-vector-filebeat",
    "dfe-transform-e2e-vrl-filebeat",
    "dfe-transform-elastic",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-ui",
}

# The fragment the Makefile chains to ship container stdout to the collector, and
# the driver options each shipped service needs, with what breaks without one.
_CONTAINER_LOGS_FRAGMENT = "docker-compose.container-logs.yml"
_LOG_DRIVER_OPTIONS = {
    "fluentd-async": (
        "Docker refuses to create the container while the collector is not listening"
    ),
    "fluentd-request-ack": (
        "Docker's port proxy accepts lines before the collector listens and drops "
        "them, and the driver counts them as sent"
    ),
}

# The fragment the Makefile chains under DFE_PROXY_TLS=true and each proxy it moves, by its (plain, TLS) config file.
_TLS_FRAGMENT = "docker-compose.tls.yml"
_TLS_PROXY_CONFIGS = {
    "dfe-hyperdx-proxy": ("config/proxy/hyperdx.yaml", "config/proxy/hyperdx.tls.yaml"),
    "dfe-proxy": ("config/proxy/envoy.yaml", "config/proxy/envoy.tls.yaml"),
}
_TLS_FORWARDING_PROXY = "dfe-proxy"
_ENVOY_CONFIG_TARGET = "/etc/envoy/envoy.yaml"
_ENVOY_CERT_TARGET = "/etc/envoy/certs"
_TLS_CERT_DIR = "./compose-check-tls-certs"
_TRUSTED_PROXIES_KEY = "DFE_API_FORWARDED_ALLOW_IPS"
# None of these is a default, so a render that ignored a dial and used a literal fails.
_TLS_SUBNET = "10.231.7.0/24"
_TLS_USER = {"DFE_DEV_GID": "4343", "DFE_DEV_UID": "4242"}

# The VOLUME paths each pinned image declares (`docker image inspect --format
# '{{json .Config.Volumes}}'`), by service: one left unmounted gets an anonymous
# volume per container that `make clean` cannot reach. A new pin that declares
# another path is an edit here.
_IMAGE_VOLUMES: dict[str, tuple[str, ...]] = {
    "clickhouse": ("/var/lib/clickhouse",),
    "hyperdx-ferretdb": ("/state",),
    "hyperdx-postgres": ("/var/lib/postgresql/data",),
    "kafka-apache": ("/etc/kafka/secrets", "/mnt/shared/config", "/var/lib/kafka/data"),
    "kafka-redpanda": ("/var/lib/redpanda/data",),
}

# A dfe-transform-vector pipeline declares its own enrichment tables, and the
# service mounts the directory holding it rather than the file.
_TRANSFORM_FILE_SUFFIXES = (".yaml", ".yml")

# Profile services whose config declares a Kafka sink topic somebody has to read.
_TRANSFORM_PREFIX = "dfe-transform-"
_LOADER_SERVICE = "dfe-loader"

# The keys `_sentinel_model` writes into each env file it plants.
_SENTINEL_LAST = "DFE_COMPOSE_CHECK_LAST_ENV_FILE"
_SENTINEL_READ = "DFE_COMPOSE_CHECK_READ_"


def _first_start_dotenv() -> dict[str, str]:
    """This checkout's .env, or the one `make init` would write, rendered in memory.

    A start goal mints .env before its first compose call, so a checkout without
    one is checked as it would start -- with nothing written to disk.
    """
    if DOTENV_FILE.is_file():
        return _dotenv_values()
    template = DOTENV_TEMPLATE.read_text(encoding="utf-8", errors="replace")
    return _parse_dotenv(text=_render_secrets(text=template))


def _check_env() -> tuple[dict[str, str], list[str]]:
    """Return (subprocess environment, keys we had to invent a value for).

    .env is folded in first so a pinned local checkout validates its REAL pins;
    placeholders then fill only what is still missing, which on CI is all of them.
    """
    env = {**_first_start_dotenv(), **os.environ}
    injected = []
    # Discover across every shipped compose file, not just docker-compose.yml --
    # the live overlay declares its own hard-fail key (DFE_SRC_ROOT).
    required = _required_compose_vars(
        files=(COMPOSE_FILE, COMPOSE_OVERRIDE_FILE, COMPOSE_LIVE_FILE)
    )
    for name in sorted(required):
        if not (env.get(name, "").strip()):
            env[name] = (
                str(REPO_ROOT) if name.endswith(_PATH_KEY_SUFFIXES) else _PLACEHOLDER
            )
            injected.append(name)
    return env, injected


def _compose_config(
    *, env: dict[str, str], files: list[str], profiles: list[str]
) -> subprocess.CompletedProcess:
    """Run `docker compose config -q` for one file set and profile set."""
    cmd = ["docker", "compose"]
    for path in files:
        cmd += ["-f", path]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config", "-q"]
    return subprocess.run(
        cmd,
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=env,
        errors="replace",
        text=True,
    )


def _retired_health_failures(*, config: dict) -> list[str]:
    """Return one message per healthcheck aimed at a retired health path.

    The surface is `/livez`, `/readyz` and `/metrics`, with no aliases: the
    retired names 404 on every image the stack pins, so a probe left on one reads
    as a service that never comes up rather than as a misconfigured probe. That
    is how the pinned dfe-engine sat in a restart loop behind `/health/ready`.

    Third-party images keep their own conventions, so only the services this
    project owns are checked.
    """
    failures = []
    for name, service in sorted(config.get("services", {}).items()):
        if name not in _DFE_OWNED_SERVICES:
            continue
        test = " ".join(
            str(part) for part in (service.get("healthcheck", {}).get("test") or [])
        )
        for retired in _RETIRED_HEALTH_PATHS:
            if retired in test:
                failures.append(
                    f"{name}: healthcheck targets the retired {retired} -- it 404s on "
                    "the pinned image; use /livez or /readyz"
                )
    return failures


def _config_json(
    *,
    env: dict[str, str],
    files: list[str],
    extra_profiles: tuple[str, ...] = (),
    env_file: Path | None = None,
    fragment: str | None = None,
) -> dict | None:
    """Return the fully interpolated compose model, or None if it did not resolve.

    Every base profile is turned on, plus one Kafka backend, so the model carries
    every service a reader might assert about. `auth` is opt-in and therefore only
    arrives through `extra_profiles`. Resolution failures are reported by the loop
    in main(), so a None here needs no second message.

    `env_file` replaces the .env compose would otherwise load, which is what lets a
    caller assert about a variable this checkout happens to have set.

    `fragment` is one more compose file, read from stdin after `files`, so a
    generated file is checked without writing it into the checkout.
    """
    cmd = ["docker", "compose"]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    for path in files:
        cmd += ["-f", path]
    if fragment is not None:
        cmd += ["-f", "-"]
    for profile in [*_BASE_PROFILES, _KAFKA_BACKENDS[0], *extra_profiles]:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=env,
        errors="replace",
        input=fragment,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _published(*, config: dict, service: str) -> list[tuple[str, str]]:
    """Return the (host_ip, published port) pairs one service maps onto the host."""
    ports = config.get("services", {}).get(service, {}).get("ports") or []
    return [(str(p.get("host_ip", "")), str(p.get("published", ""))) for p in ports]


def _ui_exposure_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the web-UI exposure dials.

    Three properties, all read off the interpolated model rather than the source
    YAML, because the whole mechanism is interpolation plus fragment merging:

    - the bind scope moves every UI port to the chosen address;
    - it moves nothing else, so ingest and backing-service ports keep the audience
      they were given;
    - each unpublish fragment drops exactly its own UI's ports.
    """
    failures: list[str] = []
    made = 0
    base = [COMPOSE_FILE.name]
    models: dict[str, dict] = {}
    for addr in _BIND_SCOPE_ADDRS:
        config = _config_json(env={**env, "DFE_UI_BIND_HOST": addr}, files=base)
        if config is None:
            return (
                [f"the base path did not resolve with DFE_UI_BIND_HOST={addr}"],
                made,
            )
        models[addr] = config

    for addr, config in models.items():
        for service in sorted(_UI_EXPOSURE):
            made += 1
            bound = _published(config=config, service=service)
            if not (bound):
                failures.append(f"{service}: publishes no host port at all")
                continue
            stray = sorted({ip for ip, _ in bound if ip != addr})
            if stray:
                failures.append(
                    f"{service}: DFE_UI_BIND_HOST={addr} but it binds {', '.join(stray)} "
                    "-- a UI port that ignores the bind scope"
                )

    # Anything outside the UI set must be identical under both addresses.
    for service in sorted(models[_BIND_SCOPE_ADDRS[0]].get("services", {})):
        if service in _UI_EXPOSURE:
            continue
        made += 1
        first, second = (
            _published(config=models[addr], service=service)
            for addr in _BIND_SCOPE_ADDRS
        )
        if first != second:
            failures.append(
                f"{service}: the bind scope moved a non-UI port ({first} -> {second}) "
                "-- ingest and backing services keep their own audience"
            )

    for service, (ui_class, fragment) in sorted(_UI_EXPOSURE.items()):
        if not ((REPO_ROOT / fragment).is_file()):
            failures.append(
                f"{fragment} is missing -- {service} has no {ui_class} opt-out"
            )
            continue
        config = _config_json(env=env, files=[*base, fragment])
        if config is None:
            failures.append(f"{fragment} did not resolve on top of the base path")
            continue
        for other in sorted(_UI_EXPOSURE):
            made += 1
            bound = _published(config=config, service=other)
            if other == service and bound:
                failures.append(f"{fragment}: {service} still publishes {bound}")
            elif other != service and not (bound):
                failures.append(f"{fragment}: it also unpublished {other}")
    return failures, made


def _auth_exposure_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the opt-in auth profile's exposure rules.

    Four properties:

    - no stack needs an OIDC setting, so the base path resolves with every one of
      them stripped from the environment;
    - armed, each infra UI stops publishing and its proxies publish exactly the
      origins that UI had, so the gate has no hole;
    - armed, the product UIs are untouched;
    - armed with the infra class killed, the PROXIES go dark too -- a gated door
      is still a door.
    """
    failures: list[str] = []
    made = 0
    base = [COMPOSE_FILE.name]
    addr = _BIND_SCOPE_ADDRS[0]

    bare = {k: v for k, v in env.items() if not k.startswith(_AUTH_ENV_PREFIXES)}
    bare["DFE_UI_BIND_HOST"] = addr
    plain = _config_json(env=bare, files=base)
    made += 1
    if plain is None:
        return (
            [
                "the base path does not resolve with the OIDC settings unset -- "
                "no profile may require an issuer to exist"
            ],
            made,
        )

    armed_env = {**env, **_AUTH_ENV, "DFE_UI_BIND_HOST": addr}
    on = _config_json(
        env=armed_env,
        files=[*base, *_AUTH_DIRECT_FRAGMENTS],
        extra_profiles=(_AUTH_PROFILE,),
    )
    if on is None:
        return ([f"the {_AUTH_PROFILE} profile path did not resolve"], made)

    for ui, (proxies, _) in sorted(_AUTH_PROXIES.items()):
        made += 2
        if _published(config=on, service=ui):
            failures.append(
                f"{ui}: still publishes with the {_AUTH_PROFILE} profile armed -- "
                "its port belongs to the proxy in front of it"
            )
        want = {port for _, port in _published(config=plain, service=ui)}
        got: set[str] = set()
        for proxy in proxies:
            bound = _published(config=on, service=proxy)
            got |= {port for _, port in bound}
            stray = sorted({ip for ip, _ in bound if ip != addr})
            if stray:
                failures.append(f"{proxy}: binds {', '.join(stray)}, not {addr}")
        if got != want:
            failures.append(
                f"{ui}: its proxies publish {sorted(got)} but the UI published "
                f"{sorted(want)} -- every origin must stay covered"
            )

    for service in sorted(_UI_EXPOSURE):
        if service in _AUTH_PROXIES:
            continue
        made += 1
        if _published(config=on, service=service) != _published(
            config=plain, service=service
        ):
            failures.append(
                f"{service}: the {_AUTH_PROFILE} profile moved a product UI port"
            )

    killed = _config_json(
        env=armed_env,
        files=[
            *base,
            *_AUTH_DIRECT_FRAGMENTS,
            *(fragment for _, fragment in _AUTH_PROXIES.values()),
        ],
        extra_profiles=(_AUTH_PROFILE,),
    )
    if killed is None:
        return (failures + ["the killed-infra auth path did not resolve"], made)
    for ui, (proxies, _) in sorted(_AUTH_PROXIES.items()):
        for proxy in proxies:
            made += 1
            if _published(config=killed, service=proxy):
                failures.append(
                    f"{proxy}: still publishes with the infra class killed -- "
                    "the kill switch covers the proxy as well as the UI"
                )
    return failures, made


def _health_cpu_failures(*, env: dict[str, str], files: list[str]) -> list[str]:
    """Return one message per service with a health endpoint and too small a CPU ceiling.

    Takes the file set rather than assuming the registry path. An override can
    lower `deploy.resources.limits.cpus` on any service, and a hand-edited
    override is precisely the vector the docs point at -- checking only
    docker-compose.yml would leave the named vector the one place unguarded.

    Reads the INTERPOLATED numbers, so it reflects whatever `.env` and the
    environment actually produce, not the defaults written in the compose file.

    A service with no limit at all is NOT flagged: unlimited means Tokio sees the
    host's CPUs, which is the situation that worked before limits existed.
    """
    config = _config_json(env=env, files=files)
    if config is None:
        return []

    failures = _retired_health_failures(config=config)
    for name, service in sorted(config.get("services", {}).items()):
        test = service.get("healthcheck", {}).get("test") or []
        if not (any(path in str(part) for part in test for path in _HEALTH_PATHS)):
            continue
        cpus = (
            service.get("deploy", {}).get("resources", {}).get("limits", {}).get("cpus")
        )
        if cpus is None:
            continue
        if float(cpus) < _MIN_HEALTH_CPUS:
            failures.append(
                f"{name}: cpus={cpus} is below {_MIN_HEALTH_CPUS}, which leaves Tokio one "
                "worker thread and takes the health port dark while the data path keeps working"
            )
    return failures


def _render_local_overlay() -> None:
    """Render the `make dev LOCAL=...` overlay for the sample component, uncommitted."""
    _LOCAL_RENDER.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_RENDER.write_text(
        overlay_text(_LOCAL_SAMPLE), encoding="utf-8", newline="\n"
    )


def _paths() -> list[tuple[str, list[str]]]:
    """Return the (label, compose file list) pairs we ship and therefore must check."""
    for shipped in (COMPOSE_OVERRIDE_FILE, COMPOSE_LIVE_FILE):
        if not (shipped.is_file()):
            # Not a skip. Both overlays are committed (`make dev` auto-loads the
            # override, `make dev LIVE=1` chains the live file), so an absence
            # means that path is broken, not absent. Skipping would shrink the
            # coverage while still printing "All compose paths resolve".
            raise FileNotFoundError(
                f"{shipped.name} is missing -- it is committed and used by "
                "`make dev`, so that path cannot be checked"
            )
    return [
        ("registry", [COMPOSE_FILE.name]),
        ("dev", [COMPOSE_FILE.name, COMPOSE_OVERRIDE_FILE.name]),
        (
            "live",
            [COMPOSE_FILE.name, COMPOSE_OVERRIDE_FILE.name, COMPOSE_LIVE_FILE.name],
        ),
        ("local", [COMPOSE_FILE.name, str(_LOCAL_RENDER.relative_to(REPO_ROOT))]),
    ]


def _transform_wiring_failures() -> tuple[list[str], int]:
    """Return (messages, assertions made) for every profile's transform topics.

    Two properties, both of them things a profile can get wrong while every
    service still starts and reports healthy:

    - a WORKING transform's output topic must be read by that profile's loader,
      or the events reach a topic and stop there (dfe-docker#66);
    - two transform instances in one profile must not consume the same topic,
      because each event would then take whichever program won the partition.
    """
    data = _parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace")
    )
    failures: list[str] = []
    made = 0
    for name, profile in sorted(data.get("profiles", {}).items()):
        services = profile.get("services", {})
        transforms = {}
        for service, config in services.items():
            if not (service.startswith(_TRANSFORM_PREFIX)):
                continue
            config_path = config.get("config_path")
            if config_path is None:
                failures.append(
                    f"{name}: {service} declares no config_path -- its topics cannot "
                    "be read, so nothing checks what it consumes or produces"
                )
                continue
            transforms[service] = config_path
        if not (transforms):
            continue
        loader = services.get(_LOADER_SERVICE, {}).get("config_path")
        consumed, _, pattern = (
            _config_topics(path=CONFIG_DIR / loader) if loader else (set(), set(), "")
        )
        seen: dict[str, str] = {}
        for service, config_path in sorted(transforms.items()):
            subscribed, written = _transform_topics(path=CONFIG_DIR / config_path)
            for topic in sorted(written):
                made += 1
                if topic in consumed or (pattern and re.fullmatch(pattern, topic)):
                    continue
                failures.append(
                    f"{name}: {service} produces {topic} and no loader in the profile "
                    "consumes it -- events reach the topic and stop there"
                )
            for topic in sorted(subscribed):
                made += 1
                if topic in seen:
                    failures.append(
                        f"{name}: {service} and {seen[topic]} both consume {topic} -- "
                        "an event would take whichever program won the partition"
                    )
                seen[topic] = service
    return failures, made


def _bind_host_path(*, binds: dict[str, Path], target: str) -> Path | None:
    """Return the file on disk a container path resolves to, or None if nothing mounts it.

    Longest mount first, so a nested mount wins over the directory containing it.
    """
    for mount, source in sorted(binds.items(), key=lambda item: -len(item[0])):
        mount = mount.rstrip("/")
        if target == mount:
            return source
        if target.startswith(f"{mount}/"):
            return source / target[len(mount) + 1 :]
    return None


def _enrichment_table_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the enrichment tables the transforms name.

    An `enrichment_tables` entry names a CONTAINER path, and a transform that
    cannot open one fails to compile its programs at startup -- a running-stack
    failure compose resolution never sees. So each path is mapped back through the
    service's own bind mounts and the file is required to exist on disk.

    Every YAML the service mounts is read, files and mounted directories alike:
    dfe-transform-vrl declares its tables in the service config, and
    dfe-transform-vector declares them in the pipeline file inside its transforms
    directory.
    """
    config = _config_json(env=env, files=[COMPOSE_FILE.name])
    if config is None:
        return (
            ["the registry path did not resolve, so enrichment tables are unknown"],
            0,
        )
    failures: list[str] = []
    made = 0
    for name, service in sorted(config.get("services", {}).items()):
        if not (name.startswith(_TRANSFORM_PREFIX)):
            continue
        binds = {
            volume["target"]: Path(volume["source"])
            for volume in service.get("volumes") or []
            if volume.get("type") == "bind" and volume.get("source")
        }
        mounted: set[Path] = set()
        for path in binds.values():
            if path.is_file():
                mounted.add(path)
            elif path.is_dir():
                mounted |= {
                    child
                    for child in path.iterdir()
                    if child.is_file() and child.suffix in _TRANSFORM_FILE_SUFFIXES
                }
        for source in sorted(mounted):
            for target in sorted(_config_enrichment_paths(path=source)):
                made += 1
                host = _bind_host_path(binds=binds, target=target)
                if host is None:
                    failures.append(
                        f"{name}: enrichment table {target} is under no bind mount -- "
                        "the transform cannot open it and fails to compile its programs"
                    )
                elif not (host.is_file()):
                    failures.append(
                        f"{name}: enrichment table {target} maps to {host}, which is "
                        "not a file -- the transform fails to compile its programs"
                    )
    return failures, made


def _credential_fault(*, config: dict) -> str:
    """Return why the engine's admin credential is unusable, or empty when it is fine."""
    service = config.get("services", {}).get(_ENGINE_SERVICE, {})
    environment = service.get("environment") or {}
    # Engine contract: settings.is_dev_posture strips and lowercases DFE_ENV, and
    # auth.bootstrap.default_credentials_in_use strips the password before comparing.
    posture = str(environment.get(_POSTURE_KEY) or "").strip().lower()
    password = str(environment.get(_ADMIN_PASSWORD_KEY) or "").strip()
    if password and password != _DEFAULT_PASSWORD:
        return ""
    if posture in _DEV_POSTURES:
        return ""
    return (
        f"{_ENGINE_SERVICE} receives "
        f"{'no ' + _ADMIN_PASSWORD_KEY if not password else _ADMIN_PASSWORD_KEY + '=' + _DEFAULT_PASSWORD}"
        f" with {_POSTURE_KEY}={posture or 'unset'} -- the engine refuses to start on "
        "that outside a dev posture, so the container crash-loops. Run `make init` to "
        f"mint one, or `make dev` for a dev stack"
    )


def _empty_env_file() -> Path:
    """Write and return an empty env file, so a render loads no .env at all."""
    _EMPTY_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EMPTY_ENV_FILE.write_text("", encoding="utf-8", newline="\n")
    return _EMPTY_ENV_FILE


def _clickhouse_dial_mismatches(
    *, config: dict, want: tuple[str, str, str]
) -> list[str]:
    """Return one message per dialler whose ClickHouse address is not `want`."""
    services = config.get("services", {})
    failures = []
    for name, keys in sorted(_CLICKHOUSE_DIALLERS.items()):
        if name not in services:
            failures.append(f"{name}: dials ClickHouse but is not in the stack")
            continue
        environment = services[name].get("environment") or {}
        for key, expected in zip(keys, want, strict=True):
            if key is None:
                continue
            got = str(environment.get(key) or "")
            if got != expected:
                failures.append(f"{name}: {key}={got or 'unset'}, want {expected}")
    return failures


def _hyperdx_connection_mismatches(*, config: dict, want: str) -> list[str]:
    """Return a message unless HyperDX seeds exactly one connection, dialling `want`."""
    services = config.get("services", {})
    if _HYPERDX_SERVICE not in services:
        return [f"{_HYPERDX_SERVICE}: dials ClickHouse but is not in the stack"]
    environment = services[_HYPERDX_SERVICE].get("environment") or {}
    raw = str(environment.get(_HYPERDX_CONNECTIONS_KEY) or "")
    try:
        connections = json.loads(raw)
    except json.JSONDecodeError:
        return [f"{_HYPERDX_SERVICE}: {_HYPERDX_CONNECTIONS_KEY} is not JSON: {raw!r}"]
    if not (isinstance(connections, list)):
        return [f"{_HYPERDX_SERVICE}: {_HYPERDX_CONNECTIONS_KEY} is not a list"]
    hosts = [
        str(connection.get("host") or "") if isinstance(connection, dict) else ""
        for connection in connections
    ]
    if hosts != [want]:
        return [
            f"{_HYPERDX_SERVICE}: {_HYPERDX_CONNECTIONS_KEY} dials {hosts}, want [{want!r}]"
        ]
    return []


def _clickhouse_dial_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the address the stack dials ClickHouse on.

    CLICKHOUSE_HTTP_PORT and CLICKHOUSE_NATIVE_PORT publish the bundled container
    on the host, and the container listens on 8123/9000 whatever they say. A
    service dialling either in-network reaches a port nothing listens on as soon
    as a second stack moves the publish (#75). An external CLICKHOUSE_HOST is the
    one case whose port can differ, and CLICKHOUSE_EXTERNAL_*_PORT carries it.
    HyperDX is held to the same address, as the URL its seeded connection names.
    """
    empty_env_file = _empty_env_file()
    base = {k: v for k, v in env.items() if k not in _CLICKHOUSE_ADDRESS_KEYS}
    failures: list[str] = []
    made = 0
    for label, overrides, want, published, hyperdx_url in _CLICKHOUSE_DIAL_CASES:
        made += 1
        config = _config_json(
            env={**base, **overrides},
            files=[COMPOSE_FILE.name],
            env_file=empty_env_file,
        )
        if config is None:
            failures.append(f"the ClickHouse case {label!r} did not resolve")
            continue
        made += len(_CLICKHOUSE_DIALLERS) + 1
        failures.extend(
            f"{label}: {message}"
            for message in _clickhouse_dial_mismatches(config=config, want=want)
        )
        failures.extend(
            f"{label}: {message}"
            for message in _hyperdx_connection_mismatches(
                config=config, want=hyperdx_url
            )
        )
        got = tuple(
            port for _, port in _published(config=config, service=_CLICKHOUSE_SERVICE)
        )
        if got != published:
            failures.append(
                f"{label}: {_CLICKHOUSE_SERVICE} publishes {list(got)}, want "
                f"{list(published)} -- the case did not move what it claims to"
            )
    return failures, made


def _credential_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the engine's admin credential.

    Two things are checked. This checkout's own resolved stack must not hand the
    engine a credential it will refuse -- that is the assertion an operator wants.
    Then the matrix in `_CREDENTIAL_CASES` runs the rule against inputs whose answer
    is known, so a rule that has stopped flagging anything fails here rather than
    passing every stack silently.
    """
    failures: list[str] = []
    made = 1
    live = _config_json(env=env, files=[COMPOSE_FILE.name])
    if live is None:
        return (
            ["the registry path did not resolve, so the credential is unknown"],
            made,
        )
    fault = _credential_fault(config=live)
    if fault:
        failures.append(fault)

    empty_env_file = _empty_env_file()
    base = {
        k: v for k, v in env.items() if k not in (_POSTURE_KEY, _ADMIN_PASSWORD_KEY)
    }
    for label, posture, password, want_flagged in _CREDENTIAL_CASES:
        made += 1
        case = {**base, _ADMIN_PASSWORD_KEY: password}
        if posture:
            case[_POSTURE_KEY] = posture
        config = _config_json(
            env=case, files=[COMPOSE_FILE.name], env_file=empty_env_file
        )
        if config is None:
            failures.append(f"the credential case {label!r} did not resolve")
            continue
        flagged = bool(_credential_fault(config=config))
        if flagged != want_flagged:
            failures.append(
                f"credential case {label!r}: the check "
                f"{'flagged it' if flagged else 'let it through'}, and it must "
                f"{'flag it' if want_flagged else 'let it through'}"
            )
    return failures, made


def _log_driver_failures(*, config: dict) -> list[str]:
    """Return one message per fluentd-logged service missing a driver option it needs."""
    failures = []
    for name, service in sorted(config.get("services", {}).items()):
        logging = service.get("logging") or {}
        if logging.get("driver") != "fluentd":
            continue
        options = logging.get("options") or {}
        for key, consequence in sorted(_LOG_DRIVER_OPTIONS.items()):
            if str(options.get(key, "")).strip().lower() != "true":
                failures.append(
                    f"{name}: fluentd logging without {key} -- {consequence}"
                )
    return failures


def _container_log_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, services checked) for the log-driver fragment as it resolves."""
    config = _config_json(env=env, files=[COMPOSE_FILE.name, _CONTAINER_LOGS_FRAGMENT])
    if config is None:
        return ([f"{_CONTAINER_LOGS_FRAGMENT} did not resolve on the registry path"], 0)
    shipped = [
        name
        for name, service in config.get("services", {}).items()
        if (service.get("logging") or {}).get("driver") == "fluentd"
    ]
    if not (shipped):
        return (
            [
                f"{_CONTAINER_LOGS_FRAGMENT} puts no service on the fluentd driver -- "
                "this check would assert nothing"
            ],
            0,
        )
    return _log_driver_failures(config=config), len(shipped)


def _proxy_tls_mismatches(*, config: dict, tls: dict[str, str] | None) -> list[str]:
    """Return one message per way the proxies, the network and the engine differ from the console TLS side they are on.

    `tls` holds the values the Makefile exports under DFE_PROXY_TLS=true; None means the dial is off.
    """
    services = config.get("services", {})
    failures = []
    for name, (plain, encrypted) in sorted(_TLS_PROXY_CONFIGS.items()):
        if name not in services:
            failures.append(f"{name!r}: not in the stack")
            continue
        service = services[name]
        mounts = {
            str(volume.get("target")): volume
            for volume in (service.get("volumes")) or ([])
        }
        # Resolved on both sides, because compose names a source through the working directory it was handed.
        want = {_ENVOY_CONFIG_TARGET: REPO_ROOT / (plain if tls is None else encrypted)}
        if tls is not None:
            want[_ENVOY_CERT_TARGET] = REPO_ROOT / tls["DFE_PROXY_CERT_DIR"]
        want = {target: path.resolve() for target, path in want.items()}
        got = {
            target: Path(str(volume.get("source", ""))).resolve()
            for target, volume in mounts.items()
        }
        if got != want:
            failures.append(f"{name!r}: mounts {got}, want {want}")
        writable = sorted(
            target for target, volume in mounts.items() if not (volume.get("read_only"))
        )
        if writable:
            failures.append(f"{name!r}: mounts {writable} writable, want read-only")
        user = service.get("user")
        want_user = (
            None if tls is None else f"{tls['DFE_DEV_UID']}:{tls['DFE_DEV_GID']}"
        )
        if user != want_user:
            failures.append(
                f"{name!r}: runs as {user!r}, want {want_user!r}. Under TLS only the user who "
                "owns the 0600 key can read it; without TLS the image's own user runs"
            )

    default_network = (config.get("networks", {}).get("default")) or ({})
    ipam = (default_network.get("ipam")) or ({})
    pinned = (ipam.get("config")) or ([])
    want_pinned = (
        []
        if tls is None
        else [
            {
                "ip_range": tls["DFE_NETWORK_IP_RANGE"],
                "subnet": tls["DFE_NETWORK_SUBNET"],
            }
        ]
    )
    if pinned != want_pinned:
        failures.append(f"the default network pins {pinned}, want {want_pinned}")

    proxy_networks = (services.get(_TLS_FORWARDING_PROXY, {}).get("networks")) or ({})
    address = ((proxy_networks.get("default")) or ({})).get("ipv4_address")
    engine_environment = (services.get(_ENGINE_SERVICE, {}).get("environment")) or ({})
    trusted = engine_environment.get(_TRUSTED_PROXIES_KEY)
    want_address = None if tls is None else tls["DFE_PROXY_IP"]
    if address != want_address:
        failures.append(
            f"{_TLS_FORWARDING_PROXY!r}: holds address {address!r}, want {want_address!r}"
        )
    if trusted != want_address:
        failures.append(
            f"{_ENGINE_SERVICE!r}: {_TRUSTED_PROXIES_KEY}={trusted!r}, want {want_address!r}. "
            f"The engine must believe X-Forwarded-Proto from {_TLS_FORWARDING_PROXY!r} alone "
            "under TLS and from nothing while TLS is off"
        )
    return failures


def _proxy_tls_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, sides checked) for the registry path with console TLS off and on.

    The on side renders from a subnet and a user that are not the defaults, with the address and range derived the way the Makefile derives them.
    """
    address, allocation = proxy_tls.derive(subnet=_TLS_SUBNET)
    tls = {
        **_TLS_USER,
        "DFE_NETWORK_IP_RANGE": allocation,
        "DFE_NETWORK_SUBNET": _TLS_SUBNET,
        "DFE_PROXY_CERT_DIR": _TLS_CERT_DIR,
        "DFE_PROXY_IP": address,
    }
    failures = []
    for label, files, side in (
        ("TLS off", [COMPOSE_FILE.name], None),
        ("TLS on", [COMPOSE_FILE.name, _TLS_FRAGMENT], tls),
    ):
        config = _config_json(env={**env, **((side) or ({}))}, files=files)
        if config is None:
            failures.append(f"{label}: the registry path did not resolve")
            continue
        failures.extend(
            f"{label}: {message}"
            for message in _proxy_tls_mismatches(config=config, tls=side)
        )
    return failures, 2


def _unmounted_image_volume_failures(*, config: dict) -> list[str]:
    """Return one message per image-declared VOLUME path with nothing named mounted on it."""
    services = config.get("services", {})
    failures = []
    for name, paths in sorted(_IMAGE_VOLUMES.items()):
        if name not in services:
            failures.append(f"{name}: in _IMAGE_VOLUMES but not in the stack")
            continue
        mounted = {
            volume.get("target")
            for volume in services[name].get("volumes") or []
            if volume.get("source")
        }
        for path in paths:
            if path not in mounted:
                failures.append(
                    f"{name}: its image declares VOLUME {path} and compose mounts "
                    "nothing named there -- every `make down` strands an anonymous "
                    "volume that `make clean` cannot remove"
                )
    return failures


def _image_volume_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, paths checked) for the image VOLUME paths on the registry path.

    Both Kafka backends are resolved into the one model, since each declares its
    own paths.
    """
    config = _config_json(
        env=env, files=[COMPOSE_FILE.name], extra_profiles=(_KAFKA_BACKENDS[1],)
    )
    if config is None:
        return (["the registry path did not resolve, so volume mounts are unknown"], 0)
    made = sum(len(paths) for paths in _IMAGE_VOLUMES.values())
    return _unmounted_image_volume_failures(config=config), made


def _instance_name_collisions(*, services: dict) -> list[str]:
    """Return one message per committed service a generated instance could be named.

    scripts/instances.py names an app's per-source instance ``<app>-<source>`` and
    Compose merges two services sharing a name, so the source that completes the
    name would run with the committed service's volumes, ports and env files.
    """
    failures = []
    for name in sorted(services):
        for app in sorted(SERVICE_CONFIG_FILE):
            if name.startswith(f"{app}-"):
                failures.append(
                    f"{name}: the {app} instance for a source named "
                    f"{name[len(app) + 1 :]!r} takes this name, and Compose merges the "
                    "two -- rename it off the <app>-<source> shape, as the "
                    "dfe-transform-e2e-* services are"
                )
    return failures


def _instance_name_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, services checked) for every service in every profile."""
    config = _config_json(env=env, files=[COMPOSE_FILE.name], extra_profiles=("*",))
    if config is None:
        return (["the registry path did not resolve, so service names are unknown"], 0)
    services = config.get("services", {})
    return _instance_name_collisions(services=services), len(services)


def _sentinel_model(*, env: dict[str, str], fragment: str) -> dict | None:
    """Return the services, fragment included, rendered over sentinel env files.

    Every env file sets `_SENTINEL_LAST` to its own name, and each custom one a
    service could name also sets a `_SENTINEL_READ` key of its own, so a service's
    environment says which custom files it read and which env file it read last.
    A custom file is planted for every service, app and env.example stem, and a
    name outside those carries no sentinel and goes unseen.
    The effect is what is read because Compose 2.38, the CI runner's, prints no
    `env_file` under any `config` flag and rejects `--no-interpolate` here.

    Rendered from a copy of the compose file in a scratch directory, so no sentinel
    lands in this checkout's env/. A copy, because Compose resolves an extended
    service's paths against the file that declares it.
    """
    plain = _config_json(
        env=env, files=[COMPOSE_FILE.name], extra_profiles=("*",), fragment=fragment
    )
    if plain is None:
        return None
    templates = sorted(ENV_TEMPLATE_DIR.glob("*.env"))
    stems = {*plain.get("services", {}), *SERVICE_CONFIG_FILE}
    stems.update(template.stem for template in templates)
    with tempfile.TemporaryDirectory() as scratch:
        compose = Path(scratch) / COMPOSE_FILE.name
        shutil.copyfile(COMPOSE_FILE, compose)
        env_dir = Path(scratch) / ENV_DIR.name
        env_dir.mkdir()
        for template in templates:
            (env_dir / template.name).write_text(
                f"{_SENTINEL_LAST}={template.name}\n", encoding="utf-8", newline="\n"
            )
        for index, stem in enumerate(sorted(stems)):
            name = f"{stem}{CUSTOM_ENV_SUFFIX}"
            (env_dir / name).write_text(
                f"{_SENTINEL_LAST}={name}\n{_SENTINEL_READ}{index}={name}\n",
                encoding="utf-8",
                newline="\n",
            )
        config = _config_json(
            env=env, files=[str(compose)], extra_profiles=("*",), fragment=fragment
        )
    return None if config is None else config.get("services", {})


def _custom_env_mismatches(*, services: dict) -> list[str]:
    """Return one message per service the engine's custom env files do not reach.

    ``services`` is `_sentinel_model` output. dfe-engine writes
    ``<service>.custom.env`` into env/ for each service it renders, an instance
    being ``<app>-<instance>``. A service reading any other custom name reads a
    file nothing writes, and one whose own file is not last lets a later file
    outvote a key the operator set through the console.
    """
    failures = []
    for name, service in sorted(services.items()):
        environment = service.get("environment") or {}
        read = sorted(
            str(value)
            for key, value in environment.items()
            if key.startswith(_SENTINEL_READ)
        )
        last = environment.get(_SENTINEL_LAST)
        app = (service.get("labels") or {}).get(instances.INSTANCE_LABEL, "")
        rendered = name in SERVICE_CONFIG_FILE or app in SERVICE_CONFIG_FILE
        writable = {f"{n}{CUSTOM_ENV_SUFFIX}" for n in (name, app) if n and rendered}
        for file in read:
            if file not in writable:
                failures.append(
                    f"{name}: reads {ENV_DIR.name}/{file}, which dfe-engine never "
                    f"writes for it -- it writes {ENV_DIR.name}/<service>"
                    f"{CUSTOM_ENV_SUFFIX}, and only for a service it renders"
                )
        if not rendered:
            continue
        own = f"{name}{CUSTOM_ENV_SUFFIX}"
        if own not in read:
            failures.append(
                f"{name}: reads no {ENV_DIR.name}/{own}, so a key set through "
                "dfe-engine never reaches it"
            )
        elif last != own:
            failures.append(
                f"{name}: reads {ENV_DIR.name}/{last} after {ENV_DIR.name}/{own}, so "
                "it outvotes a key set through dfe-engine"
            )
    return failures


def _custom_env_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, services checked), one generated instance per app included."""
    probes = {
        app: ["compose-check"]
        for app in sorted(SERVICE_CONFIG_FILE)
        if instances.extendable(app)
    }
    services = _sentinel_model(env=env, fragment=instances.fragment(probes))
    if services is None:
        return (
            ["the registry path with a generated instance per app did not resolve"],
            0,
        )
    failures = [
        f"{probe}: generated but absent from the model, so its env files are unchecked"
        for probe in instances.services(probes)
        if probe not in services
    ]
    failures += _custom_env_mismatches(services=services)
    return failures, len(services)


def _override_coverage_failures(*, env: dict[str, str]) -> list[str]:
    """Return one message per service the committed override leaves on the registry.

    Read off the interpolated dev model: every service that runs a buildable
    component's image must resolve to `<component>:local` there.
    """
    config = _config_json(
        env=env, files=[COMPOSE_FILE.name, COMPOSE_OVERRIDE_FILE.name]
    )
    if config is None:
        return ["the dev path did not resolve, so override coverage is unknown"]
    services = config.get("services", {})
    failures = []
    for service, image in sorted(local_image_services(buildable_components()).items()):
        if service not in services:
            continue
        got = services[service].get("image", "")
        if got != image:
            failures.append(
                f"{COMPOSE_OVERRIDE_FILE.name}: {service} runs {got}, not {image} -- "
                "a local build of that component would sit beside a registry one"
            )
    return failures


def _local_overlay_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for the images the generated LOCAL overlay resolves to.

    The overlay is what `make dev LOCAL=...` puts in front of the registry path,
    so it is checked by VALUE and not merely parsed: the named component and every
    service running its image must resolve to `:local`, and every other service
    must keep the image the registry path gives it.
    """
    registry = _config_json(env=env, files=[COMPOSE_FILE.name])
    local = _config_json(
        env=env,
        files=[COMPOSE_FILE.name, str(_LOCAL_RENDER.relative_to(REPO_ROOT))],
    )
    if registry is None or local is None:
        return (
            ["the local overlay path did not resolve, so its images are unknown"],
            0,
        )

    want = local_image_services(_LOCAL_SAMPLE)
    services = local.get("services", {})
    failures = []
    made = 0
    for service, image in sorted(want.items()):
        made += 1
        if service not in services:
            failures.append(
                f"{_LOCAL_RENDER.name}: {service} runs a {', '.join(_LOCAL_SAMPLE)} "
                "image but is not in the resolved stack"
            )
            continue
        got = services[service].get("image", "")
        if got != image:
            failures.append(
                f"{_LOCAL_RENDER.name}: {service} runs {got}, not {image} -- a "
                "LOCAL build leaves a follower on the registry image"
            )
    for service, service_config in sorted(services.items()):
        if service in want:
            continue
        made += 1
        pinned = registry.get("services", {}).get(service, {}).get("image", "")
        got = service_config.get("image", "")
        if got != pinned:
            failures.append(
                f"{_LOCAL_RENDER.name}: {service} runs {got}, not its pin {pinned} -- "
                "LOCAL builds only what it names and leaves the rest on the registry"
            )
    return failures, made


def _overlay_staleness_failures() -> tuple[list[str], int]:
    """Return (messages, assertions made) for the overlay always describing THIS run.

    Runs the builder for a name it cannot build. The overlay must be gone and the
    run must fail: leaving the previous run's file would start services on
    `:local` images nothing rebuilt.
    """
    stale = REPO_ROOT / ".tmp" / "compose-check-stale-local.yml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(overlay_text(_LOCAL_SAMPLE), encoding="utf-8", newline="\n")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_dev_images.py",
            "--overlay",
            str(stale),
            _UNBUILDABLE_SAMPLE,
        ],
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    failures = []
    if stale.exists():
        stale.unlink()
        failures.append(
            f"build_dev_images.py left {stale.name} in place after building nothing "
            f"-- a `make dev LOCAL={_UNBUILDABLE_SAMPLE}` would run the previous "
            "run's overlay"
        )
    if result.returncode == 0:
        failures.append(
            f"build_dev_images.py exited 0 with nothing built for "
            f"{_UNBUILDABLE_SAMPLE!r} -- the compose call that follows has no overlay"
        )
    return failures, 2


def _implicit_consumer_failures(*, env: dict[str, str]) -> tuple[list[str], int]:
    """Return (messages, assertions made) for IMPLICIT_CONSUMERS matching the compose graph.

    The builder reads that table to work out which images a profile's stack needs
    without naming the service that runs them, so a `depends_on` edge changed here
    and not there costs `make dev` an image it never builds.
    """
    config = _config_json(env=env, files=[COMPOSE_FILE.name])
    if config is None:
        return (["the registry path did not resolve, so depends_on is unknown"], 0)
    services = config.get("services", {})
    failures = []
    made = 0
    for consumer, dependents in sorted(IMPLICIT_CONSUMERS.items()):
        made += 1
        if consumer not in services:
            failures.append(
                f"build_dev_images.py: {consumer} is in IMPLICIT_CONSUMERS but not in "
                "the stack"
            )
            continue
        graph = sorted(
            [
                name
                for name, service_config in services.items()
                if consumer in (service_config.get("depends_on") or {})
            ]
        )
        if graph != sorted(dependents):
            failures.append(
                f"build_dev_images.py: IMPLICIT_CONSUMERS[{consumer!r}] lists "
                f"{sorted(dependents)}, and compose starts it from {graph}"
            )
    return failures, made


def _make(
    *,
    goals: list[str],
    variables: dict[str, str],
    dry_run: bool = False,
    bin_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run make for one goal set, with `VAR=value` overrides on the command line.

    `bin_dir` goes first on PATH, so a stub there answers in place of a host tool.
    """
    cmd = ["make", "--no-print-directory"]
    if dry_run:
        cmd.append("-n")
    cmd += goals
    cmd += [f"{key}={value}" for key, value in sorted(variables.items())]
    environment = dict(os.environ)
    if bin_dir is not None:
        environment["PATH"] = f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}"
    return subprocess.run(
        cmd,
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=environment,
        errors="replace",
        text=True,
    )


def _declared_fragments() -> set[str]:
    """Return every overlay fragment name the Makefile passes to `chain_fragment`."""
    return set(
        _CHAIN_FRAGMENT_RE.findall(
            _MAKEFILE.read_text(encoding="utf-8", errors="replace")
        )
    )


def _dev_compose_files(*, output: str) -> dict[str, list[list[str]]]:
    """Return the `-f` file lists of the `docker compose` lines `make -n dev` prints.

    Keyed by subcommand, so the pull and the start are asserted separately: they
    are built from different variables, and a fix to one says nothing about the
    other. Every matching line is kept, because a goal that prints two `up -d`
    lines gets both asserted rather than only the last.
    """
    found: dict[str, list[list[str]]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not (line.startswith("docker compose ")):
            continue
        tokens = shlex.split(line)
        for label, markers in _DEV_SUBCOMMANDS.items():
            if not (all(marker in tokens for marker in markers)):
                continue
            found.setdefault(label, []).append(
                [tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == "-f"]
            )
    return found


def _compose_chain(
    *, bin_dir: Path | None = None, variables: dict[str, str]
) -> tuple[list[str], str]:
    """Return the COMPOSE_FILE chain make resolves for these dials, with make's output instead when it fails."""
    result = _make(bin_dir=bin_dir, goals=[_COMPOSE_FILE_GOAL], variables=variables)
    if result.returncode != 0:
        return [], (result.stderr.strip()) or (result.stdout.strip())
    chain = []
    for line in result.stdout.splitlines():
        if line.startswith(_COMPOSE_FILE_PREFIX):
            chain = line[len(_COMPOSE_FILE_PREFIX) :].strip().split(":")
    return chain, ""


def _dev_run_failures(
    *, bin_dir: Path, declared: set[str], run: str, variables: dict[str, str]
) -> tuple[list[str], int, set[str]]:
    """Return (messages, assertions made, fragments compared) for one `make -n dev LOCAL=...` run."""
    chain, error = _compose_chain(bin_dir=bin_dir, variables=variables)
    if error:
        return [f"{run}: `make {_COMPOSE_FILE_GOAL}` failed:\n{error}"], 1, set()
    want = {name for name in chain if name in declared}
    dev = _make(
        bin_dir=bin_dir,
        dry_run=True,
        goals=["dev"],
        variables={**variables, "LOCAL": " ".join(_LOCAL_SAMPLE)},
    )
    if dev.returncode != 0:
        detail = (dev.stderr.strip()) or (dev.stdout.strip())
        return [f"{run}: `make -n dev LOCAL=...` failed:\n{detail}"], 1, set()
    failures = []
    made = 0
    lines = _dev_compose_files(output=dev.stdout)
    for label in sorted(_DEV_SUBCOMMANDS):
        made += 1
        occurrences = lines.get(label)
        if not (occurrences):
            failures.append(
                f"{run}: `make -n dev LOCAL=...` printed no `docker compose ... {label}` line"
            )
            continue
        for files in occurrences:
            made += 1
            missing = sorted(want - set(files))
            if missing:
                failures.append(
                    f"{run}: `make dev LOCAL=...` runs its {label} without {', '.join(missing)} -- "
                    "the COMPOSE_FILE chain carries them and this path does not, so every "
                    "dial they hold is ignored on it"
                )
    return failures, made, want


def _dev_path_fragment_failures() -> tuple[list[str], int]:
    """Return (messages, assertions made) for the LOCAL dev path's overlay fragments.

    `make dev LOCAL=...` names its compose files explicitly rather than riding
    COMPOSE_FILE, so the two lists are assembled by different code and can
    disagree -- and did (dfe-docker#104): the explicit lists expanded UI_FLAGS
    before the block that fills it had run, which dropped every unpublish
    fragment and the container-logs one from the path a developer uses most. The
    unpublish half is the dangerous one: an operator who asked for a port to be
    closed still got it published.

    Every dial is forced on first, and the chain is required to carry the full
    declared set before anything is compared -- a run that chains fewer would
    make this assert nothing.

    `make dev` itself cannot start with every dial on, because console TLS refuses the auth profile. It runs once per side (`_DEV_PATH_RUNS`) against the chain that side resolves; together the runs must compare every declared fragment.
    """
    declared = _declared_fragments()
    made = 1
    chain, error = _compose_chain(variables=_FRAGMENT_DIALS)
    if error:
        return ([f"`make {_COMPOSE_FILE_GOAL}` failed:\n{error}"], made)
    want = {name for name in chain if name in declared}
    if want != declared:
        return (
            [
                f"the COMPOSE_FILE chain leaves out {', '.join(sorted(declared - want))} "
                "with every dial forced on -- this check would compare nothing"
            ],
            made,
        )

    failures = []
    compared = set()
    # The TLS precheck reads the pair with openssl, so it has to be a real one.
    proxy_tls.throwaway_pair(directory=_DEV_PATH_CERT_DIR)
    # The precheck also asks the daemon which subnets are taken. A stack this host happens to run must not decide a static check, so a stub docker answers that none are.
    _DEV_PATH_BIN_DIR.mkdir(exist_ok=True, parents=True)
    stub = _DEV_PATH_BIN_DIR / "docker"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    try:
        for run, overrides in sorted(_DEV_PATH_RUNS.items()):
            run_failures, run_made, run_compared = _dev_run_failures(
                bin_dir=_DEV_PATH_BIN_DIR,
                declared=declared,
                run=run,
                variables={**_FRAGMENT_DIALS, **overrides},
            )
            failures += run_failures
            made += run_made
            compared |= run_compared
    finally:
        shutil.rmtree(_DEV_PATH_CERT_DIR, ignore_errors=True)
        shutil.rmtree(_DEV_PATH_BIN_DIR, ignore_errors=True)
    made += 1
    if not (failures) and compared != declared:
        failures.append(
            f"no `make -n dev` run chains {', '.join(sorted(declared - compared))}, so "
            "its dev path is never compared"
        )
    return failures, made


def main() -> int:
    if not (COMPOSE_FILE.is_file()):
        _print(header=COMPOSE_FILE.name, msg="Not found")
        return 1

    try:
        _render_local_overlay()
        paths = _paths()
    except FileNotFoundError as error:
        _print(msg=str(error))
        return 1

    env, injected = _check_env()
    if injected:
        _print(
            msg=f"Placeholders injected for {len(injected)} unpinned key(s): {', '.join(injected)}"
        )
    else:
        _print(msg="Every mandatory key resolved from .env or the environment")

    failures = 0
    for label, files in paths:
        for backend in _KAFKA_BACKENDS:
            profiles = [*_BASE_PROFILES, backend]
            result = _compose_config(env=env, files=files, profiles=profiles)
            target = f"{label} / {backend}"
            if result.returncode == 0:
                _print(msg=f"OK   {target}")
                continue
            failures += 1
            detail = result.stderr.strip() or result.stdout.strip()
            _print(msg=f"FAIL {target}\n{detail}")

    if failures:
        _print(msg=f"{failures} compose path(s) failed to resolve")
        return 1
    _print(msg="All compose paths resolve")

    cpu_failures = [
        f"{label}: {message}"
        for label, files in paths
        for message in _health_cpu_failures(env=env, files=files)
    ]
    for message in cpu_failures:
        _print(msg=f"FAIL {message}")
    if cpu_failures:
        return 1
    _print(
        msg=f"Every health-checked service carries at least {_MIN_HEALTH_CPUS} CPUs "
        f"on all {len(paths)} path(s)"
    )
    _print(msg="No healthcheck targets a retired health path")

    wiring_failures, wiring_made = _transform_wiring_failures()
    for message in wiring_failures:
        _print(msg=f"FAIL {message}")
    if wiring_failures:
        return 1
    _print(
        msg="Every transform output topic is read by its profile's loader, and no two "
        f"transforms in a profile consume the same topic ({wiring_made} assertions)"
    )

    table_failures, table_made = _enrichment_table_failures(env=env)
    for message in table_failures:
        _print(msg=f"FAIL {message}")
    if table_failures:
        return 1
    _print(
        msg="Every enrichment table a transform config names resolves to a file "
        f"through that service's bind mounts ({table_made} assertions)"
    )

    credential_failures, credential_made = _credential_failures(env=env)
    for message in credential_failures:
        _print(msg=f"FAIL {message}")
    if credential_failures:
        return 1
    _print(
        msg=f"{_ENGINE_SERVICE} never receives an empty or {_DEFAULT_PASSWORD!r} admin "
        f"password outside a dev posture ({credential_made} assertions)"
    )

    dial_failures, dial_made = _clickhouse_dial_failures(env=env)
    for message in dial_failures:
        _print(msg=f"FAIL {message}")
    if dial_failures:
        return 1
    _print(
        msg="Moving ClickHouse's host publish moves no in-network dial, and an "
        f"external CLICKHOUSE_HOST is dialled on its own ports ({dial_made} assertions)"
    )

    log_failures, log_made = _container_log_failures(env=env)
    for message in log_failures:
        _print(msg=f"FAIL {message}")
    if log_failures:
        return 1
    _print(
        msg=f"All {log_made} service(s) on the fluentd driver queue their lines and "
        "wait for the collector's ack"
    )

    tls_failures, tls_made = _proxy_tls_failures(env=env)
    for message in tls_failures:
        _print(msg=f"FAIL {message}")
    if tls_failures:
        return 1
    _print(
        msg="Console TLS changes nothing while off; on, it moves both proxies onto "
        "their TLS configs and the certificate and trusts dfe-proxy's pinned address "
        f"alone ({tls_made} sides)"
    )

    volume_failures, volume_made = _image_volume_failures(env=env)
    for message in volume_failures:
        _print(msg=f"FAIL {message}")
    if volume_failures:
        return 1
    _print(
        msg=f"Every VOLUME path the pinned images declare is mounted from a named "
        f"volume or a bind ({volume_made} assertions)"
    )

    name_failures, name_made = _instance_name_failures(env=env)
    for message in name_failures:
        _print(msg=f"FAIL {message}")
    if name_failures:
        return 1
    _print(
        msg=f"None of the {name_made} committed service(s) can take a generated "
        "per-source instance's name"
    )

    custom_failures, custom_made = _custom_env_failures(env=env)
    for message in custom_failures:
        _print(msg=f"FAIL {message}")
    if custom_failures:
        return 1
    _print(
        msg="Every service dfe-engine renders, a generated instance of each included, "
        f"reads the custom env file the engine writes for it last, and none of the "
        f"{custom_made} service(s) reads one it does not write"
    )

    coverage_failures = _override_coverage_failures(env=env)
    for message in coverage_failures:
        _print(msg=f"FAIL {message}")
    if coverage_failures:
        return 1
    _print(
        msg=f"{COMPOSE_OVERRIDE_FILE.name} repoints every service that runs a buildable "
        "component's image, consumers included"
    )

    local_failures, local_made = _local_overlay_failures(env=env)
    for message in local_failures:
        _print(msg=f"FAIL {message}")
    if local_failures:
        return 1
    _print(
        msg=f"The generated LOCAL overlay puts {', '.join(_LOCAL_SAMPLE)} and its "
        f"consumers on :local and everything else on its pin ({local_made} assertions)"
    )

    stale_failures, stale_made = _overlay_staleness_failures()
    for message in stale_failures:
        _print(msg=f"FAIL {message}")
    if stale_failures:
        return 1
    _print(
        msg="A build that builds nothing removes the overlay instead of leaving the "
        f"previous run's ({stale_made} assertions)"
    )

    implicit_failures, implicit_made = _implicit_consumer_failures(env=env)
    for message in implicit_failures:
        _print(msg=f"FAIL {message}")
    if implicit_failures:
        return 1
    _print(
        msg="Every consumer the builder treats as implicitly started is started by "
        f"exactly the services it lists ({implicit_made} assertions)"
    )

    ui_failures, ui_made = _ui_exposure_failures(env=env)
    for message in ui_failures:
        _print(msg=f"FAIL {message}")
    if ui_failures:
        return 1
    _print(
        msg=f"All {len(_UI_EXPOSURE)} web UI(s) follow the bind scope on both addresses, "
        f"and each unpublish fragment drops only its own ({ui_made} assertions)"
    )

    auth_failures, auth_made = _auth_exposure_failures(env=env)
    for message in auth_failures:
        _print(msg=f"FAIL {message}")
    if auth_failures:
        return 1
    _print(
        msg=f"No stack needs an OIDC setting, and the {_AUTH_PROFILE} profile moves every "
        f"infra UI behind a proxy the kill switch still covers ({auth_made} assertions)"
    )

    fragment_failures, fragment_made = _dev_path_fragment_failures()
    for message in fragment_failures:
        _print(msg=f"FAIL {message}")
    if fragment_failures:
        return 1
    _print(
        msg="`make dev LOCAL=...` pulls and starts with the same overlay fragments the "
        f"COMPOSE_FILE chain carries ({fragment_made} assertions)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
