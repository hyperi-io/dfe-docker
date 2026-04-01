ARG ARCHITECTURE
FROM --platform=${ARCHITECTURE} debian:trixie-slim AS archiver_download
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-archiver/${VERSION}/dfe-archiver-linux-$(dpkg --print-architecture)" \
      -o /dfe-archiver \
    && chmod +x /dfe-archiver

FROM --platform=${ARCHITECTURE} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates curl netcat-openbsd && rm -rf /var/lib/apt/lists/*
COPY --from=archiver_download /dfe-archiver /usr/local/bin/dfe-archiver
EXPOSE 9090
ENTRYPOINT ["dfe-archiver"]
CMD ["--config", "/etc/dfe-archiver/config.yaml"]
