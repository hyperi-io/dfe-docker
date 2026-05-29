# dfe-docker

Docker Compose deployment packaging for HyperI Data Forwarding Engine (DFE 2.2).

## Architecture

See the architecture diagram in [docs/SCOPE.md](docs/SCOPE.md#architecture).

## Quickstart

### Prerequisites

- Docker and Docker Compose v2
- Python 3

### Initialisation

DFE services accept environment variable overrides that are isolated per container (e.g. `DFE_ARCHIVER_DLQ_ENABLED=false`). To create the local env files from the committed templates:

```bash
make init
```

This creates `.env` from `.env.example` and `env/<service>.env` for every template found under `env.example/`. The `.env` file and the entire `env/` directory are gitignored meaning your local edits stay local. The committed templates (`.env.example` and everything under `env.example/`) are tracked, so edits there propagate to everyone.

Re-running `make init` is safe with existing files being skipped.

> **Note**: Anything explicitly listed in a service's `environment:` block in `docker-compose.yml` takes precedence over the same key in `env/<service>.env`. Use the per-service file for *new* overrides that the compose file does not already forward. For example: `LOG_LEVEL` is set by compose's `environment:` block, so adding `LOG_LEVEL=debug` to `env/fetcher.env` will have no effect - but `DFE_ARCHIVER_DLQ_ENABLED` is not forwarded by compose, so setting it in the per-service file will reach the container.

### 1. CI Mode (Uses GHCR Images)

```bash
make init   # Edit .env files as needed (versions, ports)
make dev    # Uses active_profile from service_profiles.yaml
make down   # Stop everything
```

### 2. Dev Mode (Requires Repositories Cloned Locally)

Refer to [docs/SCOPE.md](docs/SCOPE.md#source-repositories) for the required repositories.

```bash
make init   # Edit .env files as needed (versions, ports)
make dev    # Uses active_profile from service_profiles.yaml
make down   # Stop everything
```

## Service Profiles

Service selection is controlled by `service_profiles.yaml` at the repo root. Each profile declares a transport mode and which DFE services to start. You can override the active profile via the `DFE_PROFILE` env var.

For `kafka` transport profiles, the Kafka backend is selected via `KAFKA_BACKEND` (default `redpanda`).

```bash
make dev                         # Uses active_profile from service_profiles.yaml
DFE_PROFILE=grpc-full make dev   # Override profile
KAFKA_BACKEND=apache make dev    # Override Kafka backend
```

### Application Profiles (service_profiles.yaml)

| Profile                   | Transport | dfe-archiver | dfe-fetcher | dfe-loader | dfe-receiver |
|---------------------------|-----------|:------------:|:-----------:|:----------:|:------------:|
| `kafka-minimal`           | Kafka     |              |             |     X      |              |
| `kafka-full`              | Kafka     |      X       |      X      |     X      |      X       |
| `kafka-receiver`          | Kafka     |              |             |     X      |      X       |
| `kafka-receiver-archiver` | Kafka     |      X       |             |     X      |      X       |
| `kafka-fetcher`           | Kafka     |              |      X      |     X      |              |
| `grpc-minimal`            | gRPC      |              |             |     X      |              |
| `grpc-full`               | gRPC      |              |      X      |     X      |      X       |
| `grpc-receiver`           | gRPC      |              |             |     X      |      X       |
| `grpc-fetcher`            | gRPC      |              |      X      |     X      |              |

### Infrastructure Profiles (docker-compose.yml)

| Profile          | Service             | Use Case                                                |
|------------------|---------------------|---------------------------------------------------------|
| `clickhouse`     | ClickHouse          | Local analytics store                                   |
| `kafka-apache`   | Apache Kafka KRaft  | Kafka transport (opt-in - Real-Kafka compat + fallback) |
| `kafka-redpanda` | Redpanda            | Kafka transport (default - fits 4 GB CI runners)        |
| `ui`             | Kafbat UI (:8081)   | Web UI for Kafka                                        |

Infrastructure profiles are activated automatically based on the selected application profile. The `ui` profile is on by default when transport is `kafka`. Opt out by setting `KAFBAT_ENABLED=false` in `.env`.

All `kafka-*` profiles are **mutually exclusive**. They expose the network alias `kafka` on the same host ports, so only one can run at a time. Downstream services and configs always address `kafka:9092` and work with either backend.

## Make Commands

### Services

| Command            | Description                                 |
|--------------------|---------------------------------------------|
| `make dev`         | Build from local source and start stack     |
| `make build-local` | Build images from local source              |
| `make ci`          | Start stack using published registry images |
| `make pull`        | Pull latest images from registry            |
| `make infra`       | Start infrastructure only                   |
| `make certs`       | Create self-signed dev certs                |
| `make ps`          | Show running containers                     |
| `make down`        | Stop all services                           |
| `make clean`       | Stop all services and remove volumes        |

### Logging

| Command            | Description                                 |
|--------------------|---------------------------------------------|
| `make dev-logs`    | Tail all service logs                       |
| `make infra-logs`  | Tail infrastructure logs                    |

### Testing

| Command            | Description                                 |
|--------------------|---------------------------------------------|
| `make test-infra`  | Smoke test infrastructure                   |
| `make test`        | Send test events and verify in ClickHouse   |
| `make test-e2e`    | End-to-end test executor                    |
| `make test-vector` | Feed events via Vector                      |
| `make verify`      | Query ClickHouse to check ingested data     |

## Configuration

Config files live under `config/<component>/` and are self-documenting - browse the directory. The active config for each service is set by the selected profile in `service_profiles.yaml`, which can be overridden via the `DFE_PROFILE` environment variable.

## Environment Variables

See [.env.example](.env.example) for available overrides.

### Profile Selection

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_PROFILE`                                    | Override active profile from service_profiles.yaml                                   | -                                                                   |

### Image Registry

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `IMAGE_REGISTRY`                                 | OCI registry hosting published `dfe-*` images                                        | `ghcr.io/hyperi-io`                                                 |

### Dev Builds

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `PROJECTS_PATH`                                  | Parent directory containing DFE source repos (used by `docker-compose.override.yml`) | `/projects`                                                         |

### DFE Components - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `LOG_LEVEL`                                      | Log level (trace\|debug\|info\|warn\|error)                                          | `info`                                                              |
| `LOG_FORMAT`                                     | Log format                                                                           | `text`                                                              |

### DFE Archiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_ARCHIVER_VERSION`                           | Version of dfe-archiver to use                                                       | `latest`                                                            |
| `DFE_ARCHIVER_PROMETHEUS_PORT`                   | Archiver Prometheus port                                                             | `9093`                                                              |

### DFE Fetcher

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_FETCHER_VERSION`                            | Version of dfe-fetcher to use                                                        | `latest`                                                            |
| `DFE_FETCHER_INGEST_PORT`                        | Fetcher ingest port                                                                  | `8082`                                                              |
| `DFE_FETCHER_PROMETHEUS_PORT`                    | Fetcher Prometheus port                                                              | `9094`                                                              |
| `AWS_REGION`                                     | AWS region for fetcher sources                                                       | `us-east-1`                                                         |
| `AWS_ACCESS_KEY_ID`                              | AWS access key                                                                       | -                                                                   |
| `AWS_SECRET_ACCESS_KEY`                          | AWS secret key                                                                       | -                                                                   |

### DFE Loader

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_LOADER_VERSION`                             | Version of dfe-loader to use                                                         | `latest`                                                            |
| `DFE_LOADER_PROMETHEUS_PORT`                     | Loader Prometheus port                                                               | `9091`                                                              |
| `DFE_LOADER_GRPC_PORT`                           | Loader gRPC port                                                                     | `50051`                                                             |

### DFE Receiver

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_RECEIVER_VERSION`                           | Version of dfe-receiver to use                                                       | `latest`                                                            |
| `DFE_RECEIVER_BEATS_PORT`                        | Receiver Beats port                                                                  | `5044`                                                              |
| `DFE_RECEIVER_GRPC_PORT`                         | Receiver gRPC port                                                                   | `6000`                                                              |
| `DFE_RECEIVER_HEC_PORT`                          | Receiver HEC port                                                                    | `8088`                                                              |
| `DFE_RECEIVER_HTTP_PORT`                         | Receiver HTTP port                                                                   | `8080`                                                              |
| `DFE_RECEIVER_OTLP_GRPC_PORT`                    | Receiver OTLP gRPC port                                                              | `4317`                                                              |
| `DFE_RECEIVER_OTLP_HTTP_PORT`                    | Receiver OTLP HTTP port                                                              | `4318`                                                              |
| `DFE_RECEIVER_PROMETHEUS_PORT`                   | Receiver Prometheus port                                                             | `9090`                                                              |

### DFE Transform Vector

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VECTOR_VERSION`                   | Version of dfe-transform-vector to use                                               | `latest`                                                            |
| `DFE_TRANSFORM_VECTOR_PROMETHEUS_PORT`           | Transform Vector Prometheus port                                                     | `9095`                                                              |
| `DFE_TRANSFORM_VECTOR_API_PORT`                  | Transform Vector API port                                                            | `8686`                                                              |

### DFE Transform VRL

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `DFE_TRANSFORM_VRL_VERSION`                      | Version of dfe-transform-vrl to use                                                  | `latest`                                                            |

### ClickHouse

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `CLICKHOUSE_VERSION`                             | Version of ClickHouse to use                                                         | `latest`                                                            |
| `CLICKHOUSE_HOST`                                | External ClickHouse host (skips Docker container)                                    | `clickhouse`                                                        |
| `CLICKHOUSE_HTTP_PORT`                           | ClickHouse HTTP port                                                                 | `8123`                                                              |
| `CLICKHOUSE_NATIVE_PORT`                         | ClickHouse native protocol port                                                      | `9000`                                                              |
| `CLICKHOUSE_DB`                                  | ClickHouse initialisation database                                                   | `dfe`                                                               |

### Kafka - General

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFKA_BACKEND`                                  | Kafka backend for `kafka` transport profiles                                         | `redpanda`                                                          |
| `KAFKA_PLAINTEXT_HOST_PORT`                      | Kafka plaintext host port (host-facing)                                              | `19092`                                                             |
| `KAFKA_PLAINTEXT_PORT`                           | Kafka plaintext port (in-network)                                                    | `9092`                                                              |

### Apache Kafka

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `APACHE_KAFKA_VERSION`                           | Version of Apache Kafka to use                                                       | `latest`                                                            |
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
| `REDPANDA_VERSION`                               | Version of Redpanda to use                                                           | `latest`                                                            |
| `REDPANDA_MEMORY`                                | Memory cap for the Redpanda broker (Seastar reserves this up front)                  | `1G`                                                                |

### Kafka UI (Kafbat)

| Variable                                         | Use                                                                                  | Default                                                             |
|--------------------------------------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `KAFBAT_ENABLED`                                 | Toggle to turn on Kafbat                                                             | `true`                                                              |
| `KAFBAT_VERSION`                                 | Version of Kafbat to use                                                             | `latest`                                                            |
| `KAFBAT_DYNAMIC_CONFIG_ENABLED`                  | Toggle runtime config changes                                                        | `true`                                                              |
| `KAFBAT_KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS`       | Kafka bootstrap server                                                               | `kafka:9092`                                                        |
| `KAFBAT_KAFKA_CLUSTERS_0_NAME`                   | Kafka cluster name                                                                   | `dfe-local`                                                         |
| `KAFBAT_PORT`                                    | Kafbat port                                                                          | `8081`                                                              |

## Default Ports

| Port  | Service              | Protocol           |
|-------|----------------------|--------------------|
| 6000  | dfe-receiver         | gRPC               |
| 8080  | dfe-receiver         | HTTP ingest        |
| 8081  | Kafbat               | Web UI             |
| 8082  | dfe-fetcher          | HTTP ingest        |
| 8123  | ClickHouse           | HTTP API           |
| 8686  | dfe-transform-vector | Vector API         |
| 9000  | ClickHouse           | Native protocol    |
| 9090  | dfe-receiver         | Prometheus metrics |
| 9091  | dfe-loader           | Prometheus metrics |
| 9092  | Kafka (any backend)  | Plaintext          |
| 9093  | dfe-archiver         | Prometheus metrics |
| 9094  | dfe-fetcher          | Prometheus metrics |
| 9095  | dfe-transform-vector | Prometheus metrics |
| 9096  | dfe-transform-vrl    | Prometheus metrics |
| 19092 | Kafka (any backend)  | Plaintext host     |
| 50051 | dfe-loader           | gRPC               |

Additional receiver ports (commented out by default in docker-compose.yml):
4317 (OTLP gRPC), 4318 (OTLP HTTP), 5044 (Beats), 8088 (HEC).

## ClickHouse Schema

Tables are initialised via `clickhouse/init-dfe.sql`:

- `dfe.default` - catch-all for unrouted events

## Container Images

Images are published from the component repos:

- `ghcr.io/hyperi-io/dfe-receiver` - see `dfe-receiver/docs/CONTAINER-PUBLISHING.md`
- `ghcr.io/hyperi-io/dfe-loader` - see `dfe-loader/docs/CONTAINER-PUBLISHING.md`

## Licence

BUSL-1.1 - see [LICENSE](LICENSE) for details.
