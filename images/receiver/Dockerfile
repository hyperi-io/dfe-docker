FROM debian:trixie-slim AS receiver_download
ARG ARCHITECTURE
ARG JFROG_TOKEN
ARG JFROG_USERNAME
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN if [ -n "${JFROG_USERNAME}" ]; then \
      curl -fL -u "${JFROG_USERNAME}:${JFROG_TOKEN}" \
        "${REGISTRY}/dfe-receiver/${VERSION}/dfe-receiver-${VERSION}-${ARCHITECTURE}" \
        -o /dfe-receiver; \
    else \
      curl -fL -H "Authorization: Bearer ${JFROG_TOKEN}" \
        "${REGISTRY}/dfe-receiver/${VERSION}/dfe-receiver-${VERSION}-${ARCHITECTURE}" \
        -o /dfe-receiver; \
    fi && chmod +x /dfe-receiver

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=receiver_download /dfe-receiver /usr/local/bin/dfe-receiver
EXPOSE 9090
ENTRYPOINT ["dfe-receiver"]
CMD ["--config", "/etc/dfe-receiver/config.yaml"]
