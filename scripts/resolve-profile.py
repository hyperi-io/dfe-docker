#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resolve-profile.py
#  Purpose:      Read service_profiles.yaml and output Make-consumable profile variables
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve DFE service profile from service_profiles.yaml.

Reads service_profiles.yaml, resolves the active profile (overridable via DFE_PROFILE env var), validates config paths exist and outputs Make-consumable export lines to stdout. Errors go to stderr.
If KAFBAT_ENABLED, adds the `ui` profile to the PROFILE_FLAGS.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_PROFILES_FILE = REPO_ROOT / "service_profiles.yaml"
CONFIG_DIR = REPO_ROOT / "config"
PROFILE_MK = REPO_ROOT / ".profile.mk"
DOTENV_FILE = REPO_ROOT / ".env"

FALSY = {"", "0", "false", "no", "off"}

KNOWN_SERVICES = {
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader", 
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vrl"
}

SERVICE_TO_CONFIG_VAR = {
    "dfe-archiver": "DFE_ARCHIVER_CONFIG",
    "dfe-fetcher": "DFE_FETCHER_CONFIG",
    "dfe-loader": "DFE_LOADER_CONFIG",
    "dfe-receiver": "DFE_RECEIVER_CONFIG",
    "dfe-transform-vector": "DFE_TRANSFORM_VECTOR_CONFIG",
    "dfe-transform-vrl": "DFE_TRANSFORM_VRL_CONFIG"
}

UNSUPPORTED_PATTERNS = [
    ("- ", "lists (- item)"),
    ("| ", "multi-line strings (|)"),
    ("> ", "multi-line strings (>)"),
    ("{", "flow syntax ({)"),
    ("[", "flow syntax ([)"),
    ("&", "anchors (&)"),
    ("*", "aliases (*)"),
    ("!!", "tags (!!)"),
]


def die(msg):
    print(f"resolve-profile: {msg}", file=sys.stderr)
    # Remove stale .profile.mk so Make fails on include
    PROFILE_MK.unlink(missing_ok=True)
    sys.exit(1)


def load_dotenv():
    """Merge .env into os.environ. Existing env vars take precedence."""
    if not DOTENV_FILE.is_file():
        return
    for raw in DOTENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_truthy(name, default):
    """Return True unless the env var is explicitly set to a falsy value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSY


def parse_yaml(text):
    """Parse a minimal YAML subset into nested dicts with string values."""
    root = {}
    stack = [(root, -1)]

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        # Strip trailing comments preceded by space (" #").
        # Bare "#" inside a value (e.g. "secret#123") is preserved.
        comment_pos = raw_line.find(" #")
        if comment_pos >= 0:
            line = raw_line[:comment_pos].rstrip()
        elif raw_line.lstrip().startswith("#"):
            continue
        else:
            line = raw_line.rstrip()

        if not line or line.isspace():
            continue

        stripped = line.lstrip()

        # Check for unsupported syntax
        for pattern, desc in UNSUPPORTED_PATTERNS:
            if stripped.startswith(pattern):
                die(f"line {line_num}: unsupported syntax: {desc}")

        # Check for quoted strings
        if ": " in stripped:
            val_part = stripped.split(": ", 1)[1]
            if val_part and val_part[0] in ('"', "'"):
                die(f"line {line_num}: unsupported syntax: quoted strings")

        indent = len(line) - len(line.lstrip())

        # Pop stack to find parent at correct indent level
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()

        parent = stack[-1][0]

        if ": " in stripped:
            # key: value
            key, value = stripped.split(": ", 1)
            parent[key] = value
        elif stripped.endswith(":"):
            # key: (start of nested map)
            key = stripped[:-1]
            child = {}
            parent[key] = child
            stack.append((child, indent))
        else:
            die(f"line {line_num}: cannot parse: {stripped}")

    return root


def resolve_profile(data):
    """Resolve active profile and return (transport, services_dict)."""
    active = os.environ.get("DFE_PROFILE", "") or data.get("active_profile", "")
    if not active:
        die("no active profile: set active_profile in service_profiles.yaml or DFE_PROFILE env var")

    profiles = data.get("profiles")
    if not profiles or not isinstance(profiles, dict):
        die("service_profiles.yaml missing 'profiles' section")

    if active not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        die(f"profile '{active}' not found. Available: {available}")

    profile = profiles[active]
    transport = profile.get("transport", "")
    if transport not in ("kafka", "grpc"):
        die(f"profile '{active}': transport must be 'kafka' or 'grpc', got '{transport}'")

    services = profile.get("services", {})
    if not services:
        die(f"profile '{active}': no services defined")

    for svc_name, svc_conf in services.items():
        if svc_name not in KNOWN_SERVICES:
            die(f"profile '{active}': unknown service '{svc_name}'")
        if not isinstance(svc_conf, dict) or "config_path" not in svc_conf:
            die(f"profile '{active}': service '{svc_name}' missing config_path")
        config_file = CONFIG_DIR / svc_conf["config_path"]
        if not config_file.is_file():
            die(f"profile '{active}': config file not found: config/{svc_conf['config_path']}")

    return transport, services


def main():
    if not SERVICE_PROFILES_FILE.is_file():
        die(f"service_profiles.yaml not found at {SERVICE_PROFILES_FILE}")

    load_dotenv()
    data = parse_yaml(SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace"))
    transport, services = resolve_profile(data)

    # Build infra compose profile flags (DFE services have no profile and are started by name)
    profiles = ["clickhouse"]
    if transport == "kafka":
        profiles.append("kafka")
        if env_truthy("KAFBAT_ENABLED", default=True):
            profiles.append("ui")

    # Write makefile fragment ($(shell) collapses newlines, so we write a file)
    lines = []
    profile_flags = " ".join(f"--profile {p}" for p in profiles)
    lines.append(f"export PROFILE_FLAGS := {profile_flags}")
    lines.append(f"export DFE_SERVICES := {' '.join(sorted(services.keys()))}")

    for svc_name, svc_conf in services.items():
        var_name = SERVICE_TO_CONFIG_VAR[svc_name]
        lines.append(f"export {var_name} := {svc_conf['config_path']}")

    PROFILE_MK.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
