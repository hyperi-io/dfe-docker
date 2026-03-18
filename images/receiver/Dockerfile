# PLATFORM must match ARCHITECTURE: linux-amd64 -> linux/amd64, linux-arm64 -> linux/arm64
# Required on Apple Silicon when using x86_64 binaries — arm64 base lacks /lib64/ld-linux-x86-64.so.2
ARG PLATFORM=linux/amd64
FROM --platform=${PLATFORM} debian:trixie-slim AS receiver_download
ARG ARCHITECTURE
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-receiver/${VERSION}/dfe-receiver-release-${ARCHITECTURE}" \
      -o /dfe-receiver \
    && chmod +x /dfe-receiver

ARG PLATFORM=linux/amd64
FROM --platform=${PLATFORM} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=receiver_download /dfe-receiver /usr/local/bin/dfe-receiver
EXPOSE 9090
ENTRYPOINT ["dfe-receiver"]
CMD ["--config", "/etc/dfe-receiver/config.yaml"]
