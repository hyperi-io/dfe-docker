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
# librdkafka, OpenSSL, zstd and zlib link statically from hyperi-ci's static-libs prefix, as `build.rust.static_libs` does in CI, so the runtime image installs none of them.

# Pin an EXPLICIT Rust version, not `latest`. The digest alone makes the image
# immutable, but Renovate re-pins whatever `latest` currently points at, so a
# `latest` tag silently walks the builder across Rust major versions -- and if the
# builder's base drifts newer than the component's runtime base, the glibc the
# binary links against stops existing and it will not start. An explicit tag makes
# that a visible version bump in a PR instead of a digest nobody reads.
ARG RUST_IMAGE=rust:1.99-trixie@sha256:53766005847cbe02c3f59bfa49c1c4bb16695f2c9e5829c3d576a15406f5e733
FROM ${RUST_IMAGE} AS builder

# Default `sh -c` fails a pipeline only on its last command, so a failed step piped into the digest check below would pass unseen. (hadolint DL4006)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# mold because the apps' .cargo/config.toml links with -fuse-ld=mold on x86_64.
# DL3008 (pin apt versions) stays a visible warning, neither actioned nor suppressed: the digest-pinned RUST_IMAGE already fixes the package set, and Debian drops superseded point versions, so a per-package pin turns the next security update into a build break.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl pkg-config \
        libclang-dev cmake build-essential mold \
        protobuf-compiler libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

# hyperi-ci's pinned `static_libs_release` (src/hyperi_ci/config/versions.yaml): the tag and the sha256 of each arch's archive move together.
ARG STATIC_LIBS_TAG=static-libs-rdkafka2.16.0-openssl3.5.9-f87f317c3a39
ARG STATIC_LIBS_SHA256_AMD64=e00ca47ad334607e4ce4231506bc5ec3c16cd33293c60946ebb5b31d65a6236d
ARG STATIC_LIBS_SHA256_ARM64=bb0724afe40bb2d2b983f647c62bf62d73bc6b219227f336fcf5014c9c320f0e
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) triple=x86_64-unknown-linux-gnu sha256="${STATIC_LIBS_SHA256_AMD64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu sha256="${STATIC_LIBS_SHA256_ARM64}" ;; \
        *) echo "no static-libs archive for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/static-libs.tar.gz \
       "https://github.com/hyperi-io/hyperi-ci/releases/download/${STATIC_LIBS_TAG}/hyperi-static-libs-${triple}.tar.gz" \
    && echo "${sha256}  /tmp/static-libs.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/static-libs.tar.gz -C /opt \
    && rm /tmp/static-libs.tar.gz

# The variables hyperi-ci's static_libs.link_env sets. The prefix wins over the base image's own libssl-dev.
ENV PKG_CONFIG_PATH=/opt/hyperi-static-libs/lib/pkgconfig \
    RDKAFKA_STATIC=1 \
    OPENSSL_STATIC=1 \
    OPENSSL_DIR=/opt/hyperi-static-libs

WORKDIR /build

# Staged source context (Cargo.toml, Cargo.lock, src/, build.rs, proto/, etc.).
#
# `COPY . .` with no separate dependency pre-fetch layer is deliberate here. The
# usual reason to split manifests from sources is so a source-only edit does not
# re-download and rebuild every dependency -- but the cargo cache mount on the
# build below already survives across builds and does that job. The context is
# staged by scripts/build_dev_images.py with its own STAGE_EXCLUDES (target/,
# .git/), so a repo-root .dockerignore does not apply to this build.
COPY . .

# BINARY = the bin target / produced artefact (defaults to the crate's bin).
ARG BINARY
RUN --mount=type=cache,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp "/cache/cargo-target/release/${BINARY}" /out \
    && if readelf -d /out | grep -E 'NEEDED.*\[(librdkafka|libssl|libcrypto|libsasl2|libcurl|libz|libzstd)\.'; then \
        echo "${BINARY} still links a C library the runtime image does not install" >&2; exit 1; \
    fi

# Export stage: the bare binary, so the component's own Dockerfile can package it.
FROM scratch AS export
ARG BINARY
COPY --from=builder /out "/${BINARY}"
