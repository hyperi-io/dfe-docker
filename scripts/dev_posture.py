#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         dev_posture.py
#  Purpose:      Put .env and env/engine.env into the posture `make dev` needs, a tyre-kick or a real login
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Declare what kind of dev stack this is, in .env and env/engine.env, so the engine and the operator agree.

Two postures, because `make dev` is asked for two different things. The default is
a tyre-kick: a stack you can log into without looking anything up, so it writes the
KNOWN default password and DFE_ENV=dev, the one posture the engine accepts that
default in. `--real` is the other: local images running the authentication flow a
deployment gets, so it mints a password and writes a non-dev posture, leaving both
alone where they already hold real values.

The tyre-kick writes the default only where the posture is not already dev. On a
dev stack the engine makes the admin replace the default at first login and `make
post` records the replacement in env/engine.env, so writing the default back would
lock the next run out of an engine that no longer accepts it.

The tyre-kick refuses on any other DFE_ENV rather than rewriting it, exit 2. A .env
that says `production` belongs to a deployment, and quietly downgrading its posture
and overwriting its admin password is not something a build target gets to do.
`--real` never refuses, because it destroys nothing.

Only those two keys are touched, DFE_ENV in .env and the password in
env/engine.env. Everything else in either file is left as it stands. The minted
password a tyre-kick overwrites is unrecoverable once it is gone, so each file it
rewrites is first copied beside itself to `<name>.bak-<utc>`, 0600 like the
credentials in it. The path is printed. Nothing to change means nothing is
written and no backup is made, which keeps a `make dev` loop from littering the
checkout with copies.
"""

import argparse
import datetime
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    DOTENV_FILE,
    ENGINE_COMPONENT,
    _env_file,
    _print,
    _rel_path,
    write_dotenv,
    write_private,
)
from creds import (  # noqa: E402
    _ADMIN_PASSWORD_KEY,
    _DEFAULT_PASSWORD,
    _DEV_POSTURES,
    access_values,
    default_admin_password,
    is_dev_posture,
)
from init import _mint, _setting_key  # noqa: E402

# The two postures written, and what each means for the admin password.
_DEV_ENV = "dev"
_REAL_ENV = "production"
_POSTURE_KEY = "DFE_ENV"
# Exit code for the refusal, distinct from 1 (no .env at all) so a caller can tell
# "you are not a dev box" from "you have not run make init".
_REFUSED = 2


def _backup(*, path: Path, text: str) -> Path:
    """Copy one file's current text alongside it at 0600 and return the path written."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    write_private(path=backup, text=text)
    return backup


def _live_key(*, line: str) -> str | None:
    """Return the key an uncommented dotenv line assigns, None for a comment or a line that assigns nothing."""
    if line.lstrip().startswith("#"):
        return None
    return _setting_key(line=line)


def _rewrite(*, banner: str, text: str, wanted: dict[str, str]) -> str:
    """Return the dotenv text with each wanted key set on its live lines, else on its first commented line, else appended."""
    lines = text.splitlines()
    live = {_live_key(line=line) for line in lines} & wanted.keys()
    pending = {key: value for key, value in wanted.items() if key not in live}
    rewritten = []
    for line in lines:
        live_key = _live_key(line=line)
        key = _setting_key(line=line)
        if live_key in live:
            rewritten.append(f"{live_key}={wanted[live_key]}")
        elif key in pending:
            rewritten.append(f"{key}={pending.pop(key)}")
        else:
            rewritten.append(line)
    if pending:
        rewritten.append("")
        rewritten.append(banner)
        rewritten.extend(f"{key}={value}" for key, value in sorted(pending.items()))
    return "\n".join(rewritten) + "\n"


def _wanted_real(*, values: dict[str, str]) -> dict[str, str]:
    """The keys `--real` assigns, omitting any that already holds a real value."""
    wanted = {}
    if is_dev_posture(values.get(_POSTURE_KEY, "")):
        wanted[_POSTURE_KEY] = _REAL_ENV
    if default_admin_password(values.get(_ADMIN_PASSWORD_KEY, "")):
        wanted[_ADMIN_PASSWORD_KEY] = _mint(key=_ADMIN_PASSWORD_KEY)
    return wanted


def _wanted_tyre_kick(*, values: dict[str, str]) -> dict[str, str]:
    """The keys the tyre-kick assigns, keeping a password a dev stack already replaced."""
    wanted = {_POSTURE_KEY: _DEV_ENV}
    already_dev = is_dev_posture(values.get(_POSTURE_KEY, ""))
    replaced = not (default_admin_password(values.get(_ADMIN_PASSWORD_KEY, "")))
    if not (already_dev and replaced):
        wanted[_ADMIN_PASSWORD_KEY] = _DEFAULT_PASSWORD
    return wanted


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="mint an admin password and write a non-dev posture, for exercising "
        "the authentication flow against locally built images",
    )
    args = parser.parse_args(args=argv)

    engine_env = _env_file(component=ENGINE_COMPONENT)
    for path in (DOTENV_FILE, engine_env):
        if not (path.is_file()):
            _print(header=_rel_path(path=path), msg="Missing, run `make init` first")
            return 1

    values = access_values()
    environment = values.get(_POSTURE_KEY, "").strip()
    if not (args.real) and environment and not (is_dev_posture(environment)):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"DFE_ENV={environment} is not a dev posture, so `make dev` refuses to "
            f"run: it would overwrite this deployment's admin password with the shipped "
            f"default. Start it with `make up`, use `make dev AUTH=real` to keep a real "
            f"login, or set DFE_ENV to one of {', '.join(sorted(_DEV_POSTURES))} if this "
            f"really is a dev box.",
        )
        return _REFUSED

    wanted = (
        _wanted_real(values=values) if args.real else _wanted_tyre_kick(values=values)
    )
    banner = (
        "## Written by `make dev AUTH=real` - local images, real logins"
        if args.real
        else "## Written by `make dev` - a dev tyre-kick, not a deployment"
    )
    password_state = (
        "admin password set to the known default"
        if _ADMIN_PASSWORD_KEY in wanted
        else "admin password kept as recorded -- `make creds` prints it"
    )
    settled = (
        f"Real posture already set: DFE_ENV={environment}, admin password is minted"
        if args.real
        else f"Dev posture already set: DFE_ENV={_DEV_ENV}, {password_state}"
    )

    changes = []
    for path, key in ((DOTENV_FILE, _POSTURE_KEY), (engine_env, _ADMIN_PASSWORD_KEY)):
        if key not in wanted:
            continue
        text = path.read_text(encoding="utf-8")
        rewritten = _rewrite(banner=banner, text=text, wanted={key: wanted[key]})
        if rewritten != text:
            changes.append((path, text, rewritten))
    if not (changes):
        _print(header=_rel_path(path=DOTENV_FILE), msg=settled)
        return 0

    backups = []
    for path, text, rewritten in changes:
        backups.append(_rel_path(path=_backup(path=path, text=text)))
        write_dotenv(path=path, text=rewritten)
    written = (
        f"Real posture: DFE_ENV={wanted.get(_POSTURE_KEY, environment)}, admin password "
        f"minted. Read it with `make creds`."
        if args.real
        else f"Dev posture: DFE_ENV={_DEV_ENV}, {password_state}."
    )
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg=f"{written} The files it replaced are kept as {', '.join(backups)}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
