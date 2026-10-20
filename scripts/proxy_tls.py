#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/proxy_tls.py
#  Purpose:      Derive console TLS's network, mint its certificate and refuse a start its certificate or network cannot carry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console TLS checks the Makefile runs while it parses, plus the certificate `make certs` mints.

`network SUBNET` prints dfe-proxy's reserved address and the range Docker allocates from. Under DFE_PROXY_TLS=true the engine believes X-Forwarded-Proto from dfe-proxy's address alone, so no other container may take that address: the proxy holds the subnet's last host and Docker allocates everything else, the gateway included, from the first half.

`precheck` prints the first reason a start goal would fail and nothing when it would not. Make turns that line into its `$(error)`, so each reason names what to change.

`mint` writes a private CA and the console certificate it signs, then deletes the CA's key, so trusting ca.crt trusts that one certificate and nothing it could sign later.

Standalone and stdlib only, because the make guard tests copy it next to a lone Makefile.
"""

import argparse
import datetime
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_CA = "ca.crt"
_CERT = "console.crt"
# A day inside Apple's 825-day ceiling for a TLS server certificate; the CA signs once, so it needs no longer.
_DAYS = 824
# X.509 caps a common name at 64 characters; the subjectAltName carries the full host.
_MAX_CN = 64
_HOSTNAME = re.compile(
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*"
)
_KEY = "console.key"
_P384_KEY = (
    "-newkey",
    "ec",
    "-pkeyopt",
    "ec_paramgen_curve:P-384",
    "-nodes",
    "-sha384",
)
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


class MintError(Exception):
    """Why `mint` wrote nothing."""


def _cert_dir_form_problem(*, cert_dir: str) -> str:
    """Return why compose could not mount cert_dir as a path; the empty string when it can."""
    if not (cert_dir.startswith(("/", "./", "../"))):
        return f"DFE_PROXY_CERT_DIR={cert_dir!r} must be a path starting with /, ./ or ../ because compose reads a bare name as a named volume"
    return ""


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


def _is_host(*, name: str) -> bool:
    """Return whether name is an IP address or a DNS hostname a certificate can name, with nothing openssl would parse as syntax."""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return len(name) <= 253 and _HOSTNAME.fullmatch(name) is not None
    # An IPv6 zone id may hold any character, newline and comma included.
    return "%" not in name


def _last_line(*, text: str) -> str:
    """Return the last non-blank line of a tool's output; a placeholder when it printed nothing."""
    return _first_line(text="\n".join(reversed(text.splitlines())))


def _mint_step(*, args: list[str], what: str) -> None:
    """Run one openssl command for `mint`, raising MintError with the last line of its output, where openssl puts the cause, when it fails."""
    result = _run(args=args)
    if result.returncode != 0:
        raise MintError(
            f"openssl could not mint {what}: {_last_line(text=result.stderr)}"
        )


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


def _serial() -> str:
    """Return a random 128-bit certificate serial, so no two minted certificates share an issuer and serial."""
    return f"0x{secrets.token_hex(16)}"


def _subject_alt_names(*, host: str) -> list[str]:
    """Return the subjectAltName entries for host, localhost and 127.0.0.1, each once."""
    entries = []
    for name in (host, "localhost", "127.0.0.1"):
        try:
            entry = f"IP:{ipaddress.ip_address(name)}"
        except ValueError:
            entry = f"DNS:{name.lower()}"
        if entry not in entries:
            entries.append(entry)
    return entries


def cert_problem(*, cert_dir: str) -> str:
    """Return why Envoy could not load the certificate pair in cert_dir; the empty string when it can."""
    setting = f"DFE_PROXY_CERT_DIR={cert_dir!r}"
    if problem := _cert_dir_form_problem(cert_dir=cert_dir):
        return problem
    if not (Path(cert_dir).is_dir()):
        return f"{setting} is not a directory. It must hold {_CERT} and {_KEY}"
    if not (os.access(cert_dir, os.X_OK)):
        return f"{setting} exists but this user cannot open it. Under TLS both proxies run as the user who runs make, so that user must be able to read {_CERT} and {_KEY} there. Run make as the user who owns it rather than widening its mode"
    paths = {}
    for name, kind in ((_CERT, "certificate"), (_KEY, "private key")):
        shown = f"{cert_dir.rstrip('/')}/{name}"
        path = Path(cert_dir) / name
        paths[name] = path
        try:
            if path.is_symlink():
                return f"{shown} is a symlink, which dangles inside the container (certbot's live/ directory is all symlinks). Copy the file itself with `cp -L`"
            if not (path.is_file()):
                return f"DFE_PROXY_TLS=true needs the {kind} {shown} and it is missing. Run `make certs` to mint the pair or copy a CA-issued one there (docs/configuration.md, Console TLS). Set DFE_PROXY_TLS=false to start without TLS"
        except OSError as error:
            return f"{shown} cannot be checked ({error.strerror}), so the start is refused rather than assumed safe"
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


def mint(*, cert_dir: str, host: str) -> list[str]:
    """Write console.crt, console.key and the ca.crt that signed them into cert_dir, delete the CA's key and return the subjectAltName entries.

    Raises MintError before writing anything when the host, the directory or a missing openssl rules it out or when any of the three files exists, so a CA-issued pair or a CA a browser already trusts is never replaced.
    """
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if not (_is_host(name=bare)):
        raise MintError(
            f"DFE_EXTERNAL_ORIGIN names the host {host!r}, which is not a hostname or an IP address a certificate can carry"
        )
    if problem := _cert_dir_form_problem(cert_dir=cert_dir):
        raise MintError(problem)
    if shutil.which("openssl") is None:
        raise MintError(
            "openssl is not on PATH, so make cannot mint the certificate. Install openssl"
        )
    directory = Path(cert_dir)
    shown = cert_dir.rstrip("/")
    if directory.exists() and not (directory.is_dir()):
        raise MintError(f"DFE_PROXY_CERT_DIR={cert_dir!r} is a file, not a directory")
    present = [name for name in (_CERT, _KEY, _CA) if os.path.lexists(directory / name)]
    if present:
        raise MintError(
            f"{shown} already holds {', '.join(present)}. make certs never replaces them, because a CA-issued pair or a CA your browser trusts would be lost. To mint a fresh set, delete {_CERT}, {_KEY} and {_CA} there, run it again and trust the new {_CA}"
        )
    entries = _subject_alt_names(host=bare)
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    # The random tag keeps a re-minted CA from sharing a subject with one a browser still trusts.
    ca_name = f"DFE local CA {secrets.token_hex(3)} {today} {bare}"[:_MAX_CN]
    leaf_name = bare if len(bare) <= _MAX_CN else "DFE console"
    try:
        directory.mkdir(exist_ok=True, mode=0o700, parents=True)
        # The CA key stays outside cert_dir, which both proxies mount; the outputs stage inside it so they rename into place on one filesystem.
        with (
            tempfile.TemporaryDirectory() as secret,
            tempfile.TemporaryDirectory(dir=directory) as scratch,
        ):
            work = Path(scratch)
            ca_key = Path(secret) / "ca.key"
            request = work / "console.csr"
            extensions = work / "console.ext"
            _mint_step(
                args=[
                    "openssl",
                    "req",
                    "-x509",
                    *_P384_KEY,
                    "-days",
                    str(_DAYS),
                    "-set_serial",
                    _serial(),
                    "-subj",
                    f"/CN={ca_name}",
                    "-addext",
                    "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext",
                    "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext",
                    "subjectKeyIdentifier=hash",
                    "-keyout",
                    str(ca_key),
                    "-out",
                    str(work / _CA),
                ],
                what="the CA",
            )
            _mint_step(
                args=[
                    "openssl",
                    "req",
                    "-new",
                    *_P384_KEY,
                    "-subj",
                    f"/CN={leaf_name}",
                    "-keyout",
                    str(work / _KEY),
                    "-out",
                    str(request),
                ],
                what="the console key",
            )
            extensions.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature\n"
                "extendedKeyUsage=serverAuth\n"
                f"subjectAltName={','.join(entries)}\n"
                "subjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid\n",
                encoding="utf-8",
                newline="\n",
            )
            _mint_step(
                args=[
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    str(request),
                    "-CA",
                    str(work / _CA),
                    "-CAkey",
                    str(ca_key),
                    "-set_serial",
                    _serial(),
                    "-days",
                    str(_DAYS),
                    "-sha384",
                    "-extfile",
                    str(extensions),
                    "-out",
                    str(work / _CERT),
                ],
                what="the console certificate",
            )
            (work / _KEY).chmod(0o600)
            for name in (_KEY, _CERT, _CA):
                (work / name).replace(directory / name)
    except OSError as error:
        raise MintError(f"cannot write to {shown}: {error.strerror}") from error
    return entries


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
            *_P384_KEY,
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
    minter = commands.add_parser(
        "mint",
        help="write a CA and the console certificate it signs, then delete the CA's key",
    )
    minter.add_argument("--cert-dir", required=True)
    minter.add_argument("--host", required=True)
    args = parser.parse_args()

    if args.command == "mint":
        try:
            entries = mint(cert_dir=args.cert_dir, host=args.host)
        except MintError as error:
            print(error, file=sys.stderr)
            return 1
        shown = args.cert_dir.rstrip("/")
        names = ", ".join(entry.partition(":")[2] for entry in entries)
        print(
            f"Minted {shown}/{_CERT} and {shown}/{_KEY} for {names}, valid {_DAYS} days, signed by {shown}/{_CA}. The CA's key is deleted, so it signs nothing else."
        )
        print(
            f"Trust {shown}/{_CA} once in your browser or OS, then set DFE_PROXY_CA_BUNDLE={shown}/{_CA} for make test-e2e. To renew, delete the three files and run make certs again."
        )
        return 0

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
