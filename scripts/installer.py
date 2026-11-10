#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/installer.py
#  Purpose:      The dfe-docker image's entrypoint: set up and keep a folder a plain `docker compose` runs the DFE stack from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Set up and keep a folder that a plain `docker compose up -d` runs the DFE stack from.

The dfe-docker image runs this with the folder mounted at /dfe:

  docker run --rm -v "${PWD}:/dfe" ghcr.io/hyperi-io/dfe-docker init [--profile <name>]
  docker compose up -d

`init` copies the image's tree into an empty folder, mints .env and env/ with init.py, pins the profile and the stack with quickstart.py and stack.py as `make up` does, and writes the compose settings below. On a folder that already holds an install it refuses unless `--upgrade`, which copies the tree again, re-pins the stack and keeps every secret and setting. The stack is `--stack`, else the one the image was built for: a release's version tag pins the stack that release was cut against, and `latest` takes the newest published stack.

The compose settings are a block at the end of .env, between two marker lines, which `apply` rewrites. It holds what make otherwise hands compose: what `make print-compose-env` prints, .tmp/profile.env, COMPOSE_FILE and COMPOSE_PROFILES. A plain `docker compose up` starts every service in an active compose profile, where make names the services it starts, so the chain ends with .tmp/docker-compose.image.yml. That fragment moves each service the profile does not start, and that none of its services depends on, out of the active profiles. The block is checked by asking a plain `docker compose config --services` for exactly the services `make ci` names and their dependencies, and .env is put back when it answers anything else.

`apply` rewrites the block after an edit to the settings above it, or after dfe-engine has declared or removed a per-source instance. `login` prints the access summary. `certs` mints the console TLS certificate. `demo` and `demo-stop` run `make demo-run` and `make demo-stop` against the running stack. Both need the Docker socket and the folder mounted at its own path, because compose hands the Docker host the bind-mount paths it sees. `demo` also joins the stack's network, where its row counts reach ClickHouse.

Two phases. The image's copy checks the mount, takes on the owner of the folder, so a Linux host gets files it owns, and copies the tree for `init`. It then runs the folder's own copy, so a folder's code always drives that folder's files.

Exit codes:
  0  done
  1  a step failed, or the folder is not one this command can run in
  2  usage error
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import demo  # noqa: E402
import resolve_profile  # noqa: E402
import stack  # noqa: E402
from _common import (  # noqa: E402
    COMPOSE_FILE,
    DOTENV_FILE,
    PROFILE_MK,
    REPO_ROOT,
    SERVICE_PROFILES_FILE,
    _dotenv_values,
    _parse_dotenv,
    write_dotenv,
)
from _registry import RegistryError  # noqa: E402

# The folder `init`, `apply`, `login` and `certs` work on, as the user mounts it
MOUNT = Path("/dfe")
# Set on the folder's own copy, which does the work
PHASE_KEY = "DFE_DOCKER_PHASE"
WORK_PHASE = "work"
# Set by the image build: the stack `init` pins when no --stack is given, and the reference the printed commands name
STACK_KEY = "DFE_DOCKER_STACK"
IMAGE_KEY = "DFE_DOCKER_IMAGE"
DEFAULT_IMAGE = "ghcr.io/hyperi-io/dfe-docker"
NEWEST_STACK = "latest"
DOCKER_SOCKET = Path("/var/run/docker.sock")

BLOCK_BEGIN = "## BEGIN dfe-docker: written by init and apply from the settings above. Edit those, then run apply"
BLOCK_END = "## END dfe-docker"
# Every service the profile does not start moves to this compose profile, which only `--profile '*'` activates
PARKED_PROFILE = "off"
OVERLAY = Path(".tmp") / "docker-compose.image.yml"
# The separator COMPOSE_FILE is written with, set in .env too so compose on Windows splits it the same way
PATH_SEPARATOR = ":"
COMPOSE_TIMEOUT_SECONDS = 120
# How /proc/self/mountinfo writes a space, a tab, a newline or a backslash in a path
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
# What Finder and Explorer write into a folder someone has only looked at
_FILE_MANAGER_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


class InstallerError(Exception):
    """A step that could not be done, with what to do about it."""


def run_command(*, image: str, args: Sequence[str]) -> str:
    """The `docker run` that runs one command of the image against the folder in the current directory."""
    return " ".join(['docker run --rm -v "${PWD}:/dfe"', image, *args])


def login_command(*, image: str) -> str:
    """The `docker run` that prints the access summary, the admin password included, on a terminal."""
    return f'docker run --rm -t -v "${{PWD}}:/dfe" {image} login'


def demo_command(*, image: str, command: str = "demo") -> str:
    """The `docker run` for `demo` or `demo-stop`: the Docker socket, and the folder at its own path."""
    return (
        'docker run --rm -v "${PWD}:${PWD}" -w "${PWD}" '
        f"-v /var/run/docker.sock:/var/run/docker.sock {image} {command}"
    )


def strip_block(*, text: str) -> str:
    """Return .env text without the block init and apply write, refusing a block that has lost its end marker."""
    lines = text.splitlines()
    if BLOCK_BEGIN not in lines:
        return text
    start = lines.index(BLOCK_BEGIN)
    if BLOCK_END not in lines[start:]:
        raise InstallerError(
            f".env holds the line {BLOCK_BEGIN!r} and not {BLOCK_END!r} after it. "
            "Delete the block from that line down, then run apply"
        )
    end = lines.index(BLOCK_END, start)
    # The blank line with_block puts before the block goes with it
    head = lines[:start]
    while head and not (head[-1].strip()):
        head.pop()
    kept = head + lines[end + 1 :]
    while kept and not (kept[-1].strip()):
        kept.pop()
    return "\n".join(kept) + "\n" if kept else ""


def with_block(*, lines: Sequence[str], text: str) -> str:
    """Return .env text with the block holding `lines` at its end, after one blank line."""
    return (
        strip_block(text=text)
        + "\n"
        + "\n".join([BLOCK_BEGIN, *lines, BLOCK_END])
        + "\n"
    )


def parked_overlay(*, services: Sequence[str]) -> str:
    """The compose fragment that moves each of `services` out of every profile a plain `docker compose up` activates."""
    lines = [
        "# Generated by scripts/installer.py from the resolved profile. Not committed.",
        "# Each service here is one the profile does not start and none of its services",
        f"# depends on, moved to the `{PARKED_PROFILE}` profile so a plain `docker compose up`",
        "# leaves it alone.",
        "services:",
    ]
    for service in sorted(services):
        lines += [f"  {service}:", f'    profiles: !override ["{PARKED_PROFILE}"]']
    return "\n".join(lines) + "\n"


def parse_settings(*, text: str) -> dict[str, str]:
    """The KEY=value lines `make print-compose-env` prints, in order."""
    settings: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not (key.startswith("#")):
            settings[key.strip()] = value.strip()
    return settings


def mounted(*, mountinfo: str, path: Path) -> bool:
    """Whether /proc/self/mountinfo lists `path` as a mount point."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        point = _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match[1], 8)), fields[4])
        if point == str(path):
            return True
    return False


def folder_state(*, target: Path) -> str:
    """`install` when the folder holds one, `empty` when it holds nothing but what a desktop's file manager leaves, else `other`."""
    if (target / DOTENV_FILE.name).is_file() or (target / COMPOSE_FILE.name).is_file():
        return "install"
    held = [
        entry for entry in target.iterdir() if entry.name not in _FILE_MANAGER_FILES
    ]
    return "other" if held else "empty"


def refusal(*, image: str, state: str, upgrade: bool) -> str:
    """Why `init` cannot run on a folder in this state, empty when it can."""
    if state == "install" and not (upgrade):
        return (
            "this folder already holds a DFE install. To move it to this image's stack "
            "and keep its secrets and settings, run: "
            + run_command(image=image, args=["init", "--upgrade"])
        )
    if state != "install" and upgrade:
        return "there is no install here to upgrade. Run: " + run_command(
            image=image, args=["init"]
        )
    if state == "other":
        return "this folder is not empty. Run init in an empty folder, so nothing in it is overwritten"
    return ""


def profile_refusal(*, name: str, profiles: Sequence[str]) -> str:
    """Why `name` is not a profile this tree runs, empty when it is or none is named."""
    if not (name) or name in profiles:
        return ""
    return f"no profile {name!r}. Choose one of: {', '.join(profiles)}"


def _image() -> str:
    """The image reference the printed commands name."""
    return (os.environ.get(IMAGE_KEY, "").strip()) or (DEFAULT_IMAGE)


def _run(
    *,
    args: Sequence[str],
    capture: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command in the tree, raising with its output when it fails."""
    result = subprocess.run(
        list(args),
        capture_output=capture,
        check=False,
        cwd=REPO_ROOT,
        encoding="utf-8",
        env=dict(os.environ if env is None else env),
        errors="replace",
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise InstallerError(
            f"`{' '.join(args)}` failed (exit {result.returncode})"
            + (f": {detail}" if detail else ", see above")
        )
    return result


def _say(msg: str) -> None:
    """One line of progress for the person who ran the image."""
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# The image's copy: check the folder, become its owner, copy the tree, hand over
# ---------------------------------------------------------------------------


def _profiles() -> list[str]:
    """The profile names this tree's service_profiles.yaml declares, read with the resolver's own parser."""
    parsed = resolve_profile.parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )
    profiles = parsed.get(resolve_profile.PROFILE_LIST_YAML_FIELD)
    return list(profiles) if isinstance(profiles, dict) else []


def _become_owner(*, groups: Sequence[int], target: Path) -> None:
    """Take on the user and group that own `target`, adding `groups`, so what this writes there is theirs.

    A folder root owns, as Docker Desktop can present a mount, keeps root.
    """
    if os.geteuid() != 0:
        return
    owner = target.stat()
    if owner.st_uid == 0:
        return
    os.setgroups(sorted({owner.st_gid, *groups}))
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


def _copy_tree(*, target: Path) -> None:
    """Copy this tree into the folder, replacing the files it ships and leaving every other one."""
    for entry in sorted(REPO_ROOT.iterdir()):
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)


def _empty(*, target: Path) -> None:
    """Remove everything in a folder this run found empty."""
    for entry in target.iterdir():
        if entry.is_dir() and not (entry.is_symlink()):
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _host_path(*, target: Path) -> str:
    """The path the Docker host holds the folder this container sees at `target` by, empty when the daemon cannot say."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", socket.gethostname()],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=COMPOSE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return ""
    try:
        mounts = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    for mount in mounts if isinstance(mounts, list) else []:
        if isinstance(mount, dict) and mount.get("Destination") == str(target):
            return str(mount.get("Source", ""))
    return ""


def _socket_groups() -> list[int]:
    """The Docker socket's group where that group may use it, so the folder's owner reaches the daemon."""
    info = DOCKER_SOCKET.stat()
    group_rw = stat.S_IRGRP | stat.S_IWGRP
    return [info.st_gid] if (info.st_mode & group_rw) == group_rw else []


def _check_demo(*, command: str, image: str, target: Path) -> list[int]:
    """Refuse a demo run without the socket or with the folder off its own path, and return the groups that reach the daemon."""
    hint = demo_command(image=image, command=command)
    if not (DOCKER_SOCKET.exists()):
        raise InstallerError(
            f"{command} drives the stack through the Docker socket. Run: {hint}"
        )
    held = _host_path(target=target)
    if held and held != str(target):
        raise InstallerError(
            f"{command} needs the folder mounted at {held}, the path the Docker host holds it by, "
            f"because compose hands the host the paths it sees. Run: {hint}"
        )
    return _socket_groups()


def _bootstrap(*, args: argparse.Namespace, argv: Sequence[str]) -> int:
    """Check the folder, become its owner, copy the tree for init, then run the folder's own copy."""
    image = _image()
    demo = args.command in ("demo", "demo-stop")
    target = Path.cwd() if demo else MOUNT
    if not (
        mounted(
            mountinfo=Path("/proc/self/mountinfo").read_text(encoding="utf-8"),
            path=target,
        )
    ):
        hint = (
            demo_command(image=image, command=args.command)
            if demo
            else run_command(image=image, args=[args.command])
        )
        raise InstallerError(f"no folder is mounted at {target}. Run: {hint}")

    state = folder_state(target=target)
    if args.command == "init":
        reason = refusal(
            image=image, state=state, upgrade=args.upgrade
        ) or profile_refusal(name=args.profile, profiles=_profiles())
        if reason:
            raise InstallerError(reason)
    elif state != "install":
        raise InstallerError(
            f"no DFE install here. Run: {run_command(image=image, args=['init'])}"
        )
    groups = (
        _check_demo(command=args.command, image=image, target=target) if demo else []
    )

    _become_owner(groups=groups, target=target)
    if args.command == "init":
        _say(
            f"{'Upgrading' if args.upgrade else 'Setting up'} the DFE install in this folder"
        )
        _copy_tree(target=target)
    home = tempfile.mkdtemp(prefix="dfe-home-")
    try:
        done = subprocess.run(
            [sys.executable, str(target / "scripts" / Path(__file__).name), *argv],
            check=False,
            cwd=target,
            env={**os.environ, "HOME": home, PHASE_KEY: WORK_PHASE, IMAGE_KEY: image},
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if done.returncode != 0 and args.command == "init" and state == "empty":
        _empty(target=target)
        _say("init did not finish, so the folder is empty again")
    return done.returncode


# ---------------------------------------------------------------------------
# The folder's own copy: do the work against this tree
# ---------------------------------------------------------------------------


def _make(
    *, args: Sequence[str], capture: bool = False, env: Mapping[str, str] | None = None
) -> str:
    """Run one make goal in the tree and return what it printed when captured."""
    return _run(
        args=["make", "--no-print-directory", *args], capture=capture, env=env
    ).stdout


def in_stack_clickhouse(*, settings: Mapping[str, str]) -> str:
    """The ClickHouse HTTP address the stack's own services dial, as docker-compose.yml builds it for dfe-loader."""
    host = (settings.get("CLICKHOUSE_HOST", "").strip()) or ("clickhouse")
    port = (settings.get("CLICKHOUSE_EXTERNAL_HTTP_PORT", "").strip()) or ("8123")
    return f"http://{host}:{port}"


def _join_stack() -> dict[str, str]:
    """Put this container on the stack's network and return the environment that points the demo's row counts at ClickHouse there.

    The counts dial ClickHouse's published port on localhost, which in a container is the container itself. A CLICKHOUSE_URL in .env or the environment is kept.
    """
    settings = _dotenv_values()
    if (os.environ.get("CLICKHOUSE_URL", "").strip()) or (
        settings.get("CLICKHOUSE_URL", "").strip()
    ):
        return dict(os.environ)
    network = demo._network(service=demo.RECEIVER_SERVICE)
    joined = subprocess.run(
        ["docker", "network", "connect", network, socket.gethostname()],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=COMPOSE_TIMEOUT_SECONDS,
    )
    if joined.returncode != 0 and "already exists" not in joined.stderr:
        raise InstallerError(
            f"cannot join the stack's network {network!r}: {joined.stderr.strip()}"
        )
    return {**os.environ, "CLICKHOUSE_URL": in_stack_clickhouse(settings=settings)}


def _script(*, args: Sequence[str], env: Mapping[str, str] | None = None) -> None:
    """Run one of the tree's scripts."""
    _run(
        args=[sys.executable, str(REPO_ROOT / "scripts" / args[0]), *args[1:]],
        env=env,
    )


@contextlib.contextmanager
def _settings_only() -> Iterator[str]:
    """Hold .env without the block while the body runs, so no value the last run derived feeds this one.

    Yields what .env held before, and puts it back when the body raises.
    """
    before = DOTENV_FILE.read_text(encoding="utf-8")
    settings = strip_block(text=before)
    if settings != before:
        write_dotenv(path=DOTENV_FILE, text=settings)
    try:
        yield before
    except BaseException:
        write_dotenv(path=DOTENV_FILE, text=before)
        raise


def _model(
    *,
    env: Mapping[str, str],
    files: Sequence[str],
    profiles: Sequence[str],
    services: Sequence[str] = (),
) -> dict:
    """The services of the compose model the files resolve to with these profiles, only `services` and their dependencies when it names some."""
    args = ["docker", "compose"]
    args += [arg for name in files for arg in ("-f", name)]
    args += [arg for name in profiles for arg in ("--profile", name)]
    args += ["config", "--format", "json", *services]
    out = _run(args=args, capture=True, env=env, timeout=COMPOSE_TIMEOUT_SECONDS).stdout
    try:
        model = json.loads(out)
    except json.JSONDecodeError as error:
        raise InstallerError(
            f"`docker compose config` returned no model: {error}"
        ) from error
    found = model.get("services", {}) if isinstance(model, dict) else {}
    return found if isinstance(found, dict) else {}


def _block_lines() -> tuple[list[str], set[str]]:
    """The block for the settings in .env, and the services a plain `docker compose up` must start with it."""
    printed = parse_settings(text=_make(args=["print-compose-env"], capture=True))
    for key in ("COMPOSE_FILE", "COMPOSE_PROFILES"):
        if key not in printed:
            raise InstallerError(f"`make print-compose-env` printed no {key}")
    profile_env = (
        PROFILE_MK.with_name(resolve_profile.PROFILE_ENV_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    resolved = _parse_dotenv(text="\n".join(profile_env))
    services = resolved.get("DFE_SERVICES", "").split()
    if not (services):
        raise InstallerError(
            ".tmp/profile.env names no DFE_SERVICES, so which services run is unknown"
        )
    derived = {
        key: value for key, value in printed.items() if not (key.startswith("COMPOSE_"))
    }
    # Compose reads the settings from .env itself, and the derived values arrive the way make exports them
    env = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("COMPOSE_"))
    }
    env |= resolved | derived
    files = [name for name in printed["COMPOSE_FILE"].split(PATH_SEPARATOR) if name]
    base = [name for name in printed["COMPOSE_PROFILES"].split(",") if name]

    # Naming the services, as `make ci` does, brings in their dependencies with each one's profiles
    started = _model(env=env, files=files, profiles=base, services=services)
    profiles = sorted(
        set(base)
        | {name for service in started.values() for name in service.get("profiles", [])}
    )
    active = _model(env=env, files=files, profiles=profiles)
    parked = sorted(set(active) - set(started))
    overlay = REPO_ROOT / OVERLAY
    if parked:
        overlay.write_text(
            parked_overlay(services=parked), encoding="utf-8", newline="\n"
        )
        files.append(OVERLAY.as_posix())
    else:
        overlay.unlink(missing_ok=True)
    lines = [
        *profile_env,
        *[f"{key}={value}" for key, value in derived.items()],
        f"COMPOSE_PATH_SEPARATOR={PATH_SEPARATOR}",
        f"COMPOSE_FILE={PATH_SEPARATOR.join(files)}",
        f"COMPOSE_PROFILES={','.join(profiles)}",
    ]
    return lines, set(started)


def _write_block() -> None:
    """Rewrite the compose settings in .env, putting .env back when a plain `docker compose` would start anything else."""
    with _settings_only() as before:
        lines, wanted = _block_lines()
        write_dotenv(path=DOTENV_FILE, text=with_block(lines=lines, text=before))
        # Only what a person's shell carries, so compose takes every setting from .env
        shell = {
            key: os.environ[key]
            for key in ("DOCKER_HOST", "HOME", "PATH")
            if key in os.environ
        }
        listed = _run(
            args=["docker", "compose", "config", "--services"],
            capture=True,
            env=shell,
            timeout=COMPOSE_TIMEOUT_SECONDS,
        ).stdout
        got = set(listed.split())
        if got != wanted:
            raise InstallerError(
                "a plain `docker compose up` would not start what the profile starts. "
                f"Missing: {', '.join(sorted(wanted - got)) or 'none'}. "
                f"Extra: {', '.join(sorted(got - wanted)) or 'none'}"
            )
    _say(
        f"Wrote the compose settings into .env: `docker compose up -d` starts {len(wanted)} services"
    )


def _init(*, args: argparse.Namespace, image: str) -> None:
    """Mint .env and env/, pin the profile and the stack as `make up` does, then write the compose settings."""
    _script(args=["init.py"])
    requested = (
        (args.stack.strip())
        or (os.environ.get(STACK_KEY, "").strip())
        or (NEWEST_STACK)
    )
    with _settings_only():
        # A first install of the newest stack is quickstart.py's own default, which names it so
        fresh = requested == NEWEST_STACK and not (args.upgrade)
        version = "" if fresh else stack._stack_version(requested=requested)
        default = _make(args=["-s", "print-default-profile"], capture=True).strip()
        _script(
            args=[
                "quickstart.py",
                "prepare",
                "up",
                "--caller",
                "init",
                "--default-profile",
                default,
                "--profile",
                args.profile,
                "--version",
                version,
            ]
        )
    _write_block()
    _script(
        args=["creds.py", "--write"],
        env={**os.environ, "DFE_CREDS_COMMAND": login_command(image=image)},
    )
    _say("Start the stack in this folder with: docker compose up -d")
    _say(f"Show the admin password on a terminal with: {login_command(image=image)}")


def _demo(*, command: str, image: str) -> None:
    """Run `make demo-run` or `make demo-stop` against the running stack, then rewrite the block for the instances it changed."""
    if command == "demo":
        running = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "quickstart.py"), "running"],
            check=False,
            cwd=REPO_ROOT,
        )
        if running.returncode != 0:
            raise InstallerError(
                "the stack is not running here. Start it with `docker compose up -d`, then run demo again"
            )
    try:
        with _settings_only():
            if command == "demo":
                _make(args=["demo-run"], env=_join_stack())
            else:
                _make(args=["demo-stop"])
    finally:
        _write_block()
    if command == "demo":
        _say(
            "The stream runs until demo-stop, which `docker compose down` needs first: "
            + demo_command(image=image, command="demo-stop")
        )


def _work(*, args: argparse.Namespace) -> int:
    """Run one command on the tree this copy lives in."""
    image = _image()
    if args.command == "init":
        _init(args=args, image=image)
    elif args.command == "apply":
        _write_block()
        _say("Bring the stack in line with: docker compose up -d --remove-orphans")
    elif args.command == "login":
        _script(
            args=["creds.py"],
            env={**os.environ, "DFE_CREDS_COMMAND": login_command(image=image)},
        )
    elif args.command == "certs":
        _make(args=["certs"])
    else:
        _demo(command=args.command, image=image)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dfe-docker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="set up an empty folder, or --upgrade one")
    init.add_argument(
        "--profile",
        default="",
        help="the service profile, `make up`'s default when none is set",
    )
    init.add_argument(
        "--stack", default="", help="the stack to pin: X.Y.Z[-rc.N], latest or rc"
    )
    init.add_argument(
        "--upgrade",
        action="store_true",
        help="copy the tree again and re-pin an install, keeping its secrets and settings",
    )
    commands.add_parser(
        "apply",
        help="rewrite the compose settings after an edit to .env or a source change",
    )
    commands.add_parser(
        "login", help="print the access summary, the password on a terminal only"
    )
    commands.add_parser(
        "certs", help="mint the console TLS certificate DFE_PROXY_TLS=true needs"
    )
    commands.add_parser(
        "demo", help="stream the demo's syslog through the running stack"
    )
    commands.add_parser("demo-stop", help="remove everything the demo put in")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(given)
    try:
        if os.environ.get(PHASE_KEY) == WORK_PHASE:
            return _work(args=args)
        return _bootstrap(args=args, argv=given)
    except (
        InstallerError,
        OSError,
        RegistryError,
        demo.DemoError,
        stack.StackError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"{args.command}: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
