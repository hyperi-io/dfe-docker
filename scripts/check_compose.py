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

Note that on a fresh checkout Make will have created a .env before this runs (the
`-include .env` rule in the Makefile), so the two generated secrets resolve for
real and only the image pins get placeholders. That is harmless here -- this checks
compose STRUCTURE, not that any particular digest exists.

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
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from _common import (
    COMPOSE_FILE,
    COMPOSE_LIVE_FILE,
    COMPOSE_OVERRIDE_FILE,
    CONFIG_DIR,
    REPO_ROOT,
    SERVICE_PROFILES_FILE,
    _config_enrichment_paths,
    _config_topics,
    _dotenv_values,
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
    "DFE_OAUTH2_PROXY_COOKIE_SECRET": "0123456789abcdef0123456789abcdef",
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

# Services this project owns and therefore holds to that surface. hyperdx,
# clickhouse, the brokers and kafka-ui are third-party and keep their own.
_DFE_OWNED_SERVICES = {
    "dfe-archiver",
    "dfe-engine",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vector-filebeat",
    "dfe-transform-vrl",
    "dfe-transform-vrl-filebeat",
    "dfe-ui",
}

# A dfe-transform-vector pipeline declares its own enrichment tables, and the
# service mounts the directory holding it rather than the file.
_TRANSFORM_FILE_SUFFIXES = (".yaml", ".yml")

# Profile services whose config declares a Kafka sink topic somebody has to read.
_TRANSFORM_PREFIX = "dfe-transform-"
_LOADER_SERVICE = "dfe-loader"


def _check_env() -> tuple[dict[str, str], list[str]]:
    """Return (subprocess environment, keys we had to invent a value for).

    .env is folded in first so a pinned local checkout validates its REAL pins;
    placeholders then fill only what is still missing, which on CI is all of them.
    """
    env = {**_dotenv_values(), **os.environ}
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
) -> dict | None:
    """Return the fully interpolated compose model, or None if it did not resolve.

    Every base profile is turned on, plus one Kafka backend, so the model carries
    every service a reader might assert about. `auth` is opt-in and therefore only
    arrives through `extra_profiles`. Resolution failures are reported by the loop
    in main(), so a None here needs no second message.

    `env_file` replaces the .env compose would otherwise load, which is what lets a
    caller assert about a variable this checkout happens to have set.
    """
    cmd = ["docker", "compose"]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    for path in files:
        cmd += ["-f", path]
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

    _EMPTY_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EMPTY_ENV_FILE.write_text("", encoding="utf-8", newline="\n")
    base = {
        k: v for k, v in env.items() if k not in (_POSTURE_KEY, _ADMIN_PASSWORD_KEY)
    }
    for label, posture, password, want_flagged in _CREDENTIAL_CASES:
        made += 1
        case = {**base, _ADMIN_PASSWORD_KEY: password}
        if posture:
            case[_POSTURE_KEY] = posture
        config = _config_json(
            env=case, files=[COMPOSE_FILE.name], env_file=_EMPTY_ENV_FILE
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
    *, goals: list[str], variables: dict[str, str], dry_run: bool = False
) -> subprocess.CompletedProcess:
    """Run make for one goal set, with `VAR=value` overrides on the command line."""
    cmd = ["make", "--no-print-directory"]
    if dry_run:
        cmd.append("-n")
    cmd += goals
    cmd += [f"{key}={value}" for key, value in sorted(variables.items())]
    return subprocess.run(
        cmd,
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
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
    """
    declared = _declared_fragments()
    made = 1
    reference = _make(goals=[_COMPOSE_FILE_GOAL], variables=_FRAGMENT_DIALS)
    if reference.returncode != 0:
        detail = reference.stderr.strip() or reference.stdout.strip()
        return ([f"`make {_COMPOSE_FILE_GOAL}` failed:\n{detail}"], made)
    chain: list[str] = []
    for line in reference.stdout.splitlines():
        if line.startswith(_COMPOSE_FILE_PREFIX):
            chain = line[len(_COMPOSE_FILE_PREFIX) :].strip().split(":")
    want = {name for name in chain if name in declared}
    if want != declared:
        return (
            [
                f"the COMPOSE_FILE chain leaves out {', '.join(sorted(declared - want))} "
                "with every dial forced on -- this check would compare nothing"
            ],
            made,
        )

    failures: list[str] = []
    dev = _make(
        goals=["dev"],
        variables={**_FRAGMENT_DIALS, "LOCAL": " ".join(_LOCAL_SAMPLE)},
        dry_run=True,
    )
    if dev.returncode != 0:
        detail = dev.stderr.strip() or dev.stdout.strip()
        return ([f"`make -n dev LOCAL=...` failed:\n{detail}"], made)
    lines = _dev_compose_files(output=dev.stdout)
    for label in sorted(_DEV_SUBCOMMANDS):
        made += 1
        occurrences = lines.get(label)
        if not (occurrences):
            failures.append(
                f"`make -n dev LOCAL=...` printed no `docker compose ... {label}` line"
            )
            continue
        for files in occurrences:
            made += 1
            missing = sorted(want - set(files))
            if missing:
                failures.append(
                    f"`make dev LOCAL=...` runs its {label} without {', '.join(missing)} -- "
                    "the COMPOSE_FILE chain carries them and this path does not, so every "
                    "dial they hold is ignored on it"
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
