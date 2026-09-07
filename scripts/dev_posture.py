#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         dev_posture.py
#  Purpose:      Put .env into the dev posture `make dev` needs -- known password, DFE_ENV=dev
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Declare a dev tyre-kick, in .env, so the engine and the operator agree.

`make init` mints a random admin password, which is right for a deployment and
wrong for a dev loop: the point of `make dev` is a stack you can log into without
looking anything up. So it writes the KNOWN default and DFE_ENV=dev, which is the
one posture the engine accepts that default in -- it banners and asks for a change
at first login instead of refusing to start.

It refuses on any other DFE_ENV rather than rewriting it, exit 2. A .env that says
`production` belongs to a deployment, and quietly downgrading its posture and
overwriting its admin password is not something a build target gets to do.

Only the two keys are touched; everything else in .env is left as it stands. The
minted password this overwrites is unrecoverable once it is gone, so the file it
replaces is copied to `.env.bak-<utc>` first, 0600 like the credentials in it, and
the path is printed. Nothing to change means nothing is written and no backup is
made, which keeps a `make dev` loop from littering the checkout with copies.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

from _common import DOTENV_FILE, _dotenv_values, _print, _rel_path
from creds import _DEFAULT_PASSWORD, _DEV_POSTURES, is_dev_posture
from init import _setting_key

# The posture written, and the password written with it.
_DEV_ENV = "dev"
_KEYS = {"DFE_ENV": _DEV_ENV, "DFE_AUTH_LOCAL_ADMIN_PASSWORD": _DEFAULT_PASSWORD}
# Exit code for the refusal, distinct from 1 (no .env at all) so a caller can tell
# "you are not a dev box" from "you have not run make init".
_REFUSED = 2


def _rewrite(*, text: str, wanted: dict[str, str]) -> str:
    """Return the dotenv text with each wanted key assigned, in place where it exists."""
    pending = dict(wanted)
    lines = []
    for line in text.splitlines():
        key = _setting_key(line=line)
        if key in pending:
            lines.append(f"{key}={pending.pop(key)}")
            continue
        lines.append(line)
    if pending:
        lines.append("")
        lines.append("## Written by `make dev` - a dev tyre-kick, not a deployment.")
        lines.extend(f"{key}={value}" for key, value in sorted(pending.items()))
    return "\n".join(lines) + "\n"


def _backup(*, text: str) -> Path:
    """Copy the current .env alongside itself, 0600, and return the path written."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = DOTENV_FILE.with_name(f"{DOTENV_FILE.name}.bak-{stamp}")
    # Opened 0600 rather than chmod'ed after, so the copy is never world-readable.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(handle, "w", encoding="utf-8", newline="\n") as backup:
        backup.write(text)
    return path


def main() -> int:
    if not (DOTENV_FILE.is_file()):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg="Missing -- run `make init` first",
        )
        return 1

    values = _dotenv_values()
    environment = values.get("DFE_ENV", "").strip()
    if environment and not (is_dev_posture(environment)):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"DFE_ENV={environment} is not a dev posture, so `make dev` refuses to "
            f"run: it would overwrite this deployment's admin password with the shipped "
            f"default. Start it with `make up`, or set DFE_ENV to one of "
            f"{', '.join(sorted(_DEV_POSTURES))} if this really is a dev box.",
        )
        return _REFUSED

    text = DOTENV_FILE.read_text(encoding="utf-8")
    rewritten = _rewrite(text=text, wanted=_KEYS)
    if rewritten == text:
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"Dev posture already set: DFE_ENV={_DEV_ENV}, admin password is the known default",
        )
        return 0

    backup = _backup(text=text)
    with DOTENV_FILE.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rewritten)
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg=f"Dev posture: DFE_ENV={_DEV_ENV}, admin password set to the known default. "
        f"The file it replaced is {_rel_path(path=backup)}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
