#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         creds.py
#  Purpose:      Print the access summary -- where to log in, as whom, with which password
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hand the operator the credentials this deployment minted.

`make init` mints the admin and break-glass passwords into env/engine.env and
prints neither, so something has to hand them over afterwards. This is it: `make up` and
`make dev` end with it, and `make creds` prints it again on demand. The engine
names the same command on its login page while first-run setup is incomplete
(deployment_hints.credential_fetch_command for the docker target), so an operator
who lands on the login page with no password is one line from having one.

`--write` also writes access-summary.md next to .env, in the same shape
dfe-infra's `dfe-ops access-summary` writes after a kubernetes deploy -- same
heading, same table, same closing steps -- so an operator reads one artefact
whichever deployer they used. It carries BOTH minted passwords in plaintext,
which is why it is 0600, gitignored, and told to be deleted -- it exists to be
read once and removed, and the terminal print below is the every-day path.

The admin password prints ONLY to a terminal. A pipe, a file or a CI job log gets
the line that says which key in env/engine.env holds it instead, because a build
log is read by more people, for longer, than the person who ran the command.
DFE_CREDS_SHOW=0 takes the same branch on a terminal. `make ci` does not call this
at all.

The break-glass password is never printed: the engine hashes it into the deploy
repo on its first boot and never stores the plaintext, so this says where it
lives and leaves reading it a deliberate act. The OIDC fixture password is never
printed either -- it is one shared login across every tester, so the summary
names the key holding it and stops there.

Each seed account `make accounts` created is handed over the way the admin is: its password on a terminal only, its env/engine.env key otherwise.

The console and engine URLs follow DFE_EXTERNAL_ORIGIN wherever a deployment has
set one, so the operator is handed the address browsers use rather than one that
resolves only on the box the stack runs on.

Values are read out of .env and the two passwords out of env/engine.env with the
same minimal parser compose uses. A key with no value reads as missing and is
reported as such rather than printed blank.
"""

import os
import re
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    ACCESS_SUMMARY_FILE,
    DOTENV_FILE,
    ENGINE_COMPONENT,
    FALSY,
    SEED_DIR,
    _dotenv_values,
    _env_file,
    _env_file_values,
    _print,
    _published_url,
    _rel_path,
    seed_password_key,
    write_private,
)

# Set to 0 to keep the password off a terminal too.
_SHOW_KEY = "DFE_CREDS_SHOW"
# What a password line says off a terminal, under the key it names
_NOT_PRINTED = (
    "                 not printed -- stdout is not a terminal, so run "
    "`make creds` on one to see it"
)
# The accounts `make accounts` creates, one file each named for its username
_SEED_ACCOUNTS_DIR = SEED_DIR / "accounts"
_SEED_LABEL = "Seed account"

# The account names the engine seeds. `breakglass` is fixed in the engine
# (auth/breakglass.py); `admin` is overridable per deployment.
_ADMIN_NAME_KEY = "DFE_AUTH_LOCAL_ADMIN_NAME"
_ADMIN_PASSWORD_KEY = "DFE_AUTH_LOCAL_ADMIN_PASSWORD"
_BREAKGLASS_NAME = "breakglass"
_BREAKGLASS_PASSWORD_KEY = "DFE_AUTH_BREAKGLASS_PASSWORD"

# The tester login the console acceptance specs sign in with. Same names in every
# DFE repo; the default user is the one the fixture provisioners create.
_FIXTURE_USER_KEY = "DFE_OIDC_FIXTURE_USER"
_FIXTURE_PASSWORD_KEY = "DFE_OIDC_FIXTURE_PASSWORD"
_FIXTURE_DEFAULT_USER = "dfe-test@dfe-oidc.test"
# A per-provider override, where the group is the provider name uppercased as
# /api/v1/auth/setup-status reports it. The generic pair above cannot match.
_FIXTURE_OVERRIDE_RE = re.compile(r"^DFE_OIDC_([A-Z0-9]+)_FIXTURE_(?:USER|PASSWORD)$")

# Postures the engine treats as dev, where it tolerates the shipped default
# password. Mirrors dfe_engine.settings.is_dev_posture.
_DEV_POSTURES = {"dev", "development", "local", "test", "ci"}
# What an unset DFE_ENV resolves to. Mirrors compose's own `${DFE_ENV:-production}`,
# so the summary names the posture the engine is actually running in.
_UNSET_POSTURE = "production"
# The shipped placeholder the engine refuses outside a dev posture.
_DEFAULT_PASSWORD = "changeme"

# The written summary's heading, account labels and closing steps, copied from
# dfe-infra scripts/access_summary.py so both deployers hand over one file shape.
# Changing any of these diverges the two artefacts.
_SUMMARY_HEADING = "# DFE access -- first login"
_ADMIN_LABEL = "Admin"
_BREAKGLASS_LABEL = "Break-Glass"
_NEXT_STEPS = (
    "Log in at the console URL above and finish the setup wizard.",
    "Retire the bootstrap admin from the wizard's last step once your own admin exists.",
    "Keep the break-glass password somewhere safe, then delete this file.",
)


def _seed_logins(*, values: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (username, key, password) for each seed account whose password `values` holds."""
    logins = []
    for username in _seed_usernames():
        key = seed_password_key(username=username)
        password = values.get(key, "").strip()
        if password:
            logins.append((username, key, password))
    return logins


def _seed_usernames() -> list[str]:
    """Return the username of each resources/seed account, which its file is named for."""
    return sorted(path.stem for path in _SEED_ACCOUNTS_DIR.glob("*.yaml"))


def _url(
    *, default_port: str, port_key: str, proxied: bool = True, values: dict[str, str]
) -> str:
    """The URL a browser reaches one published UI on.

    A deployment that publishes beyond loopback sets DFE_EXTERNAL_ORIGIN, and that
    is the address its operator hands out -- so the summary quotes it rather than
    an address that only resolves on the box the stack runs on.
    """
    # DFE_BIND_SCOPE=all publishes on every address; name the host's own.
    host = (
        "localhost"
        if values.get("DFE_BIND_SCOPE", "").strip() == "all"
        else "127.0.0.1"
    )
    return _published_url(
        default_port=default_port,
        host=host,
        port_key=port_key,
        proxied=proxied,
        values=values,
    )


def is_dev_posture(environment: str) -> bool:
    """True when DFE_ENV names a posture the engine tolerates defaults in."""
    return (environment.strip() or _UNSET_POSTURE).lower() in _DEV_POSTURES


def default_admin_password(password: str) -> bool:
    """True when the admin password is unset or the shipped default."""
    return not password.strip() or password.strip() == _DEFAULT_PASSWORD


def show_password(*, is_tty: bool, setting: str) -> bool:
    """True when the admin password may be printed -- a terminal, not turned off."""
    # An unset DFE_CREDS_SHOW reads as "", which is in FALSY and must not count.
    if setting.strip() and setting.strip().lower() in FALSY:
        return False
    return is_tty


def fixture_providers(*, values: dict[str, str]) -> list[str]:
    """Provider names carrying a per-provider fixture override with a value."""
    return sorted(
        {
            match.group(1)
            for key, value in values.items()
            if value.strip() and (match := _FIXTURE_OVERRIDE_RE.match(key.strip()))
        }
    )


def fixture_lines(*, values: dict[str, str]) -> list[str]:
    """The OIDC fixture login, as a user and a key name -- never as a password.

    Empty when neither generic key is set: an operator who never signs in through
    an external provider has no fixture login to be told about.
    """
    user = values.get(_FIXTURE_USER_KEY, "").strip()
    password = values.get(_FIXTURE_PASSWORD_KEY, "").strip()
    if not (user or password):
        return []
    where = (
        f"the value of {_FIXTURE_PASSWORD_KEY} in {_rel_path(path=DOTENV_FILE)}"
        if password
        else f"NO PASSWORD -- {_FIXTURE_PASSWORD_KEY} is unset, so the specs skip"
    )
    lines = [f"    oidc test    {user or _FIXTURE_DEFAULT_USER} / {where}"]
    providers = fixture_providers(values=values)
    if providers:
        lines.append(
            f"                 per-provider override set for {', '.join(providers)} "
            "-- see DFE_OIDC_<PROVIDER>_FIXTURE_*"
        )
    return lines


def access_values() -> dict[str, str]:
    """Return .env's values with both logins' and each seed account's password as env/engine.env, the file compose hands the engine, sets them."""
    values = _dotenv_values()
    engine = _env_file_values(component=ENGINE_COMPONENT)
    keys = [_ADMIN_PASSWORD_KEY, _BREAKGLASS_PASSWORD_KEY]
    keys += [seed_password_key(username=username) for username in _seed_usernames()]
    for key in keys:
        values[key] = engine.get(key, "")
    return values


def summary_lines(*, values: dict[str, str], reveal: bool = True) -> list[str]:
    """The access summary, one line per thing the operator needs."""
    environment = values.get("DFE_ENV", "").strip() or _UNSET_POSTURE
    admin = values.get(_ADMIN_NAME_KEY, "").strip() or "admin"
    password = values.get(_ADMIN_PASSWORD_KEY, "").strip()
    engine_env = _rel_path(path=_env_file(component=ENGINE_COMPONENT))

    lines = [
        "",
        "  DFE access",
        f"    console      {_url(values=values, port_key='DFE_UI_PORT', default_port='3000')}",
        f"    engine API   {_url(default_port='8003', port_key='DFE_ENGINE_PORT', proxied=False, values=values)}",
    ]
    if password and reveal:
        lines.append(f"    login        {admin} / {password}")
    elif password:
        lines.append(
            f"    login        {admin} / the value of {_ADMIN_PASSWORD_KEY} in "
            f"{engine_env}"
        )
        lines.append(_NOT_PRINTED)
    else:
        lines.append(
            f"    login        {admin} / NOT MINTED -- run `make init` to mint "
            f"{_ADMIN_PASSWORD_KEY}"
        )
    # The posture decides which line; `reveal` only suppresses the accepted one,
    # so a piped run on a dev posture must not fall through to the refusal.
    if password and default_admin_password(password):
        if not (is_dev_posture(environment)):
            lines.append(
                f"                 DFE_ENV={environment} is not a dev posture, so the "
                "engine REFUSES this default and will not start -- mint one with "
                "`make dev AUTH=real`, or set DFE_ENV=dev for a tyre-kick"
            )
        elif reveal:
            lines.append(
                f"                 DFE_ENV={environment}, so the engine accepts this "
                "default and asks for a change at first login"
            )
    if values.get(_BREAKGLASS_PASSWORD_KEY, "").strip():
        lines.append(
            f"    break-glass  {_BREAKGLASS_NAME} / the value of "
            f"{_BREAKGLASS_PASSWORD_KEY} in {engine_env}"
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
    seed_logins = _seed_logins(values=values)
    for username, key, account_password in seed_logins:
        shown = account_password if reveal else f"the value of {key} in {engine_env}"
        lines.append(f"    seed account {username} / {shown}")
    if seed_logins and not (reveal):
        lines.append(_NOT_PRINTED)
    lines.extend(fixture_lines(values=values))
    lines.append("    show again   make creds")
    lines.append("")
    lines.append("  Next")
    lines.append("    1. Finish the first-run wizard in the console.")
    lines.append(
        "    2. Retire the bootstrap admin from its last step once your own admin "
        f"exists, then delete {_ADMIN_PASSWORD_KEY} from {engine_env}."
    )
    lines.append(
        "    3. Keep the break-glass password offline and delete its plaintext -- "
        "the engine keeps only the hash."
    )
    lines.append("")
    return lines


def _password_cell(*, password: str, key: str) -> str:
    """A password table cell, or what to run when the deployment has not minted one."""
    return (
        f"`{password}`" if password else f"NOT MINTED -- run `make init` to mint {key}"
    )


def summary_markdown(*, values: dict[str, str]) -> str:
    """The access summary as a file: the minted passwords and what to do next.

    The plaintext is the point -- this is the artefact an operator reads once
    while finishing setup, and the closing steps are what let them delete it.

    The one line dfe-infra's file does not carry is the env/engine.env note:
    deleting this file is the whole clean-up after a kubernetes deploy, but here
    env/engine.env keeps its own copy of both passwords and has to be cleaned up as
    well.
    """
    admin = values.get(_ADMIN_NAME_KEY, "").strip() or "admin"
    admin_password = values.get(_ADMIN_PASSWORD_KEY, "").strip()
    breakglass_password = values.get(_BREAKGLASS_PASSWORD_KEY, "").strip()
    console = _url(values=values, port_key="DFE_UI_PORT", default_port="3000")
    api = _url(
        default_port="8003", port_key="DFE_ENGINE_PORT", proxied=False, values=values
    )
    engine_env = _rel_path(path=_env_file(component=ENGINE_COMPONENT))
    seed_logins = _seed_logins(values=values)
    lines = [
        _SUMMARY_HEADING,
        "",
        f"- Console: {console}",
        f"- Engine API: {api}",
        "",
        "| Account | Username | Password |",
        "|---------|----------|----------|",
        f"| {_ADMIN_LABEL} | `{admin}` | "
        f"{_password_cell(password=admin_password, key=_ADMIN_PASSWORD_KEY)} |",
        f"| {_BREAKGLASS_LABEL} | `{_BREAKGLASS_NAME}` | "
        f"{_password_cell(password=breakglass_password, key=_BREAKGLASS_PASSWORD_KEY)} |",
    ]
    lines += [
        f"| {_SEED_LABEL} | `{username}` | `{password}` |"
        for username, _key, password in seed_logins
    ]
    lines += [
        "",
        f"Both are also in `{engine_env}`, as `{_ADMIN_PASSWORD_KEY}` and "
        f"`{_BREAKGLASS_PASSWORD_KEY}` -- delete those two keys as well as this file. "
        "`make creds` prints the admin one again on a terminal.",
        "",
    ]
    if seed_logins:
        keys = ", ".join(f"`{key}`" for _username, key, _password in seed_logins)
        lines += [
            f"Each seed account's password stays in `{engine_env}` as {keys}, so "
            "`make accounts` can create the account again after a volume wipe.",
            "",
        ]
    lines += [f"{number}. {step}" for number, step in enumerate(_NEXT_STEPS, start=1)]
    return "\n".join(lines) + "\n"


def write_summary(*, values: dict[str, str], path: Path) -> Path:
    """Write the summary 0600; a rerun reasserts the mode on an existing file."""
    write_private(path=path, text=summary_markdown(values=values))
    return path


def main(argv: list[str] | None = None) -> int:
    write = "--write" in (argv if argv is not None else sys.argv[1:])
    if not (DOTENV_FILE.is_file()):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg="Missing -- run `make init` to mint this deployment's credentials",
        )
        return 1
    values = access_values()
    reveal = show_password(
        is_tty=sys.stdout.isatty(), setting=os.environ.get(_SHOW_KEY, "")
    )
    for line in summary_lines(values=values, reveal=reveal):
        # codeql[py/clear-text-logging-sensitive-data] `make creds` exists to show the admin login, on a terminal only
        print(line)
    if write:
        path = write_summary(values=values, path=ACCESS_SUMMARY_FILE)
        print(
            f"    file         {_rel_path(path=path)} -- both passwords in plaintext, "
            "delete it when you are done"
        )
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
