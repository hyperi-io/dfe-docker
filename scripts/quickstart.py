#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/quickstart.py
#  Purpose:      Give `make up` and `make demo` a working .env, a service profile and a pinned stack from a bare clone
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Prepare a bare clone for `make up` and `make demo`.

`prepare` settles two choices, prints them as its first line and writes them to .env.

The profile is PROFILE= on the make line, else DFE_PROFILE from .env or the environment, else DEFAULT_PROFILE on a clone with no image pins, else the active_profile in service_profiles.yaml, which is what `make ci` runs on an existing deployment. One that came from the make line or the default is written to .env as DFE_PROFILE, replacing the line there, so later runs keep it. The active_profile is never written.

The stack is VERSION= on the make line. With none given, a .env that holds no image pins gets the stack in DFE_STACK_VERSION, else the newest published stable stack, pinned by its version number. A .env that holds pins is left as it is.

`make demo` also refuses, before anything starts, a profile that runs no dfe-transform-elastic and never moves a running stack to another profile.

`running` exits 0 when this checkout has a running dfe-receiver, which is how `make demo` knows whether to start the stack first.

Exit codes:
  0  prepared or the stack is running
  1  a choice cannot be made or `running` found no stack
  2  usage error
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import init  # noqa: E402
import resolve_profile  # noqa: E402
import stack  # noqa: E402
from _common import (  # noqa: E402
    DOTENV_FILE,
    REPO_ROOT,
    SERVICE_PROFILES_FILE,
    _dotenv_values,
    _print,
    write_dotenv,
)
from _registry import RegistryError  # noqa: E402
from _sources import enabled_apps  # noqa: E402

PROFILE_KEY = resolve_profile.PROFILE_ENV_VAR
TRANSFORM_APP = "dfe-transform-elastic"
RECEIVER_SERVICE = "dfe-receiver"
# The first image pin `make stack` writes, which marks a .env as already pinned
IMAGE_PIN_KEY = "DFE_ENGINE_VERSION"
NEWEST = "latest"
DOCKER_TIMEOUT_SECONDS = 30


class QuickstartError(Exception):
    """A choice `prepare` cannot make, with what to do about it."""


def _check_demo(
    *, args: argparse.Namespace, dotenv: dict[str, str], name: str, profiles_text: str
) -> None:
    """Refuse a demo this profile or stack cannot run, before anything starts."""
    up = _running()
    if args.dev and not (up):
        raise QuickstartError(
            "DEV=1 streams through a running `make dev` stack and none runs here. "
            "Run `make dev` first or drop DEV=1 to start the registry stack"
        )
    if up and args.profile and args.profile != dotenv.get(PROFILE_KEY, ""):
        raise QuickstartError(
            f"a stack is running here and PROFILE={args.profile!r} would move it to "
            "another profile. Run `make down` first"
        )
    reason = _refusal(
        apps=enabled_apps(profile_name=name, text=profiles_text),
        name=name,
        running=name if up else "",
    )
    if reason:
        raise QuickstartError(reason)


def _choice_line(
    *,
    args: argparse.Namespace,
    name: str,
    profile_from: str,
    stack_from: str,
    version: str,
) -> str:
    """The first line `prepare` prints: the two choices, where each came from and how to override them, in the words of the command that ran it."""
    if args.caller:
        given = {"make line": "given"}
        stack_given = {"make line": args.version_from or "given"}
        caller = args.caller
        override = f"{caller} --profile <profile> --stack <stack version>"
    else:
        given = {}
        stack_given = {}
        caller = f"make {args.command}"
        override = f"make {args.command} PROFILE=<profile> VERSION=<stack version>"
    held = (
        f"{version!r} ({stack_given.get(stack_from, stack_from)})"
        if version
        else "the pins already in .env"
    )
    return (
        f"{caller}: profile {name!r} ({given.get(profile_from, profile_from)}), "
        f"stack {held}. Override with: {override}"
    )


def _choose_profile(
    *, active: str, default: str, dotenv: str, environ: str, explicit: str, pinned: bool
) -> tuple[str, str]:
    """The profile to run and which source gave it.

    Inside make a .env value beats the environment, because make assigns it over the inherited variable. A .env with image pins is an existing deployment, which keeps the active_profile it has been running.
    """
    for source, value in (
        ("make line", explicit),
        (".env", dotenv),
        ("environment", environ),
    ):
        if value.strip():
            return value.strip(), source
    if pinned:
        return active, "active_profile"
    return default, "default"


def _choose_stack(*, dotenv: dict[str, str], explicit: str) -> tuple[str, str]:
    """The stack to pin and which source gave it, with an empty stack when the .env pins stay."""
    if explicit.strip():
        return explicit.strip(), "make line"
    if dotenv.get(IMAGE_PIN_KEY, "").strip():
        return "", ".env"
    recorded = dotenv.get(stack.STACK_VERSION_KEY, "").strip()
    if recorded:
        return recorded, ".env"
    return NEWEST, "default"


def _prepare(*, args: argparse.Namespace) -> None:
    """Settle the profile and stack for one make target and write them to .env."""
    profiles_text = SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace")
    parsed = resolve_profile.parse_yaml(text=profiles_text)
    dotenv = _dotenv_values()
    name, profile_from = _choose_profile(
        active=parsed["active_profile"],
        default=args.default_profile,
        dotenv=dotenv.get(PROFILE_KEY, ""),
        environ=os.environ.get(PROFILE_KEY, ""),
        explicit=args.profile,
        pinned=bool(dotenv.get(IMAGE_PIN_KEY, "").strip()),
    )
    if name not in parsed["profiles"]:
        raise QuickstartError(
            f"profile {name!r} is not one of {', '.join(parsed['profiles'])}"
        )
    requested, stack_from = (
        ("", ".env")
        if args.dev
        else _choose_stack(dotenv=dotenv, explicit=args.version)
    )
    version = (
        stack._stack_version(requested=requested)
        if stack_from == "default"
        else requested
    )
    print(
        _choice_line(
            args=args,
            name=name,
            profile_from=profile_from,
            stack_from=stack_from,
            version=version,
        ),
        flush=True,
    )

    if args.command == "demo":
        _check_demo(args=args, dotenv=dotenv, name=name, profiles_text=profiles_text)
    if args.dev:
        return

    if not (DOTENV_FILE.is_file()) and init.main() != 0:
        raise QuickstartError("`make init` failed, see above")
    if profile_from in ("make line", "default") and dotenv.get(PROFILE_KEY) != name:
        text = init._set_live(
            key=PROFILE_KEY, text=DOTENV_FILE.read_text(encoding="utf-8"), value=name
        )
        write_dotenv(path=DOTENV_FILE, text=text)
    if version:
        pinned = subprocess.run(
            [sys.executable, str(Path(stack.__file__)), version], check=False
        )
        if pinned.returncode != 0:
            raise QuickstartError(f"pinning stack {version!r} failed, see above")


def _refusal(*, apps: list[str], name: str, running: str) -> str:
    """Why `make demo` cannot use this profile, empty when it can."""
    if TRANSFORM_APP in apps:
        return ""
    if running:
        return (
            f"the stack running here is profile {running!r}, which runs no "
            f"{TRANSFORM_APP!r}. The demo needs it, so run `make down`, then "
            "`make demo PROFILE=single-kafka`"
        )
    return (
        f"profile {name!r} runs no {TRANSFORM_APP!r}, which the demo needs. Use "
        "`make demo PROFILE=single-kafka`"
    )


def _running() -> bool:
    """Whether this checkout has a running dfe-receiver."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            "status=running",
            "--filter",
            f"label=com.docker.compose.project.working_dir={REPO_ROOT}",
            "--filter",
            f"label=com.docker.compose.service={RECEIVER_SERVICE}",
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    return result.returncode == 0 and bool(result.stdout.split())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    actions = parser.add_subparsers(dest="action", required=True)
    prep = actions.add_parser("prepare", help="settle the profile and stack in .env")
    prep.add_argument("command", choices=("up", "demo"), help="the make target")
    prep.add_argument(
        "--caller",
        default="",
        help="the command a person ran, named in place of make with its own options",
    )
    prep.add_argument("--default-profile", help="when none is set", required=True)
    prep.add_argument(
        "--dev",
        action="store_true",
        help="a `make dev` stack: check the profile, write nothing",
    )
    prep.add_argument("--profile", default="", help="PROFILE= from the make line")
    prep.add_argument("--version", default="", help="VERSION= from the make line")
    prep.add_argument(
        "--version-from",
        default="",
        help="with --caller, where --version came from when the person did not give it",
    )
    actions.add_parser("running", help="exit 0 when this checkout's stack runs")
    args = parser.parse_args()
    if args.action == "running":
        return 0 if _running() else 1
    try:
        _prepare(args=args)
    except (QuickstartError, RegistryError, stack.StackError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
