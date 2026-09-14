#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/build_dev_images.py
#  Purpose:      Build local :local DFE images for `make dev` from component source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
"""Builder for local DFE images.

Source acquisition goes via git, never an assumed local layout: each component
repo is cloned/fetched into a managed cache (``DFE_SRC_CACHE``) from
``DFE_SRC_REMOTE`` at ``DFE_SRC_REF``, so a fresh machine only needs git
credentials for the hyperi-io repos. ``DFE_SRC_ROOT`` is the explicit opt-in for
building your own local checkouts (work in progress included) instead.

Each rust component is built in two phases:
1. Compile the binary from the staged source via the shared docker/dfe-rust-builder.Dockerfile
2. Package it with the component's own committed Dockerfile (the single source of truth for runtime)
Self-contained components (dfe-ui) build straight from their own Dockerfile.
Result image tag: <service>:local, consumed by docker-compose.override.yml
(every component) or by the overlay ``--overlay`` writes (only the components
built this run, so the rest of the stack stays on the pinned registry images).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import RUST_BUILDER, _load_dotenv, _print

IMAGE_TAG = "local"
# Compose services that run a component's image under another name; a local
# build must repoint all of them or the stack runs two builds of one component.
IMAGE_CONSUMERS: dict[str, tuple[str, ...]] = {
    "dfe-archiver": ("dlq-init",),
    "dfe-engine": ("dfe-schema-init", "dfe-hunt-runner"),
    "dfe-transform-elastic": ("dfe-transform-elastic-cisco-ios",),
    "dfe-transform-vector": ("dfe-transform-vector-filebeat",),
    "dfe-transform-vrl": ("dfe-transform-vrl-filebeat",),
}
# The IMAGE_CONSUMERS no profile names, mapped to the services whose
# `depends_on` starts them. The committed override repoints them at `:local`
# like every other consumer, so a build of the profile's own services alone
# leaves them on a tag nothing produced.
IMPLICIT_CONSUMERS: dict[str, tuple[str, ...]] = {
    "dfe-schema-init": ("dfe-loader", "otel-collector"),
    "dlq-init": ("dfe-archiver", "dfe-fetcher", "dfe-loader", "dfe-receiver"),
}
RUST_COMPONENTS = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-vector",
    "dfe-transform-vrl",
]
SELF_CONTAINED_COMPONENTS = ["dfe-engine", "dfe-ui", "hyperdx"]
SERVICE_BUILD_ARGS = {"hyperdx": {"NEXT_PUBLIC_IS_LOCAL_MODE": "true"}}
# Compose service name -> source repo NAME (the GitHub repo and therefore the
# checkout directory name), for the cases where they differ; hyperdx's repo and
# image are `dfe-hyperdx`.
SERVICE_REPO_DIRS = {"hyperdx": "dfe-hyperdx"}
# Source acquisition defaults; override via DFE_SRC_REMOTE / DFE_SRC_REF (or
# DFE_SRC_ROOT to build local checkouts instead of the git cache).
SRC_REMOTE_DEFAULT = "https://github.com/hyperi-io"
SRC_REF_DEFAULT = "main"
STAGE_EXCLUDES = [
    "target",
    ".git",
    "node_modules",
    ".tmp",
    ".cargo",
    ".turbo",
    ".next",
    ".dockerignore",
    # Secrets. The staged tree is fed to the builder as a build context and lands
    # in a layer via `COPY . .`, so a component checkout's own .env or certs would
    # be baked into the local builder image. The exported artefact is `FROM
    # scratch` so nothing ships, but it is still a credential sitting in the build
    # cache -- and this repo's own .dockerignore exists to prevent exactly that.
    ".env",
    ".env.*",
    "env",
    "certs",
]


class _BuilderError(Exception):
    """Raised when a docker image cannot be built."""

    def __init__(self, *, header: str | None = None, msg: str) -> None:
        """Carry the failure header and message for reporting at the boundary."""
        super().__init__(msg)
        self.header = header
        self.msg = msg


def _build_image(*, service: str) -> None:
    """Build an image for a service."""
    repo = _source_checkout(service=service)
    dockerfile = repo / "Dockerfile"
    if not (dockerfile.is_file()):
        raise _BuilderError(header=service, msg=f"Missing Dockerfile at {dockerfile!r}")

    if service in SELF_CONTAINED_COMPONENTS:
        _docker_build(context=repo, dockerfile=dockerfile, service=service)
        return
    if service in RUST_COMPONENTS:
        with tempfile.TemporaryDirectory(prefix=f"dfe-dev-{service}-") as workdir:
            context = _export_rust_binary(
                repo=repo, service=service, workdir=Path(workdir)
            )
            _docker_build(context=context, dockerfile=dockerfile, service=service)


def _cached_clone(*, repo_dir: str, service: str) -> Path:
    """Return a managed clone of repo_dir at DFE_SRC_REF, cloning/fetching as needed.

    The checkout is detached at the resolved commit and must stay clean -- the
    cache is tool-managed, not a place to work. Local edits fail the build with a
    pointer at ``DFE_SRC_ROOT``, which is the supported way to build your own
    checkouts.
    """
    remote = os.environ.get("DFE_SRC_REMOTE", "").strip() or SRC_REMOTE_DEFAULT
    ref = os.environ.get("DFE_SRC_REF", "").strip() or SRC_REF_DEFAULT
    checkout = _src_cache_root() / repo_dir
    url = f"{remote.rstrip('/')}/{repo_dir}.git"
    if not ((checkout / ".git").exists()):
        checkout.parent.mkdir(parents=True, exist_ok=True)
        # The URL is not echoed: DFE_SRC_REMOTE may carry userinfo credentials.
        _print(header=service, msg=f"Cloning {repo_dir!r} into {str(checkout)!r}")
        _run(args=["git", "clone", "--quiet", "--", url, str(checkout)])
    # Tracked edits only: untracked droppings (a Finder .DS_Store) are not
    # somebody's work and must not brick the build.
    status = _git_capture(
        args=["git", "-C", str(checkout), "status", "--porcelain", "-uno"]
    )
    if (status.returncode != 0) or (status.stdout.strip()):
        raise _BuilderError(
            header=service,
            msg=f"Source cache {str(checkout)!r} has edits to tracked files -- "
            "the cache is managed by this script; set DFE_SRC_ROOT to build "
            "local work, or delete the cache directory to reset it",
        )
    # --prune: a remote branch deleted after being cached must resolve to a
    # loud error, not silently build the stale remote-tracking ref.
    _run(
        args=[
            "git",
            "-C",
            str(checkout),
            "fetch",
            "--quiet",
            "--prune",
            "--tags",
            "origin",
        ]
    )
    commit = _resolve_ref(checkout=checkout, ref=ref, service=service)
    _run(args=["git", "-C", str(checkout), "checkout", "--quiet", "--detach", commit])
    _print(header=service, msg=f"Source {repo_dir}@{ref} ({commit[:12]})")
    return checkout


def _docker_build(*, context: Path, dockerfile: Path, service: str) -> None:
    """Build the <service>:local image from dockerfile, using context as the Docker build context."""
    service_tag = f"{service}:{IMAGE_TAG}"
    _print(
        header=service,
        msg=f"Packaging {service_tag!r} via {str(dockerfile)!r}...",
    )
    build_args = []
    for name, value in SERVICE_BUILD_ARGS.get(service, {}).items():
        build_args.extend(["--build-arg", f"{name}={value}"])
    _run(
        args=[
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "-t",
            service_tag,
            *build_args,
            str(context),
        ]
    )


def _export_rust_binary(*, repo: Path, service: str, workdir: Path) -> Path:
    """Compile service from staged local source via the shared builder. Returns the dir holding the binary."""
    bindir = workdir / "bin"
    src = workdir / "src"
    _stage_source(dest=src, repo=repo)
    bindir.mkdir()
    _print(header=service, msg=f"Building binary from {str(repo)!r}")
    _run(
        args=[
            "docker",
            "build",
            "--target",
            "export",
            "--build-arg",
            f"BINARY={service}",
            "--output",
            f"type=local,dest={bindir!s}",
            "-f",
            str(RUST_BUILDER),
            str(src),
        ]
    )
    if not ((bindir / service).is_file()):
        raise _BuilderError(
            header=service, msg=f"Builder did not produce {service!r} in {bindir!r}"
        )
    return bindir


def _git_capture(*, args: list[str]) -> subprocess.CompletedProcess:
    """Run git capturing text output as explicit UTF-8 (locale-independent)."""
    return subprocess.run(
        args=args,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


def _implicit_components(*, services: list[str]) -> dict[str, str]:
    """Return {consumer: component} for the consumers this run's stack starts and no profile names.

    A consumer is only pulled in when one of its dependents is in the stack, so a
    profile that runs none of them costs nothing.
    """
    requested = set(services)
    needed = {}
    for component, consumers in IMAGE_CONSUMERS.items():
        if component in requested:
            continue
        for consumer in consumers:
            if requested.intersection(IMPLICIT_CONSUMERS.get(consumer, ())):
                needed[consumer] = component
    return dict(sorted(needed.items()))


def _resolve_ref(*, checkout: Path, ref: str, service: str) -> str:
    """Resolve ref to a commit SHA, preferring the remote branch of that name."""
    for candidate in (f"origin/{ref}", ref):
        result = _git_capture(
            args=[
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{candidate}^{{commit}}",
            ]
        )
        if (result.returncode == 0) and (result.stdout.strip()):
            return result.stdout.strip()
    raise _BuilderError(
        header=service,
        msg=f"DFE_SRC_REF={ref!r} is not a branch, tag or commit in {str(checkout)!r}",
    )


def _run(*, args: list[str]) -> None:
    """Run a command, raising _BuilderError on a non-zero exit."""
    result = subprocess.run(args=args, check=False)
    if result.returncode != 0:
        raise _BuilderError(
            msg=f"Command failed ({result.returncode}): {' '.join(args)!r}"
        )


def _source_checkout(*, service: str) -> Path:
    """Resolve the source checkout to build service from.

    ``DFE_SRC_ROOT`` set -> that directory's checkout (your local work, wherever
    you keep it). Unset -> the managed git cache. Never an assumed sibling or a
    machine-specific path: the dependency is a repo, so it travels as git config.
    """
    repo_dir = SERVICE_REPO_DIRS.get(service, service)
    raw_root = os.environ.get("DFE_SRC_ROOT", "").strip()
    if not (raw_root):
        return _cached_clone(repo_dir=repo_dir, service=service)
    checkout = Path(raw_root).expanduser() / repo_dir
    if not ((checkout / ".git").exists()):
        raise _BuilderError(
            header=service,
            msg=f"DFE_SRC_ROOT={raw_root!r} has no {repo_dir!r} checkout -- "
            "clone it there or unset DFE_SRC_ROOT to build from the git cache",
        )
    return checkout


def _src_cache_root() -> Path:
    """Return the managed source cache root (DFE_SRC_CACHE overrides the default)."""
    raw = os.environ.get("DFE_SRC_CACHE", "").strip()
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".cache")
    return base / "dfe-docker" / "src"


def _stage_source(*, dest: Path, repo: Path) -> None:
    """Copy repo into dest, skipping STAGE_EXCLUDES."""
    shutil.copytree(dst=dest, ignore=shutil.ignore_patterns(*STAGE_EXCLUDES), src=repo)


def buildable_components() -> list[str]:
    """Every component this script can build, in a stable order."""
    return sorted([*RUST_COMPONENTS, *SELF_CONTAINED_COMPONENTS])


def local_image_services(components: list[str]) -> dict[str, str]:
    """Map every compose service that must run a local build to its image tag.

    A component maps to itself plus each service in IMAGE_CONSUMERS that runs
    its image, so a partial local build never leaves a consumer on the registry.
    """
    services: dict[str, str] = {}
    for component in components:
        for service in (component, *IMAGE_CONSUMERS.get(component, ())):
            services[service] = f"{component}:{IMAGE_TAG}"
    return services


def overlay_text(components: list[str]) -> str:
    """The compose fragment that repoints the given components' services at :local."""
    lines = [
        "# Generated by scripts/build_dev_images.py --overlay for `make dev LOCAL=...`.",
        "# Only the components built locally are listed; everything else stays on",
        "# the pinned registry image. Not committed: regenerated on every run.",
        "services:",
    ]
    for service, image in sorted(local_image_services(components).items()):
        lines += [f"  {service}:", f"    image: {image}", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build <service>:local images from component source.",
    )
    parser.add_argument("services", nargs="+", help="compose service names to build")
    parser.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help="write a compose overlay repointing ONLY the built components (and the "
        "services that share their image) at :local",
    )
    return parser.parse_args(argv)


def _write_overlay(*, built: list[str], overlay: Path) -> int:
    """Write the overlay for what was built, or delete it when nothing was.

    An earlier run's file would repoint services at `:local` images this run
    never rebuilt.
    """
    if not (built):
        overlay.unlink(missing_ok=True)
        _print(
            msg=f"No component was built, so {overlay} was removed rather than left "
            "pointing at images this run did not rebuild"
        )
        return 1
    overlay.write_text(overlay_text(built), encoding="utf-8", newline="\n")
    _print(msg=f"Overlay written: {overlay} ({', '.join(built)} -> :local)")
    return 0


def main() -> int:
    _load_dotenv()
    args = _parse_args(sys.argv[1:])
    try:
        targets = list(args.services)
        # Only the committed-override path needs this: the generated overlay
        # repoints just what this run builds, so a consumer it skips keeps the
        # registry pin, while the override repoints every consumer there is.
        if args.overlay is None:
            for consumer, component in _implicit_components(services=targets).items():
                if component in targets:
                    continue
                _print(
                    header=component,
                    msg=f"Building it too -- no profile names it, but {consumer!r} "
                    "runs its image and the stack starts that",
                )
                targets.append(component)
        built = []
        for service in targets:
            if (service in RUST_COMPONENTS) or (service in SELF_CONTAINED_COMPONENTS):
                _build_image(service=service)
                built.append(service)
            else:
                _print(
                    header=service,
                    msg="SKIPPING - Not a locally buildable DFE component",
                )
        # Settled before the empty check, because the caller's next compose
        # command names this file whether or not anything was built.
        overlay_status = (
            0
            if args.overlay is None
            else _write_overlay(built=built, overlay=args.overlay)
        )
        if not (built):
            _print(msg="Nothing to build.")
            return overlay_status
        _print(msg=f"Built:\n{'\n'.join([f'- {build}' for build in built])}")
        return overlay_status
    except _BuilderError as error:
        _print(header=error.header, msg=error.msg)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
