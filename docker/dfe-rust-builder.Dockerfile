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

# mold because the apps' .cargo/config.toml link with -fuse-ld=mold
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl libclang-dev libprotobuf-dev \
        mold pkg-config protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# hyperi-ci's pinned static_libs_release: the tag and each arch's sha256 move together
ARG STATIC_LIBS_TAG=static-libs-rdkafka2.16.0-openssl3.5.9-f87f317c3a39
ARG STATIC_LIBS_SHA256_AMD64=e00ca47ad334607e4ce4231506bc5ec3c16cd33293c60946ebb5b31d65a6236d
ARG STATIC_LIBS_SHA256_ARM64=bb0724afe40bb2d2b983f647c62bf62d73bc6b219227f336fcf5014c9c320f0e
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) triple=x86_64-unknown-linux-gnu sha256="${STATIC_LIBS_SHA256_AMD64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu sha256="${STATIC_LIBS_SHA256_ARM64}" ;; \
        *) echo "no static-libs archive for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL --retry 3 -o /tmp/static-libs.tar.gz \
       "https://github.com/hyperi-io/hyperi-ci/releases/download/${STATIC_LIBS_TAG}/hyperi-static-libs-${triple}.tar.gz" \
    && echo "${sha256}  /tmp/static-libs.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/static-libs.tar.gz -C /opt \
    && rm /tmp/static-libs.tar.gz

# hyperi-ci's static_libs.link_env. The prefix wins over the base image's libssl-dev
ENV PKG_CONFIG_PATH=/opt/hyperi-static-libs/lib/pkgconfig \
    RDKAFKA_STATIC=1 \
    OPENSSL_STATIC=1 \
    OPENSSL_DIR=/opt/hyperi-static-libs

WORKDIR /build

COPY . .

ARG BINARY
RUN --mount=type=cache,target=/cache/cargo-target \
    CARGO_TARGET_DIR=/cache/cargo-target cargo build --release \
    && cp "/cache/cargo-target/release/${BINARY}" /out \
    && needed="$(readelf -d /out | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')" \
    && extra="$(printf '%s\n' "${needed}" | grep -v -E '^(libc|libm|libgcc_s|libpthread|libdl|librt)\.so\.[0-9]+$|^ld-linux-(x86-64|aarch64)\.so\.[0-9]+$' || true)" \
    && if [ -n "${extra}" ]; then \
        echo "${BINARY} links C libraries outside the libc set: ${extra}" >&2; exit 1; \
    fi

# Just the binary, for the app's own Dockerfile to package
FROM scratch AS export
ARG BINARY
COPY --from=builder /out "/${BINARY}"
