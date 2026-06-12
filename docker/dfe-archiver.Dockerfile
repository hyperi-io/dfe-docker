# Project:   dfe-docker
# File:      docker/dfe-archiver.Dockerfile
# Purpose:   Dev build - compiles dfe-archiver from source (not for production)
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published GHCR image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-archiver + hyperi-rustlib)

FROM rust:latest@sha256:4fd8406017c992f7b8ab55a2f99a1d56aeb1d7ecd255850dfa04239a88601f73 AS builder

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

# Copy the shared library
COPY hyperi-rustlib /deps/hyperi-rustlib

# Rewrite dynamic-linking → cmake-build in hyperi-rustlib so rdkafka-sys
# compiles librdkafka from bundled source instead of requiring system lib
RUN sed -i 's|"dynamic-linking"|"cmake-build", "zstd", "zstd-pkg-config"|' /deps/hyperi-rustlib/Cargo.toml

# Copy archiver source (workspace with subcrates, no root src/)
COPY dfe-archiver/Cargo.toml ./
COPY dfe-archiver/crates ./crates

# Rewrite Cargo.toml: replace private registry refs with local paths,
# switch rdkafka from dynamic-linking to cmake-build.
RUN sed -i \
    -e '/^hyperi-rustlib/s|version = "[^"]*"|path = "/deps/hyperi-rustlib"|' \
    -e 's|"dynamic-linking"|"cmake-build", "zstd", "zstd-pkg-config"|' \
    Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-archiver-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-archiver /usr/local/bin/

# Runtime stage mirrors ../dfe-archiver/Dockerfile
FROM ubuntu:24.04@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl netcat-openbsd iputils-ping gnupg \
    && curl -fsSL https://packages.confluent.io/clients/deb/archive.key \
       | gpg --dearmor -o /usr/share/keyrings/confluent-clients.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/confluent-clients.gpg] \
       https://packages.confluent.io/clients/deb noble main" \
       > /etc/apt/sources.list.d/confluent-clients.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       librdkafka1 libssl3 libzstd1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-archiver /usr/local/bin/dfe-archiver
RUN chmod +x /usr/local/bin/dfe-archiver

RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:9090/healthz > /dev/null || exit 1

ENTRYPOINT ["dfe-archiver"]
CMD ["--config", "/etc/dfe/archiver.yaml"]
