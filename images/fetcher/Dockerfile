ARG ARCHITECTURE
FROM --platform=${ARCHITECTURE} debian:trixie-slim AS fetcher_download
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-fetcher/${VERSION}/dfe-fetcher-linux-$(dpkg --print-architecture)" \
      -o /dfe-fetcher \
    && chmod +x /dfe-fetcher

FROM --platform=${ARCHITECTURE} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates curl netcat-openbsd && rm -rf /var/lib/apt/lists/*
COPY --from=fetcher_download /dfe-fetcher /usr/local/bin/dfe-fetcher
EXPOSE 9090
ENTRYPOINT ["dfe-fetcher"]
CMD ["--config", "/etc/dfe-fetcher/config.yaml"]
