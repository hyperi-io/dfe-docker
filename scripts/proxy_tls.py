#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/proxy_tls.py
#  Purpose:      Derive console TLS's network and refuse a start its certificate or network cannot carry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console TLS checks the Makefile runs while it parses.

`network SUBNET` prints dfe-proxy's reserved address and the range Docker allocates from. Under DFE_PROXY_TLS=true the engine believes X-Forwarded-Proto from dfe-proxy's address alone, so no other container may take that address: the proxy holds the subnet's last host and Docker allocates everything else, the gateway included, from the first half.

`precheck` prints the first reason a start goal would fail and nothing when it would not. Make turns that line into its `$(error)`, so each reason names what to change.

Standalone and stdlib only, because the make guard tests copy it next to a lone Makefile.
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_CERT = "console.crt"
_KEY = "console.key"
# Private (RFC 1918) space only, so the pinned network can never shadow a routable address.
_PRIVATE = tuple(
    ipaddress.IPv4Network(block)
    for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
# The first half of a /24 holds 126 containers, well past every profile and its per-source instances together. Wider than a /16 only takes address space from other networks.
_MAX_PREFIX = 24
_MIN_PREFIX = 16
# Compose lowercases a project name, drops anything outside these and trims leading `_` and `-`; its default network is `<project>_default`.
_PROJECT_DROP = re.compile(r"[^a-z0-9_-]")


def _docker_networks() -> list[dict]:
    """Return `docker network inspect` for every network; an empty list when docker cannot answer.

    With no daemon to ask there is nothing to clash with and the start that follows fails on its own.
    """
    try:
        listed = _run(args=["docker", "network", "ls", "-q"])
    except OSError:
        return []
    ids = listed.stdout.split()
    if (listed.returncode != 0) or (not (ids)):
        return []
    inspected = _run(args=["docker", "network", "inspect", *ids])
    if inspected.returncode != 0:
        return []
    try:
        return json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return []


def _first_line(*, text: str) -> str:
    """Return the first non-blank line of a tool's output; a placeholder when it printed nothing."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no detail printed"


def _ipv4_pools(*, network: dict) -> list[tuple[ipaddress.IPv4Network, str]]:
    """Return (subnet, ip_range) for each IPv4 pool a docker network declares, the range empty where it has none."""
    pools = []
    ipam = (network.get("IPAM")) or ({})
    for pool in (ipam.get("Config")) or ([]):
        try:
            subnet = ipaddress.ip_network(str((pool.get("Subnet")) or ("")))
        except ValueError:
            continue
        if isinstance(subnet, ipaddress.IPv4Network):
            pools.append((subnet, str((pool.get("IPRange")) or (""))))
    return pools


def _run(*, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one command without a terminal, capturing its output as text."""
    return subprocess.run(
        args,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )


def cert_problem(*, cert_dir: str) -> str:
    """Return why Envoy could not load the certificate pair in cert_dir; the empty string when it can."""
    setting = f"DFE_PROXY_CERT_DIR={cert_dir!r}"
    if not (cert_dir.startswith(("/", "./", "../"))):
        return f"{setting} must be a path starting with /, ./ or ../ because compose reads a bare name as a named volume"
    if not (Path(cert_dir).is_dir()):
        return f"{setting} is not a directory. It must hold {_CERT} and {_KEY}"
    paths = {}
    for name, kind in ((_CERT, "certificate"), (_KEY, "private key")):
        shown = f"{cert_dir.rstrip('/')}/{name}"
        path = Path(cert_dir) / name
        paths[name] = path
        if path.is_symlink():
            return f"{shown} is a symlink, which dangles inside the container (certbot's live/ directory is all symlinks). Copy the file itself with `cp -L`"
        if not (path.is_file()):
            return f"DFE_PROXY_TLS=true needs the {kind} {shown} and it is missing. Generate the pair as docs/configuration.md (Console TLS) shows. Set DFE_PROXY_TLS=false to start without TLS"
        if not (os.access(path, os.R_OK)):
            return f"{shown} exists but this user cannot read it. Under TLS both proxies run as the user who runs make, so the {kind} must be readable by that user. Keep the key 0600 and chown it rather than widening the mode"
    if shutil.which("openssl") is None:
        return "openssl is not on PATH, so make cannot check the certificate and key before Envoy loads them. Install openssl"
    certificate = _run(
        args=["openssl", "x509", "-noout", "-pubkey", "-in", str(paths[_CERT])]
    )
    if certificate.returncode != 0:
        return f"{cert_dir.rstrip('/')}/{_CERT} is not a PEM certificate openssl can read: {_first_line(text=certificate.stderr)}"
    key = _run(
        args=[
            "openssl",
            "pkey",
            "-passin",
            "pass:",
            "-pubout",
            "-in",
            str(paths[_KEY]),
        ]
    )
    if key.returncode != 0:
        return f"{cert_dir.rstrip('/')}/{_KEY} is not an unencrypted PEM private key openssl can read: {_first_line(text=key.stderr)}"
    if certificate.stdout != key.stdout:
        return f"{cert_dir.rstrip('/')}/{_KEY} does not belong to {cert_dir.rstrip('/')}/{_CERT}: their public keys differ, so every TLS handshake would fail"
    return ""


def derive(*, subnet: str) -> tuple[str, str]:
    """Return dfe-proxy's address and the dynamic allocation range for a private IPv4 network in CIDR form.

    Raises ValueError for anything but the network address of a private /16 to /24.
    """
    network = ipaddress.IPv4Network(subnet)
    if not (any(network.subnet_of(block) for block in _PRIVATE)):
        raise ValueError(
            f"{network} is outside private address space (10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16)"
        )
    if not (_MIN_PREFIX <= network.prefixlen <= _MAX_PREFIX):
        raise ValueError(
            f"a /{network.prefixlen} is outside /{_MIN_PREFIX} to /{_MAX_PREFIX}"
        )
    allocation = next(network.subnets(prefixlen_diff=1))
    return str(network.broadcast_address - 1), str(allocation)


def network_problem(
    *,
    networks: list[dict],
    overlap: bool,
    project: str,
    subnet: str,
    tls: bool,
    toggle: bool,
) -> str:
    """Return why compose could not give this stack the network DFE_PROXY_TLS asks for; the empty string when it can.

    `toggle` checks the stack's own default network still has the address plan the dial wants, because Docker cannot re-address a live network. `overlap` checks no other network holds DFE_NETWORK_SUBNET.
    """
    own = f"{_PROJECT_DROP.sub('', project.lower()).lstrip('_-')}_default"
    wanted = None
    if tls:
        try:
            _, allocation = derive(subnet=subnet)
        except ValueError as error:
            return f"DFE_NETWORK_SUBNET={subnet!r} cannot hold the TLS network: {error}. Use a free private /24 such as 10.207.0.0/24"
        wanted = (ipaddress.IPv4Network(subnet), allocation)
    for network in networks:
        name = str((network.get("Name")) or (""))
        pools = _ipv4_pools(network=network)
        if name == own:
            pinned = [(str(pool), allocation) for pool, allocation in pools]
            stale = (
                pinned != [(str(wanted[0]), wanted[1])]
                if wanted is not None
                else any(allocation for _, allocation in pinned)
            )
            if toggle and stale:
                plan = ", ".join(
                    f"{pool} range {(allocation) or ('none')}"
                    for pool, allocation in pinned
                )
                return f"{name} still has the address plan it was created with ({plan}) and DFE_PROXY_TLS={'true' if tls else 'false'} needs another. Docker cannot re-address a live network, so the services this recreates would be left Exited. Run `make down` first"
            continue
        if (not (overlap)) or (wanted is None):
            continue
        for pool, _ in pools:
            if pool.overlaps(wanted[0]):
                return f"DFE_NETWORK_SUBNET={subnet!r} overlaps Docker network {name!r} ({pool}), so compose cannot create {own}. Set DFE_NETWORK_SUBNET to a free private /24"
    return ""


def throwaway_pair(*, directory: Path) -> None:
    """Write a self-signed P-384 console.crt and a 0600 console.key into directory, for checks that need a real pair."""
    directory.mkdir(exist_ok=True, parents=True)
    key = directory / _KEY
    result = _run(
        args=[
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-384",
            "-nodes",
            "-sha384",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(directory / _CERT),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"openssl could not mint a throwaway pair: {result.stderr}")
    key.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    network = commands.add_parser(
        "network", help="print dfe-proxy's address and the dynamic range"
    )
    network.add_argument("subnet")
    precheck = commands.add_parser(
        "precheck", help="print why a start would fail; nothing when it would not"
    )
    precheck.add_argument("--cert-dir", default="")
    precheck.add_argument("--certs", action="store_true")
    precheck.add_argument("--overlap", action="store_true")
    precheck.add_argument("--project", required=True)
    precheck.add_argument("--subnet", default="")
    precheck.add_argument("--tls", choices=("true", "false"), required=True)
    precheck.add_argument("--toggle", action="store_true")
    args = parser.parse_args()

    if args.command == "network":
        try:
            address, allocation = derive(subnet=args.subnet)
        except ValueError as error:
            print(f"DFE_NETWORK_SUBNET={args.subnet!r}: {error}", file=sys.stderr)
            return 1
        print(address, allocation)
        return 0

    problem = cert_problem(cert_dir=args.cert_dir) if args.certs else ""
    if (not (problem)) and ((args.overlap) or (args.toggle)):
        problem = network_problem(
            networks=_docker_networks(),
            overlap=args.overlap,
            project=args.project,
            subnet=args.subnet,
            tls=args.tls == "true",
            toggle=args.toggle,
        )
    if problem:
        print(problem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
