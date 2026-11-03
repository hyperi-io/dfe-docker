#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/bundle.py
#  Purpose:      Build the release bundle install.sh unpacks on a host with only docker and curl
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build `dfe-docker-<version>.tar.gz` and its SHA256SUMS for a release.

The bundle holds what `docker compose up` reads, so a host with no git, make or python can run the stack: docker-compose.yml, compose/, config/, redpanda/, resources/, the .env and env/ templates and service_profiles.yaml. Under `install/` it carries what `make` and the helper scripts work out on a checkout:

  profiles/<name>.env    the profile as dotenv, from resolve_profile.py, for .env
  profiles/<name>.<ui>.env  the same with an infra UI the exposure dials take off the host: kafbat, hyperdx or kafbat-hyperdx
  profiles/<name>.conf   PROFILES (the compose profiles) and SERVICES (what `up -d` names)
  profiles/<name>.usage  the CPU limit of each service as JSON, with UNLIMITED_PLACEHOLDER where a service has none, for apply.sh to fill in with the host's cores. Absent where the profile has no collector
  stack.env              the image pins of one stack, from stack.py, tag and digest
  secrets.list           `<file> <key> <kind> <length>` for each generated secret, from init.py

Run it in a clean checkout, because it writes .env and .tmp/profile.mk there. It never puts .env or env/ in the bundle.

Exit codes:
  0  the bundle and its checksum file are written
  1  a step failed, with the step named
  2  usage error
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import init  # noqa: E402
import resolve_profile  # noqa: E402
import stack  # noqa: E402
import usage_cores  # noqa: E402
from _common import (  # noqa: E402
    DOTENV_FILE,
    PROFILE_MK,
    REPO_ROOT,
    SERVICE_PROFILES_FILE,
    _print,
)
from _registry import DISCOVERY_WORDS, manifest_repo  # noqa: E402

ROOT = "dfe"
INCLUDE = (
    "docker-compose.yml",
    "compose",
    "config",
    "env.example",
    ".env.example",
    "redpanda",
    "resources",
    "service_profiles.yaml",
    "apply.sh",
    "VERSION",
    "LICENSE",
)
# Everything a bundled directory holds that a host running the stack never reads
SKIP_NAMES = {"__pycache__", ".DS_Store", "generate.py"}
_EXPORT_LINE = re.compile(r"^export (?P<key>[A-Z][A-Z0-9_]*) :=\s*(?P<value>.*)$")
# The keys of profile.mk that compose never reads as variables: its own flags and service list
FLAG_KEY = "PROFILE_FLAGS"
SERVICES_KEY = "DFE_SERVICES"
SHELL_ONLY = {
    FLAG_KEY,
    SERVICES_KEY,
    usage_cores.USAGE_LIMITS_VAR,
    usage_cores.USAGE_NODE_VAR,
}
# The infra UIs the exposure dials can take off the host, and so the profile variants apply.sh picks between
GATED_UIS = ("kafbat", "hyperdx")
_PIN_KEY = re.compile(r"^[A-Z][A-Z0-9_]*_VERSION$")


class BundleError(Exception):
    """A step of the build that could not be done."""


def dotenv_value(*, value: str) -> str:
    """One dotenv value: bare when it is plain, single-quoted when it holds anything compose would read as syntax."""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+-]*", value):
        return value
    if "'" in value:
        raise BundleError(f"a profile value holds a single quote: {value!r}")
    return f"'{value}'"


def profile_files(*, mk_text: str) -> tuple[str, str, str]:
    """The dotenv text, the shell conf and the CPU limits JSON (empty where there are none) one resolved profile.mk turns into."""
    dotenv = []
    flags = ""
    services = ""
    limits = ""
    for line in mk_text.splitlines():
        match = _EXPORT_LINE.match(line)
        if match is None:
            continue
        key, value = match["key"], match["value"].strip()
        if key == FLAG_KEY:
            flags = value
        elif key == SERVICES_KEY:
            services = value
        elif key == usage_cores.USAGE_LIMITS_VAR:
            limits = value
        elif key not in SHELL_ONLY:
            dotenv.append(f"{key}={dotenv_value(value=value)}")
    profiles = ",".join(part for part in flags.split() if part != "--profile")
    conf = f'PROFILES="{profiles}"\nSERVICES="{services}"\n'
    return "\n".join(dotenv) + "\n", conf, limits


def pin_lines(*, dotenv_text: str) -> str:
    """The stack's pins in a .env: every *_VERSION line with a digest, and the stack marker."""
    lines = []
    for raw in dotenv_text.splitlines():
        key, sep, value = raw.partition("=")
        if not sep or not _PIN_KEY.match(key):
            continue
        if key == stack.STACK_VERSION_KEY or "@sha256:" in value:
            lines.append(f"{key}={value.split('  #', 1)[0].strip()}")
    if not any("@sha256:" in line for line in lines):
        raise BundleError("the stack .env holds no image pin with a digest")
    return "\n".join(lines) + "\n"


def secrets_list() -> str:
    """One line per generated secret: file, key, kind and length, as init.py mints them."""
    rows = []
    for name, keys in init.GENERATED_SECRETS.items():
        for key, length in keys.items():
            kind = "hex" if key in init._HEX_SECRETS else "alnum"
            rows.append(f"{name} {key} {kind} {length}")
    return "\n".join(rows) + "\n"


def resolve(
    *, profile: str, unpublished: Sequence[str] = (), usage: bool = False
) -> str:
    """profile.mk for one profile, as the Makefile would resolve it on a default .env."""
    environ = {**os.environ, resolve_profile.PROFILE_ENV_VAR: profile}
    args = [f"--unpublished={ui}" for ui in unpublished]
    if usage:
        args += ["--usage", "--usage-placeholder"]
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "resolve_profile.py"), *args],
        check=False,
        env=environ,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        raise BundleError(f"resolving profile {profile}: {result.stderr.strip()}")
    return PROFILE_MK.read_text(encoding="utf-8")


def pinned_env(*, version: str) -> str:
    """Pin the stack into the checkout's .env and return the pin lines."""
    if not DOTENV_FILE.is_file() and init.main() != 0:
        raise BundleError("`make init` failed")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "stack.py"), version],
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(f"pinning stack {version} failed, see above")
    return pin_lines(dotenv_text=DOTENV_FILE.read_text(encoding="utf-8"))


def _add(*, tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(f"{ROOT}/{name}")
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def build(*, out: Path, pins: str, version: str) -> tuple[Path, Path]:
    """Write the bundle and its SHA256SUMS into out and return both paths."""
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / f"dfe-docker-{version}.tar.gz"
    names = list(
        resolve_profile.parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        )["profiles"]
    )
    members: dict[str, bytes] = {}
    for path in sorted(
        file
        for entry in INCLUDE
        for file in (
            [REPO_ROOT / entry]
            if (REPO_ROOT / entry).is_file()
            else sorted((REPO_ROOT / entry).rglob("*"))
        )
        if file.is_file() and not SKIP_NAMES.intersection(file.parts)
    ):
        members[str(path.relative_to(REPO_ROOT))] = path.read_bytes()
    for name in names:
        dotenv, conf, limits = profile_files(mk_text=resolve(profile=name, usage=True))
        members[f"install/profiles/{name}.env"] = dotenv.encode()
        members[f"install/profiles/{name}.conf"] = conf.encode()
        if limits:
            members[f"install/profiles/{name}.usage"] = f"{limits}\n".encode()
        for gated in (("kafbat",), ("hyperdx",), GATED_UIS):
            variant, _, _ = profile_files(
                mk_text=resolve(profile=name, unpublished=gated)
            )
            members[f"install/profiles/{name}.{'-'.join(gated)}.env"] = variant.encode()
    members["install/stack.env"] = pins.encode()
    members["install/secrets.list"] = secrets_list().encode()
    with bundle.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in sorted(members):
                _add(tar=tar, name=name, data=members[name])
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    installer = REPO_ROOT / "install.sh"
    sums = [f"{digest}  {bundle.name}"]
    if installer.is_file():
        sums.append(f"{hashlib.sha256(installer.read_bytes()).hexdigest()}  install.sh")
    checksums = out / "SHA256SUMS"
    checksums.write_text("\n".join(sums) + "\n", encoding="utf-8")
    return bundle, checksums


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default="dist", type=Path, help="where to write")
    parser.add_argument(
        "--version",
        default=(REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        if (REPO_ROOT / "VERSION").is_file()
        else "",
        help="the release version in the bundle's name",
    )
    parser.add_argument(
        "--stack",
        default="latest",
        help="the stack to pin: X.Y.Z or latest, the newest published one",
    )
    parser.add_argument(
        "--pins",
        type=Path,
        help="read the pins from this file of *_VERSION lines instead of pinning a stack",
    )
    args = parser.parse_args()
    if not args.version:
        parser.error("--version is needed when there is no VERSION file")
    try:
        if args.pins:
            pins = pin_lines(dotenv_text=args.pins.read_text(encoding="utf-8"))
        else:
            wanted = args.stack
            if wanted in DISCOVERY_WORDS:
                wanted = stack._stack_version(requested=wanted)
            _print(msg=f"pinning stack {wanted} from {manifest_repo()}")
            pins = pinned_env(version=wanted)
        bundle, checksums = build(out=args.out, pins=pins, version=args.version)
    except (BundleError, stack.StackError, OSError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    _print(msg=f"wrote {bundle} and {checksums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
