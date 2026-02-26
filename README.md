# dfe-docker

Docker Compose deployment packaging for HyperI Data Forwarding Engine (DFE 2.2).

## Data Flow

```
curl/Vector → dfe-receiver (:8080) → Kafka → dfe-loader → ClickHouse (dfe.*)
```

## Quickstart

### Prerequisites

- Docker and Docker Compose v2
- GHCR images published for `dfe-receiver` and `dfe-loader` (see below)

### 1. Docker — Full Stack

```bash
cp .env.example .env          # Edit if needed (versions, ports)
make up                       # Start receiver + loader + Kafka + ClickHouse
make test                     # Send test events and verify ClickHouse
make logs                     # Tail logs
make down                     # Stop everything
```

### 2. Bare-Metal — Infrastructure Only

Use this to test locally-compiled receiver/loader binaries against containerised
Kafka and ClickHouse.

```bash
make infra                    # Start Kafka + ClickHouse only

# In separate terminals:
./bin/dfe-receiver --config config/receiver.yaml
./bin/dfe-loader --config config/loader.yaml

# Test:
./scripts/send-test-events.sh
./scripts/verify-clickhouse.sh
```

Place binaries in `bin/` (gitignored). Copy from component repo `dist/`
directories or download from JFrog.

## Compose Profiles

| Profile | Services | Use Case |
|---------|----------|----------|
| `infra` | Kafka, ClickHouse | Bare-metal testing |
| `full` | infra + dfe-receiver + dfe-loader | Dockerised stack |
| `ui` | Kafbat UI (:8081) | Kafka topic debugging |

```bash
docker compose --profile infra up -d
docker compose --profile full up -d
docker compose --profile full --profile ui up -d
```

## Make Targets

| Target | Description |
|--------|-------------|
| `make infra` | Start infrastructure only |
| `make up` | Start full stack |
| `make up-ui` | Full stack + Kafbat UI |
| `make test` | Send test events + verify |
| `make verify` | Query ClickHouse row counts |
| `make logs` | Tail service logs |
| `make ps` | Show running containers |
| `make down` | Stop all services |
| `make clean` | Stop + remove volumes |
| `make pull` | Pull latest GHCR images |

## Configuration

Config files are in `config/`:

| File | Use |
|------|-----|
| `receiver.yaml` | Bare-metal (localhost:19092) |
| `receiver-docker.yaml` | Docker Compose (kafka:9092) |
| `loader.yaml` | Bare-metal (localhost:19092, localhost:9000) |
| `loader-docker.yaml` | Docker Compose (kafka:9092, clickhouse:9000) |

Docker Compose mounts the `-docker.yaml` variants into containers at
`/etc/dfe/*.yaml`.

## Environment Variables

See [.env.example](.env.example) for available overrides:

- `DFE_RECEIVER_VERSION` / `DFE_LOADER_VERSION` — pin image tags (default: `latest`)
- `LOG_LEVEL` — log verbosity (default: `info`)
- `CLICKHOUSE_NATIVE_PORT` / `CLICKHOUSE_HTTP_PORT` — port overrides if defaults conflict

## Ports

| Port | Service | Protocol |
|------|---------|----------|
| 8080 | dfe-receiver | HTTP ingest |
| 9090 | dfe-receiver | Prometheus metrics |
| 9091 | dfe-loader | Prometheus metrics |
| 8123 | ClickHouse | HTTP API |
| 9000 | ClickHouse | Native protocol |
| 19092 | Kafka | Kafka protocol (external) |
| 8081 | Kafbat UI | Web UI (profile: ui) |

## ClickHouse Schema

Tables are initialised via `clickhouse/init-dfe.sql`:

- `dfe.common` — catch-all for unrouted events
- `dfe.auth_events` — authentication events
- `dfe.api_events` — API request events
- `dfe.admin_events` — administrative actions
- `dfe.error_events` — error/exception events
- `dfe.dlq_events` — dead letter queue (30-day TTL)

## Container Images

Images are published from the component repos:

- `ghcr.io/hyperi-io/dfe-receiver` — see `dfe-receiver/docs/CONTAINER-PUBLISHING.md`
- `ghcr.io/hyperi-io/dfe-loader` — see `dfe-loader/docs/CONTAINER-PUBLISHING.md`

**Note:** Container image publishing is pending — both component repos have specs
and TODO items for implementing GHCR publishing via the CI pipeline.

## Licence

FSL-1.1-ALv2 — see [LICENSE](LICENSE) for details.
