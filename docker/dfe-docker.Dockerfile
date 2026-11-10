# Project:   dfe-docker
# File:      docker/dfe-docker.Dockerfile
# Purpose:   The dfe-docker image: sets up a folder a plain `docker compose up -d` runs the DFE stack from
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

# Built with the repo root as context, so .dockerignore decides what the image carries
ARG BASE_IMAGE=python:3.15.1-slim-trixie@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51
# The Docker CLI and its compose plugin are static binaries, so the Alpine userland of this image never ships
ARG DOCKER_CLI_IMAGE=docker:29.9.3-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d
# The oras and cosign scripts/stack.py and scripts/_registry.py already run, at the same digests
ARG ORAS_IMAGE=ghcr.io/oras-project/oras:v1.3.6@sha256:9cfb578be975b762fd898b9d004243d8003aeb807a3e15b2ed212cdd1326c9eb
ARG COSIGN_IMAGE=gcr.io/projectsigstore/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d

FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${ORAS_IMAGE} AS oras
FROM ${COSIGN_IMAGE} AS cosign

FROM ${BASE_IMAGE}

# make runs the Makefile's own compose chain, git fetches the demo's generator, openssl mints the console certificate
# Unpinned (hadolint DL3008): the trixie archive drops a superseded package version, so a pinned one stops installing
RUN apt-get update \
    && apt-get install -y --no-install-recommends git make openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=oras /bin/oras /usr/local/bin/oras
COPY --from=cosign /ko-app/cosign /usr/local/bin/cosign

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/dfe-docker
COPY . .

# latest pins the newest published stack when init runs. A release build passes the stack it was cut against, so its version tag always installs that one
ARG STACK=latest
ARG VERSION=dev
# The reference init's printed commands name, so an install from a version tag keeps using that tag
ARG IMAGE=ghcr.io/hyperi-io/dfe-docker
ENV DFE_DOCKER_IMAGE=${IMAGE} \
    DFE_DOCKER_STACK=${STACK} \
    DFE_DOCKER_VERSION=${VERSION}

LABEL org.opencontainers.image.description="Sets up a folder that runs the DFE stack with docker compose" \
      org.opencontainers.image.licenses="BUSL-1.1" \
      org.opencontainers.image.source="https://github.com/hyperi-io/dfe-docker" \
      org.opencontainers.image.title="dfe-docker" \
      org.opencontainers.image.version="${VERSION}"

# No USER: the entrypoint starts as root only to take on the owner of the mounted folder, so what it writes there is that user's
ENTRYPOINT ["python3", "/opt/dfe-docker/scripts/installer.py"]
CMD ["--help"]
