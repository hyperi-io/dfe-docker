# Project:   dfe-docker
# File:      docker/dfe-loader.Dockerfile
# Purpose:   Dev build — compiles dfe-loader from source (not for production)
# Language:  Dockerfile
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# For production: use the published JFrog image (docker-compose.yml default).
# This Dockerfile is only used via docker-compose.override.yml in dev mode.
#
# Build context: /projects/dfe-loader (the component repo root)

FROM rust:1.82-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY Cargo.toml Cargo.lock ./
COPY src ./src

RUN cargo build --release --features transport-grpc

RUN cp target/release/dfe-loader /usr/local/bin/

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/dfe-loader /usr/local/bin/

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 9090 50051

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -sf http://localhost:9090/health/live || exit 1

ENTRYPOINT ["dfe-loader"]
CMD ["--config", "/etc/dfe/loader.yaml"]
