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

Each is checked against both Kafka backends, because the two are mutually
exclusive and a change can easily satisfy one and break the other -- which is
precisely what happened with the topic-init service that ran a Redpanda image on
the Apache profile.

Beyond resolution, three semantic assertions ride along.

- No service that gates on a health endpoint may carry a CPU ceiling under
  `_MIN_HEALTH_CPUS` -- see that constant for why a lower ceiling takes the
  endpoint dark while the data path keeps working.
- The web-UI exposure dials must do what they claim: the bind scope moves every
  UI port and nothing else, and each unpublish fragment drops that UI's ports and
  no other service's. See `_UI_EXPOSURE`.
- The opt-in auth profile must gate without holes: no stack needs an OIDC setting
  to resolve, an armed profile moves every infra-UI origin behind a proxy, and
  the infra kill switch takes the proxies down with the UIs. See `_AUTH_PROXIES`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from _common import (
    COMPOSE_FILE,
    COMPOSE_LIVE_FILE,
    COMPOSE_OVERRIDE_FILE,
    REPO_ROOT,
    _dotenv_values,
    _print,
    _required_compose_vars,
)

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
    "hyperdx": ("infra", "docker-compose.unpublish-hyperdx.yml"),
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
    "hyperdx": (
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

# Services this project owns and therefore holds to that surface. hyperdx,
# clickhouse, the brokers and kafka-ui are third-party and keep their own.
_DFE_OWNED_SERVICES = {
    "dfe-archiver",
    "dfe-engine",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-ui",
}


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
    *, env: dict[str, str], files: list[str], extra_profiles: tuple[str, ...] = ()
) -> dict | None:
    """Return the fully interpolated compose model, or None if it did not resolve.

    Every base profile is turned on, plus one Kafka backend, so the model carries
    every service a reader might assert about. `auth` is opt-in and therefore only
    arrives through `extra_profiles`. Resolution failures are reported by the loop
    in main(), so a None here needs no second message.
    """
    cmd = ["docker", "compose"]
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
    ]


def main() -> int:
    if not (COMPOSE_FILE.is_file()):
        _print(header=COMPOSE_FILE.name, msg="Not found")
        return 1

    try:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
