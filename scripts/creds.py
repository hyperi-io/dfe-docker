#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         creds.py
#  Purpose:      Print the access summary -- where to log in, as whom, with which password
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hand the operator the credentials this deployment minted.

`make init` mints the admin and break-glass passwords into .env and prints
neither, so something has to hand them over afterwards. This is it: `make up`,
`make dev` and `make ci` end with it, and `make creds` prints it again on demand.
The engine names the same command on its login page while first-run setup is
incomplete (deployment_hints.credential_fetch_command for the docker target), so
an operator who lands on the login page with no password is one line from having
one.

The admin password is printed. The break-glass password is NOT: the engine hashes
it into the deploy repo on its first boot and never stores the plaintext, so this
says where it lives and leaves reading it a deliberate act.

Values are read out of .env with the same minimal parser compose uses. A key with
no value reads as missing and is reported as such rather than printed blank.
"""

from __future__ import annotations

import sys

from _common import (
    DOTENV_FILE,
    _dotenv_values,
    _print,
    _rel_path,
)

# The account names the engine seeds. `breakglass` is fixed in the engine
# (auth/breakglass.py); `admin` is overridable per deployment.
_ADMIN_NAME_KEY = "DFE_AUTH_LOCAL_ADMIN_NAME"
_ADMIN_PASSWORD_KEY = "DFE_AUTH_LOCAL_ADMIN_PASSWORD"
_BREAKGLASS_NAME = "breakglass"
_BREAKGLASS_PASSWORD_KEY = "DFE_AUTH_BREAKGLASS_PASSWORD"

# Postures the engine treats as dev, where it tolerates the shipped default
# password. Mirrors dfe_engine.settings.is_dev_posture.
_DEV_POSTURES = {"dev", "development", "local", "test", "ci"}
# The shipped placeholder the engine refuses outside a dev posture.
_DEFAULT_PASSWORD = "changeme"


def _url(*, values: dict[str, str], port_key: str, default_port: str) -> str:
    """The URL a host-side browser reaches one published UI on."""
    # DFE_BIND_SCOPE=all publishes on every address; name the host's own.
    host = (
        "localhost"
        if values.get("DFE_BIND_SCOPE", "").strip() == "all"
        else "127.0.0.1"
    )
    return f"http://{host}:{values.get(port_key, '').strip() or default_port}"


def is_dev_posture(environment: str) -> bool:
    """True when DFE_ENV names a posture the engine tolerates defaults in."""
    return (environment.strip() or "dev").lower() in _DEV_POSTURES


def default_admin_password(password: str) -> bool:
    """True when the admin password is unset or the shipped default."""
    return not password.strip() or password.strip() == _DEFAULT_PASSWORD


def summary_lines(*, values: dict[str, str]) -> list[str]:
    """The access summary, one line per thing the operator needs."""
    environment = values.get("DFE_ENV", "").strip() or "dev"
    admin = values.get(_ADMIN_NAME_KEY, "").strip() or "admin"
    password = values.get(_ADMIN_PASSWORD_KEY, "").strip()

    lines = [
        "",
        "  DFE access",
        f"    console      {_url(values=values, port_key='DFE_UI_PORT', default_port='3000')}",
        f"    engine API   {_url(values=values, port_key='DFE_ENGINE_PORT', default_port='8003')}",
    ]
    if password:
        lines.append(f"    login        {admin} / {password}")
    else:
        lines.append(
            f"    login        {admin} / NOT MINTED -- run `make init` to mint "
            f"{_ADMIN_PASSWORD_KEY}"
        )
    if default_admin_password(password) and is_dev_posture(environment):
        lines.append(
            f"                 DFE_ENV={environment}, so the engine accepts this "
            "default and asks for a change at first login"
        )
    if values.get(_BREAKGLASS_PASSWORD_KEY, "").strip():
        lines.append(
            f"    break-glass  {_BREAKGLASS_NAME} / the value of "
            f"{_BREAKGLASS_PASSWORD_KEY} in {_rel_path(path=DOTENV_FILE)}"
        )
        lines.append(
            "                 recovery only -- the engine hashed it into the deploy "
            "repo on first boot and keeps no plaintext"
        )
    else:
        lines.append(
            f"    break-glass  none -- {_BREAKGLASS_PASSWORD_KEY} is unset, so no "
            "recovery admin was minted"
        )
    lines.append("    show again   make creds")
    lines.append("")
    return lines


def main() -> int:
    if not (DOTENV_FILE.is_file()):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg="Missing -- run `make init` to mint this deployment's credentials",
        )
        return 1
    for line in summary_lines(values=_dotenv_values()):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
