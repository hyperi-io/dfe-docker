# dfe-docker

Docker Compose deployment packaging for HyperI Data Forwarding Engine (DFE 2.2).

## Data Flow

### Default
```
curl/Vector → dfe-receiver (:8080) → gRPC → dfe-loader → ClickHouse (dfe.*)
```

### via Kafka (opt-in)
```
curl/Vector → dfe-receiver (:8080) → Kafka → dfe-loader → ClickHouse (dfe.*)
```

## Quickstart

### Prerequisites

- Docker and Docker Compose v2
- GHCR images published for `dfe-receiver` and `dfe-loader` - TODO (see below)
    - Current implementation is these are in Artifactory

### 1. Docker — Full Stack

```bash
cp .env.example .env          # Edit if needed (versions, ports)
make dev                      # Start receiver + loader + Kafka (if needed) + ClickHouse
make test                     # Send test events and verify ClickHouse
make infra-logs               # Tail logs
make down                     # Stop everything
```

### 2. Bare-Metal — Infrastructure Only

Use this to test locally-compiled receiver/loader binaries against containerised
Kafka and ClickHouse.

```bash
make infra                    # Start Kafka + ClickHouse only

# In separate terminals:
./bin/dfe-receiver --config config/receiver-grpc.yaml
./bin/dfe-loader --config config/loader-grpc.yaml

# Test:
make test
make verify
```

Place binaries in `bin/` (gitignored). Copy from component repo `dist/`
directories or download from JFrog.

## Compose Profiles

| Profile | Services | Use Case |
|---------|----------|----------|
| `bare-bones` | dfe-receiver + dfe-loader | gRPC communication with external services |
| `full` | ClickHouse + dfe-receiver + dfe-loader | gRPC communication with local ClickHouse service |
| `full-kafka` | ClickHouse + Kafka + dfe-receiver + dfe-loader | Kafka communication with local ClickHouse service |
| `infra` | ClickHouse + Kafka | Local infrastructure |
| `ui` | Kafbat UI (:8081) | UI access to local Kafka |
| `debug` | dfe-receiver + dfe-loader | File-sink with no ClickHouse writes |

```bash
docker compose --profile bare-bones up -d
docker compose --profile full up -d
docker compose --profile full-kafka up -d
docker compose --profile full-kafka --profile ui up -d
docker compose --profile infra up -d
docker compose --profile debug up -d
```

## Make Commands

### Services

| Command | Description |
|--------|-------------|
| `make dev` | Start required services |
| `make build-local` | Build images from local source |
| `make ci-up` | Start required services using published registry images |
| `make pull` | Pull latest images from registry |
| `make infra` | Start infrastructure only |
| `make up-ext` | Start DFE services with external ClickHouse service |
| `make debug` | Start DFE services in debug mode |
| `make certs` | Create self-signed dev certs |
| `make ps` | Show running containers |
| `make down` | Stop all services |
| `make clean` | Stop all services and remove volumes |

### Logging

| Command | Description |
|--------|-------------|
| `make dev-logs` | Tail all service logs |
| `make infra-logs` | Tail infrastructure logs |
| `make debug-logs` | Tail DFE service logs |

### Testing

| Command | Description |
|--------|-------------|
| `make test-infra` | Smoke test infrastructure |
| `make test` | Send test events and verify in ClickHouse |
| `make test-vector` | Feed events via Vector |
| `make verify` | Query ClickHouse to check ingested data |

## Configuration

Config files are in `config/`:

| File | Use |
|------|-----|
| `loader-debug.yaml` | Loader debug mode (no ClickHouse writes) |
| `loader-ext-ch.yaml` | External ClickHouse template |
| `loader-grpc.yaml` | Default gRPC architecture |
| `loader-kafka.yaml` | Opt-in Kafka architecture |
| `loader.yaml` | Bare-metal |
| `receiver-debug.yaml` | Receiver debug mode (no forwarding) |
| `receiver-grpc.yaml` | Default gRPC architecture |
| `receiver-kafka.yaml` | Opt-in Kafka architecture |
| `receiver.yaml` | Bare-metal |

## Environment Variables

See [.env.example](.env.example) for available overrides:

### Dev Builds

| Variable Name | Use | Default |
|---------------|-----|---------|
| `PROJECTS_PATH` | Parent directory containing `dfe-receiver`, `dfe-loader`, `hyperi-rustlib`, `clickhouse-arrow`, and `dfe-docker` repos (used by `docker-compose.override.yml`) | `/projects` |

### Transport

| Variable Name | Use | Default |
|---------------|-----|---------|
| `DFE_TRANSPORT` | Internal transport mechanism to use (grpc|kafka) | `grpc` |

### DFE Components

#### General

| Variable Name | Use | Default |
|---------------|-----|---------|
| `LOG_LEVEL` | Level of logging in loader/receiver (trace|debug|info|warn|error) | `info` |
| `LOG_FORMAT` | Format of logging | `text` |

#### Registry

| Variable Name | Use | Default |
|---------------|-----|---------|
| `IMAGE_ARCHITECTURE` | Architecture of the image to use | `linux-amd64` |
| `IMAGE_PLATFORM` | Docker platform for base image (must match ARCHITECTURE). Use `linux/amd64` when using x86_64 binaries on Apple Silicon. | `linux/amd64` |
| `IMAGE_REGISTRY` | Registry holding the image | `jfrog.io/hyperi` |
| `JFROG_USERNAME` | JFrog authentication username ||
| `JFROG_TOKEN` | JFrog authentication access token ||

#### DFE Loader

| Variable Name | Use | Default |
|---------------|-----|---------|
| `DFE_LOADER_VERSION` | Version of dfe-loader to use | `latest` |
| `DFE_LOADER_PROMETHEUS_PORT` | Loader prometheus port | `9091` |
| `DFE_LOADER_GRPC_PORT` | Loader gRPC port | `50051` |

#### DFE Receiver

| Variable Name | Use | Default |
|---------------|-----|---------|
| `DFE_RECEIVER_VERSION` | Version of dfe-receiver to use | `latest` |
| `DFE_RECEIVER_BEATS_PORT` | Receiver Beats port | `5044` |
| `DFE_RECEIVER_GRPC_PORT` | Receiver gRPC port | `6000` |
| `DFE_RECEIVER_HEC_PORT` | Receiver HEC port | `8088` |
| `DFE_RECEIVER_HTTP_PORT` | Receiver HTTP port | `8080` |
| `DFE_RECEIVER_OTLP_GRPC_PORT` | Receiver OTLP gRPC port | `4317` |
| `DFE_RECEIVER_OTLP_HTTP_PORT` | Receiver OTLP HTTP port | `4318` |
| `DFE_RECEIVER_PROMETHEUS_PORT` | Receiver prometheus port | `9090` |

### ClickHouse

| Variable Name | Use | Default |
|---------------|-----|---------|
| `CLICKHOUSE_VERSION` | Version of ClickHouse to use | `latest` |
| `CLICKHOUSE_HOST` | ClickHouse host to connect to | `clickhouse` |
| `CLICKHOUSE_HTTP_PORT` | ClickHouse HTTP port | `8123` |
| `CLICKHOUSE_NATIVE_PORT` | ClickHouse native protocol port | `9000` |
| `CLICKHOUSE_DB` | ClickHouse initialisation database | `dfe` |

### Kafka

Full breakdown of Kafka settings can be found [here](https://docs.confluent.io/platform/current/installation/configuration/broker-configs.html?_ga=2.257346312.2006843211.1772681132-1951940448.1772681132&_gac=1.48538580.1772681132.CjwKCAiAzZ_NBhAEEiwAMtqKy6tLS6YMXN2BhSDBUbkx4LoD-IYK8I-CN7YAtiwJnZqsRbmdHXvXoxoCJEoQAvD_BwE&_gl=1*ryo9hl*_gcl_aw*R0NMLjE3NzI2ODExMzIuQ2p3S0NBaUF6Wl9OQmhBRUVpd0FNdHFLeTZ0TFM2WU1YTjJCaFNEQlVia3g0TG9ELUlZSzhJLUNON1lBdGl3Sm5acXNSYm1kSFh2WG94b0NKRW9RQXZEX0J3RQ..*_gcl_au*MzYxMzYzNzMuMTc3MjY4MTEzMg..*_ga*MTk1MTk0MDQ0OC4xNzcyNjgxMTMy*_ga_D2D3EGKSGD*czE3NzI2ODExMzIkbzEkZzEkdDE3NzI2ODExNTAkajQyJGwwJGgw#cp-config-brokers).

| Variable Name | Use | Default |
|---------------|-----|---------|
| `KAFKA_VERSION` | Version of Kafka to use | `latest` |
| `KAFKA_ADVERTISED_LISTENERS` | Listener addresses advertised to clients/brokers | `PLAINTEXT://kafka:9092,PLAINTEXT_HOST://localhost:19092` |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE` | Toggle auto creation of topics | `true` |
| `KAFKA_CLUSTER_ID` | Name of the Kafka cluster | `dfe-docker-dev-cluster-01` |
| `KAFKA_CONTROLLER_LISTENER_NAMES` | Listeners used by the controller | `CONTROLLER` |
| `KAFKA_CONTROLLER_QUORUM_VOTERS` | Set of voters | `1@kafka:29092` |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS` | Time (in ms) group coordinator waits before initial rebalance | `0` |
| `KAFKA_INTER_BROKER_LISTENER_NAME` | Listener used for communication between brokers | `PLAINTEXT` |
| `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP` | Map of listener names and security protocols | `CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT` |
| `KAFKA_LISTENERS` | List of listeners | `PLAINTEXT://:9092,PLAINTEXT_HOST://:19092,CONTROLLER://:29092` |
| `KAFKA_NODE_ID` | Node ID associated with the roles | `1` |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR` | Replication factor for the offsets topic | `1` |
| `KAFKA_PLAINTEXT_PORT` | Kafka plaintext port | `9092` |
| `KAFKA_PLAINTEXT_HOST_PORT` | Kafka plaintext host port | `19092` |
| `KAFKA_PROCESS_ROLES` | Roles the process will use | `broker,controller` |
| `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR` | Replication factor for the transaction topic | `1` |
| `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR` | Minimum number of replicas acknowledged written to transaction to be considered successful | `1` |

### Kafka UI (Kafbat)

| Variable Name | Use | Default |
|---------------|-----|---------|
| `KAFBAT_VERSION` | Version of Kafbat to use | `latest` |
| `KAFBAT_DYNAMIC_CONFIG_ENABLED` | Toggle application settings to change at runtime | `true` |
| `KAFBAT_KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS` | Kafka boostrap server to connect to | `kafka:9092` |
| `KAFBAT_KAFKA_CLUSTERS_0_NAME` | Kafka cluster name to connect to | `dfe-local` |
| `KAFBAT_PORT` | Kafbat port | `8081` |

## Default Ports

| Port | Service | Protocol |
|------|---------|----------|
| 4317 | dfe-receiver | OTLP_gRPC |
| 4318 | dfe-receiver | OTLP_HTTP |
| 5044 | dfe-receiver | Beats |
| 6000 | dfe-receiver | gRPC |
| 8080 | dfe-receiver | HTTP ingest |
| 8081 | Kafbat | Web UI |
| 8088 | dfe-receiver | HEC |
| 8123 | ClickHouse | HTTP API |
| 9000 | ClickHouse | Native protocol |
| 9090 | dfe-receiver | Prometheus metrics |
| 9091 | dfe-loader | Prometheus metrics |
| 9092 | Kafka | Plaintext |
| 19092 | Kafka | Plaintext host |
| 50051 | dfe-loader | gRPC |

## ClickHouse Schema

Tables are initialised via `clickhouse/init-dfe.sql`:

- `dfe.default` — catch-all for unrouted events
- `dfe.auth_events` — authentication events
- `dfe.api_events` — API request events
- `dfe.admin_events` — administrative actions
- `dfe.error_events` — error/exception events
- `dfe.dlq_events` — dead letter queue (30-day TTL)

## Container Images

Images are published from the component repos:

- `ghcr.io/hyperi-io/dfe-receiver` — see `dfe-receiver/docs/CONTAINER-PUBLISHING.md`
- `ghcr.io/hyperi-io/dfe-loader` — see `dfe-loader/docs/CONTAINER-PUBLISHING.md`

**Note:** Container image publishing is pending — both component repos have specs and TODO items for implementing GHCR publishing via the CI pipeline.

## Licence

FSL-1.1-ALv2 — see [LICENSE](LICENSE) for details.
