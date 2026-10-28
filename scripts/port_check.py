#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/port_check.py
#  Purpose:      Refuse a start when a host port the stack publishes is already taken, naming the variables to move
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Gate `make dev` / `make ci` on the host ports the stack publishes.

A second stack on one host (a different COMPOSE_PROJECT_NAME and DFE_CONTAINER_PREFIX)
publishes the same defaults as the first, 8123 and 9000 for ClickHouse among them, and
compose fails halfway through `up` with "port is already allocated". This resolves the
compose config the start is about to use, tries to bind every published host port, and
exits 1 before anything starts, naming each port taken and the variable that sets it.

It runs after `make down`, so a port held by this stack's own containers is already free.
Skipped when DOCKER_HOST points at a remote daemon, because a bind here says nothing about
a port on that host.

Usage: port_check.py -- <docker compose flags the start uses, e.g. -f docker-compose.yml>
Exit codes: 0 every port is free (or the check cannot apply), 1 a port is taken.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import _dotenv_values, _print  # noqa: E402

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"

# A published port line: `- ${DFE_BIND_HOST:-127.0.0.1}:${CLICKHOUSE_HTTP_PORT:-8123}:8123`
_PORT_VAR_RE = re.compile(r"\$\{(\w+):-(\d+)\}:\d+\s*$")


def published_ports(*, config: dict) -> list[tuple[str, str, int, str]]:
    """Return (service, host_ip, port, protocol) for every port the resolved config publishes."""
    found: list[tuple[str, str, int, str]] = []
    for name, service in (config.get("services") or {}).items():
        for entry in service.get("ports") or []:
            if not isinstance(entry, dict) or not entry.get("published"):
                continue
            found.append(
                (
                    name,
                    entry.get("host_ip") or "0.0.0.0",
                    int(entry["published"]),
                    entry.get("protocol") or "tcp",
                )
            )
    return found


def is_taken(*, host_ip: str, port: int, protocol: str) -> bool:
    """True when binding host_ip:port fails with EADDRINUSE."""
    kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    family = socket.AF_INET6 if ":" in host_ip else socket.AF_INET
    with socket.socket(family, kind) as probe:
        # A port the last stack left in TIME_WAIT is free to docker, so only a live listener counts.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host_ip, port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def variables_for(*, compose_text: str, env: dict[str, str]) -> dict[int, str]:
    """Map each published port to the variable that sets it, from the compose file's own port lines."""
    mapping: dict[int, str] = {}
    for line in compose_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        hit = _PORT_VAR_RE.search(stripped)
        if not hit:
            continue
        var, default = hit.groups()
        value = env.get(var) or default
        if value.isdigit():
            mapping[int(value)] = var
    return mapping


def conflicts(
    *, ports: list[tuple[str, str, int, str]], names: dict[int, str]
) -> list[str]:
    """One line per published port something else already holds."""
    lines: list[str] = []
    for service, host_ip, port, protocol in ports:
        if not is_taken(host_ip=host_ip, port=port, protocol=protocol):
            continue
        var = names.get(port)
        lines.append(
            f"{host_ip}:{port}/{protocol} ({service}) is in use"
            + (f" -- set {var} to a free port" if var else "")
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host and not docker_host.startswith("unix://"):
        return 0
    result = subprocess.run(
        ["docker", "compose", *args, "config", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        _print(
            msg=f"WARNING: port check skipped, compose config failed: {result.stderr.strip()[:300]}"
        )
        return 0
    ports = published_ports(config=json.loads(result.stdout))
    env = {**_dotenv_values(), **os.environ}
    taken = conflicts(
        ports=ports,
        names=variables_for(
            compose_text=COMPOSE_FILE.read_text(encoding="utf-8", errors="replace"),
            env=env,
        ),
    )
    if not taken:
        return 0
    _print(
        msg="error: another process or stack on this host holds host ports this stack publishes. "
        "A second stack needs its own value for each variable below (in .env), alongside its "
        "own COMPOSE_PROJECT_NAME and DFE_CONTAINER_PREFIX:"
    )
    for line in taken:
        _print(msg=f"  {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
