ARG ARCHITECTURE
FROM --platform=${ARCHITECTURE} debian:trixie-slim AS receiver_download
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-receiver/${VERSION}/dfe-receiver-linux-$(dpkg --print-architecture)" \
      -o /dfe-receiver \
    && chmod +x /dfe-receiver

FROM --platform=${ARCHITECTURE} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates curl netcat-openbsd && rm -rf /var/lib/apt/lists/*
COPY --from=receiver_download /dfe-receiver /usr/local/bin/dfe-receiver
EXPOSE 9090
ENTRYPOINT ["dfe-receiver"]
CMD ["--config", "/etc/dfe-receiver/config.yaml"]
