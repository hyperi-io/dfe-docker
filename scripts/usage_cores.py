#  Project:      dfe-docker
#  File:         scripts/usage_cores.py
#  Purpose:      Derive the CPU limit of each service the stack runs, for the usage rollup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The CPU limit of every service the resolved stack runs, for the cores-deployed metric.

dfe-engine's usage rollup (dfe-schemas ``views/usage_source_*.sql``) counts a deployment's cores from three readings: ``k8s.container.cpu_request`` per container, a Running ``k8s.pod.phase`` reading at the same instant and each node's ``system.cpu.logical.count``, joined on ``k8s.node.name``. On Compose the collector's ``sql_query/usage`` receiver reports the first two from the limits this module derives and its ``host_metrics`` receiver reports the third (config/otel-collector/config.yaml).

Compose carries CPU limits where Kubernetes carries requests, so a service counts its ceiling. A one-shot (``restart: "no"``) exits once its job is done and is not counted, as a completed init container is not. A service with no limit may use every core on the docker host, so it counts that many.

The limits are read from what ``docker compose up`` would start for the resolved services, so a dependency pulled in by ``depends_on`` counts too. Only docker-compose.yml and the generated instances fragment set limits. scripts/tests/test_usage_cores.py holds the other compose files to that.
"""

import json
import os
import re
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from _common import COMPOSE_FILE, ComposeConfigError, _compose_model, _print

# The variables .tmp/profile.mk exports and the collector reads, by the names docker-compose.yml hands it
USAGE_LIMITS_VAR = "DFE_USAGE_CPU_LIMITS"
USAGE_NODE_VAR = "DFE_USAGE_NODE_NAME"

# The node name docker-compose.yml falls back to, used here when the docker host names itself outside _NODE_NAME
DEFAULT_NODE = "docker-host"

# The restart policy of a service that exits once its job is done
ONE_SHOT_RESTART = "no"

# Compose's own service-name charset, which also keeps the JSON exported here inert inside the collector's SQL string literal
_SERVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
# A host name, the only shape the node name may take where the collector writes it
_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


def _cpu_limits(*, host_cores: float, model: Mapping) -> dict[str, float]:
    """Return {service: cores} for every long-running service in a resolved compose model."""
    limits = {}
    for name, service in sorted(model.get("services", {}).items()):
        if service.get("restart") == ONE_SHOT_RESTART:
            continue
        if not (_SERVICE_NAME.fullmatch(name)):
            raise ValueError(f"service {name!r} is named outside the compose charset")
        resources = service.get("deploy", {}).get("resources", {})
        cpus = float((resources.get("limits", {}).get("cpus")) or (0))
        # Docker reads 0 as no limit at all
        limits[name] = cpus if cpus > 0 else host_cores
    return limits


def _docker_host() -> tuple[str, float]:
    """Return the docker host's name and logical CPU count, else this machine's.

    Asked of the daemon, because with a remote DOCKER_HOST the containers and the CPUs host_metrics reads are on that machine, not the one running make.
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
        (os.cpu_count()) or (1)
    )


def _exports(*, limits: Mapping[str, float], node: str) -> list[str]:
    """Return the .tmp/profile.mk lines that hand the limits and the node name to compose.

    Sorted and compact, so the included file settles: make re-reads an include it remade.
    """
    encoded = json.dumps(dict(limits), separators=(",", ":"), sort_keys=True)
    return [
        f"export {USAGE_LIMITS_VAR} := {encoded}",
        f"export {USAGE_NODE_VAR} := {node}",
    ]


def profile_lines(
    *, fragment: Path | None, profiles: Sequence[str], services: Sequence[str]
) -> list[str]:
    """Return the usage exports for the resolved stack, empty limits where they cannot be read."""
    node, host_cores = _docker_host()
    files = [COMPOSE_FILE] if fragment is None else [COMPOSE_FILE, fragment]
    try:
        # Naming the services brings in their dependencies and any whose compose profile is off, HyperDX's among them
        model = _compose_model(files=files, profiles=profiles, services=services)
        limits = _cpu_limits(host_cores=host_cores, model=model)
    except (ComposeConfigError, ValueError) as error:
        # The stack still starts: compose reports the same fault on its own up and cores deployed reads as not measured
        _print(msg=f"WARNING: cores deployed reads nothing on this stack: {error}")
        limits = {}
    return _exports(limits=limits, node=node)
