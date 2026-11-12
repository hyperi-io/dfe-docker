#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/secrets_init.py
#  Purpose:      Mint a published stack's secrets once into its own volume and write each service the files it reads them from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Mint a published stack's secrets into its secrets volume, once, and hand each service its own.

The compose files scripts/publish_compose.py writes carry no secret. Their secrets-init one-shot runs this from the dfe-docker image as root, with the volume at --root and the plan the generator wrote at --plan:

  {"consumers": {"<service>": {"gid": 61001, "env": {"NAME": "...@@KEY@@..."}, "files": {"NAME": "@@KEY@@"}}}}

Each @@KEY@@ is a GENERATED_SECRETS key. Its value is minted by init.py's own rule the first time and kept in <root>/.minted/KEY, so a restart reuses it and only `down -v` mints afresh. Each service then gets <root>/<service>/, readable by root and the group the plan names: `env` holds the KEY=value lines its shell wrapper sources, and each `files` name a file holding the value alone. Every run rewrites those from the minted values.

Exit codes:
  0  every service's files written
  1  the plan names a key init.py does not mint, or a write failed
  2  usage error
"""

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import init  # noqa: E402
from _common import _print  # noqa: E402

MINTED_DIR = ".minted"
ENV_FILE = "env"
PLACEHOLDER = re.compile(r"@@(?P<key>[A-Z][A-Z0-9_]*)@@")
# Root alone writes, the service's group reads and nobody else sees a thing
DIR_MODE = 0o750
FILE_MODE = 0o440
MINTED_MODE = 0o400


class SecretsError(Exception):
    """A plan this cannot carry out."""


def referenced(*, plan: Mapping) -> list[str]:
    """Every key the plan's templates name, sorted."""
    keys = set()
    for spec in plan.get("consumers", {}).values():
        for kind in ("env", "files"):
            for template in spec.get(kind, {}).values():
                keys.update(match["key"] for match in PLACEHOLDER.finditer(template))
    return sorted(keys)


def render(*, template: str, values: Mapping[str, str]) -> str:
    """A template with each @@KEY@@ replaced by its minted value."""
    return PLACEHOLDER.sub(lambda match: values[match["key"]], template)


def env_text(*, env: Mapping[str, str], values: Mapping[str, str]) -> str:
    """The KEY=value lines a POSIX shell sources, each value quoted."""
    return "".join(
        f"{name}={shlex.quote(render(template=template, values=values))}\n"
        for name, template in sorted(env.items())
    )


def _write(*, gid: int, mode: int, path: Path, text: str) -> None:
    """Replace one file in a single step, in the given group and at the given mode before it holds anything."""
    tmp = path.with_name(f".{path.name}.tmp")
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        os.fchmod(handle, mode)
        os.fchown(handle, -1, gid)
        out.write(text)
    tmp.replace(path)


def minted(*, key: str, root: Path) -> str:
    """The key's value, minted and kept the first time it is asked for."""
    if key not in init._SECRET_LENGTHS:
        raise SecretsError(f"the plan names {key!r}, which init.py does not mint")
    store = root / MINTED_DIR
    store.mkdir(mode=0o700, exist_ok=True)
    path = store / key
    if path.is_file() and path.read_text(encoding="utf-8"):
        return path.read_text(encoding="utf-8")
    value = init._mint(key=key)
    _write(gid=store.stat().st_gid, mode=MINTED_MODE, path=path, text=value)
    return value


def apply(*, plan: Mapping, root: Path) -> list[str]:
    """Write every consumer's directory from the minted values and return the lines that say what each holds."""
    values = {key: minted(key=key, root=root) for key in referenced(plan=plan)}
    said = []
    for name, spec in sorted(plan.get("consumers", {}).items()):
        gid = int(spec["gid"])
        directory = root / name
        directory.mkdir(exist_ok=True)
        os.chown(directory, -1, gid)
        directory.chmod(DIR_MODE)
        env = spec.get("env", {})
        if env:
            _write(
                gid=gid,
                mode=FILE_MODE,
                path=directory / ENV_FILE,
                text=env_text(env=env, values=values),
            )
        for file_name, template in sorted(spec.get("files", {}).items()):
            _write(
                gid=gid,
                mode=FILE_MODE,
                path=directory / file_name,
                text=render(template=template, values=values),
            )
        held = sorted([*env, *spec.get("files", {})])
        said.append(f"{name}: {', '.join(held)}")
    return said


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--plan", type=Path, required=True, help="the generator's plan")
    parser.add_argument(
        "--root", type=Path, required=True, help="where the secrets volume is mounted"
    )
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        for line in apply(plan=plan, root=args.root):
            _print(msg=line)
    except (OSError, KeyError, ValueError, SecretsError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
