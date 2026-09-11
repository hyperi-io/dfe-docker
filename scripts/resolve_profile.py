#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/resolve_profile.py
#  Purpose:      Read service_profiles.yaml and output Make-consumable profile variables
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve DFE service profile from service_profiles.yaml.

Reads service_profiles.yaml, resolves the active profile (overridable via DFE_PROFILE env var), validates config paths exist and writes to the .profile.mk file.
For kafka transport, KAFKA_BACKEND selects the backend (defaults to redpanda).

A profile declares its whole FOOTPRINT, not just the data plane. The optional
``core`` / ``kafbat`` / ``hyperdx`` / ``clickhouse`` / ``otel`` / ``auth`` keys
say which of those components run; an absent key keeps the historical default, so
a profile that declares none behaves exactly as it did before the keys existed.

``auth`` defaults OFF and is the only key whose settings are validated here: the
oauth2-proxies it starts need an OIDC issuer, and no dfe-docker profile may
require one to exist.

The matching env var wins over the profile key, because ``.env`` is what a deploy
writes (via the deployment dial) while the profile is the committed shape -- the
same precedence ``DFE_PROFILE`` already has over ``active_profile``.
"""

from __future__ import annotations

import os
from pathlib import Path

from _common import (
    CONFIG_DIR,
    FALSY,
    PROFILE_MK,
    SERVICE_PROFILES_FILE,
    _config_topics,
    _load_dotenv,
    _print,
    _rel_path,
)
from build_dev_images import buildable_components

AUTH_ENABLED_ENV_VAR = "DFE_AUTH_ENABLED"
# One proxy per infra-UI ORIGIN, not per UI: oauth2-proxy serves a single
# --http-address, and HyperDX's app and API are two origins of one UI. Each list
# rides on its UI being in the footprint -- a proxy in front of a UI that is not
# running is a port bound to nothing.
AUTH_KAFBAT_SERVICES = ["oauth2-proxy-kafbat"]
AUTH_HYPERDX_SERVICES = ["oauth2-proxy-hyperdx", "oauth2-proxy-hyperdx-api"]

# The auth profile needs a real issuer, client and cookie secret. Compose cannot
# demand them: it interpolates the whole file before it filters by profile, so a
# hard-fail key inside a profiled service aborts a stack that never armed that
# profile. This is the only place that knows the auth footprint resolved on.
AUTH_REQUIRED_ENV_VARS = (
    "DFE_OIDC_ISSUER_URL",
    "DFE_OIDC_CLIENT_ID",
    "DFE_OIDC_CLIENT_SECRET",
    "DFE_OAUTH2_PROXY_COOKIE_SECRET",
)
# Absent is fine (compose supplies the default group list). Set-but-BLANK is the
# dangerous shape: oauth2-proxy with no allowed group authenticates but checks no
# membership, which is authn alone -- exactly what the infra gate exists to stop.
AUTH_NON_BLANK_IF_SET = ("DFE_OIDC_ALLOWED_GROUPS",)

ENGINE_BROKER_ENV_VAR = "DFE_KAFKA_BOOTSTRAP_SERVERS"
IN_STACK_BROKER = "kafka:9092"

# `make stack` pins dfe-ui as a container reference (tag@sha256:...), which is
# what the image line needs and is not a version anyone reads. The engine reports
# the console's version, so it is handed the tag under its own name.
UI_VERSION_ENV_VAR = "DFE_UI_VERSION"
UI_VERSION_TAG_VAR = "DFE_UI_VERSION_TAG"

CORE_ENABLED_ENV_VAR = "DFE_CORE_ENABLED"
CORE_SERVICES = ["dfe-engine", "dfe-hunt-runner", "dfe-ui", "dfe-proxy"]

HYPERDX_ENABLED_ENV_VAR = "DFE_HYPERDX_ENABLED"
# dfe-dashboards is the one-shot that writes the engine's shipped dashboards into
# the volume HyperDX's provisioner reads; it exits before hyperdx starts.
# dfe-hyperdx-proxy carries the host ports, because HyperDX takes its identity
# from headers that proxy injects.
HYPERDX_SERVICES = [
    "dfe-dashboards",
    "dfe-hyperdx-proxy",
    "hyperdx",
    "hyperdx-ferretdb",
    "hyperdx-postgres",
]

OTEL_ENABLED_ENV_VAR = "DFE_OTEL_ENABLED"
OTEL_SERVICES = ["otel-collector"]
# Where the services push when the bundled collector runs and .env names no
# endpoint of its own. Empty exports nothing, which is the stack default.
OTEL_ENDPOINT_ENV_VAR = "DFE_OTEL_EXPORTER_ENDPOINT"
OTEL_BUNDLED_ENDPOINT = "http://otel-collector:4317"

# The Rust services push whenever the endpoint is set. dfe-engine also needs its
# backend switched, because scalo-py's CLI defaults it to prometheus (scalo-py#11).
# `opentelemetry` is dual -- it pushes AND keeps serving /metrics, so switching
# costs a scraping estate nothing.
OTEL_ENGINE_BACKEND_ENV_VAR = "DFE_ENGINE_METRICS_BACKEND"
OTEL_ENGINE_BACKEND = "opentelemetry"

# Footprint components a profile may declare: yaml key -> (env override, default
# when neither the key nor the env var is set). kafbat's default only applies on
# the kafka transport; there is no Kafka UI without a broker.
FOOTPRINT_KEYS = {
    "auth": (AUTH_ENABLED_ENV_VAR, False),
    "clickhouse": ("DFE_CLICKHOUSE_ENABLED", True),
    "core": (CORE_ENABLED_ENV_VAR, True),
    "hyperdx": (HYPERDX_ENABLED_ENV_VAR, False),
    "kafbat": ("KAFBAT_ENABLED", True),
    "otel": (OTEL_ENABLED_ENV_VAR, False),
}

# Topics kafka-init pre-creates: the stack default plus the ones this profile's
# transforms name. dfe-transform-vrl exits on a missing topic, its sink included.
KAFKA_INIT_TOPICS_VAR = "KAFKA_INIT_TOPICS"
KAFKA_INIT_TOPIC_DEFAULT = "main_land"
TRANSFORM_SERVICE_PREFIX = "dfe-transform-"

PROFILE_ENV_VAR = "DFE_PROFILE"
PROFILE_ACTIVE_YAML_FIELD = "active_profile"
PROFILE_LIST_YAML_FIELD = "profiles"
PROFILE_SERVICE_CONFIG_YAML_FIELD = "config_path"

SERVICE_KAFKA_DEFAULT = "redpanda"
SERVICE_KAFKA_OPTIONS = {
    "redpanda": "kafka-redpanda",
    "apache": "kafka-apache",
}
SERVICE_TO_CONFIG_VAR = {
    "dfe-archiver": "DFE_ARCHIVER_CONFIG",
    "dfe-fetcher": "DFE_FETCHER_CONFIG",
    "dfe-loader": "DFE_LOADER_CONFIG",
    "dfe-receiver": "DFE_RECEIVER_CONFIG",
    "dfe-transform-vector": "DFE_TRANSFORM_VECTOR_CONFIG",
    "dfe-transform-vector-filebeat": "DFE_TRANSFORM_VECTOR_FILEBEAT_CONFIG",
    "dfe-transform-vrl": "DFE_TRANSFORM_VRL_CONFIG",
    "dfe-transform-vrl-filebeat": "DFE_TRANSFORM_VRL_FILEBEAT_CONFIG",
}
# A per-source transform instance is a service of its own here, because a
# profile has to be able to run one without the other.
SERVICES = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vector-filebeat",
    "dfe-transform-vrl",
    "dfe-transform-vrl-filebeat",
]

TRANSPORT_TYPES = ["grpc", "kafka"]


class _ProfileError(Exception):
    """Raised when the active profile cannot be resolved or validated."""

    def __init__(self, *, header: str | None = None, msg: str) -> None:
        """Carry the failure header and message for reporting at the boundary."""
        super().__init__(msg)
        self.header = header
        self.msg = msg


def _env_truthy(*, default: bool, name: str) -> bool:
    """Return True unless the env var is explicitly set to a falsy value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSY


def _validate_auth() -> None:
    """Raise unless every setting the armed auth profile needs carries a real value.

    Naming the missing key matters more than the count: an oauth2-proxy started
    without an issuer accepts connections and redirects them nowhere, which reads
    as a broken UI rather than as configuration nobody supplied.
    """
    missing = [
        name for name in AUTH_REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()
    ]
    blank = [
        name
        for name in AUTH_NON_BLANK_IF_SET
        if name in os.environ and not os.environ[name].strip()
    ]
    if not (missing) and not (blank):
        return
    detail = [f"- {name} is empty or unset" for name in missing]
    detail += [
        f"- {name} is set to an empty value, which allows any group" for name in blank
    ]
    raise _ProfileError(
        header="auth",
        msg="The auth profile is armed but its OIDC settings are incomplete:\n"
        + "\n".join(detail)
        + f"\nSet them in .env, or turn the profile off with {AUTH_ENABLED_ENV_VAR}=false",
    )


def _init_topics(*, services: dict[str, object]) -> list[str]:
    """Return the topics kafka-init must create for this profile, sorted."""
    topics = {KAFKA_INIT_TOPIC_DEFAULT}
    for service_name, service_config in services.items():
        if not (service_name.startswith(TRANSFORM_SERVICE_PREFIX)):
            continue
        path = CONFIG_DIR / service_config[PROFILE_SERVICE_CONFIG_YAML_FIELD]
        subscribed, produced, _ = _config_topics(path=path)
        topics |= subscribed | produced
    return sorted(topics)


def _footprint(*, profile: dict[str, object], profile_name: str) -> dict[str, bool]:
    """Resolve which footprint components run: env var, else profile key, else default."""
    resolved: dict[str, bool] = {}
    for key, (env_var, default) in FOOTPRINT_KEYS.items():
        declared = profile.get(key)
        if declared is not None and not (isinstance(declared, str)):
            raise _ProfileError(
                header=profile_name,
                msg=f"Footprint key {key!r} must be true or false, not a block",
            )
        fallback = (
            default if declared is None else declared.strip().lower() not in FALSY
        )
        resolved[key] = _env_truthy(default=fallback, name=env_var)
    return resolved


def _parse_yaml(*, text: str) -> dict[str, object]:
    """Parse a minimal YAML subset into nested dicts with string values."""
    root = {}
    stack = [(root, -1)]

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not (line) or (line.isspace()) or (line.startswith("#")):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()
        parent = stack[-1][0]

        if ": " in line:
            key, value = line.split(": ", 1)
            value = value.strip('"')
            parent[key] = value
        elif line.endswith(":"):
            key = line[:-1]
            child = {}
            parent[key] = child
            stack.append((child, indent))
        else:
            raise _ProfileError(msg=f"Line {line_num} cannot be parsed: {line!r}")
    return root


def _resolve_profile(
    *, data: dict[str, object]
) -> tuple[str, str, dict[str, object], dict[str, bool]]:
    """Resolve active profile and return (name, transport, services_dict, footprint)."""
    active_profile = os.environ.get(PROFILE_ENV_VAR, "") or data.get(
        PROFILE_ACTIVE_YAML_FIELD, None
    )
    if not (active_profile):
        raise _ProfileError(
            msg=f"No active profile. Set {PROFILE_ACTIVE_YAML_FIELD!r} in {Path(SERVICE_PROFILES_FILE).name!r} or use the {PROFILE_ENV_VAR!r} environment variable"
        )

    profiles = data.get(PROFILE_LIST_YAML_FIELD)
    if not (profiles) or not (isinstance(profiles, dict)):
        raise _ProfileError(
            msg=f"{Path(SERVICE_PROFILES_FILE).name!r} missing {PROFILE_LIST_YAML_FIELD!r} section"
        )

    if active_profile not in profiles:
        available_profiles = [f"- {profile_name}" for profile_name in profiles.keys()]
        raise _ProfileError(
            msg=f"Profile {active_profile!r} not found. Available profiles:\n{'\n'.join(available_profiles)}"
        )

    _print(msg=f"Using profile {active_profile!r}...")
    profile = profiles[active_profile]

    # A typo like `cor: true` would otherwise resolve to the default and start a
    # footprint nobody asked for, silently.
    known_keys = {"transport", "services"} | set(FOOTPRINT_KEYS)
    unknown = sorted(set(profile) - known_keys)
    if unknown:
        raise _ProfileError(
            header=active_profile,
            msg=f"Unknown profile key(s) {', '.join(unknown)}. Known keys:\n{'\n'.join(f'- {key}' for key in sorted(known_keys))}",
        )

    transport = profile.get("transport", "")
    if transport not in TRANSPORT_TYPES:
        raise _ProfileError(
            header=active_profile,
            msg=f"Transport type {transport!r} unknown. Available transport types:\n{'\n'.join([f'- {transport_type}' for transport_type in TRANSPORT_TYPES])}",
        )

    services = profile.get("services", {})
    if not (services):
        raise _ProfileError(header=active_profile, msg="Undefined services")

    for service_name, service_config in services.items():
        if service_name not in SERVICES:
            raise _ProfileError(
                header=active_profile,
                msg=f"Service {service_name!r} unknown. Available services:\n{'\n'.join([f'- {service}' for service in SERVICES])}",
            )
        if not (isinstance(service_config, dict)) or (
            PROFILE_SERVICE_CONFIG_YAML_FIELD not in service_config
        ):
            raise _ProfileError(
                header=active_profile,
                msg=f"Missing {PROFILE_SERVICE_CONFIG_YAML_FIELD!r} value for service {service_name!r}",
            )
        config_file = CONFIG_DIR / service_config[PROFILE_SERVICE_CONFIG_YAML_FIELD]
        if not (config_file.is_file()):
            raise _ProfileError(
                header=active_profile,
                msg=f"Config file {_rel_path(path=config_file)!r} not found.",
            )

    return (
        active_profile,
        transport,
        services,
        _footprint(profile=profile, profile_name=active_profile),
    )


def main() -> int:
    try:
        if not (SERVICE_PROFILES_FILE.is_file()):
            raise _ProfileError(
                header=_rel_path(path=SERVICE_PROFILES_FILE),
                msg=f"File not found at {_rel_path(path=SERVICE_PROFILES_FILE)!r}",
            )

        _load_dotenv()
        data = _parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace")
        )
        active_profile, transport, services, footprint = _resolve_profile(data=data)

        # Build infra compose profile flags
        profiles = []
        if footprint["auth"]:
            _validate_auth()
            profiles.append("auth")
        if footprint["clickhouse"]:
            profiles.append("clickhouse")
        if footprint["otel"]:
            profiles.append("otel")
        kafka_ui_enabled = False
        if transport == "kafka":
            backend = (
                os.environ.get("KAFKA_BACKEND", SERVICE_KAFKA_DEFAULT).strip().lower()
            )
            if backend not in SERVICE_KAFKA_OPTIONS:
                raise _ProfileError(
                    msg=f"Kafka backend {backend!r} unknown. Available Kafka backends:\n{'\n'.join([f'- {option}' for option in SERVICE_KAFKA_OPTIONS])}",
                )
            profiles.append(SERVICE_KAFKA_OPTIONS[backend])
            # No broker, no Kafka UI -- the grpc transport starts neither.
            if footprint["kafbat"]:
                profiles.append("kafka-ui")
                kafka_ui_enabled = True

        # Write to .profile.mk ($(shell) collapses newlines)
        lines = []
        profile_flags = " ".join(f"--profile {profile}" for profile in profiles)
        lines.append(f"export PROFILE_FLAGS := {profile_flags}")
        lines.append(
            f"export {KAFKA_INIT_TOPICS_VAR} := {' '.join(_init_topics(services=services))}"
        )
        # ClickHouse stays out of this list -- it starts via its compose profile
        # and the depends_on of whatever needs it. DFE_SERVICES is also what
        # build_dev_images.py builds and what `SERVICES=` narrows against.
        service_list = sorted(services.keys())
        if kafka_ui_enabled:
            service_list.append("kafka-ui")
        if footprint["core"]:
            service_list.extend(CORE_SERVICES)
        if footprint["hyperdx"]:
            service_list.extend(HYPERDX_SERVICES)
        if footprint["otel"]:
            service_list.extend(OTEL_SERVICES)
        if footprint["auth"]:
            if kafka_ui_enabled:
                service_list.extend(AUTH_KAFBAT_SERVICES)
            if footprint["hyperdx"]:
                service_list.extend(AUTH_HYPERDX_SERVICES)
        lines.append(f"export DFE_SERVICES := {' '.join(service_list)}")

        # The manifest's name for this Compose tier (dfe-infra apps.yaml says
        # docker-slim, docker-single), which the engine seeds its default app
        # instances from and reports as its deployment profile.
        lines.append(f"export DFE_STACK_PROFILE := docker-{active_profile}")

        # The one transport this tier binds its stages to, in the engine's own
        # vocabulary: it refuses a source on the other one at save.
        bus = transport == "kafka"
        lines.append(f"export DFE_TRANSPORT_DEFAULT := {'bus' if bus else 'direct'}")
        lines.append(
            f"export DFE_TRANSPORT_BUS_PRESENT := {'true' if bus else 'false'}"
        )
        # The engine creates a source's topics on the in-stack broker, which every
        # kafka tier reaches by the `kafka` alias; .env names an external broker
        # instead, and a direct tier hands the engine no broker at all.
        if bus:
            broker = (
                os.environ.get(ENGINE_BROKER_ENV_VAR, "").strip() or IN_STACK_BROKER
            )
            lines.append(f"export {ENGINE_BROKER_ENV_VAR} := {broker}")

        # The console version the engine reports. Emitted unconditionally, empty
        # when nothing is pinned, so the included file settles on every make pass.
        ui_ref = os.environ.get(UI_VERSION_ENV_VAR, "").strip()
        lines.append(f"export {UI_VERSION_TAG_VAR} := {ui_ref.split('@', 1)[0]}")

        # `LOCAL=` names components to BUILD, which is a smaller set than the
        # services a profile runs, so make validates it against the builder's own
        # list rather than against DFE_SERVICES.
        lines.append(
            f"export DFE_BUILDABLE_SERVICES := {' '.join(buildable_components())}"
        )

        # The Makefile decides the compose fragment chain from this, so profile
        # key and env var can never disagree about whether auth is armed.
        # Emitted unconditionally -- a line that vanishes when its own value is
        # false would make the included file re-settle on every make pass.
        lines.append(
            f"export DFE_AUTH_RESOLVED := {'true' if footprint['auth'] else 'false'}"
        )

        # Same contract for the collector: container stdout is shipped to it, so
        # the log-driver fragment only makes sense where it is running.
        lines.append(
            f"export DFE_OTEL_RESOLVED := {'true' if footprint['otel'] else 'false'}"
        )

        # Point the services at the bundled collector, unless .env already names
        # an endpoint -- an external OTLP backend is the other supported shape.
        #
        # Emitted UNCONDITIONALLY, taking the env value when there is one. Make
        # re-execs after rebuilding an included makefile, so a line that appears
        # only when its own variable is unset removes itself on the next pass and
        # the file never settles -- `make ci` then restarts forever.
        if footprint["otel"]:
            endpoint = (
                os.environ.get(OTEL_ENDPOINT_ENV_VAR, "").strip()
                or OTEL_BUNDLED_ENDPOINT
            )
            lines.append(f"export {OTEL_ENDPOINT_ENV_VAR} := {endpoint}")
            backend = (
                os.environ.get(OTEL_ENGINE_BACKEND_ENV_VAR, "").strip()
                or OTEL_ENGINE_BACKEND
            )
            lines.append(f"export {OTEL_ENGINE_BACKEND_ENV_VAR} := {backend}")

        for service_name, service_config in services.items():
            var_name = SERVICE_TO_CONFIG_VAR[service_name]
            lines.append(f"export {var_name} := {service_config['config_path']}")

        new_content = "\n".join(lines) + "\n"
        if (
            not (PROFILE_MK.exists())
            or PROFILE_MK.read_text(encoding="utf-8") != new_content
        ):
            PROFILE_MK.write_text(new_content, encoding="utf-8")
    except _ProfileError as error:
        PROFILE_MK.unlink(missing_ok=True)
        _print(header=error.header, msg=error.msg)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
