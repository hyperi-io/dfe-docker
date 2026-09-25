<!--
Project:   dfe-docker
File:      docs/configuration.md
Purpose:   Every environment variable, port and image the stack reads
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Configuration reference

Lookup, not narrative. Every variable the stack reads, what it does, and what it
defaults to. [.env.example](../.env.example) is the annotated template these come
from.

Image pins (`*_VERSION`) are written by `make stack VERSION=X.Y.Z` from the DFE
stack SSoT. Compose hard-fails on an unset pin rather than resolving `latest`, so
run it before any compose command -- [deploying.md](deploying.md).

## Environment Variables

### Profile Selection

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_PROFILE`                                    | Override active profile from service_profiles.yaml                                   | -                                                                   |
| `DFE_CONTAINER_PREFIX`                           | Put in front of every container name, per-source instances included, so a second stack can share the daemon; `make` writes `docker-compose.prefix.yml` from it | -                                     |

### Host exposure

Which interface each published port binds. Ports are grouped by audience --
[operating.md](operating.md#port-exposure-is-split-by-audience) has the per-port
table and the UI classes.

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_INGRESS_BIND_HOST`                          | Interface the receiver and fetcher ingest ports bind                                 | `0.0.0.0`                                                           |
| `DFE_BIND_HOST`                                  | Interface the datastore, broker, metrics and internal gRPC ports bind                | `127.0.0.1`                                                         |
| `DFE_BIND_SCOPE`                                 | Where every web UI publishes (`localhost`\|`all`); `make` resolves it to `DFE_UI_BIND_HOST`, and `all` requires `DFE_EXTERNAL_ORIGIN` | `localhost`                            |
| `DFE_INFRA_UIS_EXTERNAL`                         | Kill switch -- `false` unpublishes every infra-class UI and beats their own flags    | `true`                                                              |
| `DFE_UI_EXTERNAL`                                | Publish the DFE UI (dfe-proxy `:3000`); product class, kill switch never covers it   | `true`                                                              |
| `DFE_ENGINE_API_EXTERNAL`                        | Publish the engine API (`:8003`); product class                                      | `true`                                                              |
| `DFE_KAFBAT_UI_EXTERNAL`                         | Publish Kafbat (`:8081`); infra class                                                | `true`                                                              |
| `DFE_HYPERDX_UI_EXTERNAL`                        | Publish HyperDX (`:8090` and its API `:8000`); infra class                           | `true`                                                              |

### External origin

The scheme and host a browser reaches this box on, with no port and no trailing
slash -- every service appends its own port. Four surfaces build absolute URLs
from it, and each is wrong when it says `localhost` and the browser did not: the
DFE UI's post-logout redirect, the oauth2-proxy callbacks registered with the
IdP, HyperDX's link-backs into the DFE UI, and the `frame-ancestors` policy the
proxy serves HyperDX under. The first three send the browser somewhere it cannot
follow; the fourth blocks the console's embedded views outright.

It defaults to `http://localhost`, and nothing infers the real one -- so
reaching the stack on a hostname without setting this is what sends every
logout to `http://localhost:3000`. Set it on any deployment a browser reaches
by anything other than `localhost`; `DFE_BIND_SCOPE=all` requires it, and `make`
stops the start goals until it is set.

The ancestors list keeps `http://localhost` and `http://127.0.0.1` at the UI port
alongside it, so setting an origin never takes the console away from an operator
working on the box itself.

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_EXTERNAL_ORIGIN`                            | Browser-facing scheme and host for every absolute URL the stack builds                | `http://localhost`                                                  |

### Infra UI authentication (opt-in)

Arms an oauth2-proxy per infra-UI origin --
[operating.md](operating.md#gating-the-infra-uis-with-oidc). Off by default; no
other profile may require an issuer.

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_AUTH_ENABLED`                               | Arm the `auth` compose profile (also a `service_profiles.yaml` footprint key)        | `false`                                                             |
| `DFE_OIDC_ISSUER_URL`                            | OIDC issuer; required when the profile is armed                                      | -                                                                   |
| `DFE_OIDC_CLIENT_ID`                             | OAuth client id; required when the profile is armed                                  | -                                                                   |
| `DFE_OIDC_CLIENT_SECRET`                         | OAuth client secret; required when the profile is armed                              | -                                                                   |
| `DFE_OIDC_ALLOWED_GROUPS`                        | Groups allowed through the gate; blank means authn alone and is refused              | `dfe-infra,dfe-admin`                                               |
| `DFE_OIDC_EMAIL_DOMAINS`                         | Extra email-domain restriction on top of the group check                             | `*`                                                                 |
| `DFE_OAUTH2_PROXY_COOKIE_SECRET`                 | Session cookie key, shared by all three proxies; `make init` generates 32 bytes      | generated                                                           |
| `DFE_OAUTH2_PROXY_COOKIE_DOMAIN`                 | Cookie domain; blank is a host-only cookie, which shares one session across the box  | blank                                                               |
| `DFE_OAUTH2_PROXY_COOKIE_SECURE`                 | Set the Secure cookie flag; `true` only behind real TLS                              | `false`                                                             |
| `DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN`               | Base origin the OAuth redirect URLs are built from; overrides `DFE_EXTERNAL_ORIGIN`  | follows `DFE_EXTERNAL_ORIGIN`                                       |
| `DFE_OAUTH2_PROXY_VERSION`                       | Override the oauth2-proxy image pin; not SSoT-derived, keep the `tag@sha256` form    | pinned in `docker-compose.yml`                                      |

### Image Registry

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DOCKER_DEFAULT_PLATFORM`                        | Image architecture to pull from docker (if not wanting automatic determination)      | -                                                                   |
| `IMAGE_REGISTRY`                                 | OCI registry hosting published `dfe-*` images                                        | `ghcr.io/hyperi-io`                                                 |

### Dev Builds

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_SRC_ROOT`                                   | Directory holding your local `dfe-*` checkouts; set = build (and `LIVE=1` mount) those instead of the git cache | unset (git cache)                                 |
| `DFE_SRC_REMOTE`                                 | Git base URL the managed cache clones component repos from                           | `https://github.com/hyperi-io`                                      |
| `DFE_SRC_REF`                                    | Branch, tag or commit the managed cache builds                                       | `main`                                                              |
| `DFE_SRC_CACHE`                                  | Location of the managed source cache                                                 | `~/.cache/dfe-docker/src`                                           |

### DFE Components - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `LOG_LEVEL`                                      | Log level (trace\|debug\|info\|warn\|error)                                          | `info`                                                              |
| `LOG_FORMAT`                                     | Log format                                                                           | `text`                                                              |

### DFE Archiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_ARCHIVER_VERSION`                           | Version of dfe-archiver to use                                                       | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_ARCHIVER_PROMETHEUS_PORT`                   | Archiver Prometheus port                                                             | `9093`                                                              |

### DFE Fetcher

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_FETCHER_VERSION`                            | Version of dfe-fetcher to use                                                        | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_FETCHER_INGEST_PORT`                        | Fetcher ingest port                                                                  | `8082`                                                              |
| `DFE_FETCHER_PROMETHEUS_PORT`                    | Fetcher Prometheus port                                                              | `9094`                                                              |
| `AWS_ACCESS_KEY_ID`                              | AWS access key. Goes in `env/fetcher.env`, NOT `.env` -- the fetcher config reads it via `env:`  | -                                    |
| `AWS_SECRET_ACCESS_KEY`                          | AWS secret key. Same file as above                                                   | -                                                                   |

The AWS **region** is not an environment variable: it is a literal in
`config/fetcher/aws-*.yaml` (`region: us-west-2`), because only the two
credential fields are `env:`-interpolated. Change it there.

### DFE Loader

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_LOADER_VERSION`                             | Version of dfe-loader to use                                                         | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_LOADER_PROMETHEUS_PORT`                     | Loader Prometheus port                                                               | `9091`                                                              |
| `DFE_LOADER_GRPC_PORT`                           | Host port the loader's gRPC listener (container `:6000`) is published on             | `50051`                                                             |

### DFE Receiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_RECEIVER_VERSION`                           | Version of dfe-receiver to use                                                       | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_RECEIVER_BEATS_PORT`                        | Receiver Beats port                                                                  | `5044`                                                              |
| `DFE_RECEIVER_GRPC_PORT`                         | Receiver gRPC port                                                                   | `6000`                                                              |
| `DFE_RECEIVER_HEC_PORT`                          | Receiver HEC port                                                                    | `8088`                                                              |
| `DFE_RECEIVER_HTTP_PORT`                         | Receiver HTTP port                                                                   | `8080`                                                              |
| `DFE_RECEIVER_OTLP_GRPC_PORT`                    | Receiver OTLP gRPC port                                                              | `4317`                                                              |
| `DFE_RECEIVER_OTLP_HTTP_PORT`                    | Receiver OTLP HTTP port                                                              | `4318`                                                              |
| `DFE_RECEIVER_PROMETHEUS_PORT`                   | Receiver Prometheus port                                                             | `9090`                                                              |

### DFE Transform Elastic

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_ELASTIC_VERSION`                  | Version of dfe-transform-elastic to use                                              | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_TRANSFORM_ELASTIC_PROMETHEUS_PORT`          | Transform Elastic Prometheus port                                                    | `9099`                                                              |
| `DFE_TRANSFORM_ELASTIC_CISCO_IOS_PROMETHEUS_PORT` | Prometheus port of the cisco-ios instance -- it runs the same image, so it needs its own; 9100 is node_exporter's | `9089`                                                          |

### DFE Transform Vector

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VECTOR_VERSION`                   | Version of dfe-transform-vector to use                                               | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_TRANSFORM_VECTOR_PROMETHEUS_PORT`           | Transform Vector Prometheus port                                                     | `9095`                                                              |

### DFE Transform VRL

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VRL_VERSION`                      | Version of dfe-transform-vrl to use                                                  | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_TRANSFORM_VRL_PROMETHEUS_PORT`              | Transform VRL Prometheus port                                                        | `9096`                                                              |
| `DFE_TRANSFORM_VRL_FILEBEAT_PROMETHEUS_PORT`     | Prometheus port of the filebeat instance -- it runs the same image, so it needs its own | `9097`                                                           |

The filebeat instance takes its version from `DFE_TRANSFORM_VRL_VERSION` as well:
it is a second deployment of the one component, not a component of its own.

### ClickHouse

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `CLICKHOUSE_VERSION`                             | Version of ClickHouse to use                                                         | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `CLICKHOUSE_HOST`                                | External ClickHouse host (skips Docker container)                                    | `clickhouse`                                                        |
| `CLICKHOUSE_HTTP_PORT`                           | ClickHouse HTTP port, in-network as well as on the host                              | `8123`                                                              |
| `CLICKHOUSE_NATIVE_PORT`                         | ClickHouse native protocol port, in-network as well as on the host                   | `9000`                                                              |
| `CLICKHOUSE_HTTP_HOST_PORT`                      | The host side of the HTTP publish alone, for a second stack on one box               | `CLICKHOUSE_HTTP_PORT`                                              |
| `CLICKHOUSE_NATIVE_HOST_PORT`                    | The host side of the native publish alone, for a second stack on one box             | `CLICKHOUSE_NATIVE_PORT`                                            |
| `CLICKHOUSE_DB`                                  | ClickHouse initialisation database                                                   | `default`                                                           |
| `CLICKHOUSE_USERNAME`                            | ClickHouse username to connect with                                                  | `default`                                                           |
| `CLICKHOUSE_PASSWORD`                            | ClickHouse password associated to user                                               | -                                                                   |
| `DFE_CLICKHOUSE_DEFAULT_TTL_DAYS`                | Days every time-series table keeps rows, the OTel tables included; 0 disables the default TTL; a source or dfe-schemas TTL overrides it | `90`                                                                |

### Kafka - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFKA_BACKEND`                                  | Kafka backend for `kafka` transport profiles                                         | `redpanda`                                                          |
| `KAFKA_PLAINTEXT_HOST_PORT`                      | Kafka plaintext host port (host-facing)                                              | `19092`                                                             |
| `KAFKA_PLAINTEXT_PORT`                           | Kafka plaintext port (in-network)                                                    | `9092`                                                              |

### Apache Kafka

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `APACHE_KAFKA_VERSION`                           | Version of Apache Kafka to use                                                       | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `KAFKA_ADVERTISED_LISTENERS`                     | Listener addresses advertised to clients/brokers                                     | `PLAINTEXT://kafka:9092,PLAINTEXT_HOST://localhost:19092`           |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE`                | Toggle auto creation of topics                                                       | `true`                                                              |
| `KAFKA_CLUSTER_ID`                               | Name of the Kafka cluster                                                            | `dfe-docker-dev-cluster-01`                                         |
| `KAFKA_CONTROLLER_LISTENER_NAMES`                | Listeners used by the controller                                                     | `CONTROLLER`                                                        |
| `KAFKA_CONTROLLER_QUORUM_VOTERS`                 | Set of voters                                                                        | `1@kafka:29092`                                                     |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS`         | Time (ms) group coordinator waits before initial rebalance                           | `0`                                                                 |
| `KAFKA_INTER_BROKER_LISTENER_NAME`               | Listener used for communication between brokers                                      | `PLAINTEXT`                                                         |
| `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP`           | Map of listener names and security protocols                                         | `CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT` |
| `KAFKA_LISTENERS`                                | List of listeners                                                                    | `PLAINTEXT://:9092,PLAINTEXT_HOST://:19092,CONTROLLER://:29092`     |
| `KAFKA_NODE_ID`                                  | Node ID associated with the roles                                                    | `1`                                                                 |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`         | Replication factor for the offsets topic                                             | `1`                                                                 |
| `KAFKA_PROCESS_ROLES`                            | Roles the process will use                                                           | `broker,controller`                                                 |
| `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR` | Replication factor for the transaction topic                                         | `1`                                                                 |
| `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR`            | Minimum ISR for transaction topic                                                    | `1`                                                                 |

### Kafka Redpanda

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `REDPANDA_VERSION`                               | Version of Redpanda to use                                                           | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `REDPANDA_MEMORY`                                | Memory for the Redpanda broker (Seastar reserves this up front); `redpanda/start.sh` lowers it to what the container limit leaves after the host's `vm.min_free_kbytes` | `1G`                                                                |

### Kafka UI (Kafbat)

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFBAT_ENABLED`                                 | Toggle to turn on Kafbat                                                             | `true`                                                              |
| `KAFBAT_VERSION`                                 | Version of Kafbat to use                                                             | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `KAFBAT_DYNAMIC_CONFIG_ENABLED`                  | Toggle runtime config changes                                                        | `true`                                                              |
| `KAFBAT_KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS`       | Kafka bootstrap server                                                               | `kafka:9092`                                                        |
| `KAFBAT_KAFKA_CLUSTERS_0_NAME`                   | Kafka cluster name                                                                   | `dfe-local`                                                         |
| `KAFBAT_PORT`                                    | Kafbat port                                                                          | `8081`                                                              |

## Core DFE Components

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_CORE_ENABLED`                               | Toggle to turn on core components                                                    | `true`                                                              |
| `DFE_ENGINE_VERSION`                             | Version of dfe-engine to use                                                         | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_ENGINE_PORT`                                | Port used by dfe-engine                                                              | `8003`                                                              |
| `DFE_ENGINE_CONFIG_DIR`                          | Path to config directory                                                             | `/app/config`                                                       |
| `DFE_ENGINE_SCHEMAS_DIR`                         | Path to schemas directory                                                            | `/app/schemas`                                                      |
| `DFE_ENGINE_CONTENT_DIR`                         | Path the content volume mounts on, holding the emitted contracts and the seed library | `/app/content`                                                      |
| `DFE_E2E_SERVER`                                 | Mount the engine's unauthenticated `/api/e2e` seeding routes; refused in a production posture, and `make e2e-posture` sets it beside `DFE_ENV=test` | `false`                                  |
| `DFE_UI_VERSION`                                 | Version of dfe-ui to use                                                             | none -- `make stack` pins it; unset is a hard-fail                                                            |
| `DFE_UI_PORT`                                    | Port used by dfe-ui                                                                  | `3000`                                                              |
| `DFE_UI_NODE_ENV`                                | Node environment of dfe-ui                                                           | `production`                                                        |

### App contracts, and the custom environment the console writes

The console's app settings page reports what an app can be configured with, and
it reads that from the app itself rather than from a list the engine carries. One
`contract-<app>` one-shot per app runs that app's pinned image, writes its config
schema and capability catalogue into the shared content volume, and exits; the
engine reads them at `DFE_APP_CONTRACT_DIR`. An app that cannot emit leaves its
contract absent and the engine starts anyway, so a settings page with nothing on
it for one app means that app's one-shot logged a failure -- `docker compose logs
contract-<app>`.

A setting the app's own schema does not declare is written as an environment
variable instead, into `env/<app>.custom.env` beside the file `make init` creates.
Each app reads both, the custom file second, so a custom key beats the same key in
`env/<app>.env`; the `environment:` block in `docker-compose.yml` beats both, which
is what keeps a custom key from taking over the broker address or the warehouse
credentials. Neither file has to exist.

**A custom env write needs `docker compose up -d <service>`, not a restart.**
Compose reads `env_file` when it creates a container, so `docker compose restart`
gives you the same container with the environment it already had, and the change
looks like it did nothing.

The engine writes those files into the `env/` directory of the checkout it is
started from: `DFE_DEPLOYMENT_APP_ENV_DIR` names it (`/app/app-env` inside the
container, the sibling of `DFE_DEPLOYMENT_APP_CONFIG_DIR`) and it is a read-write
bind mount of `./env`, so the deployment's own working tree is what changes when
an operator saves a setting.

## Self-monitoring (opt-in)

Off by default. `DFE_OTEL_ENABLED=true` (or a profile declaring `otel: true`, as `single` does) starts a collector that takes the stack's own telemetry over OTLP and writes the `dfe.otel_*` tables, which HyperDX reads. Nothing is scraped, and the collector's OTLP ports stay on the Compose network. Full picture, including which services report today: [observability.md](observability.md).

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_OTEL_ENABLED`                               | Toggle to start the collector                                                        | `false`                                                             |
| `DFE_OTEL_COLLECTOR_VERSION`                     | Version of otel/opentelemetry-collector-contrib                                      | none -- `make stack` pins it; unset is a hard-fail                  |
| `DFE_OTEL_EXPORTER_ENDPOINT`                     | Where services push; empty exports nothing                                           | empty (the profile points it at the bundled collector)              |
| `DFE_OTEL_DATABASE`                              | ClickHouse database the collector writes                                             | `dfe`                                                              |
| `DFE_OTEL_HEALTH_PORT`                           | Collector `health_check` host port                                                   | `13133`                                                             |
| `DFE_OTEL_COLLECTOR_LOG_LEVEL`                   | Collector's own log level                                                            | `warn`                                                              |
| `DFE_OTEL_FLUENT_PORT`                           | Collector `fluentforward` host port, where the daemon sends container stdout         | `24224`                                                             |
| `DFE_CONTAINER_LOGS_ENABLED`                     | Ship DFE container stdout; `false` puts every service back on `json-file`            | `true`                                                              |
| `DFE_CONTAINER_LOG_ADDRESS`                      | Where the DOCKER DAEMON sends it                                                     | `tcp://127.0.0.1:24224`                                             |
| `DFE_ENGINE_METRICS_BACKEND`                     | dfe-engine metrics backend; `opentelemetry` is dual (push + `/metrics`)              | `prometheus` (the profile sets `opentelemetry`)                     |

## HyperDX (opt-in observability)

Off by default. `DFE_HYPERDX_ENABLED=true` starts `hyperdx` (API + App), the `dfe-hyperdx-proxy` that fronts both origins, a one-shot `dfe-dashboards` that copies the engine's shipped dashboards into a volume, plus `hyperdx-ferretdb` and `hyperdx-postgres`, sharing the always-on ClickHouse.

HyperDX runs in `oidc-proxy` mode, as it does on Kubernetes: it verifies the engine's ES384 token against the engine's JWKS, and a caller carrying none is unauthenticated. Signing in to the console is what signs a browser in here -- dfe-ui mirrors the session into a `dfe_token` cookie, and cookies ignore ports. The engine identifies with a service token of its own, which is how the team gets seeded with the connection and sources from `config/hyperdx/default-sources.json` before anyone logs in, and how a source added through the console reaches HyperDX at all.

The same toggle points the engine at HyperDX: with it on, the engine receives `DFE_HYPERDX_ENABLED=true` and `DFE_HYPERDX_BASE_URL=http://hyperdx:8000`, so its `/api/v1/hyperdx/*` surface answers instead of returning `503 hyperdx_absent`.

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_HYPERDX_ENABLED`                            | Toggle to start HyperDX + its proxy + FerretDB + Postgres                            | `false`                                                             |
| `DFE_HYPERDX_VERSION`                            | Version of the dfe-hyperdx fork image to use                                         | pinned by `make stack` from the SSoT (fail-loud, like every image)  |
| `DFE_HYPERDX_API_PORT`                           | HyperDX API host port, on `dfe-hyperdx-proxy`                                        | `8000`                                                              |
| `DFE_HYPERDX_APP_PORT`                           | HyperDX App UI host port, on `dfe-hyperdx-proxy`                                     | `8090`                                                              |
| `DFE_HYPERDX_APP_URL`                            | Base URL the browser uses to reach HyperDX; overrides `DFE_EXTERNAL_ORIGIN`          | follows `DFE_EXTERNAL_ORIGIN`                                       |
| `DFE_HYPERDX_AUTH_MODE`                          | `header-dev` runs HyperDX off request headers, for a stack with no engine            | `oidc-proxy`                                                        |
| `DFE_HYPERDX_BASE_URL`                           | Where the ENGINE reaches HyperDX; name an external one here                          | `http://hyperdx:8000`                                               |
| `DFE_HYPERDX_TEAM`                               | Team name when the token carries no group claim                                      | `dfe`                                                               |
| `HYPERDX_THEME`                                  | UI theme (NEXT_PUBLIC_THEME)                                                         | `dfe`                                                               |
| `HYPERDX_POSTGRES_USER`                          | FerretDB/Postgres user                                                               | `hyperdx`                                                           |
| `HYPERDX_POSTGRES_PASSWORD`                      | FerretDB/Postgres password                                                           | `hyperdx`                                                           |
| `HYPERDX_FERRETDB_VERSION`                       | FerretDB image version                                                               | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |
| `HYPERDX_POSTGRES_VERSION`                       | Postgres/DocumentDB image version                                                    | none -- `make stack` pins it from the DFE stack SSoT; unset is a hard-fail                |

## Default Ports

| Port  | Service              | Protocol           |
|-------|----------------------|--------------------|
| 3000  | dfe-proxy            | Web UI (envoy fronts dfe-ui, which publishes no host port) |
| 6000  | dfe-receiver         | gRPC               |
| 8000  | hyperdx              | API                |
| 8003  | dfe-engine           | HTTP API           |
| 8080  | dfe-receiver         | HTTP ingest        |
| 8081  | Kafbat               | Web UI             |
| 8082  | dfe-fetcher          | HTTP ingest        |
| 8090  | hyperdx              | App UI             |
| 8123  | ClickHouse           | HTTP API           |
| 9000  | ClickHouse           | Native protocol    |
| 9089  | dfe-transform-elastic-cisco-ios | Prometheus metrics |
| 9090  | dfe-receiver         | Prometheus metrics |
| 9091  | dfe-loader           | Prometheus metrics |
| 9092  | Kafka (any backend)  | Plaintext          |
| 9093  | dfe-archiver         | Prometheus metrics |
| 9094  | dfe-fetcher          | Prometheus metrics |
| 9095  | dfe-transform-vector | Prometheus metrics |
| 9096  | dfe-transform-vrl    | Prometheus metrics |
| 9097  | dfe-transform-vrl-filebeat | Prometheus metrics |
| 9098  | dfe-transform-vector-filebeat | Prometheus metrics |
| 9099  | dfe-transform-elastic | Prometheus metrics |
| 13133 | otel-collector       | health_check       |
| 19092 | Kafka (any backend)  | Plaintext host     |
| 50051 | dfe-loader           | gRPC, container `:6000` |

Additional receiver ports (commented out by default in docker-compose.yml):
4317 (OTLP gRPC), 4318 (OTLP HTTP), 5044 (Beats), 8088 (HEC).

## ClickHouse Schema

**dfe-engine is the schema authority.** It ships `/app/schemas` inside its own
image (pinned by `DFE_ENGINE_VERSION`) and creates the ClickHouse objects at
startup; the loader pre-warms those schemas into its cache. Nothing in this repo
provisions tables.

- `dfe` - master database for all DFE related tables
- `dfe.main` - catch-all for unrouted events, carrying `_tags` (JSON) among
  the profile columns

That last detail matters more than it looks: the e2e suite and the power-on self
test both identify their own rows by reading `_tags.marker` back out. If a
deployment provisions a schema without `_tags`, both will correctly refuse to
claim they verified anything.

## Container Images

Images are published from the component repos:

- `ghcr.io/hyperi-io/dfe-archiver`
- `ghcr.io/hyperi-io/dfe-engine`
- `ghcr.io/hyperi-io/dfe-fetcher`
- `ghcr.io/hyperi-io/dfe-loader`
- `ghcr.io/hyperi-io/dfe-receiver`
- `ghcr.io/hyperi-io/dfe-transform-elastic`
- `ghcr.io/hyperi-io/dfe-transform-vector`
- `ghcr.io/hyperi-io/dfe-transform-vrl`
- `ghcr.io/hyperi-io/dfe-ui`


## Related

- [deploying.md](deploying.md) -- the dial, pinning, upgrades
- [operating.md](operating.md) -- what these settings mean in production
- [observability.md](observability.md) -- the self-monitoring variables in context
