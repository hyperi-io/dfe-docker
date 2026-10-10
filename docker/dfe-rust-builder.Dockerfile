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

# Pin an EXPLICIT Rust version, not `latest`. The digest alone makes the image
# immutable, but Renovate re-pins whatever `latest` currently points at, so a
# `latest` tag silently walks the builder across Rust major versions -- and if the
# builder's base drifts newer than the component's runtime base, the glibc the
# binary links against stops existing and it will not start. An explicit tag makes
# that a visible version bump in a PR instead of a digest nobody reads.
ARG RUST_IMAGE=rust:1.99-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181
FROM ${RUST_IMAGE} AS builder

# Default `sh -c` does not fail a pipeline when an EARLY stage fails -- only the
# last command's status counts. The Confluent key install below is `curl | gpg`,
# so without pipefail a failed download would still write a valid-looking (empty)
# keyring and the build would carry on to a confusing apt error. (hadolint DL4006)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The Confluent apt suite below is `bookworm` on a trixie base. That is a
# deliberate cross-release install, not a stale copy-paste: Confluent publishes no
# trixie suite (checked 2026-07-20 -- the newest Debian suite they ship is
# bookworm). librdkafka-dev is a C library against glibc, and trixie's glibc is
# backward compatible with bookworm's, so the bookworm build links fine. Revisit
# the day a trixie suite appears.
#
# DL3008 (pin apt versions) fires here and is deliberately NOT actioned, and
# deliberately NOT suppressed either -- it stays visible as a warning. The gate
# is `--failure-threshold error`, matching the house gate, so a warning informs
# without blocking; an `# hadolint ignore=` pragma would delete the information
# instead.
#
# Why we do not pin: reproducibility here comes from ARG RUST_IMAGE being
# DIGEST-pinned, so the digest already fixes the package set and per-package apt
# pins restate it in a second place that can disagree. They also rot badly --
# Debian drops superseded point versions from the archive, so a pinned version
# becomes an unbuildable image the moment a security update lands, turning a
# security patch into a build break. If RUST_IMAGE ever stops being
# digest-pinned, that reasoning stops holding.
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
    && cp "/cache/cargo-target/release/${BINARY}" /out

# Export stage: the bare binary, so the component's own Dockerfile can package it.
FROM scratch AS export
ARG BINARY
COPY --from=builder /out "/${BINARY}"
