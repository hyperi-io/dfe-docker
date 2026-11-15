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

The admin login is the one pair a person may choose. DFE_ADMIN_NAME and DFE_ADMIN_PASSWORD, when set and not empty, replace the stored name and password on that run, after the password is checked against what the engine accepts. Unset, the stored values stay, and a first run mints the password and names the admin `admin`.

`login` as the last argument prints the stored name and password. It mints them first when none are stored and writes no service's files.

Exit codes:
  0  every service's files written, or the login printed
  1  the plan names a key init.py does not mint, a value set for the admin login is refused, or a write failed
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
from _common import LOGIN_COMMAND, _print  # noqa: E402

ADMIN_NAME_KEY = "DFE_ENGINE_AUTH_LOCAL_ADMIN_NAME"
ADMIN_PASSWORD_KEY = "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD"
DEFAULT_ADMIN_NAME = "admin"
# Each key a person may choose, by the variable that carries it into secrets-init
ADMIN_NAME_VAR = "DFE_ADMIN_NAME"
ADMIN_PASSWORD_VAR = "DFE_ADMIN_PASSWORD"
SUPPLIED = {
    ADMIN_NAME_KEY: ADMIN_NAME_VAR,
    ADMIN_PASSWORD_KEY: ADMIN_PASSWORD_VAR,
}
# dfe_engine.names.VALID_NAME
ADMIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# dfe_engine.auth.bootstrap.MIN_ADMIN_PASSWORD_LENGTH, the floor on any password the engine API stores
MIN_PASSWORD_LENGTH = 12
# bcrypt hashes no more, and the engine refuses to start on a longer admin password
MAX_PASSWORD_BYTES = 72
# The engine's shipped placeholder, which it refuses outside a dev posture
SHIPPED_PASSWORD = "changeme"  # noqa: S105
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


def refused(*, key: str, value: str, variable: str) -> str | None:
    """Why the engine would not take a value chosen for the admin login, naming the variable and never the value."""
    if key == ADMIN_NAME_KEY:
        if not ADMIN_NAME.fullmatch(value):
            return f"{variable} must be letters, digits, `.`, `_` and `-`, starting with a letter or digit, at most 128 characters"
        return None
    if not value.strip():
        return f"{variable} is blank, which the engine refuses"
    if not value.isprintable():
        return f"{variable} holds a newline, tab or other control character"
    if len(value) < MIN_PASSWORD_LENGTH:
        return f"{variable} must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"{variable} must be at most {MAX_PASSWORD_BYTES} bytes, the most the engine hashes"
    if value.strip() == SHIPPED_PASSWORD:
        return f"{variable} is the shipped placeholder, which the engine refuses"
    return None


def minted(*, environ: Mapping[str, str], key: str, root: Path) -> str:
    """The key's value: the one chosen for it in environ, else the one kept, else a fresh one, which is kept."""
    variable = SUPPLIED.get(key)
    if key not in init._SECRET_LENGTHS and variable is None:
        raise SecretsError(f"the plan names {key!r}, which init.py does not mint")
    store = root / MINTED_DIR
    store.mkdir(mode=0o700, exist_ok=True)
    path = store / key
    kept = path.read_text(encoding="utf-8") if path.is_file() else ""
    chosen = environ.get(variable, "") if variable else ""
    if chosen and (reason := refused(key=key, value=chosen, variable=variable)):
        raise SecretsError(reason)
    value = chosen or kept
    if not value:
        value = DEFAULT_ADMIN_NAME if key == ADMIN_NAME_KEY else init._mint(key=key)
    if value != kept:
        _write(gid=store.stat().st_gid, mode=MINTED_MODE, path=path, text=value)
    return value


def login(*, root: Path) -> str:
    """The two lines `login` prints: the admin's name and password, and what the console does with them."""
    name, password = (
        minted(environ={}, key=key, root=root)
        for key in (ADMIN_NAME_KEY, ADMIN_PASSWORD_KEY)
    )
    return (
        f"Console login: {name} / {password}\n"
        "The console makes you change this password at first login.\n"
    )


def apply(
    *, environ: Mapping[str, str] | None = None, plan: Mapping, root: Path
) -> list[str]:
    """Write every consumer's directory from the minted values and return the lines that say what each holds."""
    values = {
        key: minted(environ=environ or {}, key=key, root=root)
        for key in referenced(plan=plan)
    }
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
    parser.add_argument(
        "command",
        choices=[LOGIN_COMMAND],
        nargs="?",
        help="`login` prints the admin's name and password instead of writing the plan's files",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == LOGIN_COMMAND:
            # Printing the password is the whole job of this command
            sys.stdout.write(login(root=args.root))
            return 0
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        for line in apply(environ=os.environ, plan=plan, root=args.root):
            _print(msg=line)
    except (OSError, KeyError, ValueError, SecretsError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
