# PLATFORM must match ARCHITECTURE: linux-amd64 -> linux/amd64, linux-arm64 -> linux/arm64
# Required on Apple Silicon when using x86_64 binaries — arm64 base lacks /lib64/ld-linux-x86-64.so.2
ARG PLATFORM=linux/amd64
FROM --platform=${PLATFORM} debian:trixie-slim AS loader_download
ARG ARCHITECTURE
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-loader/${VERSION}/dfe-loader-release-${ARCHITECTURE}" \
      -o /dfe-loader \
    && chmod +x /dfe-loader

ARG PLATFORM=linux/amd64
FROM --platform=${PLATFORM} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=loader_download /dfe-loader /usr/local/bin/dfe-loader
EXPOSE 9090
ENTRYPOINT ["dfe-loader"]
CMD ["--config", "/etc/dfe/loader.yaml"]
