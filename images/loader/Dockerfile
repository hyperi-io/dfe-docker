FROM debian:trixie-slim AS loader_download
ARG ARCHITECTURE
ARG JFROG_TOKEN
ARG JFROG_USERNAME
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN if [ -n "${JFROG_USERNAME}" ]; then \
      curl -fL -u "${JFROG_USERNAME}:${JFROG_TOKEN}" \
        "${REGISTRY}/dfe-loader/${VERSION}/dfe-loader-${VERSION}-${ARCHITECTURE}" \
        -o /dfe-loader; \
    else \
      curl -fL -H "Authorization: Bearer ${JFROG_TOKEN}" \
        "${REGISTRY}/dfe-loader/${VERSION}/dfe-loader-${VERSION}-${ARCHITECTURE}" \
        -o /dfe-loader; \
    fi && chmod +x /dfe-loader

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=loader_download /dfe-loader /usr/local/bin/dfe-loader
EXPOSE 9090
ENTRYPOINT ["dfe-loader"]
CMD ["--config", "/etc/dfe/loader.yaml"]
