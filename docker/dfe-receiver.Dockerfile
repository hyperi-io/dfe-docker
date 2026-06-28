# Project:   dfe-docker
# File:      docker/dfe-receiver.Dockerfile
# Purpose:   Dev build - compiles dfe-receiver from source (not for production)
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published GHCR image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-receiver + hyperi-rustlib)

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
    -e 's|"cmake-build", "ssl", "sasl"|"cmake-build", "ssl", "sasl", "zstd", "zstd-pkg-config"|' \
    Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-receiver-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-receiver /usr/local/bin/

# Runtime stage mirrors ../dfe-receiver/Dockerfile
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
       librdkafka1 libssl3 zlib1g libzstd1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-receiver /usr/local/bin/dfe-receiver
RUN chmod +x /usr/local/bin/dfe-receiver

# Ubuntu 24.04 ships with ubuntu user at UID 1000 - remove before creating appuser
RUN userdel -r ubuntu && useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 9090 8080 6000 4317 4318 5044 8088 9091 514 6514 24224 12201

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:9090/health/live > /dev/null || exit 1

ENTRYPOINT ["dfe-receiver"]
CMD ["--config", "/etc/dfe-receiver/config.yaml"]
