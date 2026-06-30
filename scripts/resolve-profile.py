#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resolve-profile.py
#  Purpose:      Read service_profiles.yaml and output Make-consumable profile variables
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve DFE service profile from service_profiles.yaml.

Reads service_profiles.yaml, resolves the active profile (overridable via DFE_PROFILE env var), validates config paths exist and writes to the .profile.mk file.
If KAFBAT_ENABLED, adds the `kafka-ui` profile to the PROFILE_FLAGS.
For kafka transport, KAFKA_BACKEND selects the backend (defaults to redpanda).
"""

from __future__ import annotations

import os
from pathlib import Path

from _common import (
    CONFIG_DIR,
    DOTENV_FILE,
    FALSY,
    PROFILE_MK,
    SERVICE_PROFILES_FILE,
    _print,
    _rel_path,
)

CORE_ENABLED_ENV_VAR = "DFE_CORE_ENABLED"
CORE_SERVICES = ["dfe-ui"]

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
    "dfe-transform-vrl": "DFE_TRANSFORM_VRL_CONFIG",
}
SERVICES = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vrl",
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


def _load_dotenv() -> None:
    """Merge .env into os.environ. Existing env vars take precedence."""
    if not (DOTENV_FILE.is_file()):
        return
    for raw in DOTENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not (line) or (line.startswith("#")) or ("=" not in line):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] not in ("'", '"'):
            value = value.split(" #", 1)[0].strip()
        value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)


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


def _resolve_profile(*, data: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Resolve active profile and return (transport, services_dict)."""
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

    return transport, services


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
        transport, services = _resolve_profile(data=data)

        # Build infra compose profile flags
        profiles = ["clickhouse"]
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
            if _env_truthy(default=True, name="KAFBAT_ENABLED"):
                profiles.append("kafka-ui")
                kafka_ui_enabled = True

        # Write to .profile.mk ($(shell) collapses newlines)
        lines = []
        profile_flags = " ".join(f"--profile {profile}" for profile in profiles)
        lines.append(f"export PROFILE_FLAGS := {profile_flags}")
        service_list = sorted(services.keys())
        if kafka_ui_enabled:
            service_list.append("kafka-ui")
        if _env_truthy(default=True, name=CORE_ENABLED_ENV_VAR):
            service_list.extend(CORE_SERVICES)
        lines.append(f"export DFE_SERVICES := {' '.join(service_list)}")

        for service_name, service_config in services.items():
            var_name = SERVICE_TO_CONFIG_VAR[service_name]
            lines.append(f"export {var_name} := {service_config['config_path']}")

        PROFILE_MK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except _ProfileError as error:
        PROFILE_MK.unlink(missing_ok=True)
        _print(header=error.header, msg=error.msg)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
