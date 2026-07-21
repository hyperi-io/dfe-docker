#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/show_limits.py
#  Purpose:      Report resource limits and totals from the resolved compose config
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Report the stack's resource limits, computed rather than written down.

This exists because the totals used to live in a comment and in three documents,
and they were wrong in all four the moment a service changed tier. A number that
has to be maintained by hand is a number that will be stale, and a stale number in
a document is worse than no number, because people believe it.

So: ask Compose. It reports per-service limits for the profile set you name, plus
a total, and the two Kafka backends are called out because they are mutually
exclusive -- adding all services together double-counts them.

    make limits                       # the stack the ACTIVE profile actually starts
    make limits PROFILES="clickhouse dfe core kafka-redpanda"   # a what-if
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from _common import (
    COMPOSE_FILE,
    REPO_ROOT,
    _load_dotenv,
    _print,
    _required_compose_vars,
    _resolved_profiles,
    _resolved_services,
)

# Enough to resolve interpolation; we are reading limits, not pulling images.
_PLACEHOLDER = "0.0.0-limits"

_MUTUALLY_EXCLUSIVE = ("kafka-redpanda", "kafka-apache")


def _env() -> dict[str, str]:
    """Return an environment with every mandatory compose key satisfied."""
    env = dict(os.environ)
    for name in _required_compose_vars():
        if not (env.get(name, "").strip()):
            env[name] = _PLACEHOLDER
    return env


def _resolved(profiles: list[str]) -> dict:
    """Return the resolved compose config as a dict for the given profiles."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE.name]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=_env(),
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        _print(msg=f"compose config failed:\n{result.stderr.strip()}")
        raise SystemExit(1)
    return json.loads(result.stdout)


def _limits(service: dict) -> tuple[int, str]:
    """Return (memory bytes, cpus) for a service, or (0, '-') if unlimited."""
    limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
    memory = limits.get("memory")
    cpus = limits.get("cpus", "-")
    try:
        return int(memory), str(cpus)
    except (TypeError, ValueError):
        return 0, str(cpus)


def _active_view() -> tuple[list[str], set[str] | None, str]:
    """Return (profiles to resolve, service allow-list or None, a label).

    Default is the RESOLVED stack, because that is the number an operator sizes a
    box with. Showing every profile by default over-reports by roughly double --
    it counts both Kafka backends, HyperDX, and every DFE service including ones
    the active profile does not run. That is the same class of wrong number this
    command exists to eliminate, just arrived at automatically instead of by hand.

    `PROFILE_FLAGS` selects only the infrastructure services; the DFE ones are
    named on the `up` command line, so `dfe` and `core` are added here and then
    filtered down to `DFE_SERVICES`.
    """
    override = os.environ.get("PROFILES", "").split()
    if override:
        return override, None, f"PROFILES={' '.join(override)}"

    profiles = _resolved_profiles()
    services = _resolved_services()
    if not (profiles or services):
        # No .profile.mk yet (fresh checkout, before any make target resolved it).
        return (
            ["clickhouse", "dfe", "core", "hyperdx", "kafka-ui", *_MUTUALLY_EXCLUSIVE],
            None,
            "EVERY profile -- no .profile.mk yet, so this is a ceiling, not your stack",
        )

    return [*profiles, "dfe", "core"], set(services), "the resolved profile"


def _with_dependencies(*, allowed: set[str], services: dict) -> set[str]:
    """Grow `allowed` to include everything those services pull in via depends_on.

    `docker compose up dfe-loader` also starts what dfe-loader depends on, so a
    service can consume its memory limit without ever appearing in DFE_SERVICES.
    `dlq-init` is the live example: it belongs to the `dfe` profile, is named by no
    service profile, and runs on every one of them. Leaving it out under-reports,
    which is the same defect as over-reporting, just quieter.

    Transitive, because a dependency can have dependencies of its own.
    """
    grown = set(allowed)
    while True:
        added = {
            dep
            for name in grown
            if name in services
            for dep in services[name].get("depends_on", {})
            if dep not in grown
        }
        if not (added):
            return grown
        grown |= added


def main() -> int:
    _load_dotenv()

    profiles, allowed, label = _active_view()
    services = _resolved(profiles).get("services", {})
    if allowed is not None:
        allowed = _with_dependencies(allowed=allowed, services=services)
        # Keep every infra service its profiles brought in; keep only the DFE and
        # core services the active profile actually starts.
        services = {
            name: service
            for name, service in services.items()
            if not (set(service.get("profiles", [])) & {"dfe", "core"})
            or name in allowed
        }
    if not (services):
        _print(msg="no services resolved -- check the profile names")
        return 1

    _print(msg=f"showing {label}")

    total = 0
    exclusive = {}
    unlimited = []

    print(f"{'SERVICE':24} {'MEMORY':>9}  CPUS")
    for name, service in sorted(services.items()):
        memory, cpus = _limits(service)
        if not (memory):
            unlimited.append(name)
        print(f"{name:24} {memory / 2**30:8.2f}G  {cpus}")
        total += memory
        for backend in _MUTUALLY_EXCLUSIVE:
            # Match on the backend TOKEN, not the profile name. `startswith` looks
            # right and silently misses `kafka-init-redpanda`, which does not begin
            # with `kafka-redpanda` -- so a per-backend init service was counted in
            # the total but never subtracted, inflating the ceiling by its 512M.
            if name.endswith(backend.removeprefix("kafka-")):
                exclusive[backend] = exclusive.get(backend, 0) + memory

    print()
    print(f"{'services':24} {len(services)}")
    print(f"{'sum of all limits':24} {total / 2**30:8.2f}G")

    # The Kafka backends cannot both run, so the honest ceiling drops the larger.
    if len(exclusive) > 1:
        heaviest = max(exclusive.values())
        print(
            f"{'realistic ceiling':24} {(total - heaviest) / 2**30:8.2f}G  "
            f"(one Kafka backend; they are mutually exclusive)"
        )

    if unlimited:
        print()
        _print(msg=f"UNLIMITED (no memory limit): {', '.join(unlimited)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
