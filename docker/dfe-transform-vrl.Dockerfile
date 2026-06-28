# Project:   dfe-docker
# File:      docker/dfe-transform-vrl.Dockerfile
# Purpose:   Dev build - compiles dfe-transform-vrl from source (not for production)
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published GHCR image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-transform-vrl + hyperi-rustlib)

FROM rust:latest@sha256:6df234c1eb92b0545468fab8c18fc5f9adfb994e7d4f67d81d45fe2fcabf5657 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    libsasl2-dev \
    libzstd-dev \
    libclang-dev \
    cmake \
    build-essential \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Shared library (path dep)
COPY hyperi-rustlib /deps/hyperi-rustlib

# Rewrite dynamic-linking -> cmake-build in hyperi-rustlib so rdkafka-sys
# compiles librdkafka from bundled source instead of requiring system lib
RUN sed -i 's|"dynamic-linking"|"cmake-build", "zstd", "zstd-pkg-config"|' /deps/hyperi-rustlib/Cargo.toml

# Transform-vrl source
COPY dfe-transform-vrl/Cargo.toml ./
COPY dfe-transform-vrl/src ./src

# Repoint hyperi-rustlib at the local checkout
RUN sed -i '/^hyperi-rustlib/s|version = "[^"]*"|path = "/deps/hyperi-rustlib"|' Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-transform-vrl-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-transform-vrl /usr/local/bin/

# Runtime stage mirrors ../dfe-transform-vrl/Dockerfile
FROM ubuntu:26.04@sha256:53958ec7b67c2c9355df922dd08dbf0360611f8c3cdb656875e81873db9ffdba

LABEL io.hyperi.profile="production"

# Runtime shared libraries for dynamically-linked Rust crates.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl netcat-openbsd iputils-ping gnupg \
    && curl -fsSL https://packages.confluent.io/clients/deb/archive.key \
       | gpg --dearmor -o /usr/share/keyrings/confluent-clients.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/confluent-clients.gpg] \
       https://packages.confluent.io/clients/deb noble main" \
       > /etc/apt/sources.list.d/confluent-clients.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       librdkafka1 libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-transform-vrl /usr/local/bin/dfe-transform-vrl
RUN chmod +x /usr/local/bin/dfe-transform-vrl

# Ubuntu 24.04 ships with ubuntu user at UID 1000 - remove before creating appuser
RUN userdel -r ubuntu && useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 9090 9000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:9090/health/live > /dev/null || exit 1

ENTRYPOINT ["dfe-transform-vrl"]
CMD ["--config", "/etc/dfe-transform-vrl/config.yaml"]

