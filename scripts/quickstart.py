#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/quickstart.py
#  Purpose:      Give `make up` and `make demo` a working .env, a service profile and a pinned stack from a bare clone
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Prepare a bare clone for `make up` and `make demo`.

`prepare` settles two choices, prints them as its first line, and writes them to .env.

The profile is PROFILE= on the make line, else DFE_PROFILE from the environment or .env, else DEFAULT_PROFILE. One that came from the make line or the default is written to .env as DFE_PROFILE, replacing the line there, so later runs keep it.

The stack is VERSION= on the make line. With none given, a .env that holds no image pins gets the stack in DFE_STACK_VERSION, else the newest published stable stack, pinned by its version number. A .env that holds pins is left as it is.

`make demo` also refuses, before anything starts, a profile that runs no dfe-transform-elastic, and never moves a running stack to another profile.

`running` exits 0 when this checkout has a running dfe-receiver, which is how `make demo` knows whether to start the stack first.

Exit codes:
  0  prepared, or the stack is running
  1  a choice cannot be made, or `running` found no stack
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


def choose_profile(
    *, default: str, dotenv: str, environ: str, explicit: str
) -> tuple[str, str]:
    """The profile to run, and which of the make line, environment, .env or default gave it."""
    for source, value in (
        ("make line", explicit),
        ("environment", environ),
        (".env", dotenv),
    ):
        if value.strip():
            return value.strip(), source
    return default, "default"


def choose_stack(*, dotenv: dict[str, str], explicit: str) -> tuple[str, str]:
    """The stack to pin and which source gave it, or an empty stack when the .env pins stay."""
    if explicit.strip():
        return explicit.strip(), "make line"
    if dotenv.get(IMAGE_PIN_KEY, "").strip():
        return "", ".env"
    recorded = dotenv.get(stack.STACK_VERSION_KEY, "").strip()
    if recorded:
        return recorded, ".env"
    return NEWEST, "default"


def refusal(*, apps: list[str], name: str, running: str) -> str:
    """Why `make demo` cannot use this profile, empty when it can."""
    if TRANSFORM_APP in apps:
        return ""
    if running:
        return (
            f"the stack running here is profile {running}, which runs no {TRANSFORM_APP}, "
            "and the demo needs it. Run `make down`, then `make demo PROFILE=single-kafka`"
        )
    return (
        f"profile {name} runs no {TRANSFORM_APP}, which the demo needs. Use "
        "`make demo PROFILE=single-kafka` or `make demo PROFILE=single-grpc`"
    )


def running() -> bool:
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


def prepare(*, args: argparse.Namespace) -> None:
    profiles_text = SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace")
    valid = list(resolve_profile.parse_yaml(text=profiles_text)["profiles"])
    dotenv = _dotenv_values()
    name, profile_from = choose_profile(
        default=args.default_profile,
        dotenv=dotenv.get(PROFILE_KEY, ""),
        environ=os.environ.get(PROFILE_KEY, ""),
        explicit=args.profile,
    )
    if name not in valid:
        raise QuickstartError(f"profile {name!r} is not one of {', '.join(valid)}")
    requested, stack_from = (
        ("", ".env") if args.dev else choose_stack(dotenv=dotenv, explicit=args.version)
    )
    version = (
        stack._stack_version(requested=requested)
        if stack_from == "default"
        else requested
    )
    held = "the pins already in .env" if not version else f"{version} ({stack_from})"
    print(
        f"make {args.command}: profile {name} ({profile_from}), stack {held}. "
        f"Override with: make {args.command} PROFILE=<profile> VERSION=<stack version>",
        flush=True,
    )

    if args.command == "demo":
        up = running()
        if up and args.profile and args.profile != dotenv.get(PROFILE_KEY, ""):
            raise QuickstartError(
                f"a stack is running here, and PROFILE={args.profile} would move it off "
                f"{dotenv.get(PROFILE_KEY) or 'its profile'}. Run `make down` first"
            )
        reason = refusal(
            apps=enabled_apps(profile_name=name, text=profiles_text),
            name=name,
            running=name if up else "",
        )
        if reason:
            raise QuickstartError(reason)
    if args.dev:
        return

    if not DOTENV_FILE.is_file():
        init.main()
    if profile_from in ("make line", "default") and dotenv.get(PROFILE_KEY) != name:
        text = init._set_live(
            text=DOTENV_FILE.read_text(encoding="utf-8"), key=PROFILE_KEY, value=name
        )
        write_dotenv(path=DOTENV_FILE, text=text)
    if version:
        pinned = subprocess.run(
            [sys.executable, str(Path(stack.__file__)), version], check=False
        )
        if pinned.returncode != 0:
            raise QuickstartError(f"pinning stack {version} failed, see above")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    actions = parser.add_subparsers(dest="action", required=True)
    prep = actions.add_parser("prepare", help="settle the profile and stack in .env")
    prep.add_argument("command", choices=("up", "demo"), help="the make target")
    prep.add_argument("--default-profile", required=True, help="when none is set")
    prep.add_argument("--profile", default="", help="PROFILE= from the make line")
    prep.add_argument("--version", default="", help="VERSION= from the make line")
    prep.add_argument(
        "--dev",
        action="store_true",
        help="a `make dev` stack: check the profile, write nothing",
    )
    actions.add_parser("running", help="exit 0 when this checkout's stack runs")
    args = parser.parse_args()
    if args.action == "running":
        return 0 if running() else 1
    try:
        prepare(args=args)
    except (QuickstartError, RegistryError, stack.StackError) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
