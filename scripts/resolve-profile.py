#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resolve-profile.py
#  Purpose:      Read services.yaml and output Make-consumable profile variables
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve DFE service profile from services.yaml.

Reads services.yaml, resolves the active profile (overridable via DFE_PROFILE
env var), validates config paths exist, and outputs Make-consumable export
lines to stdout. Errors go to stderr.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_FILE = REPO_ROOT / "services.yaml"
CONFIG_DIR = REPO_ROOT / "config"
PROFILE_MK = REPO_ROOT / ".profile.mk"

KNOWN_SERVICES = {"dfe-archiver", "dfe-fetcher", "dfe-loader", "dfe-receiver"}

SERVICE_TO_PROFILE = {
    "dfe-archiver": "archiver",
    "dfe-fetcher": "fetcher",
    "dfe-loader": "loader",
    "dfe-receiver": "receiver",
}

SERVICE_TO_CONFIG_VAR = {
    "dfe-archiver": "DFE_ARCHIVER_CONFIG",
    "dfe-fetcher": "DFE_FETCHER_CONFIG",
    "dfe-loader": "DFE_LOADER_CONFIG",
    "dfe-receiver": "DFE_RECEIVER_CONFIG",
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
        die("no active profile: set active_profile in services.yaml or DFE_PROFILE env var")

    profiles = data.get("profiles")
    if not profiles or not isinstance(profiles, dict):
        die("services.yaml missing 'profiles' section")

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
    if not SERVICES_FILE.is_file():
        die(f"services.yaml not found at {SERVICES_FILE}")

    data = parse_yaml(SERVICES_FILE.read_text())
    transport, services = resolve_profile(data)

    # Build compose profile flags
    profiles = ["clickhouse"]
    if transport == "kafka":
        profiles.extend(["kafka", "ui"])

    for svc_name in services:
        profiles.append(SERVICE_TO_PROFILE[svc_name])

    # Write makefile fragment ($(shell) collapses newlines, so we write a file)
    lines = []
    profile_flags = " ".join(f"--profile {p}" for p in profiles)
    lines.append(f"export PROFILE_FLAGS := {profile_flags}")

    for svc_name, svc_conf in services.items():
        var_name = SERVICE_TO_CONFIG_VAR[svc_name]
        lines.append(f"export {var_name} := {svc_conf['config_path']}")

    PROFILE_MK.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
