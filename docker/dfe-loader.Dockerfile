# Project:   dfe-docker
# File:      docker/dfe-loader.Dockerfile
# Purpose:   Dev build - compiles dfe-loader from source (not for production)
# Language:  Dockerfile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published GHCR image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: PROJECTS_PATH (parent dir containing dfe-loader + hyperi-rustlib + clickhouse-arrow)

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

# Copy shared libraries
COPY hyperi-rustlib /deps/hyperi-rustlib
COPY clickhouse-arrow /deps/clickhouse-arrow
COPY clickhouse-rs /deps/clickhouse-rs

# Strip registry = "hyperi" from clickhouse-arrow's internal dep
# (it already has path = "../clickhouse-arrow-derive", just needs registry removed)
RUN find /deps/clickhouse-arrow -name Cargo.toml -exec \
    sed -i 's|, registry = "hyperi"||g' {} +

# Rewrite dynamic-linking → cmake-build in hyperi-rustlib so rdkafka-sys
# compiles librdkafka from bundled source instead of requiring system lib
RUN sed -i 's|"dynamic-linking"|"cmake-build", "zstd", "zstd-pkg-config"|' /deps/hyperi-rustlib/Cargo.toml

# Copy loader source
COPY dfe-loader/Cargo.toml ./
COPY dfe-loader/src ./src
COPY dfe-loader/benches ./benches
COPY dfe-loader/mappings ./mappings

# Rewrite Cargo.toml: replace private registry refs with local paths,
# add zstd-pkg-config feature so librdkafka links system libzstd.
# Delete Cargo.lock so Cargo resolves fresh against the new paths.
RUN sed -i \
    -e '/^hyperi-rustlib/s|version = "[^"]*"|path = "/deps/hyperi-rustlib"|' \
    -e 's|version = ">=0.4.0", registry = "hyperi"|path = "/deps/clickhouse-arrow/clickhouse-arrow"|' \
    -e 's|"dynamic-linking", "ssl", "sasl"|"cmake-build", "ssl", "sasl", "zstd", "zstd-pkg-config"|' \
    -e 's|clickhouse = { git = "[^"]*", branch = "[^"]*" }|clickhouse = { path = "/deps/clickhouse-rs" }|' \
    Cargo.toml

# Cache mount keeps compiled deps across builds; source changes trigger recompile
# but dep crates stay cached in /cache/cargo-target (~30s rebuild vs ~3min full).
RUN --mount=type=cache,id=dfe-loader-target,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp /cache/cargo-target/release/dfe-loader /usr/local/bin/

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-loader /usr/local/bin/

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/spool/dfe/dlq \
    && chown -R appuser:appuser /var/spool/dfe
USER appuser

EXPOSE 9090 50051

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -sf http://localhost:9090/health || exit 1

ENTRYPOINT ["dfe-loader"]
CMD ["--config", "/etc/dfe/loader.yaml"]
