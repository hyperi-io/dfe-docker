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

It refuses on any other DFE_ENV rather than rewriting it. A .env that says
`production` belongs to a deployment, and quietly downgrading its posture and
overwriting its admin password is not something a build target gets to do.

Only the two keys are touched; everything else in .env is left as it stands.
"""

from __future__ import annotations

import sys

from _common import DOTENV_FILE, _dotenv_values, _print, _rel_path
from creds import _DEFAULT_PASSWORD, _DEV_POSTURES, is_dev_posture
from init import _setting_key

# The posture written, and the password written with it.
_DEV_ENV = "dev"
_KEYS = {"DFE_ENV": _DEV_ENV, "DFE_AUTH_LOCAL_ADMIN_PASSWORD": _DEFAULT_PASSWORD}


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
        return 1

    text = DOTENV_FILE.read_text(encoding="utf-8")
    with DOTENV_FILE.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(_rewrite(text=text, wanted=_KEYS))
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg=f"Dev posture: DFE_ENV={_DEV_ENV}, admin password set to the known default",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
