ARG ARCHITECTURE
FROM --platform=${ARCHITECTURE} debian:trixie-slim AS loader_download
ARG REGISTRY
ARG VERSION
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fL \
      "${REGISTRY}/dfe-loader/${VERSION}/dfe-loader-linux-$(dpkg --print-architecture)" \
      -o /dfe-loader \
    && chmod +x /dfe-loader

FROM --platform=${ARCHITECTURE} debian:trixie-slim
RUN apt-get update && apt-get install -y libssl3 libsasl2-2 librdkafka1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=loader_download /dfe-loader /usr/local/bin/dfe-loader
EXPOSE 9090
ENTRYPOINT ["dfe-loader"]
CMD ["--config", "/etc/dfe/loader.yaml"]
