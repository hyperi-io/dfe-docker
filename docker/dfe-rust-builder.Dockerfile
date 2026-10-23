# Project:   dfe-docker
# File:      docker/dfe-rust-builder.Dockerfile
# Purpose:   Compiles a DFE Rust app from local source for make dev
# Language:  Dockerfile
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

# Trixie to match the glibc of the apps' runtime image
ARG RUST_IMAGE=rust:1.99-trixie@sha256:53766005847cbe02c3f59bfa49c1c4bb16695f2c9e5829c3d576a15406f5e733
FROM ${RUST_IMAGE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Confluent has no trixie suite, so librdkafka comes from bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl gnupg libclang-dev libprotobuf-dev \
        libsasl2-dev libssl-dev libzstd-dev pkg-config protobuf-compiler \
    && curl -fsSL https://packages.confluent.io/clients/deb/archive.key \
        | gpg --dearmor -o /usr/share/keyrings/confluent-clients.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/confluent-clients.gpg] https://packages.confluent.io/clients/deb bookworm main" \
        > /etc/apt/sources.list.d/confluent-clients.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY . .

ARG BINARY
RUN --mount=type=cache,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp "/cache/cargo-target/release/${BINARY}" /out

# Just the binary, for the app's own Dockerfile to package
FROM scratch AS export
ARG BINARY
COPY --from=builder /out "/${BINARY}"
