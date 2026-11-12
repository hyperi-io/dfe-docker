#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/publish_compose.py
#  Purpose:      Write the stack and demo compose files a release publishes as OCI artifacts, from docker-compose.yml
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write the two compose files a release publishes, so a host with only Docker runs the stack with one command.

  docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose up -d
  docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo up -d

Each is generated, never edited. The tree the dfe-docker image carries is copied into a scratch folder, set up there by scripts/installer.py exactly as the image's `init` sets up a folder, and `docker compose config` resolves what a plain `docker compose up` starts in it. That model is then made publishable:

- every bind mount of a file in the tree becomes a compose config with the file inline, `$` escaped, at the same target. The engine's env/ directory, which nothing outside a checkout reads, is dropped with the setting that names it. The collector's host /proc goes and it reads its own, which shows the host's CPUs.
- no secret ships. Every environment value holding one the scratch install minted leaves the file, and a secrets-init one-shot (scripts/secrets_init.py) mints it on the host at first start into the stack's own volume. Each service that read one gets a directory of that volume holding only its own, read-only, and either a shell that sources its env file before it execs the image's own command, or, for the collector, which has no shell, its config's file provider.
- every published port binds to 127.0.0.1, and no profile, env_file or interpolation is left, so `-f oci://` has nothing to ask.
- the admin's issued password is FIRST_LOGIN_PASSWORD, the one value the file names on purpose, which the engine makes its owner replace at first login.

The stack file is project `dfe`. The demo file is that model with the demo's per-source instances declared, its own project `dfe-demo` and containers prefixed `demo-`, plus the demo overlay: one-shots that seed its sources and console through scripts/demo_seed.py, the syslog stream `make demo` runs and the archive pruner. dfe-receiver and the instances start once the sources exist. secrets-init and the demo's helpers run the dfe-docker image --image names.

`--check FILE...` reads published files and exits 1 on any bind mount, env_file, profile, interpolation, literal secret or port off 127.0.0.1, and with `--require-digests` on any image not pinned by digest.

Exit codes:
  0  written, or the files checked clean
  1  a step failed, or a check found something
  2  usage error
"""

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import demo  # noqa: E402
import init  # noqa: E402
import installer  # noqa: E402
import instances  # noqa: E402
import stack  # noqa: E402
from _common import (  # noqa: E402
    ENGINE_COMPONENT,
    FETCHER_SERVICE,
    FIRST_LOGIN_PASSWORD,
    IN_STACK_KEY,
    PUBLISHED_DEMO_TAG,
    PUBLISHED_STACK_TAG,
    REPO_ROOT,
    _parse_dotenv,
    _print,
    write_dotenv,
)

STACK_PROJECT = "dfe"
DEMO_PROJECT = "dfe-demo"
DEMO_CONTAINER_PREFIX = "demo-"
STACK_FILE = "dfe.yaml"
DEMO_FILE = "dfe-demo.yaml"
DEFAULT_IMAGE = installer.DEFAULT_IMAGE
LOOPBACK = "127.0.0.1"
ADMIN_PASSWORD_KEY = "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD"
BREAKGLASS_PASSWORD_KEY = "DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD"
ENGINE_SERVICE = "dfe-engine"
RECEIVER_SERVICE = "dfe-receiver"
# Where the engine writes env files and instance indexes for a checkout's make to read, which no published install has
APP_ENV_DIR_KEY = "DFE_ENGINE_DEPLOYMENT_APP_ENV_DIR"
ENV_DIR_NAME = "env"
# The one host path the stack binds, which the collector reads the host's CPUs from
HOST_PROC = "/proc"
# The collector's own /proc counts the same host CPUs once the host's is no longer mounted at /hostfs
CONTENT_EDITS = {
    "config/otel-collector/config.yaml": (
        ("    root_path: /hostfs\n", ""),
        ("from its /proc at /hostfs", "from the collector's own /proc"),
    )
}
# The service whose deploy limits and DNS settings the demo's helpers take
SIDECAR_SERVICE = "dlq-init"
TRANSFORM_ELASTIC_CONTRACT = "contract-dfe-transform-elastic"
# Where the dfe-docker image holds this tree
IMAGE_TREE = "/opt/dfe-docker"
DEMO_FEED_VOLUME = "demo-feed"
DEMO_FEED_MOUNT = "/demo-feed"
# A subdirectory, because staging the feed replaces it and a volume's root cannot be removed
DEMO_FEED_SUBPATH = "feed"
DEMO_UID = "1000:1000"
ROOT_UID = "0:0"
SECRETS_SERVICE = "secrets-init"
SECRETS_VOLUME = "dfe-secrets"
SECRETS_ROOT = "/secrets"
SECRETS_MOUNT = "/run/dfe-secrets"
SECRETS_PLAN = "secrets-plan.json"
SECRETS_PLAN_TARGET = "/etc/dfe/secrets-plan.json"
# Each consumer gets a group of its own that can read its directory, because two images name their user rather than give its uid
SECRETS_GID_BASE = 61000
# What a minted value is written as in the plan, which secrets_init.py renders
PLACEHOLDER = "@@{key}@@"
# Sources a consumer's env file, then runs exactly the command the service ran before
WRAPPER = f'set -a && . {SECRETS_MOUNT}/env && set +a && exec "$$@"'
WRAPPER_NAME = "dfe-secrets"
# Services with no shell, which read each secret through their own config's file provider instead
FILE_CONSUMERS = {"otel-collector": "config/otel-collector/config.yaml"}
# Environment names that hold a secret wherever they are set, and not a path to one
_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|KEY|MONGO_URI|IDENTITY)$")
# The values a published file names on purpose
PUBLIC_VALUES = {ADMIN_PASSWORD_KEY: FIRST_LOGIN_PASSWORD}
# A `$` that compose would interpolate: one followed by `{` or a name, where `$$` is the escape
_DOLLAR = re.compile(r"\$(\$|\{|[A-Za-z_])")
# A demo source file's top-level fetcher mapping, which is what makes it a fetcher (demo.py _role)
_FETCHER_KEY = re.compile(r"(?m)^fetcher:\s*$")
_COMPOSE_TIMEOUT_SECONDS = 180
# Each line the dfe-docker build context keeps, so the scratch tree holds what the image carries
_ALLOWLIST_LINE = re.compile(r"^!(?P<path>[^*\s]+?)/?$")
_IMAGE_EXCLUDES = ("tests", "__pycache__", "*.pyc")
_BLOCKING = (
    "bind mounts",
    "env_file",
    "interpolation",
    "literal secrets",
    "profiles",
    "unlooped ports",
)


type ImageCommand = Callable[..., tuple[list[str], list[str]]]


class PublishError(Exception):
    """A step that could not be done, with what to do about it."""


# ---------------------------------------------------------------------------
# The scratch install
# ---------------------------------------------------------------------------


def image_allowlist(*, dockerignore: str) -> list[str]:
    """The paths the dfe-docker build context keeps, read from .dockerignore's `!` lines."""
    return [
        match["path"]
        for line in dockerignore.splitlines()
        if (match := _ALLOWLIST_LINE.match(line.strip()))
    ]


def stage_tree(*, dest: Path) -> None:
    """Copy what the dfe-docker image carries into dest."""
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for name in image_allowlist(dockerignore=dockerignore):
        source = REPO_ROOT / name
        target = dest / name
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns(*_IMAGE_EXCLUDES)
            )
        elif source.is_file():
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy2(source, target)


def _installer(*, args: Sequence[str], tree: Path) -> None:
    """Run the tree's own installer in its work phase, as the image's copy hands over to it."""
    result = subprocess.run(
        [sys.executable, str(tree / "scripts" / "installer.py"), *args],
        check=False,
        cwd=tree,
        env={**os.environ, "DFE_DOCKER_PHASE": "work"},
    )
    if result.returncode != 0:
        raise PublishError(f"installer.py {' '.join(args)} failed in the scratch tree")


def install(*, profile: str, stack_version: str, tree: Path) -> None:
    """Set the scratch tree up as the image's `init` sets a folder up, then issue the first-login password."""
    _installer(args=["init", "--stack", stack_version, "--profile", profile], tree=tree)
    engine_env = tree / ENV_DIR_NAME / f"{ENGINE_COMPONENT}.env"
    write_dotenv(
        path=engine_env,
        text=init._set_live(
            text=engine_env.read_text(encoding="utf-8"),
            key=ADMIN_PASSWORD_KEY,
            value=FIRST_LOGIN_PASSWORD,
        ),
    )


def declare_instances(*, declared: Mapping[str, Sequence[str]], tree: Path) -> None:
    """Write the instance indexes the engine would for `declared`, then re-resolve the tree's compose settings."""
    for service, names in declared.items():
        instances.index_file(service, env_dir=tree / ENV_DIR_NAME).write_text(
            "".join(f"{name}\n" for name in names), encoding="utf-8", newline="\n"
        )
    _installer(args=["apply"], tree=tree)


def _compose(*, args: Sequence[str], cwd: Path) -> str:
    """Run one docker compose command with only what a person's shell carries, so every setting comes from the folder, and return its output."""
    shell = {
        key: value
        for key, value in os.environ.items()
        if key in ("HOME", "PATH") or key.startswith("DOCKER_")
    }
    result = subprocess.run(
        ["docker", "compose", *args],
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
        env=shell,
        errors="replace",
        text=True,
        timeout=_COMPOSE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise PublishError(
            f"`docker compose {' '.join(args)}` failed: {result.stderr.strip()}"
        )
    return result.stdout


def _json_model(*, out: str) -> dict:
    """A model `docker compose config --format json` printed, refused when it holds no services."""
    try:
        model = json.loads(out)
    except json.JSONDecodeError as error:
        raise PublishError(
            f"`docker compose config` returned no model: {error}"
        ) from error
    if not (isinstance(model, dict)) or not (isinstance(model.get("services"), dict)):
        raise PublishError("`docker compose config` returned no services")
    return model


def compose_model(*, tree: Path) -> dict:
    """The model a plain `docker compose up` starts in the tree."""
    return _json_model(out=_compose(args=["config", "--format", "json"], cwd=tree))


def file_model(*, path: Path) -> dict:
    """A published file as compose reads it, from a folder with no .env for compose to pick up."""
    with tempfile.TemporaryDirectory(prefix="dfe-publish-") as empty:
        out = _compose(
            args=[
                "-f",
                str(path.resolve()),
                "config",
                "--format",
                "json",
                "--no-interpolate",
                "--no-normalize",
            ],
            cwd=Path(empty),
        )
    return _json_model(out=out)


def demo_roles(*, events: Path, sources: Path) -> dict[str, list[str]]:
    """The demo's source names by role: each vendor has an events directory, each fetcher a fetcher mapping, and the one left is the intake."""
    names = sorted(path.stem for path in sources.glob("*.yaml"))
    vendors = sorted(
        path.name for path in events.iterdir() if path.is_dir() and path.name in names
    )
    fetchers = [
        name
        for name in names
        if _FETCHER_KEY.search((sources / f"{name}.yaml").read_text(encoding="utf-8"))
    ]
    intakes = [name for name in names if name not in (*vendors, *fetchers)]
    if len(intakes) != 1 or not (vendors):
        raise PublishError(
            f"{str(sources)!r} needs one intake and at least one vendor source with events in {str(events)!r}, as `make demo` does. It holds {names}"
        )
    return {"fetcher": fetchers, "intake": intakes, "vendor": vendors}


def demo_instances(*, roles: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """The per-source instances the engine declares for the demo's sources: the intake's transform and one per fetcher."""
    declared = {demo.TRANSFORM_APP: list(roles["intake"])}
    if roles["fetcher"]:
        declared[FETCHER_SERVICE] = list(roles["fetcher"])
    return declared


def minted_secrets(*, tree: Path) -> dict[str, str]:
    """Each value init.py minted into the scratch tree, mapped to its key: what a published file must never carry."""
    found: dict[str, str] = {}
    for file_name, keys in init.GENERATED_SECRETS.items():
        path = (
            tree / file_name if file_name == ".env" else tree / ENV_DIR_NAME / file_name
        )
        values = _parse_dotenv(text=path.read_text(encoding="utf-8"))
        for key in keys:
            if values.get(key) and key not in PUBLIC_VALUES:
                found[values[key]] = key
    return found


def image_command(*, image: str) -> tuple[list[str], list[str]]:
    """The entrypoint and command an image runs by default, from its registry config, refused unless every platform agrees."""
    out = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            "{{json .Image}}",
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=_COMPOSE_TIMEOUT_SECONDS,
    )
    if out.returncode != 0:
        raise PublishError(f"cannot read {image}'s config: {out.stderr.strip()}")
    images = json.loads(out.stdout)
    platforms = [images] if "config" in images else list(images.values())
    commands = {
        json.dumps(
            [
                platform.get("config", {}).get("Entrypoint") or [],
                platform.get("config", {}).get("Cmd") or [],
            ]
        )
        for platform in platforms
    }
    if len(commands) != 1:
        raise PublishError(f"{image}'s platforms run different commands: {commands}")
    entrypoint, command = json.loads(commands.pop())
    return entrypoint, command


# ---------------------------------------------------------------------------
# The publishable model
# ---------------------------------------------------------------------------


def config_name(*, relative: str) -> str:
    """The compose config name one tree file is published under."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", relative)


def config_content(*, relative: str, text: str) -> str:
    """A tree file as a config's inline content: its published edits applied, then every `$` escaped from interpolation."""
    for old, new in CONTENT_EDITS.get(relative, ()):
        if old not in text:
            raise PublishError(
                f"{relative} no longer holds {old.strip()!r}, which the published file rewrites"
            )
        text = text.replace(old, new)
    return text.replace("$", "$$")


def _bind_files(*, source: Path, target: str) -> list[tuple[Path, str]]:
    """Each file a bind mount carries, with the target it lands at."""
    if source.is_file():
        return [(source, target)]
    return [
        (path, f"{target.rstrip('/')}/{path.relative_to(source).as_posix()}")
        for path in sorted(source.rglob("*"))
        if path.is_file()
    ]


def _service_mounts(
    *, configs: dict[str, dict], service: Mapping, tree: Path
) -> tuple[list, list[dict], bool]:
    """The service's volumes with its binds taken out, the configs that replace them, and whether it lost the engine's env/ directory."""
    kept: list = []
    mounted: list[dict] = []
    env_dir = False
    for volume in service.get("volumes", []):
        if not (isinstance(volume, dict)) or volume.get("type") != "bind":
            kept.append(volume)
            continue
        source = Path(str(volume.get("source", "")))
        if source == tree / ENV_DIR_NAME:
            env_dir = True
            continue
        if source == Path(HOST_PROC):
            continue
        if not (source.is_relative_to(tree)):
            raise PublishError(
                f"a bind mount of {str(source)!r} is neither in the tree nor one the published file knows how to drop"
            )
        for path, target in _bind_files(source=source, target=str(volume["target"])):
            relative = path.relative_to(tree).as_posix()
            name = config_name(relative=relative)
            configs[name] = {
                "content": config_content(
                    relative=relative, text=path.read_text(encoding="utf-8")
                )
            }
            mounted.append({"source": name, "target": target})
    return kept, mounted, env_dir


def published(*, container_prefix: str, model: Mapping, name: str, tree: Path) -> dict:
    """The model as a file a host with only Docker can run: no bind mount, no profile, loopback ports, project `name`."""
    tree = tree.resolve()
    services: dict[str, dict] = {}
    configs: dict[str, dict] = {}
    for service_name, original in model["services"].items():
        service = {
            key: value
            for key, value in original.items()
            if key not in ("profiles", "group_add", "env_file") and value is not None
        }
        # Normalising adds an empty placement, which compose then warns about on every run
        if isinstance(service.get("deploy"), dict) and not (
            service["deploy"].get("placement")
        ):
            service["deploy"] = {
                key: value
                for key, value in service["deploy"].items()
                if key != "placement"
            }
        if "container_name" in service:
            service["container_name"] = f"{container_prefix}{service['container_name']}"
        volumes, mounted, env_dir = _service_mounts(
            configs=configs, service=original, tree=tree
        )
        if "volumes" in original:
            service["volumes"] = volumes
        if mounted:
            service["configs"] = [*service.get("configs", []), *mounted]
        environment = {
            key: value
            for key, value in (original.get("environment") or {}).items()
            if value is not None
        }
        if env_dir:
            environment[APP_ENV_DIR_KEY] = ""
        if "environment" in original or env_dir:
            service["environment"] = environment
        if "ports" in original:
            service["ports"] = [
                {**port, "host_ip": LOOPBACK} for port in original["ports"]
            ]
        services[service_name] = service
    for service in services.values():
        if "depends_on" in service:
            service["depends_on"] = {
                dependency: condition
                for dependency, condition in service["depends_on"].items()
                if dependency in services
            }
    result: dict = {"name": name, "services": services}
    if configs:
        result["configs"] = configs
    for kind in ("networks", "volumes"):
        if model.get(kind):
            result[kind] = {
                key: {k: v for k, v in (value or {}).items() if k != "name"}
                for key, value in model[kind].items()
            }
    return result


def demo_overlay(
    *,
    archive: Mapping,
    image: str,
    model: Mapping,
    roles: Mapping[str, Sequence[str]],
    stream: Sequence[str],
) -> dict:
    """The published stack model with the demo's helpers added and its data path held until the sources exist.

    `stream` is the replay script, protocol and rate `make demo` streams with.
    """
    demo_model = json.loads(json.dumps(model))
    services = demo_model["services"]
    engine_env = services[ENGINE_SERVICE].get("environment", {})
    contract_image = str(services[TRANSFORM_ELASTIC_CONTRACT]["image"])
    sidecar = services[SIDECAR_SERVICE]
    helper = {
        "deploy": sidecar["deploy"],
        "dns_search": sidecar.get("dns_search", ["."]),
        "image": image,
    }
    seeding = {
        IN_STACK_KEY: "true",
        BREAKGLASS_PASSWORD_KEY: engine_env[BREAKGLASS_PASSWORD_KEY],
    }
    seed_entrypoint = ["python3", f"{IMAGE_TREE}/scripts/demo_seed.py"]
    generator, protocol, rate = stream
    feed_mount = {
        "type": "volume",
        "source": DEMO_FEED_VOLUME,
        "target": DEMO_FEED_MOUNT,
    }
    services["demo-sources"] = {
        **helper,
        "command": ["sources"],
        "depends_on": {ENGINE_SERVICE: {"condition": "service_healthy"}},
        "entrypoint": seed_entrypoint,
        "environment": seeding,
        "restart": "no",
        "user": DEMO_UID,
    }
    services["demo-feed"] = {
        **helper,
        "command": [
            "feed",
            "--feed",
            f"{DEMO_FEED_MOUNT}/{DEMO_FEED_SUBPATH}",
            "--generator",
            generator,
        ],
        "entrypoint": seed_entrypoint,
        # The tag the stack's dfe-transform-elastic runs, so the replayed lines match its detector
        "environment": {
            IN_STACK_KEY: "true",
            demo.TRANSFORM_VERSION_KEY: contract_image.rsplit("/", 1)[-1].partition(
                ":"
            )[2],
        },
        "restart": "no",
        "user": ROOT_UID,
        "volumes": [feed_mount],
    }
    services["demo-console"] = {
        **helper,
        "command": ["console"],
        "depends_on": {
            "demo-sources": {"condition": "service_completed_successfully"},
            "dfe-hyperdx": {"condition": "service_healthy"},
        },
        "entrypoint": seed_entrypoint,
        "environment": seeding,
        "restart": "no",
        "user": DEMO_UID,
    }
    # generator_command builds a `docker run`, and what follows its image is the command the stream runs
    marker = "<image>"
    replay = demo.generator_command(
        container="",
        feed=Path(),
        generator=generator,
        image=marker,
        network="",
        protocol=protocol,
        rate=float(rate),
        sources=list(roles["vendor"]),
    )
    services["demo-stream"] = {
        **helper,
        "depends_on": {
            "demo-feed": {"condition": "service_completed_successfully"},
            RECEIVER_SERVICE: {"condition": "service_healthy"},
        },
        "entrypoint": replay[replay.index(marker) + 1 :],
        "restart": "unless-stopped",
        "user": DEMO_UID,
        "volumes": [
            {
                **feed_mount,
                "read_only": True,
                "target": "/demo",
                "volume": {"subpath": DEMO_FEED_SUBPATH},
            }
        ],
    }
    services["demo-prune"] = {
        **helper,
        "depends_on": {
            SIDECAR_SERVICE: {"condition": "service_completed_successfully"}
        },
        "entrypoint": demo.pruner_command(
            archive=archive, tool=f"{IMAGE_TREE}/resources/demo/archive.py"
        ),
        "restart": "unless-stopped",
        "user": DEMO_UID,
        "volumes": [
            {
                "type": "volume",
                "source": "archiver-data",
                "target": demo.ARCHIVE_ROOT,
            }
        ],
    }
    held = [RECEIVER_SERVICE, *instances.services(demo_instances(roles=roles))]
    for service in held:
        if service not in services:
            raise PublishError(
                f"the demo model has no {service!r} to hold for its sources"
            )
        services[service].setdefault("depends_on", {})["demo-sources"] = {
            "condition": "service_completed_successfully",
            "required": True,
        }
    demo_model.setdefault("volumes", {})[DEMO_FEED_VOLUME] = {}
    return demo_model


def secret_templates(
    *, environment: Mapping, minted: Mapping[str, str]
) -> dict[str, str]:
    """Each environment value that holds a minted secret, as the template secrets_init.py renders it from the key."""
    templates = {}
    for name, value in environment.items():
        if not (isinstance(value, str)):
            continue
        template = value.replace("$$", "$")
        for secret, key in minted.items():
            template = template.replace(secret, PLACEHOLDER.format(key=key))
        if template != value.replace("$$", "$"):
            templates[name] = template
    return templates


def _escaped(argv: Sequence[str]) -> list[str]:
    """Strings from an image's config as compose takes them, with every `$` escaped."""
    return [str(arg).replace("$", "$$") for arg in argv]


def wrapped_command(
    *, image_command: ImageCommand, service: Mapping
) -> tuple[list[str], list[str]]:
    """The entrypoint and command that source the service's env file, then exec exactly what it ran before."""
    if service.get("entrypoint"):
        argv = [*service["entrypoint"], *(service.get("command") or [])]
    else:
        entrypoint, command = image_command(image=str(service["image"]))
        argv = [
            *_escaped(entrypoint),
            *(service.get("command") or _escaped(command)),
        ]
    if not (argv):
        raise PublishError(
            f"{service['image']} names no command to run after its secrets"
        )
    return ["/bin/sh", "-c", WRAPPER, WRAPPER_NAME], argv


def wrapped_healthcheck(*, healthcheck: Mapping) -> dict:
    """A healthcheck that sources the env file first, since a check runs in a process of its own."""
    test = list(healthcheck.get("test") or [])
    if healthcheck.get("disable") or not (test) or test[0] not in ("CMD", "CMD-SHELL"):
        return dict(healthcheck)
    if test[0] == "CMD-SHELL":
        test = [
            "CMD-SHELL",
            f"set -a && . {SECRETS_MOUNT}/env && set +a && {' '.join(test[1:])}",
        ]
    else:
        test = ["CMD", "/bin/sh", "-c", WRAPPER, WRAPPER_NAME, *test[1:]]
    return {**healthcheck, "test": test}


def _read_from_files(*, configs: dict, names: Sequence[str], relative: str) -> None:
    """Point a config's `${env:NAME}` references at the file secrets_init.py writes for each name."""
    config = configs[config_name(relative=relative)]
    for name in names:
        reference = f"$${{env:{name}}}"
        if reference not in config["content"]:
            raise PublishError(
                f"{relative} reads {name} some other way than {reference}"
            )
        config["content"] = config["content"].replace(
            reference, f"$${{file:{SECRETS_MOUNT}/{name}}}"
        )


def secure(
    *,
    image: str,
    image_command: ImageCommand,
    minted: Mapping[str, str],
    model: Mapping,
) -> dict:
    """The model with every minted secret taken out, minted instead at first start by secrets-init into the stack's own volume."""
    secured = json.loads(json.dumps(model))
    services = secured["services"]
    consumers: dict[str, dict] = {}
    for name in sorted(services):
        service = services[name]
        templates = secret_templates(
            environment=service.get("environment") or {}, minted=minted
        )
        if not (templates):
            continue
        gid = SECRETS_GID_BASE + len(consumers) + 1
        service["environment"] = {
            key: value
            for key, value in service["environment"].items()
            if key not in templates
        }
        if name in FILE_CONSUMERS:
            consumers[name] = {"gid": gid, "files": templates}
            _read_from_files(
                configs=secured["configs"],
                names=sorted(templates),
                relative=FILE_CONSUMERS[name],
            )
        else:
            consumers[name] = {"env": templates, "gid": gid}
            service["entrypoint"], service["command"] = wrapped_command(
                image_command=image_command, service=service
            )
            if service.get("healthcheck"):
                service["healthcheck"] = wrapped_healthcheck(
                    healthcheck=service["healthcheck"]
                )
        service["group_add"] = [str(gid)]
        service.setdefault("volumes", []).append(
            {
                "read_only": True,
                "source": SECRETS_VOLUME,
                "target": SECRETS_MOUNT,
                "type": "volume",
                "volume": {"subpath": name},
            }
        )
        service.setdefault("depends_on", {})[SECRETS_SERVICE] = {
            "condition": "service_completed_successfully",
            "required": True,
        }
    if not (consumers):
        return secured
    sidecar = services[SIDECAR_SERVICE]
    services[SECRETS_SERVICE] = {
        "configs": [{"source": SECRETS_PLAN, "target": SECRETS_PLAN_TARGET}],
        "deploy": sidecar["deploy"],
        "dns_search": sidecar.get("dns_search", ["."]),
        "entrypoint": [
            "python3",
            f"{IMAGE_TREE}/scripts/secrets_init.py",
            "--plan",
            SECRETS_PLAN_TARGET,
            "--root",
            SECRETS_ROOT,
        ],
        "image": image,
        "restart": "no",
        "user": ROOT_UID,
        "volumes": [
            {"source": SECRETS_VOLUME, "target": SECRETS_ROOT, "type": "volume"}
        ],
    }
    plan = json.dumps({"consumers": consumers}, indent=2, sort_keys=True) + "\n"
    secured.setdefault("configs", {})[SECRETS_PLAN] = {
        "content": plan.replace("$", "$$")
    }
    secured.setdefault("volumes", {})[SECRETS_VOLUME] = {}
    return secured


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def render(*, model: Mapping, tag: str, stack_version: str) -> str:
    """The published file's text, as compose writes the model without adding the names and defaults it derives."""
    with tempfile.TemporaryDirectory(prefix="dfe-publish-") as scratch:
        source = Path(scratch) / "model.json"
        source.write_text(json.dumps(model), encoding="utf-8", newline="\n")
        body = _compose(
            args=[
                "-f",
                str(source),
                "config",
                "--format",
                "yaml",
                "--no-interpolate",
                "--no-normalize",
            ],
            cwd=Path(scratch),
        )
    return (
        f"# Generated by scripts/publish_compose.py for DFE {stack_version}. Do not edit.\n"
        f"# docker compose -f oci://{DEFAULT_IMAGE}:{tag} up -d\n" + body
    )


def interpolations(*, text: str) -> list[str]:
    """Every `$` in the text compose would interpolate."""
    return [
        text[match.start() : match.start() + 24]
        for match in _DOLLAR.finditer(text)
        if match.group(1) != "$"
    ]


def findings(*, model: Mapping, text: str) -> dict[str, list[str]]:
    """What stops a file publishing, or running from `-f oci://` with no questions and nothing off loopback, by kind."""
    found: dict[str, list[str]] = {
        "bind mounts": [],
        "env_file": [],
        "interpolation": interpolations(text=text),
        "literal secrets": [],
        "profiles": [],
        "undigested images": [],
        "unlooped ports": [],
    }
    for name, service in model["services"].items():
        for volume in service.get("volumes", []):
            if not (isinstance(volume, dict)) or volume.get("type") == "bind":
                found["bind mounts"].append(f"{name}: {volume}")
        for key, value in (service.get("environment") or {}).items():
            public = PUBLIC_VALUES.get(key)
            if value and _SECRET_NAME.search(key) and value != public:
                found["literal secrets"].append(f"{name}: {key}")
        for key in ("env_file", "profiles"):
            if service.get(key):
                found[key].append(name)
        if "@sha256:" not in str(service.get("image", "")):
            found["undigested images"].append(f"{name}: {service.get('image')}")
        for port in service.get("ports", []):
            if not (isinstance(port, dict)) or port.get("host_ip") != LOOPBACK:
                found["unlooped ports"].append(f"{name}: {port}")
    return found


def check(*, paths: Sequence[Path], require_digests: bool) -> int:
    """Print each file's findings and return 1 when one blocks publishing."""
    blocking = {*_BLOCKING, "undigested images"} if require_digests else {*_BLOCKING}
    code = 0
    for path in paths:
        found = findings(
            model=file_model(path=path), text=path.read_text(encoding="utf-8")
        )
        for kind, hits in found.items():
            _print(msg=f"{path.name}: {kind} {len(hits)}")
            if hits and kind in blocking:
                code = 1
                for hit in hits:
                    _print(msg=f"FAIL {path.name}: {kind}: {hit}")
    return code


def _make_print(*, goal: str) -> list[str]:
    """The words one of the Makefile's print goals prints."""
    result = subprocess.run(
        ["make", "--no-print-directory", "-s", goal],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    words = result.stdout.split()
    if result.returncode != 0 or not (words):
        raise PublishError(f"`make {goal}` printed nothing: {result.stderr.strip()}")
    return words


def published_tags(*, moving: bool, release: str, tag: str) -> list[str]:
    """The tags one file is pushed under: the release's, and the moving one when the release is not a pre-release."""
    return [f"{tag}-{release}", *([tag] if moving else [])]


def publish(
    *, files: Mapping[str, Path], moving: bool, release: str, repository: str
) -> None:
    """Push each file as a compose OCI artifact under its tags, answering yes to compose's checks, which flag the first-login password and the inline configs."""
    for tag, path in files.items():
        for name in published_tags(moving=moving, release=release, tag=tag):
            with tempfile.TemporaryDirectory(prefix="dfe-publish-") as empty:
                _compose(
                    args=[
                        "-f",
                        str(path.resolve()),
                        "publish",
                        "--yes",
                        "--with-env",
                        f"{repository}:{name}",
                    ],
                    cwd=Path(empty),
                )
            _print(msg=f"published {str(path.name)!r} as {repository}:{name}")


def generate(*, image: str, out_dir: Path, stack_version: str) -> dict[str, Path]:
    """Write both files into out_dir and return each one's path by the tag it moves on, refusing one that holds a value the scratch install minted."""
    profile = _make_print(goal="print-default-profile")[0]
    stream = _make_print(goal="print-demo-stream")
    manifest = demo._read_manifest(path=demo.MANIFEST_FILE)
    roles = demo_roles(events=demo.EVENTS_DIR, sources=demo.SOURCES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dfe-publish-") as scratch:
        tree = Path(scratch)
        stage_tree(dest=tree)
        install(profile=profile, stack_version=stack_version, tree=tree)
        minted = minted_secrets(tree=tree)
        base = compose_model(tree=tree)
        declare_instances(declared=demo_instances(roles=roles), tree=tree)
        with_instances = compose_model(tree=tree)
        stack_model = published(
            container_prefix="", model=base, name=STACK_PROJECT, tree=tree
        )
        demo_model = demo_overlay(
            archive=manifest["archive"],
            image=image,
            model=published(
                container_prefix=DEMO_CONTAINER_PREFIX,
                model=with_instances,
                name=DEMO_PROJECT,
                tree=tree,
            ),
            roles=roles,
            stream=stream,
        )
    # Both files run the same images, so each config is read from the registry once
    commands = functools.cache(image_command)
    written = {}
    for file_name, model, tag in (
        (STACK_FILE, stack_model, PUBLISHED_STACK_TAG),
        (DEMO_FILE, demo_model, PUBLISHED_DEMO_TAG),
    ):
        secured = secure(
            image=image, image_command=commands, minted=minted, model=model
        )
        text = render(model=secured, tag=tag, stack_version=stack_version)
        leaked = sorted({key for secret, key in minted.items() if secret in text})
        if leaked:
            raise PublishError(f"{file_name} would carry {', '.join(leaked)}")
        path = out_dir / file_name
        path.write_text(text, encoding="utf-8", newline="\n")
        _print(msg=f"wrote {str(path)!r}")
        written[tag] = path
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        nargs="+",
        metavar="FILE",
        type=Path,
        help="check published files instead of writing them",
    )
    parser.add_argument(
        "--require-digests",
        action="store_true",
        help="also refuse an image not pinned by digest",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="the dfe-docker image the demo's helpers run, by digest on a release",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / ".tmp" / "publish",
        help="where to write both files",
    )
    parser.add_argument(
        "--stack",
        default="",
        help="the stack to pin, deployment.example.yaml's version.pin when unset",
    )
    parser.add_argument(
        "--publish",
        metavar="REPOSITORY",
        help="push both files to this repository once they check clean, which needs a registry login",
    )
    parser.add_argument(
        "--release", default="", help="with --publish, the release version, no v"
    )
    parser.add_argument(
        "--moving",
        action="store_true",
        help=f"with --publish, also move the {PUBLISHED_STACK_TAG!r} and {PUBLISHED_DEMO_TAG!r} tags",
    )
    args = parser.parse_args(argv)
    if args.publish and not (args.release.strip()):
        parser.error("--publish needs --release to name the versioned tags")
    try:
        if args.check:
            return check(paths=args.check, require_digests=args.require_digests)
        written = generate(
            image=args.image,
            out_dir=args.out_dir,
            stack_version=args.stack.strip() or stack._dial_pin(),
        )
        # A file is published only digest-pinned, and only once both check clean
        code = check(
            paths=list(written.values()),
            require_digests=args.require_digests or bool(args.publish),
        )
        if code == 0 and args.publish:
            publish(
                files=written,
                moving=args.moving,
                release=args.release.strip().removeprefix("v"),
                repository=args.publish,
            )
        return code
    except (
        OSError,
        PublishError,
        demo.DemoError,
        stack.StackError,
        subprocess.TimeoutExpired,
    ) as error:
        _print(msg=f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
