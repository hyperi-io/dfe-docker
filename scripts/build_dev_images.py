#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/build_dev_images.py
#  Purpose:      Build local :local DFE images for `make dev` from local source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
"""Builder for local DFE images.

Each rust component is built in two phases:
1. Compile the binary from local source via the shared docker/dfe-rust-builder.Dockerfile
2. Package it with the component's own committed Dockerfile (the single source of truth for runtime)
Self-contained components (dfe-ui) build straight from their own Dockerfile. Result image tag: <service>:local (consumed by docker-compose.override.yml).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import PROJECTS_PATH, RUST_BUILDER, _print

IMAGE_TAG = "local"
RUST_COMPONENTS = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vrl",
]
SELF_CONTAINED_COMPONENTS = ["dfe-engine", "dfe-ui", "hyperdx"]
SERVICE_BUILD_ARGS = {"hyperdx": {"NEXT_PUBLIC_IS_LOCAL_MODE": "true"}}
# Compose service name -> source repo DIRECTORY under PROJECTS_PATH, for the
# cases where they differ. hyperdx is the one: the repo is `dfe-hyperdx`, while
# the image it publishes is `hyperi-hyperdx`. This mapped to the IMAGE name, so a
# dev build looked for a directory that does not exist.
SERVICE_REPO_DIRS = {"hyperdx": "dfe-hyperdx"}
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
    repo = PROJECTS_PATH / SERVICE_REPO_DIRS.get(service, service)
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


def _run(*, args: list[str]) -> None:
    """Run a command, raising _BuilderError on a non-zero exit."""
    result = subprocess.run(args=args, check=False)
    if result.returncode != 0:
        raise _BuilderError(
            msg=f"Command failed ({result.returncode}): {' '.join(args)!r}"
        )


def _stage_source(*, dest: Path, repo: Path) -> None:
    """Copy repo into dest, skipping STAGE_EXCLUDES."""
    shutil.copytree(dst=dest, ignore=shutil.ignore_patterns(*STAGE_EXCLUDES), src=repo)


def main() -> int:
    try:
        requested = sys.argv[1:]
        if not (requested):
            _print(msg="Usage: `build_dev_images.py <service> [<service> ...]`")
            return 2

        built = []
        for service in requested:
            if (service in RUST_COMPONENTS) or (service in SELF_CONTAINED_COMPONENTS):
                _build_image(service=service)
                built.append(service)
            else:
                _print(
                    header=service,
                    msg="SKIPPING - Not a locally buildable DFE component",
                )
        if not (built):
            _print(msg="Nothing to build.")
            return 0
        _print(msg=f"Built:\n{'\n'.join([f'- {build}' for build in built])}")
        return 0
    except _BuilderError as error:
        _print(header=error.header, msg=error.msg)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
