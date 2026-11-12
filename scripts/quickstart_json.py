#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/quickstart_json.py
#  Purpose:      Write the quickstart.json a release carries, from service_profiles.yaml, the Makefile and the compose files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write the quickstart.json each dfe-docker release carries as an asset.

dfe-infra's docs generator pastes its strings verbatim, so every command in it is complete and every value comes from a file this repo already reads: the default profile and its transport from the Makefile and service_profiles.yaml, the stack from deployment.example.yaml's version.pin through stack.py, the console URL from the address next-auth is handed in docker-compose.yml, and the commands from the OCI artifacts scripts/publish_compose.py writes and the release publishes beside the dfe-docker image.

Schema 1:

  schema       1
  stack        the stack the release's versioned artifacts install
  min_compose  the oldest Docker Compose that runs a published file: the newest feature the compose files use, and `-f oci://`
  min_engine   the oldest Docker Engine that mounts a volume subpath, which the published files do
  console_url  where the console answers on a default install
  login        the console login a published file issues, which the console makes its owner change
  kind         "compose"
  profiles     [{name, default, transport, ref_latest, ref_pinned}], the one profile the stack file runs, default true
  up, demo     {latest: [command], pinned: [command]}, the one command that starts the stack and the demo
  notes        {up, demo}, what to do once each command has run

`--print-stack` prints the stack alone, which the release workflow builds the version-tagged image with.

Exit codes:
  0  written
  1  a value could not be read from its file
  2  usage error
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import installer  # noqa: E402
import resolve_profile  # noqa: E402
import stack  # noqa: E402
from _common import (  # noqa: E402
    COMPOSE_FILE,
    FIRST_LOGIN_PASSWORD,
    PUBLISHED_DEMO_TAG,
    PUBLISHED_STACK_TAG,
    REPO_ROOT,
    SERVICE_PROFILES_FILE,
)

SCHEMA = 1
# Compose reads `-f oci://` by default from this release (docker/compose pkg/remote/oci.go: COMPOSE_EXPERIMENTAL_OCI_REMOTE defaults false in v2.33.0, true in v2.34.0)
OCI_REMOTE_COMPOSE = "2.34.0"
# VolumeOptions.Subpath, API 1.45 (https://docs.docker.com/engine/release-notes/26.0/)
MIN_ENGINE = "26.0"
ADMIN_NAME = "admin"
# Where next-auth is told the console lives, which is the address a browser must use for a sign-in to come back
_CONSOLE_ORIGIN = re.compile(
    r"NEXTAUTH_URL: \$\{DFE_EXTERNAL_ORIGIN:-(?P<origin>[^}]+)\}:\$\{DFE_UI_PORT:-(?P<port>\d+)\}"
)
# Each compose feature these files can use, the Docker Compose release that first reads it and where that release says so
COMPOSE_FEATURES = (
    (
        "!reset",
        re.compile(r"!reset"),
        "2.18.0",
        "https://github.com/compose-spec/compose-go/pull/380, compose-go v1.13.5 in https://github.com/docker/compose/releases/tag/v2.18.0",
    ),
    (
        "include",
        re.compile(r"(?m)^include:"),
        "2.20.0",
        "https://github.com/docker/compose/releases/tag/v2.20.0",
    ),
    (
        "depends_on.required",
        re.compile(r"(?m)^ +condition: \S+\n +required:"),
        "2.20.2",
        "https://github.com/docker/compose/releases/tag/v2.20.2",
    ),
    (
        "env_file path and required",
        re.compile(r"(?m)^ +- path: \S+\n +required:"),
        "2.24.0",
        "https://github.com/compose-spec/compose-go/pull/509, compose-go v2 in https://github.com/docker/compose/releases/tag/v2.24.0",
    ),
    (
        "!override",
        re.compile(r"!override"),
        "2.24.4",
        "https://github.com/compose-spec/compose-go/pull/552, compose-go v2.0.0-rc.3 in https://github.com/docker/compose/releases/tag/v2.24.4",
    ),
    (
        "volume subpath",
        re.compile(r"(?m)^ +subpath:"),
        "2.26.0",
        "https://github.com/compose-spec/compose-go/pull/594 and docker/compose 3950460703 in https://github.com/docker/compose/releases/tag/v2.26.0",
    ),
)


class QuickstartJsonError(Exception):
    """A value the quickstart cannot be read from its file."""


def _version_key(version: str) -> tuple[int, ...]:
    """A Compose version as numbers, for ordering."""
    return tuple(int(part) for part in version.split("."))


def compose_texts() -> list[str]:
    """Every committed compose file: docker-compose.yml and what it includes, extends and chains."""
    files = [COMPOSE_FILE, *sorted((REPO_ROOT / "compose").glob("*.yml"))]
    return [path.read_text(encoding="utf-8") for path in files]


def min_compose(*, texts: list[str]) -> str:
    """The oldest Docker Compose that reads every feature the texts use."""
    joined = "\n".join(texts)
    used = [
        version for _, pattern, version, _ in COMPOSE_FEATURES if pattern.search(joined)
    ]
    if not (used):
        raise QuickstartJsonError(
            "the compose files use none of the features this knows"
        )
    return max(used, key=_version_key)


def console_url(*, compose_text: str) -> str:
    """Where the console answers on a default install, as dfe-ui's NEXTAUTH_URL defaults it."""
    match = _CONSOLE_ORIGIN.search(compose_text)
    if match is None:
        raise QuickstartJsonError(
            f"{COMPOSE_FILE.name} sets no NEXTAUTH_URL from DFE_EXTERNAL_ORIGIN and DFE_UI_PORT"
        )
    return f"{match['origin']}:{match['port']}"


def default_profile() -> str:
    """The profile `make up` and init start when none is chosen."""
    result = subprocess.run(
        ["make", "--no-print-directory", "-s", "print-default-profile"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    name = result.stdout.strip()
    if result.returncode != 0 or not (name):
        raise QuickstartJsonError(
            f"`make print-default-profile` printed no profile: {result.stderr.strip()}"
        )
    return name


def artifact(*, image: str, tag: str, version: str = "") -> str:
    """One published compose file's reference, its moving tag or that tag for one release."""
    return f"{image}:{tag}-{version}" if version else f"{image}:{tag}"


def compose_command(*, ref: str, verb: str) -> str:
    """The docker compose command that runs `verb` on a published file."""
    return f"docker compose -f oci://{ref} {verb}"


def notes(*, console: str, image: str) -> dict[str, str]:
    """What the quickstart tells someone once each command has run."""
    login = (
        f"Point your browser to {console} and log in as `{ADMIN_NAME}` with password "
        f"`{FIRST_LOGIN_PASSWORD}`. The console makes you change it at first login."
    )
    stack_down = compose_command(
        ref=artifact(image=image, tag=PUBLISHED_STACK_TAG), verb="down"
    )
    demo_down = compose_command(
        ref=artifact(image=image, tag=PUBLISHED_DEMO_TAG), verb="down -v"
    )
    return {
        "up": (
            f"{login} Every port listens on 127.0.0.1 only. `{stack_down}` stops the "
            "stack and keeps its data."
        ),
        "demo": (
            "A throwaway stack that seeds its own sources and streams vendor syslog "
            f"into one table per vendor, beside its fetchers. {login} It takes the "
            f"Docker install's ports, so stop that first, and `{demo_down}` removes it all."
        ),
    }


def quickstart(
    *, default: str, image: str, profiles: dict, stack_version: str, version: str
) -> dict:
    """The quickstart.json document for one release."""
    if default not in profiles:
        raise QuickstartJsonError(
            f"the default profile {default!r} is not in {SERVICE_PROFILES_FILE.name}"
        )
    body = profiles[default]
    console = console_url(compose_text=COMPOSE_FILE.read_text(encoding="utf-8"))
    refs = {
        kind: {
            "latest": artifact(image=image, tag=tag),
            "pinned": artifact(image=image, tag=tag, version=version),
        }
        for kind, tag in (("up", PUBLISHED_STACK_TAG), ("demo", PUBLISHED_DEMO_TAG))
    }
    return {
        "schema": SCHEMA,
        "stack": stack_version,
        "min_compose": max(
            min_compose(texts=compose_texts()), OCI_REMOTE_COMPOSE, key=_version_key
        ),
        "min_engine": MIN_ENGINE,
        "console_url": console,
        "login": f"{ADMIN_NAME} / {FIRST_LOGIN_PASSWORD}",
        "kind": "compose",
        "profiles": [
            {
                "name": default,
                "default": True,
                "transport": body.get("transport", "")
                if isinstance(body, dict)
                else "",
                "ref_latest": refs["up"]["latest"],
                "ref_pinned": refs["up"]["pinned"],
            }
        ],
        **{
            kind: {
                form: [compose_command(ref=ref, verb="up -d")]
                for form, ref in forms.items()
            }
            for kind, forms in refs.items()
        },
        "notes": notes(console=console, image=image),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--image", default=installer.DEFAULT_IMAGE, help="the image repository"
    )
    parser.add_argument("--out", type=Path, help="where to write it, else stdout")
    parser.add_argument(
        "--print-stack", action="store_true", help="print the stack alone"
    )
    parser.add_argument(
        "--version", default="", help="the release version, without the v"
    )
    args = parser.parse_args()
    try:
        stack_version = stack._dial_pin()
        if args.print_stack:
            print(stack_version)
            return 0
        if not (args.version.strip()):
            parser.error("--version is needed to name the version-tagged image")
        parsed = resolve_profile.parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        )
        document = quickstart(
            default=default_profile(),
            image=args.image,
            profiles=parsed.get(resolve_profile.PROFILE_LIST_YAML_FIELD, {}),
            stack_version=stack_version,
            version=args.version.strip().removeprefix("v"),
        )
    except (QuickstartJsonError, stack.StackError, OSError) as error:
        print(f"quickstart_json: {error}", file=sys.stderr)
        return 1
    text = json.dumps(document, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
