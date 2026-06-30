# Project:   dfe-docker
# File:      docker/dfe-rust-builder.Dockerfile
# Purpose:   Dev builder - compiles any DFE rust component from local source
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Shared across every DFE rust component (dev mode only).
# scripts/build_dev_images.py stages the component's source into a clean context (minus target/.git) and builds this with BINARY set, exporting just the compiled binary.
# The runtime image is then built from the component's own Dockerfile (single source of truth) with that binary in context - so this file owns the build, the component owns the runtime.
#
# Post-scalo-2.10: components depend on the published `scalo` crate (no local hyperi-rustlib), so there are no sibling-dep COPYs or Cargo.toml rewrites. rdkafka uses dynamic-linking against librdkafka-dev from the Confluent apt repo, matching the runtime image's Confluent librdkafka1 soname (mirrors hyperi-ci's build recipe).

ARG RUST_IMAGE=rust:latest@sha256:6df234c1eb92b0545468fab8c18fc5f9adfb994e7d4f67d81d45fe2fcabf5657
FROM ${RUST_IMAGE} AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg pkg-config \
        libssl-dev libsasl2-dev libzstd-dev libclang-dev cmake build-essential \
        protobuf-compiler libprotobuf-dev \
    && curl -fsSL https://packages.confluent.io/clients/deb/archive.key \
       | gpg --dearmor -o /usr/share/keyrings/confluent-clients.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/confluent-clients.gpg] \
       https://packages.confluent.io/clients/deb bookworm main" \
       > /etc/apt/sources.list.d/confluent-clients.list \
    && apt-get update && apt-get install -y --no-install-recommends librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Staged source context (Cargo.toml, Cargo.lock, src/, build.rs, proto/, etc.).
COPY . .

# BINARY = the bin target / produced artefact (defaults to the crate's bin).
ARG BINARY
RUN --mount=type=cache,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp "/cache/cargo-target/release/${BINARY}" /out

# Export stage: the bare binary, so the component's own Dockerfile can package it.
FROM scratch AS export
ARG BINARY
COPY --from=builder /out "/${BINARY}"
