# Project:   dfe-docker
# File:      docker/dfe-transform-vector.Dockerfile
# Purpose:   Dev build - compiles dfe-transform-vector from source (not for production)
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published GHCR image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-transform-vector + hyperi-rustlib)

FROM rust:latest@sha256:6df234c1eb92b0545468fab8c18fc5f9adfb994e7d4f67d81d45fe2fcabf5657 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Shared library (path dep)
COPY hyperi-rustlib /deps/hyperi-rustlib

# Transform-vector source
COPY dfe-transform-vector/Cargo.toml ./
COPY dfe-transform-vector/src ./src

# Repoint hyperi-rustlib at the local checkout
RUN sed -i '/^hyperi-rustlib/s|version = "[^"]*"|path = "/deps/hyperi-rustlib"|' Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-transform-vector-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-transform-vector /usr/local/bin/

# Runtime stage mirrors ../dfe-transform-vector/Dockerfile
FROM ubuntu:26.04@sha256:53958ec7b67c2c9355df922dd08dbf0360611f8c3cdb656875e81873db9ffdba

LABEL io.hyperi.profile="production"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl netcat-openbsd iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-transform-vector /usr/local/bin/dfe-transform-vector
RUN chmod +x /usr/local/bin/dfe-transform-vector

# Ubuntu 24.04 ships with ubuntu user at UID 1000 - remove before creating appuser
RUN userdel -r ubuntu && useradd --create-home --uid 1000 appuser
# Vector binary - downloaded inside the build for portability.
# Pinned to 0.48.0; bump deliberately (CLI flags + config
# schema can shift between minor versions). Multi-arch via $TARGETARCH.
ARG VECTOR_VERSION=0.48.0
ARG TARGETARCH
RUN set -eu \
 && case "${TARGETARCH:-amd64}" in \
     amd64) ARCH=x86_64 ;; \
     arm64) ARCH=aarch64 ;; \
     *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
 esac \
 && curl -fsSL "https://packages.timber.io/vector/${VECTOR_VERSION}/vector-${VECTOR_VERSION}-${ARCH}-unknown-linux-gnu.tar.gz" \
         -o /tmp/vector.tar.gz \
 && tar xz -C /tmp -f /tmp/vector.tar.gz \
 && mv "/tmp/vector-${ARCH}-unknown-linux-gnu/bin/vector" /usr/local/bin/vector \
 && chmod +x /usr/local/bin/vector \
 && rm -rf /tmp/vector.tar.gz "/tmp/vector-${ARCH}-unknown-linux-gnu" \
 && /usr/local/bin/vector --version

# Vector data and config directories
RUN mkdir -p /var/lib/vector /var/run/vector/config /etc/dfe-transform-vector/transforms \
     && chown -R appuser:appuser /var/lib/vector /var/run/vector /etc/dfe-transform-vector

LABEL io.hyperi.vector.version="0.48.0"

USER appuser

EXPOSE 9090 9000 8686

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:9090/health/live > /dev/null || exit 1

ENTRYPOINT ["dfe-transform-vector"]
CMD ["--config", "/etc/dfe-transform-vector/config.yaml"]
