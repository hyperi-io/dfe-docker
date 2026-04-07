#  Project:   dfe-docker
#  File:      docker/dfe-archiver.Dockerfile
#  Purpose:   Dev build — compiles dfe-archiver from source (not for production)
#  Language:  Dockerfile
#
#  License:   FSL-1.1-ALv2
#  Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published JFrog image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-archiver + hyperi-rustlib)

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

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-archiver /usr/local/bin/

RUN mkdir -p /var/data/archive

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -sf http://localhost:9090/health || exit 1

ENTRYPOINT ["dfe-archiver"]
CMD ["--config", "/etc/dfe-archiver/config.yaml"]
