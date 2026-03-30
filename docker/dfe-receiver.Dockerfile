# Project:   dfe-docker
# File:      docker/dfe-receiver.Dockerfile
# Purpose:   Dev build — compiles dfe-receiver from source (not for production)
# Language:  Dockerfile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published JFrog image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-receiver + hyperi-rustlib)

FROM rust:latest AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    libsasl2-dev \
    libzstd-dev \
    libclang-dev \
    cmake \
    build-essential \
    protobuf-compiler \
    libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the shared library
COPY hyperi-rustlib /deps/hyperi-rustlib

# Copy receiver source
COPY dfe-receiver/Cargo.toml ./
COPY dfe-receiver/build.rs ./build.rs
COPY dfe-receiver/src ./src
COPY dfe-receiver/proto ./proto
COPY dfe-receiver/benches ./benches

# Rewrite dynamic-linking → cmake-build in hyperi-rustlib so rdkafka-sys
# compiles librdkafka from bundled source instead of requiring system lib
RUN sed -i 's|"dynamic-linking"|"cmake-build", "zstd", "zstd-pkg-config"|' /deps/hyperi-rustlib/Cargo.toml

# Rewrite Cargo.toml: replace private registry refs with local paths,
# remove optional plugin deps (not available locally),
# add zstd-pkg-config feature so librdkafka links system libzstd.
# Delete Cargo.lock so Cargo resolves fresh against the new paths.
RUN sed -i \
    -e '/^hyperi-rustlib/s|version = "[^"]*"|path = "/deps/hyperi-rustlib"|' \
    -e '/^dfe-plugin-loader.*registry = "hyperi"/d' \
    -e '/^dfe-protocol-sdk.*registry = "hyperi"/d' \
    -e '/^plugins = \[/d' \
    -e 's|"dynamic-linking"|"cmake-build"|' \
    -e 's|"cmake-build", "ssl", "sasl"|"cmake-build", "ssl", "sasl", "zstd", "zstd-pkg-config"|' \
    Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-receiver-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-receiver /usr/local/bin/

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-receiver /usr/local/bin/

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 9090 8080 6000 4317 4318 5044 8088

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -sf http://localhost:9090/health || exit 1

ENTRYPOINT ["dfe-receiver"]
CMD ["--config", "/etc/dfe-receiver/config.yaml"]
