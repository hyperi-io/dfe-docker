#  Project:      dfe-docker
#  File:         scripts/usage_cores.py
#  Purpose:      Derive the CPU limit of each service the stack runs, for the usage rollup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The CPU limit of every service the resolved stack runs, for the cores-deployed metric.

dfe-engine's usage rollup (dfe-schemas ``views/usage_source_*.sql``) counts a
deployment's cores from three readings: ``k8s.container.cpu_request`` per
container, a Running ``k8s.pod.phase`` reading at the same instant, and each
node's ``system.cpu.logical.count``, joined on ``k8s.node.name``. On Compose the
first two come from the collector's ``sql_query/usage`` receiver, which reports
the limits this module derives, and the third from its ``host_metrics`` receiver
(config/otel-collector/config.yaml).

Compose carries CPU limits where Kubernetes carries requests, so a service counts
its ceiling. A one-shot (``restart: "no"``) exits once its job is done and is not
counted, as a completed init container is not. A service with no limit may use
every core on the docker host, so it counts that many.

The limits are read from what ``docker compose up`` would start for the resolved
services, so a dependency pulled in by ``depends_on`` counts too. Only
docker-compose.yml and the generated instances fragment set limits, and
scripts/tests/test_usage_cores.py holds the other compose files to that.
"""

import json
import os
import re
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from _common import COMPOSE_FILE, REPO_ROOT, _print, _required_compose_vars

# The variables .tmp/profile.mk exports and the collector reads, by the names docker-compose.yml hands it.
USAGE_LIMITS_VAR = "DFE_USAGE_CPU_LIMITS"
USAGE_NODE_VAR = "DFE_USAGE_NODE_NAME"

# The node name docker-compose.yml falls back to, used here when the docker host names itself outside _NODE_NAME.
DEFAULT_NODE = "docker-host"

# The restart policy of a service that exits once its job is done.
ONE_SHOT_RESTART = "no"

# Enough to resolve interpolation: the limits do not depend on which image runs.
_PLACEHOLDER = "0.0.0-usage"

# Compose's own service-name charset, which also keeps the JSON exported here inert inside the collector's SQL string literal.
_SERVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
# A host name, the only shape the node name may take where the collector writes it.
_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


class UsageError(Exception):
    """Raised when the compose model the limits are read from cannot be resolved."""


def cpu_limits(*, model: Mapping, host_cores: float) -> dict[str, float]:
    """Return {service: cores} for every long-running service in a resolved compose model.

    Args:
        model: ``docker compose config --format json`` output, parsed.
        host_cores: What a service with no CPU limit counts: the docker host's logical CPUs.

    Returns:
        Each service that is not a one-shot, with its CPU limit.

    Raises:
        ValueError: A service is named outside the compose charset, or its limit is not a number.
    """
    limits: dict[str, float] = {}
    for name, service in sorted(model.get("services", {}).items()):
        if service.get("restart") == ONE_SHOT_RESTART:
            continue
        if not (_SERVICE_NAME.fullmatch(name)):
            raise ValueError(f"service {name!r} is named outside the compose charset")
        resources = service.get("deploy", {}).get("resources", {})
        cpus = float(resources.get("limits", {}).get("cpus") or 0)
        # Docker reads 0 as no limit at all.
        limits[name] = cpus if cpus > 0 else host_cores
    return limits


def exports(*, limits: Mapping[str, float], node: str) -> list[str]:
    """Return the .tmp/profile.mk lines that hand the limits and the node name to compose.

    Sorted and compact, so the included file settles: make re-reads an include it remade.
    """
    encoded = json.dumps(dict(limits), separators=(",", ":"), sort_keys=True)
    return [
        f"export {USAGE_LIMITS_VAR} := {encoded}",
        f"export {USAGE_NODE_VAR} := {node}",
    ]


def compose_env() -> dict[str, str]:
    """Return the environment with every mandatory compose key set, a placeholder filling the unset ones."""
    env = dict(os.environ)
    for name in _required_compose_vars():
        if not (env.get(name, "").strip()):
            env[name] = _PLACEHOLDER
    return env


def compose_model(
    *, files: Sequence[Path], profiles: Sequence[str], services: Sequence[str]
) -> dict:
    """Return the resolved compose model of what ``up`` starts for the named services.

    Naming them is what brings in a service whose profile is off but that the
    resolved profile starts by name, HyperDX's among them, and every dependency.

    Raises:
        UsageError: Compose refused the files or returned no model.
    """
    cmd = ["docker", "compose"]
    for path in files:
        cmd += ["-f", str(path)]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json", *services]
    result = subprocess.run(
        cmd,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=compose_env(),
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        raise UsageError(f"docker compose config failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise UsageError(f"docker compose config returned no model: {error}") from error


def docker_host() -> tuple[str, float]:
    """Return the docker host's name and logical CPU count, else this machine's.

    Asked of the daemon, because with a remote DOCKER_HOST the containers and the
    CPUs host_metrics reads are on that machine, not the one running make.
    """
    result = subprocess.run(
        ["docker", "info", "--format", "{{.NCPU}} {{.Name}}"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    cpus, _, name = result.stdout.strip().partition(" ")
    if result.returncode == 0 and cpus.isdigit() and int(cpus) > 0:
        return (name if _NODE_NAME.fullmatch(name) else DEFAULT_NODE), float(cpus)
    local = socket.gethostname()
    return (local if _NODE_NAME.fullmatch(local) else DEFAULT_NODE), float(
        os.cpu_count() or 1
    )


def profile_lines(
    *, profiles: Sequence[str], services: Sequence[str], fragment: Path | None
) -> list[str]:
    """Return the usage exports for the resolved stack, empty limits where they cannot be read.

    A stack whose limits cannot be read still starts: compose reports the same
    fault on its own ``up``, and cores deployed then reads as not measured.

    Args:
        profiles: The compose profiles the resolved profile turns on.
        services: The services ``make`` names on its ``up``.
        fragment: The generated instances fragment, when the stack runs instances.
    """
    node, host_cores = docker_host()
    files = [COMPOSE_FILE] if fragment is None else [COMPOSE_FILE, fragment]
    try:
        model = compose_model(files=files, profiles=profiles, services=services)
        limits = cpu_limits(model=model, host_cores=host_cores)
    except (UsageError, ValueError) as error:
        _print(msg=f"WARNING: cores deployed reads nothing on this stack: {error}")
        limits = {}
    return exports(limits=limits, node=node)
